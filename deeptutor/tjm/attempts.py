from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Literal
import unicodedata
import uuid

from .catalog import CatalogService
from .domain import DomainValidationError, InvalidTransitionError, grade_responses
from .storage import LearningStore

AttemptMode = Literal["practice", "exam", "review"]


def _now_datetime() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class AttemptNotFoundError(DomainValidationError):
    pass


class AlreadySubmittedError(InvalidTransitionError):
    pass


class AttemptExpiredError(InvalidTransitionError):
    pass


class IdempotencyConflictError(InvalidTransitionError):
    pass


@dataclass(frozen=True, slots=True)
class ReviewPolicy:
    low_confidence_threshold: int = 50
    slow_correct_ms: int = 60_000

    def __post_init__(self) -> None:
        if not 0 <= self.low_confidence_threshold <= 100:
            raise DomainValidationError("low_confidence_threshold must be between 0 and 100")
        if self.slow_correct_ms <= 0:
            raise DomainValidationError("slow_correct_ms must be positive")


_KANJI_ORDINALS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def _recognized_choice_key(transcript: str, choices: list[dict[str, Any]]) -> str | None:
    """Map an explicit spoken ordinal/key to one choice, failing closed on ambiguity."""
    normalized = unicodedata.normalize("NFKC", transcript).strip()
    candidates: set[str] = set()
    for match in re.finditer(r"(?<!\d)(\d+)\s*(?:番|ばん|番目|つ目)", normalized):
        ordinal = int(match.group(1))
        if 1 <= ordinal <= len(choices):
            candidates.add(str(choices[ordinal - 1]["key"]))
    for kanji, ordinal in _KANJI_ORDINALS.items():
        if re.search(rf"{kanji}\s*(?:番|ばん|番目|つ目)", normalized):
            if ordinal <= len(choices):
                candidates.add(str(choices[ordinal - 1]["key"]))
    for choice in choices:
        key = str(choice["key"])
        if normalized.casefold() == key.casefold():
            candidates.add(key)
    return next(iter(candidates)) if len(candidates) == 1 else None


