from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deeptutor.tjm.attempts import (
    AlreadySubmittedError,
    AttemptExpiredError,
    AttemptNotFoundError,
    AttemptService,
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


def test_review_attempt_cannot_bypass_review_queue(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    service = AttemptService(catalog, LearningStore(tmp_path / "learning.db"), owner_id="u_alice")

    with pytest.raises(DomainValidationError, match="unsupported attempt mode"):
        service.start_attempt(exam_id="exam-attempt", mode="review")


def test_practice_records_append_only_changes_hint_confidence_and_time(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "alice.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")
    attempt = service.start_attempt(exam_id="exam-attempt", mode="practice")
    version_id = attempt["items"][0]["question_version_id"]
    correct = catalog.get_question_version(version_id)["correct_option_key"]
    wrong = "A" if correct != "A" else "B"

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


def test_voice_candidate_requires_explicit_confirmation_before_answer_changes(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    learning = LearningStore(tmp_path / "alice.db")
    service = AttemptService(catalog, learning, owner_id="u_alice")
    attempt = service.start_attempt(exam_id="exam-attempt", mode="practice")

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
    submitted = service.submit_attempt(attempt["id"])
    assert submitted["status"] == "expired"
    assert submitted["answered_count"] == 0


def test_attempt_id_is_not_visible_in_another_user_database(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _activate(catalog)
    alice = AttemptService(catalog, LearningStore(tmp_path / "alice.db"), owner_id="u_alice")
    bob = AttemptService(catalog, LearningStore(tmp_path / "bob.db"), owner_id="u_bob")
    attempt = alice.start_attempt(exam_id="exam-attempt", mode="practice")

    with pytest.raises(AttemptNotFoundError):
        bob.get_attempt(attempt["id"])
