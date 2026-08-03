from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

import deeptutor.tjm.attempts as attempts_module
from deeptutor.tjm.attempts import (
    AlreadySubmittedError,
    AttemptExpiredError,
    AttemptNotFoundError,
    AttemptService,
    IdempotencyConflictError,
)
from deeptutor.tjm.catalog import CatalogService
from deeptutor.tjm.domain import (
    Choice,
    DomainValidationError,
    ExamSpec,
    InvalidTransitionError,
    QuestionVersionDraft,
)
from deeptutor.tjm.storage import CatalogStore, LearningStore


def _catalog(tmp_path: Path) -> CatalogService:
    catalog = CatalogService(CatalogStore(tmp_path / "catalog.db"))
    catalog.create_exam(
        ExamSpec(
            id="exam-attempt",
            title="Attempt Exam",
            duration_seconds=601,
            question_count=3,
            pass_score=2,
            blueprint={"area-a": 2, "area-b": 1},
        ),
        actor_id="admin-1",
    )
    return catalog


def _publish(catalog: CatalogService, stable_id: str, area: str, correct: str = "B") -> str:
    version = catalog.create_question_version(
        QuestionVersionDraft(
            exam_id="exam-attempt",
            stable_id=stable_id,
            stem=f"Question {stable_id}",
            choices=(Choice("A", "First"), Choice("B", "Second")),
            correct_option_key=correct,
            area=area,
            explanation=f"Explanation {stable_id}",
            hints=(f"Hint {stable_id}",),
            source={"license": "test-fixture"},
        ),
        actor_id="admin-1",
    )
    catalog.review_question_version(version["id"], actor_id="reviewer-1")
    catalog.publish_question_version(version["id"], actor_id="admin-1")
    return str(version["id"])


def _activate(catalog: CatalogService) -> list[str]:
    version_ids = [
        _publish(catalog, "q-a1", "area-a"),
        _publish(catalog, "q-a2", "area-a"),
        _publish(catalog, "q-b1", "area-b"),
    ]
    catalog.activate_exam("exam-attempt", actor_id="admin-1")
    return version_ids