class AttemptService:
    """Owner-scoped attempt lifecycle backed by immutable catalog versions."""

    def __init__(
        self,
        catalog: CatalogService,
        learning: LearningStore,
        *,
        owner_id: str,
        review_policy: ReviewPolicy | None = None,
    ) -> None:
        owner = owner_id.strip()
        if not owner:
            raise DomainValidationError("owner_id is required")
        self.catalog = catalog
        self.learning = learning
        self.owner_id = owner
        self.review_policy = review_policy or ReviewPolicy()

    def start_attempt(
        self, *, exam_id: str, mode: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        if mode not in {"practice", "exam"}:
            raise DomainValidationError(f"unsupported attempt mode: {mode}")
        canonical_exam_id = self._normalize_exam_id(exam_id)
        request_payload = {"exam_id": canonical_exam_id, "mode": mode}
        replay = self._start_command_replay(
            idempotency_key=idempotency_key,
            command_type="start_attempt",
            target_id=f"{canonical_exam_id}:{mode}",
            request_payload=request_payload,
        )
        if replay is not None:
            return replay
        try:
            exam = self.catalog.get_exam(canonical_exam_id)
            versions = self.catalog.selected_published_versions(canonical_exam_id)
        except (DomainValidationError, InvalidTransitionError):
            replay = self._start_command_replay(
                idempotency_key=idempotency_key,
                command_type="start_attempt",
                target_id=f"{canonical_exam_id}:{mode}",
                request_payload=request_payload,
            )
            if replay is not None:
                return replay
            raise
        return self._create_attempt(
            exam=exam,
            versions=versions,
            mode=mode,
            idempotency_key=idempotency_key,
            command_type="start_attempt",
            request_payload=request_payload,
        )

    def _create_attempt(
        self,
        *,
        exam: dict[str, Any],
        versions: list[dict[str, Any]],
        mode: str,
        idempotency_key: str | None = None,
        command_type: str,
        request_payload: dict[str, Any],
    ) -> dict[str, Any]:
        key = self._normalize_idempotency_key(idempotency_key)
        target_id = f"{exam['id']}:{mode}"
        request_hash = self._request_hash(request_payload)
        with self.learning.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            started = _now_datetime()
            replay = self._command_replay(
                conn,
                idempotency_key=key,
                command_type=command_type,
                target_id=target_id,
                request_hash=request_hash,
            )
            if isinstance(replay, IdempotencyConflictError):
                raise replay
            if replay is not None:
                replay_attempt_id = replay.get("id")
                if isinstance(replay_attempt_id, str):
                    self._finalize_if_expired(conn, replay_attempt_id, started)
                return replay
            deadline = (
                started + timedelta(seconds=int(exam["duration_seconds"]))
                if mode == "exam"
                else None
            )
            attempt_id = f"att_{uuid.uuid4().hex}"
            snapshot = {
                "id": exam["id"],
                "title": exam["title"],
                "description": exam["description"],
                "duration_seconds": exam["duration_seconds"],
                "question_count": exam["question_count"],
                "pass_score": exam["pass_score"],
                "blueprint": exam["blueprint"],
                "revision": exam["revision"],
            }
            conn.execute(
                """
                INSERT INTO attempts (
                    id, exam_id, mode, status, exam_snapshot_json,
                    started_at, deadline_at
                ) VALUES (?, ?, ?, 'in_progress', ?, ?, ?)
                """,
                (
                    attempt_id,
                    exam["id"],
                    mode,
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    _timestamp(started),
                    _timestamp(deadline) if deadline else None,
                ),
            )
            for position, version in enumerate(versions):
                conn.execute(
                    """
                    INSERT INTO attempt_items (
                        attempt_id, position, question_version_id, area
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (attempt_id, position, version["id"], version["area"]),
                )
            result = self._attempt_view(conn, attempt_id)
            self._save_command(
                conn,
                idempotency_key=key,
                command_type=command_type,
                target_id=target_id,
                request_hash=request_hash,
                response=result,
                created_at=_timestamp(started),
            )
        return result

    def start_review_attempt(
        self,
        *,
        exam_id: str,
        limit: int = 20,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise DomainValidationError("review limit must be between 1 and 100")
        canonical_exam_id = self._normalize_exam_id(exam_id)
        request_payload = {"exam_id": canonical_exam_id, "limit": limit}
        replay = self._start_command_replay(
            idempotency_key=idempotency_key,
            command_type="start_review_attempt",
            target_id=f"{canonical_exam_id}:review",
            request_payload=request_payload,
        )
        if replay is not None:
            return replay
        try:
            exam = self.catalog.get_exam(canonical_exam_id)
            with self.learning.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._finalize_due_attempts(conn, _now_datetime())
                rows = conn.execute(
                    """
                    SELECT question_version_id, MAX(priority) AS priority, MIN(id) AS first_id
                    FROM review_queue
                    WHERE status = 'pending'
                    GROUP BY question_version_id
                    ORDER BY priority DESC, first_id
                    """
                ).fetchall()
            versions: list[dict[str, Any]] = []
            for row in rows:
                version = self.catalog.get_question_version(str(row["question_version_id"]))
                if version["exam_id"] == canonical_exam_id:
                    versions.append(version)
                if len(versions) >= limit:
                    break
            if not versions:
                raise InvalidTransitionError("review queue has no pending questions for this exam")
        except (DomainValidationError, InvalidTransitionError):
            replay = self._start_command_replay(
                idempotency_key=idempotency_key,
                command_type="start_review_attempt",
                target_id=f"{canonical_exam_id}:review",
                request_payload=request_payload,
            )
            if replay is not None:
                return replay
            raise
        return self._create_attempt(
            exam=exam,
            versions=versions,
            mode="review",
            idempotency_key=idempotency_key,
            command_type="start_review_attempt",
            request_payload=request_payload,
        )

    def get_attempt(self, attempt_id: str) -> dict[str, Any]:
        with self.learning.connect() as conn:
            attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
            if attempt is None:
                raise AttemptNotFoundError(f"unknown attempt: {attempt_id}")
            if attempt["status"] == "in_progress" and self._deadline_passed(
                dict(attempt), _now_datetime()
            ):
                conn.execute("BEGIN IMMEDIATE")
                self._finalize_if_expired(conn, attempt_id, _now_datetime())
            return self._attempt_view(conn, attempt_id)

    def present_item(self, attempt_id: str, *, position: int) -> dict[str, Any]:
        expired = False
        result: dict[str, Any] | None = None
        with self.learning.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now_dt = _now_datetime()
            now = _timestamp(now_dt)
            attempt, _ = self._attempt_and_item(conn, attempt_id, position)
            expired = self._finalize_if_expired(conn, attempt_id, now_dt)
            if not expired:
                self._ensure_answerable(dict(attempt))
                conn.execute(
                    """
                    UPDATE attempt_items SET
                        first_presented_at = COALESCE(first_presented_at, ?),
                        opened_at = COALESCE(opened_at, ?)
                    WHERE attempt_id = ? AND position = ?
                    """,
                    (now, now, attempt_id, position),
                )
                result = self._attempt_view(conn, attempt_id)["items"][position]
        if expired:
            raise AttemptExpiredError("exam deadline has passed")
        if result is None:
            raise RuntimeError("item presentation did not produce a result")
        return result

    def record_answer(
        self,
        attempt_id: str,
        *,
        position: int,
        selected_option_key: str,
        confidence: int | None,
        elapsed_ms: int,
        confirmed: bool,
        client_created_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        option_key = selected_option_key.strip()
        self._validate_metrics(confidence=confidence, elapsed_ms=elapsed_ms)
        if not option_key:
            raise DomainValidationError("selected choice is required")
        key = self._normalize_idempotency_key(idempotency_key)
        command_type = "record_answer"
        target_id = f"{attempt_id}:{position}"
        request_hash = self._request_hash(
            {
                "position": position,
                "selected_option_key": option_key,
                "confidence": confidence,
                "elapsed_ms": elapsed_ms,
                "confirmed": confirmed,
                "client_created_at": client_created_at,
            }
        )
        expired = False
        result: dict[str, Any] | None = None
        with self.learning.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now_dt = _now_datetime()
            now = _timestamp(now_dt)
            replay = self._command_replay(
                conn,
                idempotency_key=key,
                command_type=command_type,
                target_id=target_id,
                request_hash=request_hash,
            )
            if isinstance(replay, IdempotencyConflictError):
                if self._finalize_if_expired(conn, attempt_id, now_dt):
                    conn.commit()
                raise replay
            if replay is not None:
                self._finalize_if_expired(conn, attempt_id, now_dt)
                return replay
            attempt, item = self._attempt_and_item(conn, attempt_id, position)
            expired = self._finalize_if_expired(conn, attempt_id, now_dt)
            if not expired:
                self._ensure_answerable(dict(attempt))
                self._ensure_presented(dict(item))
                self._ensure_item_mutable(dict(attempt), dict(item))
                version = self.catalog.get_question_version(str(item["question_version_id"]))
                valid_keys = {str(choice["key"]) for choice in version["choices"]}
                if option_key not in valid_keys:
                    raise DomainValidationError(f"unknown choice key: {option_key}")
                server_elapsed_ms = self._server_elapsed_ms(item["first_presented_at"], now_dt)
                conn.execute(
                    """
                    INSERT INTO answer_events (
                        attempt_id, position, event_type, option_key, confidence,
                        elapsed_ms, client_created_at, created_at, client_event_id,
                        server_elapsed_ms, client_active_elapsed_ms
                    ) VALUES (?, ?, 'selected', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        position,
                        option_key,
                        confidence,
                        elapsed_ms,
                        client_created_at,
                        now,
                        key,
                        server_elapsed_ms,
                        elapsed_ms,
                    ),
                )
                if confidence is not None:
                    conn.execute(
                        """
                        INSERT INTO answer_events (
                            attempt_id, position, event_type, option_key, confidence,
                            elapsed_ms, client_created_at, created_at, client_event_id,
                            server_elapsed_ms, client_active_elapsed_ms
                        ) VALUES (?, ?, 'confidence', ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            attempt_id,
                            position,
                            option_key,
                            confidence,
                            elapsed_ms,
                            client_created_at,
                            now,
                            key,
                            server_elapsed_ms,
                            elapsed_ms,
                        ),
                    )
                if confirmed:
                    conn.execute(
                        """
                        INSERT INTO answer_events (
                            attempt_id, position, event_type, option_key, confidence,
                            elapsed_ms, client_created_at, created_at, client_event_id,
                            server_elapsed_ms, client_active_elapsed_ms
                        ) VALUES (?, ?, 'confirmed', ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            attempt_id,
                            position,
                            option_key,
                            confidence,
                            elapsed_ms,
                            client_created_at,
                            now,
                            key,
                            server_elapsed_ms,
                            elapsed_ms,
                        ),
                    )
                conn.execute(
                    """
                    UPDATE attempt_items SET
                        answered_at = CASE WHEN ? THEN ? ELSE answered_at END,
                        first_answered_at = CASE
                            WHEN ? THEN COALESCE(first_answered_at, ?)
                            ELSE first_answered_at
                        END,
                        final_answered_at = CASE WHEN ? THEN ? ELSE final_answered_at END,
                        server_elapsed_ms = CASE
                            WHEN ? THEN COALESCE(server_elapsed_ms, ?)
                            ELSE server_elapsed_ms
                        END,
                        confirmed_option_key = CASE WHEN ? THEN ? ELSE confirmed_option_key END,
                        confidence = ?, elapsed_ms = ?, client_active_elapsed_ms = ?
                    WHERE attempt_id = ? AND position = ?
                    """,
                    (
                        confirmed,
                        now,
                        confirmed,
                        now,
                        confirmed,
                        now,
                        confirmed,
                        server_elapsed_ms,
                        confirmed,
                        option_key,
                        confidence,
                        elapsed_ms,
                        elapsed_ms,
                        attempt_id,
                        position,
                    ),
                )
                result = self._attempt_view(conn, attempt_id)["items"][position]
                self._save_command(
                    conn,
                    idempotency_key=key,
                    command_type=command_type,
                    target_id=target_id,
                    request_hash=request_hash,
                    response=result,
                    created_at=now,
                )
        if expired:
            raise AttemptExpiredError("exam deadline has passed")
        if result is None:
            raise RuntimeError("answer command did not produce a result")
        return result

    def use_hint(
        self,
        attempt_id: str,
        *,
        position: int,
        elapsed_ms: int,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._validate_metrics(confidence=None, elapsed_ms=elapsed_ms)
        key = self._normalize_idempotency_key(idempotency_key)
        command_type = "use_hint"
        target_id = f"{attempt_id}:{position}"
        request_hash = self._request_hash({"position": position, "elapsed_ms": elapsed_ms})
        expired = False
        result: dict[str, Any] | None = None
        with self.learning.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now_dt = _now_datetime()
            now = _timestamp(now_dt)
            replay = self._command_replay(
                conn,
                idempotency_key=key,
                command_type=command_type,
                target_id=target_id,
                request_hash=request_hash,
            )
            if isinstance(replay, IdempotencyConflictError):
                if self._finalize_if_expired(conn, attempt_id, now_dt):
                    conn.commit()
                raise replay
            if replay is not None:
                self._finalize_if_expired(conn, attempt_id, now_dt)
                return replay
            attempt, item = self._attempt_and_item(conn, attempt_id, position)
            expired = self._finalize_if_expired(conn, attempt_id, now_dt)
            if not expired:
                self._ensure_answerable(dict(attempt))
                self._ensure_presented(dict(item))
                self._ensure_item_mutable(dict(attempt), dict(item))
                if attempt["mode"] == "exam":
                    raise InvalidTransitionError("hints are not available in exam mode")
                version = self.catalog.get_question_version(str(item["question_version_id"]))
                hint_index = int(item["hint_count"])
                hints = version["hints"]
                if hint_index >= len(hints):
                    raise InvalidTransitionError("no additional hint is available")
                server_elapsed_ms = self._server_elapsed_ms(item["first_presented_at"], now_dt)
                conn.execute(
                    """
                    INSERT INTO answer_events (
                        attempt_id, position, event_type, elapsed_ms, created_at,
                        client_event_id, server_elapsed_ms, client_active_elapsed_ms
                    ) VALUES (?, ?, 'hint', ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        position,
                        elapsed_ms,
                        now,
                        key,
                        server_elapsed_ms,
                        elapsed_ms,
                    ),
                )
                conn.execute(
                    """
                    UPDATE attempt_items SET hint_count = hint_count + 1
                    WHERE attempt_id = ? AND position = ?
                    """,
                    (attempt_id, position),
                )
                result = {"hint": hints[hint_index], "hint_number": hint_index + 1}
                self._save_command(
                    conn,
                    idempotency_key=key,
                    command_type=command_type,
                    target_id=target_id,
                    request_hash=request_hash,
                    response=result,
                    created_at=now,
                )
        if expired:
            raise AttemptExpiredError("exam deadline has passed")
        if result is None:
            raise RuntimeError("hint command did not produce a result")
        return result

    def record_voice_candidate(
        self,
        attempt_id: str,
        *,
        position: int,
        transcript: str,
        elapsed_ms: int,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._validate_metrics(confidence=None, elapsed_ms=elapsed_ms)
        recognized = transcript.strip()
        if not recognized:
            raise DomainValidationError("voice transcript is required")
        if len(recognized) > 2000:
            raise DomainValidationError("voice transcript exceeds 2000 characters")
        key = self._normalize_idempotency_key(idempotency_key)
        command_type = "record_voice_candidate"
        target_id = f"{attempt_id}:{position}"
        request_hash = self._request_hash(
            {"position": position, "transcript": recognized, "elapsed_ms": elapsed_ms}
        )
        expired = False
        result: dict[str, Any] | None = None
        with self.learning.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now_dt = _now_datetime()
            now = _timestamp(now_dt)
            replay = self._command_replay(
                conn,
                idempotency_key=key,
                command_type=command_type,
                target_id=target_id,
                request_hash=request_hash,
            )
            if isinstance(replay, IdempotencyConflictError):
                if self._finalize_if_expired(conn, attempt_id, now_dt):
                    conn.commit()
                raise replay
            if replay is not None:
                self._finalize_if_expired(conn, attempt_id, now_dt)
                return replay
            attempt, item = self._attempt_and_item(conn, attempt_id, position)
            expired = self._finalize_if_expired(conn, attempt_id, now_dt)
            if not expired:
                self._ensure_answerable(dict(attempt))
                self._ensure_presented(dict(item))
                self._ensure_item_mutable(dict(attempt), dict(item))
                version = self.catalog.get_question_version(str(item["question_version_id"]))
                proposed = _recognized_choice_key(recognized, version["choices"])
                server_elapsed_ms = self._server_elapsed_ms(item["first_presented_at"], now_dt)
                cursor = conn.execute(
                    """
                    INSERT INTO answer_events (
                        attempt_id, position, event_type, option_key, elapsed_ms,
                        transcript, created_at, client_event_id, server_elapsed_ms,
                        client_active_elapsed_ms
                    ) VALUES (?, ?, 'voice_candidate', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        position,
                        proposed,
                        elapsed_ms,
                        recognized,
                        now,
                        key,
                        server_elapsed_ms,
                        elapsed_ms,
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("SQLite did not return a voice candidate id")
                result = {
                    "candidate_id": int(cursor.lastrowid),
                    "transcript": recognized,
                    "proposed_option_key": proposed,
                    "elapsed_ms": elapsed_ms,
                }
                self._save_command(
                    conn,
                    idempotency_key=key,
                    command_type=command_type,
                    target_id=target_id,
                    request_hash=request_hash,
                    response=result,
                    created_at=now,
                )
        if expired:
            raise AttemptExpiredError("exam deadline has passed")
        if result is None:
            raise RuntimeError("voice candidate command did not produce a result")
        return result

    def confirm_voice_candidate(
        self,
        attempt_id: str,
        *,
        position: int,
        candidate_id: int,
        confidence: int | None,
        elapsed_ms: int,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._validate_metrics(confidence=confidence, elapsed_ms=elapsed_ms)
        key = self._normalize_idempotency_key(idempotency_key)
        command_type = "confirm_voice_candidate"
        target_id = f"{attempt_id}:{position}:{candidate_id}"
        request_hash = self._request_hash(
            {
                "position": position,
                "candidate_id": candidate_id,
                "confidence": confidence,
                "elapsed_ms": elapsed_ms,
            }
        )
        expired = False
        result: dict[str, Any] | None = None
        with self.learning.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now_dt = _now_datetime()
            now = _timestamp(now_dt)
            replay = self._command_replay(
                conn,
                idempotency_key=key,
                command_type=command_type,
                target_id=target_id,
                request_hash=request_hash,
            )
            if isinstance(replay, IdempotencyConflictError):
                if self._finalize_if_expired(conn, attempt_id, now_dt):
                    conn.commit()
                raise replay
            if replay is not None:
                self._finalize_if_expired(conn, attempt_id, now_dt)
                return replay
            attempt, item = self._attempt_and_item(conn, attempt_id, position)
            expired = self._finalize_if_expired(conn, attempt_id, now_dt)
            if not expired:
                self._ensure_answerable(dict(attempt))
                self._ensure_presented(dict(item))
                self._ensure_item_mutable(dict(attempt), dict(item))
                candidate = self._pending_voice_candidate(
                    conn, attempt_id=attempt_id, position=position, candidate_id=candidate_id
                )
                option_key = candidate["option_key"]
                if option_key is None:
                    raise InvalidTransitionError("voice candidate has no recognized choice")
                server_elapsed_ms = self._server_elapsed_ms(item["first_presented_at"], now_dt)
                conn.execute(
                    """
                    INSERT INTO answer_events (
                        attempt_id, position, event_type, option_key, confidence,
                        elapsed_ms, transcript, created_at, client_event_id,
                        server_elapsed_ms, client_active_elapsed_ms
                    ) VALUES (?, ?, 'voice_confirmed', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        position,
                        option_key,
                        confidence,
                        elapsed_ms,
                        candidate["transcript"],
                        now,
                        key,
                        server_elapsed_ms,
                        elapsed_ms,
                    ),
                )
                conn.execute(
                    """
                    UPDATE attempt_items SET
                        answered_at = ?,
                        first_answered_at = COALESCE(first_answered_at, ?),
                        final_answered_at = ?,
                        server_elapsed_ms = COALESCE(server_elapsed_ms, ?),
                        confirmed_option_key = ?, confidence = ?, elapsed_ms = ?,
                        client_active_elapsed_ms = ?
                    WHERE attempt_id = ? AND position = ?
                    """,
                    (
                        now,
                        now,
                        now,
                        server_elapsed_ms,
                        option_key,
                        confidence,
                        elapsed_ms,
                        elapsed_ms,
                        attempt_id,
                        position,
                    ),
                )
                result = self._attempt_view(conn, attempt_id)["items"][position]
                self._save_command(
                    conn,
                    idempotency_key=key,
                    command_type=command_type,
                    target_id=target_id,
                    request_hash=request_hash,
                    response=result,
                    created_at=now,
                )
        if expired:
            raise AttemptExpiredError("exam deadline has passed")
        if result is None:
            raise RuntimeError("voice confirmation did not produce a result")
        return result

    def cancel_voice_candidate(
        self,
        attempt_id: str,
        *,
        position: int,
        candidate_id: int,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        key = self._normalize_idempotency_key(idempotency_key)
        command_type = "cancel_voice_candidate"
        target_id = f"{attempt_id}:{position}:{candidate_id}"
        request_hash = self._request_hash({"position": position, "candidate_id": candidate_id})
        expired = False
        result: dict[str, Any] | None = None
        with self.learning.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now_dt = _now_datetime()
            now = _timestamp(now_dt)
            replay = self._command_replay(
                conn,
                idempotency_key=key,
                command_type=command_type,
                target_id=target_id,
                request_hash=request_hash,
            )
            if isinstance(replay, IdempotencyConflictError):
                if self._finalize_if_expired(conn, attempt_id, now_dt):
                    conn.commit()
                raise replay
            if replay is not None:
                self._finalize_if_expired(conn, attempt_id, now_dt)
                return replay
            attempt, _ = self._attempt_and_item(conn, attempt_id, position)
            expired = self._finalize_if_expired(conn, attempt_id, now_dt)
            if not expired:
                self._ensure_answerable(dict(attempt))
                candidate = self._pending_voice_candidate(
                    conn, attempt_id=attempt_id, position=position, candidate_id=candidate_id
                )
                conn.execute(
                    """
                    INSERT INTO answer_events (
                        attempt_id, position, event_type, option_key, transcript, created_at,
                        client_event_id
                    ) VALUES (?, ?, 'voice_cancelled', ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        position,
                        candidate["option_key"],
                        candidate["transcript"],
                        now,
                        key,
                    ),
                )
                result = {"candidate_id": candidate_id, "status": "cancelled"}
                self._save_command(
                    conn,
                    idempotency_key=key,
                    command_type=command_type,
                    target_id=target_id,
                    request_hash=request_hash,
                    response=result,
                    created_at=now,
                )
        if expired:
            raise AttemptExpiredError("exam deadline has passed")
        if result is None:
            raise RuntimeError("voice cancellation did not produce a result")
        return result

    def submit_attempt(
        self, attempt_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        key = self._normalize_idempotency_key(idempotency_key)
        command_type = "submit_attempt"
        target_id = attempt_id
        request_hash = self._request_hash({"attempt_id": attempt_id})
        with self.learning.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now_dt = _now_datetime()
            now = _timestamp(now_dt)
            replay = self._command_replay(
                conn,
                idempotency_key=key,
                command_type=command_type,
                target_id=target_id,
                request_hash=request_hash,
            )
            if isinstance(replay, IdempotencyConflictError):
                if self._finalize_if_expired(conn, attempt_id, now_dt):
                    conn.commit()
                raise replay
            if replay is not None:
                self._finalize_if_expired(conn, attempt_id, now_dt)
                return replay
            attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
            if attempt is None:
                raise AttemptNotFoundError(f"unknown attempt: {attempt_id}")
            if attempt["status"] == "expired":
                result = self._attempt_view(conn, attempt_id)
            elif attempt["status"] != "in_progress":
                raise AlreadySubmittedError("attempt is already finalized")
            else:
                expired = self._finalize_if_expired(conn, attempt_id, now_dt)
                if not expired:
                    self._finalize_attempt(
                        conn,
                        attempt=dict(attempt),
                        final_status="submitted",
                        submitted_at=now,
                    )
                result = self._attempt_view(conn, attempt_id)
            self._save_command(
                conn,
                idempotency_key=key,
                command_type=command_type,
                target_id=target_id,
                request_hash=request_hash,
                response=result,
                created_at=now,
            )
        return result

    def list_review_queue(self) -> list[dict[str, Any]]:
        with self.learning.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._finalize_due_attempts(conn, _now_datetime())
            rows = conn.execute(
                """
                SELECT question_version_id, reason, priority, due_at, created_at
                FROM review_queue
                WHERE status = 'pending'
                ORDER BY priority DESC, created_at, id
                """
            ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            version_id = str(row["question_version_id"])
            entry = grouped.get(version_id)
            if entry is None:
                version = self.catalog.get_question_version(version_id)
                entry = {
                    "question_version_id": version_id,
                    "stable_id": version["stable_id"],
                    "exam_id": version["exam_id"],
                    "stem": version["stem"],
                    "area": version["area"],
                    "reasons": [],
                    "priority": float(row["priority"]),
                    "due_at": row["due_at"],
                }
                grouped[version_id] = entry
            if row["reason"] not in entry["reasons"]:
                entry["reasons"].append(row["reason"])
            entry["priority"] = max(entry["priority"], float(row["priority"]))
        return list(grouped.values())

    def analytics(self) -> dict[str, Any]:
        with self.learning.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._finalize_due_attempts(conn, _now_datetime())
            attempts = conn.execute(
                """
                SELECT id, mode, submitted_at, correct_count, total_count
                FROM attempts
                WHERE status IN ('submitted', 'expired')
                ORDER BY submitted_at, id
                """
            ).fetchall()
            item_rows = conn.execute(
                """
                SELECT ai.*, a.submitted_at
                FROM attempt_items ai
                JOIN attempts a ON a.id = ai.attempt_id
                WHERE a.status IN ('submitted', 'expired')
                ORDER BY a.submitted_at, ai.attempt_id, ai.position
                """
            ).fetchall()

        confidence: dict[str, dict[str, Any]] = {
            name: {"answered": 0, "correct": 0, "accuracy": None}
            for name in ("low", "medium", "high")
        }
        by_area: dict[str, dict[str, Any]] = {}
        total = len(item_rows)
        answered = 0
        correct = 0
        elapsed_values: list[int] = []
        hint_items = 0
        for raw in item_rows:
            item = dict(raw)
            version = self.catalog.get_question_version(str(item["question_version_id"]))
            selected = item["confirmed_option_key"]
            item_answered = selected is not None
            item_correct = item_answered and selected == version["correct_option_key"]
            answered += int(item_answered)
            correct += int(item_correct)
            if item["server_elapsed_ms"] is not None and item_answered:
                elapsed_values.append(int(item["server_elapsed_ms"]))
            hint_items += int(int(item["hint_count"]) > 0)

            area = str(item["area"])
            area_result = by_area.setdefault(
                area,
                {"total": 0, "answered": 0, "correct": 0, "accuracy": None},
            )
            area_result["total"] += 1
            area_result["answered"] += int(item_answered)
            area_result["correct"] += int(item_correct)

            if item_answered and item["confidence"] is not None:
                value = int(item["confidence"])
                band = "low" if value < 50 else "medium" if value < 80 else "high"
                confidence[band]["answered"] += 1
                confidence[band]["correct"] += int(item_correct)

        for result in by_area.values():
            result["accuracy"] = result["correct"] / result["total"] if result["total"] else None
        for result in confidence.values():
            result["accuracy"] = (
                result["correct"] / result["answered"] if result["answered"] else None
            )
        trend = [
            {
                "attempt_id": row["id"],
                "mode": row["mode"],
                "submitted_at": row["submitted_at"],
                "correct": int(row["correct_count"] or 0),
                "total": int(row["total_count"] or 0),
            }
            for row in attempts
        ]
        return {
            "overall": {
                "total": total,
                "answered": answered,
                "correct": correct,
                "accuracy": correct / total if total else None,
                "average_elapsed_ms": (
                    sum(elapsed_values) / len(elapsed_values) if elapsed_values else None
                ),
                "hint_use_rate": hint_items / total if total else None,
            },
            "by_area": by_area,
            "confidence": confidence,
            "trend": trend,
        }

    def list_history(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise DomainValidationError("history limit must be between 1 and 500")
        with self.learning.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._finalize_due_attempts(conn, _now_datetime())
            rows = conn.execute(
                """
                SELECT id FROM attempts ORDER BY started_at DESC, id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self.get_attempt(str(row["id"])) for row in rows]

    def _attempt_view(self, conn, attempt_id: str) -> dict[str, Any]:
        attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if attempt is None:
            raise AttemptNotFoundError(f"unknown attempt: {attempt_id}")
        item_rows = conn.execute(
            """
            SELECT * FROM attempt_items WHERE attempt_id = ? ORDER BY position
            """,
            (attempt_id,),
        ).fetchall()
        result = dict(attempt)
        result["exam_snapshot"] = json.loads(result.pop("exam_snapshot_json"))
        result["items"] = [self._present_item(result, dict(item)) for item in item_rows]
        result["answered_count"] = sum(
            item["confirmed_option_key"] is not None for item in item_rows
        )
        return result

    def _finalize_due_attempts(self, conn, now: datetime) -> None:
        rows = conn.execute(
            """
            SELECT id, deadline_at
            FROM attempts
            WHERE status = 'in_progress' AND mode = 'exam' AND deadline_at IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            if self._deadline_passed(dict(row), now):
                self._finalize_if_expired(conn, str(row["id"]), now)

    def _finalize_if_expired(self, conn, attempt_id: str, now: datetime) -> bool:
        row = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise AttemptNotFoundError(f"unknown attempt: {attempt_id}")
        attempt = dict(row)
        if (
            attempt["status"] != "in_progress"
            or attempt["mode"] != "exam"
            or not self._deadline_passed(attempt, now)
        ):
            return False
        submitted_at = str(attempt["deadline_at"])
        self._finalize_attempt(
            conn,
            attempt=attempt,
            final_status="expired",
            submitted_at=submitted_at,
        )
        return True

    def _finalize_attempt(
        self,
        conn,
        *,
        attempt: dict[str, Any],
        final_status: Literal["submitted", "expired"],
        submitted_at: str,
    ) -> None:
        attempt_id = str(attempt["id"])
        item_rows = conn.execute(
            """
            SELECT * FROM attempt_items WHERE attempt_id = ? ORDER BY position
            """,
            (attempt_id,),
        ).fetchall()
        answer_key: dict[str, str] = {}
        responses: dict[str, str | None] = {}
        for item in item_rows:
            version_id = str(item["question_version_id"])
            version = self.catalog.get_question_version(version_id)
            answer_key[version_id] = str(version["correct_option_key"])
            responses[version_id] = item["confirmed_option_key"]
        grade = grade_responses(answer_key=answer_key, responses=responses)
        if attempt["mode"] == "review":
            version_ids = [str(item["question_version_id"]) for item in item_rows]
            placeholders = ",".join("?" for _ in version_ids)
            conn.execute(
                f"""
                UPDATE review_queue SET status = 'completed', resolved_at = ?
                WHERE status = 'pending' AND question_version_id IN ({placeholders})
                """,
                (submitted_at, *version_ids),
            )
        for item in item_rows:
            version_id = str(item["question_version_id"])
            self._enqueue_review_reasons(
                conn,
                attempt_id=attempt_id,
                item=dict(item),
                is_correct=grade.items[version_id],
                now=submitted_at,
            )
        updated = conn.execute(
            """
            UPDATE attempts SET
                status = ?, submitted_at = ?, correct_count = ?, total_count = ?
            WHERE id = ? AND status = 'in_progress'
            """,
            (final_status, submitted_at, grade.correct, grade.total, attempt_id),
        )
        if updated.rowcount != 1:
            raise AlreadySubmittedError("attempt is already finalized")

    def _start_command_replay(
        self,
        *,
        idempotency_key: str | None,
        command_type: str,
        target_id: str,
        request_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        key = self._normalize_idempotency_key(idempotency_key)
        if key is None:
            return None
        with self.learning.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = _now_datetime()
            replay = self._command_replay(
                conn,
                idempotency_key=key,
                command_type=command_type,
                target_id=target_id,
                request_hash=self._request_hash(request_payload),
            )
            if isinstance(replay, IdempotencyConflictError):
                raise replay
            if replay is None:
                return None
            replay_attempt_id = replay.get("id")
            if isinstance(replay_attempt_id, str):
                self._finalize_if_expired(conn, replay_attempt_id, now)
            return replay

    @staticmethod
    def _normalize_exam_id(value: str) -> str:
        if not isinstance(value, str):
            raise DomainValidationError("exam_id must be a string")
        exam_id = value.strip()
        if not exam_id:
            raise DomainValidationError("exam_id is required")
        return exam_id

    @staticmethod
    def _normalize_idempotency_key(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise DomainValidationError("idempotency key must be a string")
        key = value.strip()
        if not 1 <= len(key) <= 200:
            raise DomainValidationError("idempotency key must contain 1 to 200 characters")
        return key

    @staticmethod
    def _request_hash(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _command_replay(
        conn,
        *,
        idempotency_key: str | None,
        command_type: str,
        target_id: str,
        request_hash: str,
    ) -> dict[str, Any] | IdempotencyConflictError | None:
        if idempotency_key is None:
            return None
        row = conn.execute(
            "SELECT * FROM learning_commands WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        if (
            row["command_type"] != command_type
            or row["target_id"] != target_id
            or row["request_hash"] != request_hash
        ):
            return IdempotencyConflictError(
                "idempotency key was already used for a different command"
            )
        response = json.loads(str(row["response_json"]))
        if not isinstance(response, dict):
            raise RuntimeError("stored idempotency response is not an object")
        return response

    @staticmethod
    def _save_command(
        conn,
        *,
        idempotency_key: str | None,
        command_type: str,
        target_id: str,
        request_hash: str,
        response: dict[str, Any],
        created_at: str,
    ) -> None:
        if idempotency_key is None:
            return
        conn.execute(
            """
            INSERT INTO learning_commands (
                idempotency_key, command_type, target_id, request_hash,
                response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                command_type,
                target_id,
                request_hash,
                json.dumps(response, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                created_at,
            ),
        )

    @staticmethod
    def _server_elapsed_ms(first_presented_at: str | None, now: datetime) -> int | None:
        if not first_presented_at:
            return None
        presented = datetime.fromisoformat(str(first_presented_at).replace("Z", "+00:00"))
        return max(0, int((now - presented).total_seconds() * 1000))

    @staticmethod
    def _validate_metrics(*, confidence: int | None, elapsed_ms: int) -> None:
        if (
            isinstance(elapsed_ms, bool)
            or not isinstance(elapsed_ms, int)
            or not 0 <= elapsed_ms <= 9_223_372_036_854_775_807
        ):
            raise DomainValidationError("elapsed_ms must fit a non-negative SQLite integer")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, int)
            or not 0 <= confidence <= 100
        ):
            raise DomainValidationError("confidence must be between 0 and 100")

    @staticmethod
    def _attempt_and_item(conn, attempt_id: str, position: int):
        attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if attempt is None:
            raise AttemptNotFoundError(f"unknown attempt: {attempt_id}")
        item = conn.execute(
            """
            SELECT * FROM attempt_items WHERE attempt_id = ? AND position = ?
            """,
            (attempt_id, position),
        ).fetchone()
        if item is None:
            raise DomainValidationError(f"unknown attempt position: {position}")
        return attempt, item

    def _ensure_answerable(self, attempt: dict[str, Any]) -> None:
        if attempt["status"] != "in_progress":
            raise AlreadySubmittedError("attempt is already finalized")

    @staticmethod
    def _ensure_presented(item: dict[str, Any]) -> None:
        if not item["first_presented_at"]:
            raise InvalidTransitionError("attempt item must be opened before interaction")

    @staticmethod
    def _ensure_item_mutable(attempt: dict[str, Any], item: dict[str, Any]) -> None:
        if attempt["mode"] in {"practice", "review"} and item["confirmed_option_key"] is not None:
            raise InvalidTransitionError("revealed practice and review answers are immutable")

    @staticmethod
    def _pending_voice_candidate(conn, *, attempt_id: str, position: int, candidate_id: int):
        candidate = conn.execute(
            """
            SELECT * FROM answer_events
            WHERE id = ? AND attempt_id = ? AND position = ?
              AND event_type = 'voice_candidate'
            """,
            (candidate_id, attempt_id, position),
        ).fetchone()
        if candidate is None:
            raise DomainValidationError(f"unknown voice candidate: {candidate_id}")
        latest = conn.execute(
            """
            SELECT MAX(id) FROM answer_events
            WHERE attempt_id = ? AND position = ? AND event_type = 'voice_candidate'
            """,
            (attempt_id, position),
        ).fetchone()[0]
        if int(latest) != candidate_id:
            raise InvalidTransitionError("only the latest voice candidate can be resolved")
        resolution = conn.execute(
            """
            SELECT 1 FROM answer_events
            WHERE attempt_id = ? AND position = ? AND id > ?
              AND event_type IN ('voice_confirmed', 'voice_cancelled')
            LIMIT 1
            """,
            (attempt_id, position, candidate_id),
        ).fetchone()
        if resolution is not None:
            raise InvalidTransitionError("voice candidate is already resolved")
        return candidate

    def _enqueue_review_reasons(
        self,
        conn,
        *,
        attempt_id: str,
        item: dict[str, Any],
        is_correct: bool,
        now: str,
    ) -> None:
        reasons: list[tuple[str, float]] = []
        if not is_correct:
            reasons.append(("incorrect", 100.0))
        if (
            item["confidence"] is not None
            and int(item["confidence"]) < self.review_policy.low_confidence_threshold
        ):
            reasons.append(("low_confidence", 60.0))
        if int(item["hint_count"]) > 0:
            reasons.append(("hint_used", 70.0))
        if (
            is_correct
            and item["server_elapsed_ms"] is not None
            and int(item["server_elapsed_ms"]) >= self.review_policy.slow_correct_ms
        ):
            reasons.append(("slow_correct", 40.0))
        for reason, priority in reasons:
            conn.execute(
                """
                INSERT INTO review_queue (
                    question_version_id, reason, priority, due_at, status,
                    source_attempt_id, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    item["question_version_id"],
                    reason,
                    priority,
                    now,
                    attempt_id,
                    now,
                ),
            )

    @staticmethod
    def _deadline_passed(attempt: dict[str, Any], now: datetime) -> bool:
        raw = attempt.get("deadline_at")
        if not raw:
            return False
        deadline = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return now >= deadline

    def _present_item(self, attempt: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        version = self.catalog.get_question_version(str(item["question_version_id"]))
        result = {
            "position": item["position"],
            "question_version_id": item["question_version_id"],
            "stable_id": version["stable_id"],
            "stem": version["stem"],
            "choices": version["choices"],
            "area": version["area"],
            "opened_at": item["opened_at"],
            "answered_at": item["answered_at"],
            "first_presented_at": item["first_presented_at"],
            "first_answered_at": item["first_answered_at"],
            "final_answered_at": item["final_answered_at"],
            "confirmed_option_key": item["confirmed_option_key"],
            "confidence": item["confidence"],
            "elapsed_ms": item["elapsed_ms"],
            "server_elapsed_ms": item["server_elapsed_ms"],
            "client_active_elapsed_ms": item["client_active_elapsed_ms"],
            "hint_count": item["hint_count"],
        }
        may_reveal = attempt["status"] in {"submitted", "expired"} or (
            attempt["mode"] in {"practice", "review"} and item["confirmed_option_key"] is not None
        )
        if may_reveal:
            result["correct_option_key"] = version["correct_option_key"]
            result["explanation"] = version["explanation"]
            result["is_correct"] = item["confirmed_option_key"] == version["correct_option_key"]
        return result


__all__ = [
    "AlreadySubmittedError",
    "AttemptExpiredError",
    "AttemptMode",
    "AttemptNotFoundError",
    "AttemptService",
    "IdempotencyConflictError",
    "ReviewPolicy",
]
