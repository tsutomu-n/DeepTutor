from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any
import uuid

from .domain import (
    DomainValidationError,
    DuplicateRecordError,
    ExamSpec,
    ImmutableVersionError,
    InvalidTransitionError,
    QuestionVersionDraft,
)
from .storage import CatalogStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_actor(actor_id: str) -> str:
    actor = actor_id.strip()
    if not actor:
        raise DomainValidationError("actor_id is required")
    return actor


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _content_payload(draft: QuestionVersionDraft) -> dict[str, Any]:
    return {
        "stem": draft.stem,
        "choices": [{"key": item.key, "text": item.text} for item in draft.choices],
        "correct_option_key": draft.correct_option_key,
        "area": draft.area,
        "explanation": draft.explanation,
        "hints": list(draft.hints),
        "source": dict(draft.source),
    }


def _content_hash(draft: QuestionVersionDraft) -> str:
    encoded = _json(_content_payload(draft)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CatalogService:
    """Human-governed exam definitions and immutable official question versions."""

    def __init__(self, store: CatalogStore) -> None:
        self.store = store

    def create_exam(self, spec: ExamSpec, *, actor_id: str) -> dict[str, Any]:
        normalized = spec.normalized()
        actor = _require_actor(actor_id)
        now = _now()
        try:
            with self.store.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO exam_definitions (
                        id, title, description, duration_seconds, question_count,
                        pass_score, blueprint_json, status, revision, created_by,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', 1, ?, ?, ?)
                    """,
                    (
                        normalized.id,
                        normalized.title,
                        normalized.description,
                        normalized.duration_seconds,
                        normalized.question_count,
                        normalized.pass_score,
                        _json(dict(normalized.blueprint)),
                        actor,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateRecordError(f"exam already exists: {normalized.id}") from exc
        return self.get_exam(normalized.id)

    def get_exam(self, exam_id: str) -> dict[str, Any]:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM exam_definitions WHERE id = ?", (exam_id.strip(),)
            ).fetchone()
        if row is None:
            raise DomainValidationError(f"unknown exam: {exam_id}")
        result = dict(row)
        result["blueprint"] = json.loads(result.pop("blueprint_json"))
        return result

    def replace_exam(self, exam_id: str, spec: ExamSpec, *, actor_id: str) -> dict[str, Any]:
        normalized = spec.normalized()
        _require_actor(actor_id)
        target_id = exam_id.strip()
        if normalized.id != target_id:
            raise DomainValidationError("exam id cannot be changed")
        now = _now()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM exam_definitions WHERE id = ?", (target_id,)
            ).fetchone()
            if row is None:
                raise DomainValidationError(f"unknown exam: {exam_id}")
            if row["status"] != "draft":
                raise InvalidTransitionError("only draft exam definitions can be replaced")
            conn.execute(
                """
                UPDATE exam_definitions SET
                    title = ?, description = ?, duration_seconds = ?, question_count = ?,
                    pass_score = ?, blueprint_json = ?, revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    normalized.title,
                    normalized.description,
                    normalized.duration_seconds,
                    normalized.question_count,
                    normalized.pass_score,
                    _json(dict(normalized.blueprint)),
                    now,
                    target_id,
                ),
            )
        return self.get_exam(target_id)

    def list_exams(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in {"draft", "active", "retired"}:
            raise DomainValidationError(f"unsupported exam status: {status}")
        with self.store.connect() as conn:
            if status is None:
                rows = conn.execute("SELECT id FROM exam_definitions ORDER BY title, id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM exam_definitions WHERE status = ? ORDER BY title, id",
                    (status,),
                ).fetchall()
        return [self.get_exam(str(row["id"])) for row in rows]

    def activate_exam(self, exam_id: str, *, actor_id: str) -> dict[str, Any]:
        _require_actor(actor_id)
        now = _now()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM exam_definitions WHERE id = ?", (exam_id.strip(),)
            ).fetchone()
            if row is None:
                raise DomainValidationError(f"unknown exam: {exam_id}")
            if row["status"] == "retired":
                raise InvalidTransitionError("retired exam cannot be activated")
            blueprint = json.loads(row["blueprint_json"])
            counts = {
                str(item["area"]): int(item["count"])
                for item in conn.execute(
                    """
                    SELECT qv.area, COUNT(*) AS count
                    FROM question_versions qv
                    JOIN questions q ON q.id = qv.question_id
                    WHERE q.exam_id = ? AND qv.status = 'published'
                      AND EXISTS (
                          SELECT 1
                          FROM review_bindings AS binding
                          JOIN review_events AS event
                            ON event.id = binding.review_event_id
                          WHERE binding.question_version_id = qv.id
                            AND binding.content_revision = qv.content_revision
                            AND binding.content_hash = qv.content_hash
                            AND event.action = 'reviewed'
                      )
                    GROUP BY qv.area
                    """,
                    (exam_id.strip(),),
                ).fetchall()
            }
            if blueprint:
                missing = [
                    f"{area}: requires {required}, has {counts.get(area, 0)}"
                    for area, required in blueprint.items()
                    if counts.get(area, 0) < int(required)
                ]
            else:
                available = sum(counts.values())
                required = int(row["question_count"])
                missing = (
                    [f"published questions: requires {required}, has {available}"]
                    if available < required
                    else []
                )
            if missing:
                raise InvalidTransitionError(
                    "published question blueprint is incomplete: " + "; ".join(missing)
                )
            conn.execute(
                """
                UPDATE exam_definitions
                SET status = 'active', revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (now, exam_id.strip()),
            )
        return self.get_exam(exam_id)

    def selected_published_versions(self, exam_id: str) -> list[dict[str, Any]]:
        exam = self.get_exam(exam_id)
        if exam["status"] != "active":
            raise InvalidTransitionError("exam must be active before starting an attempt")
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT qv.id, qv.area
                FROM question_versions qv
                JOIN questions q ON q.id = qv.question_id
                WHERE q.exam_id = ? AND qv.status = 'published'
                  AND EXISTS (
                      SELECT 1
                      FROM review_bindings AS binding
                      JOIN review_events AS event ON event.id = binding.review_event_id
                      WHERE binding.question_version_id = qv.id
                        AND binding.content_revision = qv.content_revision
                        AND binding.content_hash = qv.content_hash
                        AND event.action = 'reviewed'
                  )
                ORDER BY qv.area, q.stable_id, qv.version
                """,
                (exam_id.strip(),),
            ).fetchall()
        selected_ids: list[str] = []
        blueprint = exam["blueprint"]
        if blueprint:
            for area, required in sorted(blueprint.items()):
                matches = [str(row["id"]) for row in rows if row["area"] == area]
                selected_ids.extend(matches[: int(required)])
        else:
            selected_ids = [str(row["id"]) for row in rows[: int(exam["question_count"])]]
        if len(selected_ids) != int(exam["question_count"]):
            raise InvalidTransitionError("active exam no longer has enough published questions")
        return [self.get_question_version(version_id) for version_id in selected_ids]

    def create_question_version(
        self, draft: QuestionVersionDraft, *, actor_id: str
    ) -> dict[str, Any]:
        normalized = draft.normalized()
        actor = _require_actor(actor_id)
        now = _now()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version_id = self._insert_question_version(conn, normalized, actor=actor, now=now)
        return self.get_question_version(version_id)

    def _insert_question_version(
        self,
        conn: sqlite3.Connection,
        draft: QuestionVersionDraft,
        *,
        actor: str,
        now: str,
    ) -> str:
        """Insert one normalized draft using the caller's transaction."""
        digest = _content_hash(draft)
        exam = conn.execute(
            "SELECT id FROM exam_definitions WHERE id = ?", (draft.exam_id,)
        ).fetchone()
        if exam is None:
            raise DomainValidationError(f"unknown exam: {draft.exam_id}")

        question = conn.execute(
            "SELECT id FROM questions WHERE exam_id = ? AND stable_id = ?",
            (draft.exam_id, draft.stable_id),
        ).fetchone()
        if question is None:
            question_id = f"q_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO questions (id, exam_id, stable_id, created_at) VALUES (?, ?, ?, ?)",
                (question_id, draft.exam_id, draft.stable_id, now),
            )
        else:
            question_id = str(question["id"])

        duplicate = conn.execute(
            "SELECT id FROM question_versions WHERE question_id = ? AND content_hash = ?",
            (question_id, digest),
        ).fetchone()
        if duplicate is not None:
            raise DuplicateRecordError(
                f"identical question content already exists: {duplicate['id']}"
            )
        active_draft = conn.execute(
            "SELECT id FROM question_versions WHERE question_id = ? AND status = 'draft'",
            (question_id,),
        ).fetchone()
        if active_draft is not None:
            raise InvalidTransitionError(
                f"question already has an editable draft: {active_draft['id']}"
            )
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM question_versions WHERE question_id = ?",
            (question_id,),
        ).fetchone()
        version = int(row[0])
        version_id = f"qv_{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO question_versions (
                id, question_id, version, stem, options_json, correct_option_key,
                area, explanation, hints_json, source_json, content_hash, status,
                created_by, created_at, updated_at, updated_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?)
            """,
            (
                version_id,
                question_id,
                version,
                draft.stem,
                _json([{"key": item.key, "text": item.text} for item in draft.choices]),
                draft.correct_option_key,
                draft.area,
                draft.explanation,
                _json(list(draft.hints)),
                _json(dict(draft.source)),
                digest,
                actor,
                now,
                now,
                actor,
            ),
        )
        return version_id

    def replace_draft(
        self, version_id: str, draft: QuestionVersionDraft, *, actor_id: str
    ) -> dict[str, Any]:
        normalized = draft.normalized()
        actor = _require_actor(actor_id)
        digest = _content_hash(normalized)
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT qv.status, qv.content_hash, q.exam_id, q.stable_id
                FROM question_versions qv JOIN questions q ON q.id = qv.question_id
                WHERE qv.id = ?
                """,
                (version_id,),
            ).fetchone()
            if row is None:
                raise DomainValidationError(f"unknown question version: {version_id}")
            if row["status"] != "draft":
                raise ImmutableVersionError("only draft question versions can be replaced")
            if (row["exam_id"], row["stable_id"]) != (
                normalized.exam_id,
                normalized.stable_id,
            ):
                raise DomainValidationError("exam_id and stable_id cannot be changed")
            if row["content_hash"] != digest:
                try:
                    conn.execute(
                        """
                    UPDATE question_versions SET
                        stem = ?, options_json = ?, correct_option_key = ?, area = ?,
                        explanation = ?, hints_json = ?, source_json = ?, content_hash = ?,
                        content_revision = content_revision + 1, updated_at = ?, updated_by = ?
                    WHERE id = ?
                    """,
                        (
                            normalized.stem,
                            _json(
                                [
                                    {"key": item.key, "text": item.text}
                                    for item in normalized.choices
                                ]
                            ),
                            normalized.correct_option_key,
                            normalized.area,
                            normalized.explanation,
                            _json(list(normalized.hints)),
                            _json(dict(normalized.source)),
                            digest,
                            _now(),
                            actor,
                            version_id,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise DuplicateRecordError("identical question content already exists") from exc
        return self.get_question_version(version_id)

    def review_question_version(
        self, version_id: str, *, actor_id: str, note: str = ""
    ) -> dict[str, Any]:
        actor = _require_actor(actor_id)
        now = _now()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT
                    qv.status,
                    qv.content_revision,
                    qv.content_hash,
                    EXISTS (
                        SELECT 1 FROM review_events
                        WHERE question_version_id = qv.id AND action = 'reviewed'
                    ) AS has_legacy_review,
                    EXISTS (
                        SELECT 1
                        FROM review_bindings AS binding
                        JOIN review_events AS event ON event.id = binding.review_event_id
                        WHERE binding.question_version_id = qv.id
                          AND binding.content_revision = qv.content_revision
                          AND binding.content_hash = qv.content_hash
                          AND event.action = 'reviewed'
                    ) AS has_current_review
                FROM question_versions AS qv WHERE qv.id = ?
                """,
                (version_id,),
            ).fetchone()
            if row is None:
                raise DomainValidationError(f"unknown question version: {version_id}")
            is_legacy_publication = (
                row["status"] == "published"
                and row["has_legacy_review"]
                and not row["has_current_review"]
            )
            if row["status"] != "draft" and not is_legacy_publication:
                raise InvalidTransitionError(
                    "only draft or unverified legacy published versions can be reviewed"
                )
            cursor = conn.execute(
                """
                INSERT INTO review_events (question_version_id, action, actor_id, note, created_at)
                VALUES (?, 'reviewed', ?, ?, ?)
                """,
                (version_id, actor, note.strip(), now),
            )
            conn.execute(
                """
                INSERT INTO review_bindings (
                    review_event_id, question_version_id, content_revision, content_hash
                ) VALUES (?, ?, ?, ?)
                """,
                (cursor.lastrowid, version_id, row["content_revision"], row["content_hash"]),
            )
        return self.get_question_version(version_id)

    def reject_question_version(
        self, version_id: str, *, actor_id: str, note: str = ""
    ) -> dict[str, Any]:
        actor = _require_actor(actor_id)
        now = _now()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self._version_status(conn, version_id) != "draft":
                raise InvalidTransitionError("only draft versions can be rejected")
            conn.execute(
                "UPDATE question_versions SET status = 'rejected', updated_at = ? WHERE id = ?",
                (now, version_id),
            )
            conn.execute(
                """
                INSERT INTO review_events (question_version_id, action, actor_id, note, created_at)
                VALUES (?, 'rejected', ?, ?, ?)
                """,
                (version_id, actor, note.strip(), now),
            )
        return self.get_question_version(version_id)

    def retire_question_version(self, version_id: str, *, actor_id: str) -> dict[str, Any]:
        actor = _require_actor(actor_id)
        now = _now()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self._version_status(conn, version_id) != "published":
                raise InvalidTransitionError("only published versions can be retired")
            conn.execute(
                "UPDATE question_versions SET status = 'retired', updated_at = ? WHERE id = ?",
                (now, version_id),
            )
            conn.execute(
                """
                INSERT INTO review_events (question_version_id, action, actor_id, note, created_at)
                VALUES (?, 'retired', ?, '', ?)
                """,
                (version_id, actor, now),
            )
        return self.get_question_version(version_id)

    def publish_question_version(self, version_id: str, *, actor_id: str) -> dict[str, Any]:
        actor = _require_actor(actor_id)
        now = _now()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT question_id, status, content_revision, content_hash
                FROM question_versions WHERE id = ?
                """,
                (version_id,),
            ).fetchone()
            if row is None:
                raise DomainValidationError(f"unknown question version: {version_id}")
            if row["status"] != "draft":
                raise InvalidTransitionError("only draft versions can be published")
            reviewed = conn.execute(
                """
                SELECT 1
                FROM review_bindings AS binding
                JOIN review_events AS event ON event.id = binding.review_event_id
                WHERE binding.question_version_id = ?
                  AND binding.content_revision = ?
                  AND binding.content_hash = ?
                  AND event.action = 'reviewed'
                LIMIT 1
                """,
                (version_id, row["content_revision"], row["content_hash"]),
            ).fetchone()
            if reviewed is None:
                raise InvalidTransitionError("current revision must be reviewed before publication")

            current = conn.execute(
                """
                SELECT id FROM question_versions
                WHERE question_id = ? AND status = 'published'
                """,
                (row["question_id"],),
            ).fetchone()
            if current is not None:
                conn.execute(
                    "UPDATE question_versions SET status = 'retired', updated_at = ? WHERE id = ?",
                    (now, current["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO review_events
                        (question_version_id, action, actor_id, note, created_at)
                    VALUES (?, 'retired', ?, 'superseded by a newer version', ?)
                    """,
                    (current["id"], actor, now),
                )
            conn.execute(
                """
                UPDATE question_versions
                SET status = 'published', published_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, version_id),
            )
            conn.execute(
                """
                INSERT INTO review_events
                    (question_version_id, action, actor_id, note, created_at)
                VALUES (?, 'published', ?, '', ?)
                """,
                (version_id, actor, now),
            )
        return self.get_question_version(version_id)

    def get_question_version(self, version_id: str) -> dict[str, Any]:
        with self.store.connect() as conn:
            row = conn.execute(
                """
                SELECT qv.*, q.exam_id, q.stable_id
                FROM question_versions qv JOIN questions q ON q.id = qv.question_id
                WHERE qv.id = ?
                """,
                (version_id,),
            ).fetchone()
            if row is None:
                raise DomainValidationError(f"unknown question version: {version_id}")
            review = conn.execute(
                """
                SELECT event.actor_id, event.note, event.created_at, binding.content_revision
                FROM review_bindings AS binding
                JOIN review_events AS event ON event.id = binding.review_event_id
                WHERE binding.question_version_id = ?
                  AND binding.content_revision = ?
                  AND binding.content_hash = ?
                  AND event.action = 'reviewed'
                ORDER BY event.id DESC LIMIT 1
                """,
                (version_id, row["content_revision"], row["content_hash"]),
            ).fetchone()
            review_summary = conn.execute(
                """
                SELECT
                    EXISTS(
                        SELECT 1 FROM review_events
                        WHERE question_version_id = ? AND action = 'reviewed'
                    ) AS has_review,
                    EXISTS(
                        SELECT 1 FROM review_bindings WHERE question_version_id = ?
                    ) AS has_binding
                """,
                (version_id, version_id),
            ).fetchone()
        result = dict(row)
        result["choices"] = json.loads(result.pop("options_json"))
        result["hints"] = json.loads(result.pop("hints_json"))
        result["source"] = json.loads(result.pop("source_json"))
        result["reviewed_by"] = str(review["actor_id"]) if review is not None else None
        result["review_note"] = str(review["note"]) if review is not None else None
        result["reviewed_at"] = str(review["created_at"]) if review is not None else None
        result["reviewed_revision"] = (
            int(review["content_revision"]) if review is not None else None
        )
        if review is not None:
            result["review_binding_state"] = "current"
        elif review_summary is not None and review_summary["has_review"]:
            result["review_binding_state"] = (
                "stale" if review_summary["has_binding"] else "legacy_unverified"
            )
        else:
            result["review_binding_state"] = "unreviewed"
        return result

    def list_question_versions(self, *, status: str) -> list[dict[str, Any]]:
        if status not in {"draft", "rejected", "published", "retired"}:
            raise DomainValidationError(f"unsupported question version status: {status}")
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM question_versions
                WHERE status = ? ORDER BY created_at, id
                """,
                (status,),
            ).fetchall()
        return [self.get_question_version(str(row["id"])) for row in rows]

    @staticmethod
    def _version_status(conn: sqlite3.Connection, version_id: str) -> str:
        row = conn.execute(
            "SELECT status FROM question_versions WHERE id = ?", (version_id,)
        ).fetchone()
        if row is None:
            raise DomainValidationError(f"unknown question version: {version_id}")
        return str(row["status"])


__all__ = ["CatalogService"]