def test_exam_cannot_activate_until_published_blueprint_is_satisfied(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _publish(catalog, "q-a1", "area-a")
    _publish(catalog, "q-b1", "area-b")

    with pytest.raises(InvalidTransitionError, match="area-a"):
        catalog.activate_exam("exam-attempt", actor_id="admin-1")

    _publish(catalog, "q-a2", "area-a")
    activated = catalog.activate_exam("exam-attempt", actor_id="admin-1")
    assert activated["status"] == "active"


def test_attempt_order_and_exam_snapshot_survive_service_restart(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    published_ids = set(_activate(catalog))
    learning_path = tmp_path / "u_alice" / "user" / "tjm_learning.db"
    service = AttemptService(catalog, LearningStore(learning_path), owner_id="u_alice")

    created = service.start_attempt(exam_id="exam-attempt", mode="exam")
    reloaded = AttemptService(
        catalog, LearningStore(learning_path), owner_id="u_alice"
    ).get_attempt(created["id"])

    assert created["mode"] == "exam"
    assert created["status"] == "in_progress"
    assert [item["question_version_id"] for item in created["items"]] == [
        item["question_version_id"] for item in reloaded["items"]
    ]
    assert {item["question_version_id"] for item in created["items"]} == published_ids
    assert [item["position"] for item in created["items"]] == [0, 1, 2]
    assert all("correct_option_key" not in item for item in created["items"])
    assert all("explanation" not in item for item in created["items"])
    started = datetime.fromisoformat(created["started_at"].replace("Z", "+00:00"))
    deadline = datetime.fromisoformat(created["deadline_at"].replace("Z", "+00:00"))
    assert (deadline - started).total_seconds() == 601


def test_inactive_exam_cannot_start_attempt(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    service = AttemptService(catalog, LearningStore(tmp_path / "learning.db"), owner_id="u_alice")

    with pytest.raises(InvalidTransitionError, match="active"):
        service.start_attempt(exam_id="exam-attempt", mode="practice")


def test_active_exam_blocks_parallel_exam_and_practice_attempts(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    service = AttemptService(
        catalog,
        LearningStore(tmp_path / "learning.db"),
        owner_id="u_alice",
    )

    active = service.start_attempt(exam_id="exam-attempt", mode="exam")

    with pytest.raises(InvalidTransitionError, match="already in progress"):
        service.start_attempt(exam_id="exam-attempt", mode="practice")
    with pytest.raises(InvalidTransitionError, match="already in progress"):
        service.start_attempt(exam_id="exam-attempt", mode="exam")
    assert service.get_attempt(active["id"])["status"] == "in_progress"


def test_existing_practice_is_frozen_only_while_exam_is_active(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    service = AttemptService(
        catalog,
        LearningStore(tmp_path / "learning.db"),
        owner_id="u_alice",
    )

    practice = service.start_attempt(exam_id="exam-attempt", mode="practice")
    service.present_item(practice["id"], position=0)
    initial_feedback = service.record_answer(
        practice["id"],
        position=0,
        selected_option_key="B",
        confidence=70,
        elapsed_ms=100,
        confirmed=True,
        idempotency_key="practice-answer-before-exam",
    )
    exam = service.start_attempt(exam_id="exam-attempt", mode="exam")

    with pytest.raises(InvalidTransitionError, match="already in progress"):
        service.get_attempt(practice["id"])
    with pytest.raises(InvalidTransitionError, match="already in progress"):
        service.record_answer(
            practice["id"],
            position=0,
            selected_option_key="B",
            confidence=70,
            elapsed_ms=100,
            confirmed=True,
            idempotency_key="practice-answer-before-exam",
        )

    service.submit_attempt(exam["id"])
    resumed = service.record_answer(
        practice["id"],
        position=0,
        selected_option_key="B",
        confidence=70,
        elapsed_ms=100,
        confirmed=True,
        idempotency_key="practice-answer-before-exam",
    )

    assert resumed == initial_feedback


def test_active_exam_hides_same_exam_history_analytics_and_review_queue(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    service = AttemptService(
        catalog,
        LearningStore(tmp_path / "learning.db"),
        owner_id="u_alice",
    )
    practice = service.start_attempt(exam_id="exam-attempt", mode="practice")
    service.submit_attempt(practice["id"])
    assert service.list_review_queue()
    assert service.analytics()["overall"]["total"] == 3

    exam = service.start_attempt(exam_id="exam-attempt", mode="exam")

    assert [attempt["id"] for attempt in service.list_history()] == [exam["id"]]
    assert service.analytics()["overall"]["total"] == 0
    assert service.list_review_queue() == []
    with pytest.raises(InvalidTransitionError, match="already in progress"):
        service.start_review_attempt(exam_id="exam-attempt")

    service.submit_attempt(exam["id"])
    assert {attempt["id"] for attempt in service.list_history()} == {
        practice["id"],
        exam["id"],
    }
    assert service.analytics()["overall"]["total"] == 6
    assert service.list_review_queue()


def test_active_exam_freezes_direct_access_and_submit_replay_for_prior_exam(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    service = AttemptService(
        catalog,
        LearningStore(tmp_path / "learning.db"),
        owner_id="u_alice",
    )
    prior_exam = service.start_attempt(exam_id="exam-attempt", mode="exam")
    service.present_item(prior_exam["id"], position=0)
    service.record_answer(
        prior_exam["id"],
        position=0,
        selected_option_key="B",
        confidence=90,
        elapsed_ms=100,
        confirmed=True,
    )
    submitted = service.submit_attempt(
        prior_exam["id"],
        idempotency_key="prior-exam-submit",
    )
    assert submitted["items"][0]["correct_option_key"] == "B"
    active_exam = service.start_attempt(exam_id="exam-attempt", mode="exam")

    with pytest.raises(InvalidTransitionError, match="already in progress"):
        service.get_attempt(prior_exam["id"])
    with pytest.raises(InvalidTransitionError, match="already in progress"):
        service.submit_attempt(
            prior_exam["id"],
            idempotency_key="prior-exam-submit",
        )

    service.submit_attempt(active_exam["id"])
    assert service.get_attempt(prior_exam["id"])["id"] == prior_exam["id"]


def test_expired_exam_is_finalized_before_parallel_attempt_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    service = AttemptService(
        catalog,
        LearningStore(tmp_path / "learning.db"),
        owner_id="u_alice",
    )
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(attempts_module, "_now_datetime", lambda: started_at)
    exam_attempt = service.start_attempt(exam_id="exam-attempt", mode="exam")

    monkeypatch.setattr(
        attempts_module,
        "_now_datetime",
        lambda: started_at + timedelta(seconds=602),
    )
    practice = service.start_attempt(exam_id="exam-attempt", mode="practice")

    assert practice["status"] == "in_progress"
    assert service.get_attempt(exam_attempt["id"])["status"] == "expired"


def test_start_attempt_idempotent_replay_precedes_active_exam_lock(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "learning.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")

    first = service.start_attempt(
        exam_id="exam-attempt",
        mode="exam",
        idempotency_key="same-active-exam",
    )
    replay = service.start_attempt(
        exam_id="exam-attempt",
        mode="exam",
        idempotency_key="same-active-exam",
    )

    assert replay == first
    with learning.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1


def test_parallel_exam_starts_create_only_one_active_attempt(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "learning.db")
    barrier = Barrier(2)

    def start(key: str) -> str:
        service = AttemptService(catalog, learning, owner_id="u_alice")
        barrier.wait()
        try:
            return service.start_attempt(
                exam_id="exam-attempt",
                mode="exam",
                idempotency_key=key,
            )["id"]
        except InvalidTransitionError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(start, ("parallel-exam-1", "parallel-exam-2")))

    assert results.count("blocked") == 1
    assert len({result for result in results if result != "blocked"}) == 1
    with learning.connect() as conn:
        assert (
            conn.execute(
                """
            SELECT COUNT(*) FROM attempts
            WHERE exam_id = 'exam-attempt' AND mode = 'exam' AND status = 'in_progress'
            """
            ).fetchone()[0]
            == 1
        )


def test_start_attempt_replay_precedes_changed_catalog_and_normalizes_exam_id(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    published_ids = _activate(catalog)
    learning = LearningStore(tmp_path / "learning.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")
    first = service.start_attempt(
        exam_id=" exam-attempt ",
        mode="exam",
        idempotency_key="start-before-catalog-change",
    )

    canonical_replay = service.start_attempt(
        exam_id="exam-attempt",
        mode="exam",
        idempotency_key="start-before-catalog-change",
    )
    service.present_item(first["id"], position=0)
    service.record_answer(
        first["id"],
        position=0,
        selected_option_key="A",
        confidence=50,
        elapsed_ms=100,
        confirmed=True,
    )
    progressed_replay = service.start_attempt(
        exam_id="exam-attempt",
        mode="exam",
        idempotency_key="start-before-catalog-change",
    )
    catalog.retire_question_version(published_ids[0], actor_id="admin-1", reason="invalid_content")
    changed_catalog_replay = service.start_attempt(
        exam_id="exam-attempt",
        mode="exam",
        idempotency_key="start-before-catalog-change",
    )

    assert canonical_replay == first
    assert progressed_replay == first
    invalidated = next(
        item
        for item in changed_catalog_replay["items"]
        if item["question_version_id"] == published_ids[0]
    )
    assert invalidated["catalog_disposition"] == "invalid_content"
    assert invalidated["grading_status"] == "content_invalidated"
    assert changed_catalog_replay["content_invalidated_count"] == 1
    with learning.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1


def test_review_attempt_cannot_bypass_review_queue(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    service = AttemptService(catalog, LearningStore(tmp_path / "learning.db"), owner_id="u_alice")

    with pytest.raises(DomainValidationError, match="unsupported attempt mode"):
        service.start_attempt(exam_id="exam-attempt", mode="review")


def test_start_review_replay_precedes_empty_queue_check(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "learning.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")
    attempt = service.start_attempt(exam_id="exam-attempt", mode="exam")
    service.submit_attempt(attempt["id"])
    first = service.start_review_attempt(
        exam_id="exam-attempt",
        idempotency_key="review-before-queue-change",
    )
    with learning.connect() as conn:
        conn.execute(
            """
            UPDATE review_queue SET
                status = 'dismissed', resolved_at = ?,
                resolution_reason = 'test_queue_cleared'
            """,
            ("2026-01-01T00:00:00Z",),
        )

    replay = service.start_review_attempt(
        exam_id="exam-attempt",
        idempotency_key="review-before-queue-change",
    )

    assert replay == first
    with learning.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 2


def test_practice_records_append_only_changes_hint_confidence_and_time(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "alice.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")
    attempt = service.start_attempt(exam_id="exam-attempt", mode="practice")
    version_id = attempt["items"][0]["question_version_id"]
    correct = catalog.get_question_version(version_id)["correct_option_key"]
    wrong = "A" if correct != "A" else "B"

    service.present_item(attempt["id"], position=0)
    hint = service.use_hint(attempt["id"], position=0, elapsed_ms=500)
    service.record_answer(
        attempt["id"],
        position=0,
        selected_option_key=wrong,
        confidence=20,
        elapsed_ms=1000,
        confirmed=False,
    )
    answered = service.record_answer(
        attempt["id"],
        position=0,
        selected_option_key=correct,
        confidence=80,
        elapsed_ms=2300,
        confirmed=True,
    )

    assert hint["hint_number"] == 1
    assert hint["hint"].startswith("Hint")
    assert answered["confirmed_option_key"] == correct
    assert answered["confidence"] == 80
    assert answered["elapsed_ms"] == 2300
    assert answered["hint_count"] == 1
    assert answered["is_correct"] is True
    assert answered["correct_option_key"] == correct
    assert answered["explanation"].startswith("Explanation")
    with learning.connect() as conn:
        events = conn.execute(
            """
            SELECT event_type, option_key, confidence, elapsed_ms, created_at
            FROM answer_events WHERE attempt_id = ? AND position = 0 ORDER BY id
            """,
            (attempt["id"],),
        ).fetchall()
    assert [event["event_type"] for event in events] == [
        "hint",
        "selected",
        "confidence",
        "selected",
        "confidence",
        "confirmed",
    ]
    assert all(event["created_at"] for event in events)


def test_item_presentation_sets_server_authoritative_elapsed_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "alice.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    monkeypatch.setattr(attempts_module, "_now_datetime", lambda: clock["now"])
    attempt = service.start_attempt(exam_id="exam-attempt", mode="practice")

    clock["now"] += timedelta(seconds=5)
    opened = service.present_item(attempt["id"], position=0)
    first_presented_at = opened["first_presented_at"]
    clock["now"] += timedelta(seconds=1)
    assert service.present_item(attempt["id"], position=0)["first_presented_at"] == (
        first_presented_at
    )
    clock["now"] += timedelta(milliseconds=2500)
    answered = service.record_answer(
        attempt["id"],
        position=0,
        selected_option_key="B",
        confidence=80,
        elapsed_ms=999_999,
        confirmed=True,
    )

    assert answered["server_elapsed_ms"] == 3500
    assert answered["client_active_elapsed_ms"] == 999_999
    assert answered["first_answered_at"] == answered["final_answered_at"]
    with learning.connect() as conn:
        event = conn.execute(
            """
            SELECT server_elapsed_ms, client_active_elapsed_ms
            FROM answer_events
            WHERE attempt_id = ? AND event_type = 'confirmed'
            """,
            (attempt["id"],),
        ).fetchone()
        assert dict(event) == {
            "server_elapsed_ms": 3500,
            "client_active_elapsed_ms": 999_999,
        }


def test_unpresented_item_rejects_screen_and_voice_interaction(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "alice.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")
    attempt = service.start_attempt(exam_id="exam-attempt", mode="exam")

    with pytest.raises(InvalidTransitionError, match="opened"):
        service.record_answer(
            attempt["id"],
            position=0,
            selected_option_key="A",
            confidence=50,
            elapsed_ms=100,
            confirmed=True,
        )
    with pytest.raises(InvalidTransitionError, match="opened"):
        service.record_voice_candidate(attempt["id"], position=0, transcript="1番", elapsed_ms=100)
    with learning.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM answer_events").fetchone()[0] == 0


def test_answer_change_preserves_first_server_time_and_updates_final_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    service = AttemptService(catalog, LearningStore(tmp_path / "alice.db"), owner_id="u_alice")
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    monkeypatch.setattr(attempts_module, "_now_datetime", lambda: clock["now"])
    attempt = service.start_attempt(exam_id="exam-attempt", mode="exam")
    service.present_item(attempt["id"], position=0)
    clock["now"] += timedelta(seconds=2)
    first = service.record_answer(
        attempt["id"],
        position=0,
        selected_option_key="A",
        confidence=40,
        elapsed_ms=20,
        confirmed=True,
    )
    clock["now"] += timedelta(seconds=5)
    changed = service.record_answer(
        attempt["id"],
        position=0,
        selected_option_key="B",
        confidence=80,
        elapsed_ms=999_999,
        confirmed=True,
    )

    assert changed["first_answered_at"] == first["first_answered_at"]
    assert changed["server_elapsed_ms"] == first["server_elapsed_ms"] == 2000
    assert changed["final_answered_at"] != first["final_answered_at"]
    assert changed["client_active_elapsed_ms"] == 999_999


def test_answer_command_is_idempotent_under_parallel_retries(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "alice.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")
    attempt = service.start_attempt(exam_id="exam-attempt", mode="exam")
    service.present_item(attempt["id"], position=0)
    request = {
        "position": 0,
        "selected_option_key": "A",
        "confidence": 50,
        "elapsed_ms": 100,
        "confirmed": True,
        "idempotency_key": "answer-command-1",
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _: service.record_answer(attempt["id"], **request), range(2))
        )

    assert results[0] == results[1]
    with learning.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM answer_events WHERE attempt_id = ?",
                (attempt["id"],),
            ).fetchone()[0]
            == 3
        )
        event_group = conn.execute(
            """
            SELECT COUNT(DISTINCT client_event_id), MIN(client_event_id), MAX(client_event_id)
            FROM answer_events WHERE attempt_id = ?
            """,
            (attempt["id"],),
        ).fetchone()
        assert tuple(event_group) == (1, "answer-command-1", "answer-command-1")
        assert conn.execute("SELECT COUNT(*) FROM learning_commands").fetchone()[0] == 1
    with pytest.raises(IdempotencyConflictError):
        service.record_answer(
            attempt["id"],
            **{**request, "selected_option_key": "B"},
        )
    restarted = AttemptService(catalog, LearningStore(learning.db_path), owner_id="u_alice")
    assert restarted.record_answer(attempt["id"], **request) == results[0]
    with pytest.raises(IdempotencyConflictError):
        service.use_hint(
            attempt["id"],
            position=0,
            elapsed_ms=100,
            idempotency_key="answer-command-1",
        )


def test_hint_and_voice_commands_replay_one_logical_event_group(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "alice.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")
    attempt = service.start_attempt(exam_id="exam-attempt", mode="practice")
    service.present_item(attempt["id"], position=0)

    first_hint = service.use_hint(
        attempt["id"], position=0, elapsed_ms=100, idempotency_key="hint-1"
    )
    assert (
        service.use_hint(attempt["id"], position=0, elapsed_ms=100, idempotency_key="hint-1")
        == first_hint
    )
    candidate = service.record_voice_candidate(
        attempt["id"],
        position=0,
        transcript="2番",
        elapsed_ms=200,
        idempotency_key="voice-candidate-1",
    )
    assert (
        service.record_voice_candidate(
            attempt["id"],
            position=0,
            transcript="2番",
            elapsed_ms=200,
            idempotency_key="voice-candidate-1",
        )
        == candidate
    )
    confirmed = service.confirm_voice_candidate(
        attempt["id"],
        position=0,
        candidate_id=candidate["candidate_id"],
        confidence=70,
        elapsed_ms=300,
        idempotency_key="voice-confirm-1",
    )
    assert (
        service.confirm_voice_candidate(
            attempt["id"],
            position=0,
            candidate_id=candidate["candidate_id"],
            confidence=70,
            elapsed_ms=300,
            idempotency_key="voice-confirm-1",
        )
        == confirmed
    )
    with learning.connect() as conn:
        events = conn.execute(
            "SELECT event_type, client_event_id FROM answer_events ORDER BY id"
        ).fetchall()
        assert [tuple(event) for event in events] == [
            ("hint", "hint-1"),
            ("voice_candidate", "voice-candidate-1"),
            ("voice_confirmed", "voice-confirm-1"),
        ]


def test_practice_answer_is_immutable_after_feedback_reveal(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    service = AttemptService(catalog, LearningStore(tmp_path / "alice.db"), owner_id="u_alice")
    attempt = service.start_attempt(exam_id="exam-attempt", mode="practice")
    service.present_item(attempt["id"], position=0)
    service.record_answer(
        attempt["id"],
        position=0,
        selected_option_key="A",
        confidence=50,
        elapsed_ms=100,
        confirmed=True,
    )

    with pytest.raises(InvalidTransitionError, match="immutable"):
        service.record_answer(
            attempt["id"],
            position=0,
            selected_option_key="B",
            confidence=90,
            elapsed_ms=200,
            confirmed=True,
        )
    with pytest.raises(InvalidTransitionError, match="immutable"):
        service.use_hint(attempt["id"], position=0, elapsed_ms=200)


def test_answer_replay_after_submit_returns_original_non_grading_response(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    service = AttemptService(catalog, LearningStore(tmp_path / "alice.db"), owner_id="u_alice")
    attempt = service.start_attempt(exam_id="exam-attempt", mode="exam")
    service.present_item(attempt["id"], position=0)
    request = {
        "position": 0,
        "selected_option_key": "A",
        "confidence": 50,
        "elapsed_ms": 100,
        "confirmed": True,
        "idempotency_key": "answer-before-submit",
    }
    first = service.record_answer(attempt["id"], **request)
    service.submit_attempt(attempt["id"])

    replay = service.record_answer(attempt["id"], **request)

    assert replay == first
    assert "correct_option_key" not in replay
    assert "explanation" not in replay
    assert "is_correct" not in replay


def test_answer_replay_after_deadline_returns_original_and_commits_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "alice.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    monkeypatch.setattr(attempts_module, "_now_datetime", lambda: clock["now"])
    attempt = service.start_attempt(exam_id="exam-attempt", mode="exam")
    service.present_item(attempt["id"], position=0)
    request = {
        "position": 0,
        "selected_option_key": "A",
        "confidence": 50,
        "elapsed_ms": 100,
        "confirmed": True,
        "idempotency_key": "answer-replay-at-deadline",
    }
    first = service.record_answer(attempt["id"], **request)
    clock["now"] = datetime.fromisoformat(attempt["deadline_at"].replace("Z", "+00:00"))

    replay = service.record_answer(attempt["id"], **request)

    assert replay == first
    finalized = service.get_attempt(attempt["id"])
    assert finalized["status"] == "expired"
    assert finalized["submitted_at"] == attempt["deadline_at"]
    with learning.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM learning_commands").fetchone()[0] == 1


def test_due_idempotency_conflict_still_commits_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "alice.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    monkeypatch.setattr(attempts_module, "_now_datetime", lambda: clock["now"])
    attempt = service.start_attempt(exam_id="exam-attempt", mode="exam")
    service.present_item(attempt["id"], position=0)
    service.record_answer(
        attempt["id"],
        position=0,
        selected_option_key="A",
        confidence=50,
        elapsed_ms=100,
        confirmed=True,
        idempotency_key="answer-conflict-at-deadline",
    )
    clock["now"] = datetime.fromisoformat(attempt["deadline_at"].replace("Z", "+00:00"))

    with pytest.raises(IdempotencyConflictError):
        service.record_answer(
            attempt["id"],
            position=0,
            selected_option_key="B",
            confidence=50,
            elapsed_ms=100,
            confirmed=True,
            idempotency_key="answer-conflict-at-deadline",
        )

    assert service.get_attempt(attempt["id"])["status"] == "expired"
    with learning.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 3


def test_submit_command_replay_returns_original_result_without_duplicate_queue(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "alice.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")
    attempt = service.start_attempt(exam_id="exam-attempt", mode="exam")

    first = service.submit_attempt(attempt["id"], idempotency_key="submit-command-1")
    with learning.connect() as conn:
        first_queue_count = conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
    _publish(catalog, "q-a1", "area-a", correct="A")
    replay = service.submit_attempt(attempt["id"], idempotency_key="submit-command-1")

    assert replay == first
    with learning.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == first_queue_count
        assert conn.execute("SELECT COUNT(*) FROM learning_commands").fetchone()[0] == 1


def test_voice_candidate_requires_explicit_confirmation_before_answer_changes(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "alice.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")
    attempt = service.start_attempt(exam_id="exam-attempt", mode="practice")
    service.present_item(attempt["id"], position=0)

    candidate = service.record_voice_candidate(
        attempt["id"], position=0, transcript="2番", elapsed_ms=900
    )
    unchanged = service.get_attempt(attempt["id"])["items"][0]

    assert candidate["transcript"] == "2番"
    assert candidate["proposed_option_key"] == "B"
    assert unchanged["confirmed_option_key"] is None

    cancelled = service.cancel_voice_candidate(
        attempt["id"], position=0, candidate_id=candidate["candidate_id"]
    )
    assert cancelled["status"] == "cancelled"
    assert service.get_attempt(attempt["id"])["items"][0]["confirmed_option_key"] is None

    replacement = service.record_voice_candidate(
        attempt["id"], position=0, transcript="二番です", elapsed_ms=1200
    )
    confirmed = service.confirm_voice_candidate(
        attempt["id"],
        position=0,
        candidate_id=replacement["candidate_id"],
        confidence=70,
        elapsed_ms=1400,
    )

    assert confirmed["confirmed_option_key"] == "B"
    assert confirmed["confidence"] == 70
    with learning.connect() as conn:
        events = conn.execute(
            """
            SELECT event_type, option_key, transcript
            FROM answer_events WHERE attempt_id = ? AND position = 0 ORDER BY id
            """,
            (attempt["id"],),
        ).fetchall()
    assert [event["event_type"] for event in events] == [
        "voice_candidate",
        "voice_cancelled",
        "voice_candidate",
        "voice_confirmed",
    ]
    assert events[-1]["option_key"] == "B"


def test_voice_candidate_is_fail_closed_for_ambiguous_or_stale_transcript(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    service = AttemptService(catalog, LearningStore(tmp_path / "alice.db"), owner_id="u_alice")
    attempt = service.start_attempt(exam_id="exam-attempt", mode="exam")
    service.present_item(attempt["id"], position=0)

    ambiguous = service.record_voice_candidate(
        attempt["id"], position=0, transcript="一番か二番", elapsed_ms=200
    )
    assert ambiguous["proposed_option_key"] is None
    with pytest.raises(InvalidTransitionError, match="recognized choice"):
        service.confirm_voice_candidate(
            attempt["id"],
            position=0,
            candidate_id=ambiguous["candidate_id"],
            confidence=50,
            elapsed_ms=300,
        )

    old = service.record_voice_candidate(
        attempt["id"], position=0, transcript="1番", elapsed_ms=400
    )
    service.record_voice_candidate(attempt["id"], position=0, transcript="2番", elapsed_ms=500)
    with pytest.raises(InvalidTransitionError, match="latest"):
        service.confirm_voice_candidate(
            attempt["id"],
            position=0,
            candidate_id=old["candidate_id"],
            confidence=50,
            elapsed_ms=600,
        )


def test_exam_withholds_answer_until_submit_and_rejects_double_submit(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    service = AttemptService(catalog, LearningStore(tmp_path / "alice.db"), owner_id="u_alice")
    attempt = service.start_attempt(exam_id="exam-attempt", mode="exam")
    first = attempt["items"][0]
    service.present_item(attempt["id"], position=0)

    answered = service.record_answer(
        attempt["id"],
        position=0,
        selected_option_key="A",
        confidence=60,
        elapsed_ms=1200,
        confirmed=True,
    )

    assert "correct_option_key" not in answered
    assert "is_correct" not in answered
    assert "explanation" not in answered
    assert "correct_option_key" not in service.get_attempt(attempt["id"])["items"][0]
    with pytest.raises(InvalidTransitionError, match="exam"):
        service.use_hint(attempt["id"], position=0, elapsed_ms=1500)

    submitted = service.submit_attempt(attempt["id"])
    assert submitted["status"] == "submitted"
    assert submitted["total_count"] == 3
    assert submitted["correct_count"] in {0, 1}
    assert submitted["items"][0]["question_version_id"] == first["question_version_id"]
    assert "correct_option_key" in submitted["items"][0]
    assert "explanation" in submitted["items"][0]
    with pytest.raises(AlreadySubmittedError):
        service.submit_attempt(attempt["id"])


def test_unknown_choice_creates_no_answer_event(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "alice.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")
    attempt = service.start_attempt(exam_id="exam-attempt", mode="practice")
    service.present_item(attempt["id"], position=0)

    with pytest.raises(DomainValidationError, match="choice"):
        service.record_answer(
            attempt["id"],
            position=0,
            selected_option_key="missing",
            confidence=50,
            elapsed_ms=100,
            confirmed=True,
        )
    with learning.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM answer_events").fetchone()[0] == 0

    with pytest.raises(DomainValidationError, match="SQLite integer"):
        service.record_answer(
            attempt["id"],
            position=0,
            selected_option_key="A",
            confidence=50,
            elapsed_ms=9_223_372_036_854_775_808,
            confirmed=True,
        )


def test_expired_exam_rejects_new_answers_but_submits_existing_state(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "alice.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")
    attempt = service.start_attempt(exam_id="exam-attempt", mode="exam")
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with learning.connect() as conn:
        conn.execute(
            "UPDATE attempts SET deadline_at = ? WHERE id = ?",
            (expired_at, attempt["id"]),
        )

    with pytest.raises(AttemptExpiredError):
        service.record_answer(
            attempt["id"],
            position=0,
            selected_option_key="A",
            confidence=50,
            elapsed_ms=999999,
            confirmed=True,
        )
    finalized = service.get_attempt(attempt["id"])
    assert finalized["status"] == "expired"
    assert finalized["answered_count"] == 0


def test_deadline_is_finalized_at_the_exact_instant_and_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "alice.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    monkeypatch.setattr(attempts_module, "_now_datetime", lambda: clock["now"])
    attempt = service.start_attempt(exam_id="exam-attempt", mode="exam")
    deadline = datetime.fromisoformat(attempt["deadline_at"].replace("Z", "+00:00"))
    clock["now"] = deadline

    with ThreadPoolExecutor(max_workers=2) as executor:
        get_future = executor.submit(service.get_attempt, attempt["id"])
        submit_future = executor.submit(service.submit_attempt, attempt["id"])
        results = [get_future.result(), submit_future.result()]

    assert {result["status"] for result in results} == {"expired"}
    assert all(result["submitted_at"] == attempt["deadline_at"] for result in results)
    with learning.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 3


def test_direct_submit_at_exact_deadline_returns_expired_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "alice.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    monkeypatch.setattr(attempts_module, "_now_datetime", lambda: clock["now"])
    attempt = service.start_attempt(exam_id="exam-attempt", mode="exam")
    clock["now"] = datetime.fromisoformat(attempt["deadline_at"].replace("Z", "+00:00"))

    finalized = service.submit_attempt(attempt["id"])

    assert finalized["status"] == "expired"
    assert finalized["submitted_at"] == attempt["deadline_at"]
    with learning.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 3


def test_attempt_id_is_not_visible_in_another_user_database(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    alice = AttemptService(catalog, LearningStore(tmp_path / "alice.db"), owner_id="u_alice")
    bob = AttemptService(catalog, LearningStore(tmp_path / "bob.db"), owner_id="u_bob")
    attempt = alice.start_attempt(exam_id="exam-attempt", mode="practice")

    with pytest.raises(AttemptNotFoundError):
        bob.get_attempt(attempt["id"])
