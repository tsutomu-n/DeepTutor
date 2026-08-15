from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any, Literal, Mapping
import uuid

from .domain import (
    DomainValidationError,
    DuplicateRecordError,
    ExamSpec,
    ImmutableVersionError,
    InvalidTransitionError,
    OfficialPassingScoreSource,
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
        official_source_json = (
            _json(normalized.official_passing_score_source.as_dict())
            if normalized.official_passing_score_source is not None
            else None
        )
        try:
            with self.store.connect() as conn:
                if normalized.official_passing_score is None:
                    conn.execute(
                        """
                        INSERT INTO exam_definitions (
                            id, title, description, duration_seconds, question_count,
                            blueprint_json, status, revision, created_by,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'draft', 1, ?, ?, ?)
                        """,
                        (
                            normalized.id,
                            normalized.title,
                            normalized.description,
                            normalized.duration_seconds,
                            normalized.question_count,
                            _json(dict(normalized.blueprint)),
                            actor,
                            now,
                            now,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO exam_definitions (
                            id, title, description, duration_seconds, question_count,
                            blueprint_json, official_passing_score,
                            official_passing_score_source_json, status, revision,
                            created_by, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', 1, ?, ?, ?)
                        """,
                        (
                            normalized.id,
                            normalized.title,
                            normalized.description,
                            normalized.duration_seconds,
                            normalized.question_count,
                            _json(dict(normalized.blueprint)),
                            normalized.official_passing_score,
                            official_source_json,
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
        result.pop("pass_score", None)
        result["blueprint"] = json.loads(result.pop("blueprint_json"))
        source_json = result.pop("official_passing_score_source_json", None)
        result.setdefault("official_passing_score", None)
        result["official_passing_score_source"] = (
            OfficialPassingScoreSource.from_value(json.loads(source_json)).as_dict()
            if source_json is not None
            else None
        )
        return result

    def get_legacy_practice_target(self, exam_id: str) -> int | None:
        """Read the retired pass_score column solely as a personal-target candidate."""
        target_id = exam_id.strip()
        with self.store.connect() as conn:
            row = conn.execute(
                """
                SELECT pass_score, typeof(pass_score) AS pass_score_type,
                       question_count
                FROM exam_definitions WHERE id = ?
                """,
                (target_id,),
            ).fetchone()
        if row is None:
            raise DomainValidationError(f"unknown exam: {exam_id}")
        score = row["pass_score"]
        if score is None:
            return None
        if row["pass_score_type"] != "integer":
            return None
        candidate = int(score)
        return candidate if 0 <= candidate <= int(row["question_count"]) else None

    def set_official_passing_score(
        self,
        exam_id: str,
        *,
        score: int | None,
        source: OfficialPassingScoreSource | Mapping[str, Any] | None,
        actor_id: str,
    ) -> dict[str, Any]:
        """Set evidence-backed official scoring for future attempt snapshots."""
        _require_actor(actor_id)
        target_id = exam_id.strip()
        if isinstance(score, bool) or (score is not None and not isinstance(score, int)):
            raise DomainValidationError("official passing score must be an integer")
        if score is not None and score < 0:
            raise DomainValidationError("official passing score cannot be negative")
        if (score is None) != (source is None):
            raise DomainValidationError("official passing score and source must be set together")
        normalized_source = (
            OfficialPassingScoreSource.from_value(source).as_dict() if source is not None else None
        )
        encoded_source = _json(normalized_source) if normalized_source is not None else None
        now = _now()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT status, question_count, official_passing_score,
                       official_passing_score_source_json
                FROM exam_definitions WHERE id = ?
                """,
                (target_id,),
            ).fetchone()
            if row is None:
                raise DomainValidationError(f"unknown exam: {exam_id}")
            if row["status"] == "retired":
                raise InvalidTransitionError(
                    "retired exam official passing score cannot be changed"
                )
            if score is not None and score > int(row["question_count"]):
                raise DomainValidationError(
                    "official passing score must be between zero and question_count"
                )
            existing_source = (
                json.loads(row["official_passing_score_source_json"])
                if row["official_passing_score_source_json"] is not None
                else None
            )
            if row["official_passing_score"] != score or existing_source != normalized_source:
                conn.execute(
                    """
                    UPDATE exam_definitions SET
                        official_passing_score = ?,
                        official_passing_score_source_json = ?,
                        revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (score, encoded_source, now, target_id),
                )
        return self.get_exam(target_id)

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
                """
                SELECT status, official_passing_score
                FROM exam_definitions WHERE id = ?
                """,
                (target_id,),
            ).fetchone()
            if row is None:
                raise DomainValidationError(f"unknown exam: {exam_id}")
            if row["status"] != "draft":
                raise InvalidTransitionError("only draft exam definitions can be replaced")
            conn.execute(
                """
                UPDATE exam_definitions SET
                    title = ?, description = ?, duration_seconds = ?, question_count = ?,
                    blueprint_json = ?, official_passing_score = ?,
                    official_passing_score_source_json = ?,
                    revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    normalized.title,
                    normalized.description,
                    normalized.duration_seconds,
                    normalized.question_count,
                    _json(dict(normalized.blueprint)),
                    normalized.official_passing_score,
                    (
                        _json(normalized.official_passing_score_source.as_dict())
                        if normalized.official_passing_score_source is not None
                        else None
                    ),
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

    def retire_question_version(
        self,
        version_id: str,
        *,
        actor_id: str,
        reason: Literal["invalid_content"],
        note: str = "",
    ) -> dict[str, Any]:
        actor = _require_actor(actor_id)
        if reason != "invalid_content":
            raise DomainValidationError(
                "manual retirement reason must be invalid_content; "
                "superseded is recorded when a replacement is published"
            )
        now = _now()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT status, retirement_reason
                FROM question_versions WHERE id = ?
                """,
                (version_id,),
            ).fetchone()
            if row is None:
                raise DomainValidationError(f"unknown question version: {version_id}")
            if row["status"] == "published":
                conn.execute(
                    """
                    UPDATE question_versions SET
                        status = 'retired', retirement_reason = 'invalid_content',
                        retired_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, version_id),
                )
            elif row["status"] == "retired" and row["retirement_reason"] in {
                None,
                "superseded",
            }:
                conn.execute(
                    """
                    UPDATE question_versions SET
                        retirement_reason = 'invalid_content', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, version_id),
                )
            elif row["status"] == "retired":
                raise InvalidTransitionError("question version is already invalidated")
            else:
                raise InvalidTransitionError(
                    "only published or previously retired versions can be invalidated"
                )
            conn.execute(
                """
                INSERT INTO review_events (question_version_id, action, actor_id, note, created_at)
                VALUES (?, 'retired', ?, ?, ?)
                """,
                (version_id, actor, note.strip(), now),
            )
        return self.get_question_version(version_id)

    def classify_legacy_retirement(
        self,
        version_id: str,
        *,
        replacement_version_id: str,
        actor_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Classify one migrated, unclassified retirement using verified evidence."""
        actor = _require_actor(actor_id)
        replacement_id = replacement_version_id.strip()
        if not replacement_id:
            raise DomainValidationError("replacement question version is required")
        now = _now()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT question_id, version, status, retirement_reason
                FROM question_versions WHERE id = ?
                """,
                (version_id,),
            ).fetchone()
            if row is None:
                raise DomainValidationError(f"unknown question version: {version_id}")
            if row["status"] != "retired":
                raise InvalidTransitionError("only a retired question version can be classified")
            if row["retirement_reason"] is not None:
                raise InvalidTransitionError("legacy retirement is already classified")
            replacement = conn.execute(
                """
                SELECT question_id, version, status, retirement_reason
                FROM question_versions WHERE id = ?
                """,
                (replacement_id,),
            ).fetchone()
            if replacement is None:
                raise DomainValidationError(
                    f"unknown replacement question version: {replacement_id}"
                )
            replacement_is_valid_history = replacement["status"] == "published" or (
                replacement["status"] == "retired"
                and replacement["retirement_reason"] == "superseded"
            )
            if (
                replacement_id == version_id
                or replacement["question_id"] != row["question_id"]
                or int(replacement["version"]) <= int(row["version"])
                or not replacement_is_valid_history
            ):
                raise DomainValidationError(
                    "replacement must be a later valid version of the same question"
                )
            updated = conn.execute(
                """
                UPDATE question_versions SET
                    retirement_reason = 'superseded',
                    replacement_question_version_id = ?, updated_at = ?
                WHERE id = ? AND status = 'retired' AND retirement_reason IS NULL
                """,
                (replacement_id, now, version_id),
            )
            if updated.rowcount != 1:
                raise InvalidTransitionError("legacy retirement was already classified")
            audit_note = f"classified_superseded_by:{replacement_id}"
            if note.strip():
                audit_note = f"{audit_note}\n{note.strip()}"
            conn.execute(
                """
                INSERT INTO review_events (question_version_id, action, actor_id, note, created_at)
                VALUES (?, 'retired', ?, ?, ?)
                """,
                (version_id, actor, audit_note, now),
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
                    """
                    UPDATE question_versions SET
                        status = 'retired', retirement_reason = 'superseded',
                        retired_at = ?, replacement_question_version_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, version_id, now, current["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO review_events
                        (question_version_id, action, actor_id, note, created_at)
                    VALUES (?, 'retired', ?, ?, ?)
                    """,
                    (current["id"], actor, f"superseded_by:{version_id}", now),
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

    def get_question_version_states(
        self, version_ids: list[str] | tuple[str, ...] | set[str]
    ) -> dict[str, dict[str, Any]]:
        """Read retirement state for a set of immutable catalog versions."""
        normalized = sorted({str(version_id).strip() for version_id in version_ids})
        if any(not version_id for version_id in normalized):
            raise DomainValidationError("question version id is required")
        if not normalized:
            return {}
        result: dict[str, dict[str, Any]] = {}
        with self.store.connect() as conn:
            for offset in range(0, len(normalized), 500):
                batch = normalized[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"""
                    SELECT id, status, retirement_reason, retired_at, updated_at,
                           replacement_question_version_id
                    FROM question_versions
                    WHERE id IN ({placeholders})
                    """,
                    tuple(batch),
                ).fetchall()
                result.update({str(row["id"]): dict(row) for row in rows})
        missing = set(normalized) - set(result)
        if missing:
            raise DomainValidationError(f"unknown question version: {sorted(missing)[0]}")
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
