from __future__ import annotations

from pathlib import Path

from deeptutor.tjm.attempts import AttemptService, ReviewPolicy
from deeptutor.tjm.catalog import CatalogService
from deeptutor.tjm.domain import Choice, ExamSpec, QuestionVersionDraft
from deeptutor.tjm.storage import CatalogStore, LearningStore


def _ready_services(tmp_path: Path) -> tuple[CatalogService, AttemptService, list[str]]:
    catalog = CatalogService(CatalogStore(tmp_path / "catalog.db"))
    catalog.create_exam(
        ExamSpec(
            id="exam-review",
            title="Review Exam",
            duration_seconds=600,
            question_count=3,
            blueprint={"area-a": 2, "area-b": 1},
        ),
        actor_id="admin-1",
    )
    version_ids: list[str] = []
    for stable_id, area, correct in (
        ("review-a1", "area-a", "B"),
        ("review-a2", "area-a", "A"),
        ("review-b1", "area-b", "B"),
    ):
        version = catalog.create_question_version(
            QuestionVersionDraft(
                exam_id="exam-review",
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
        version_ids.append(str(version["id"]))
    catalog.activate_exam("exam-review", actor_id="admin-1")
    attempts = AttemptService(
        catalog,
        LearningStore(tmp_path / "learning.db"),
        owner_id="u_alice",
        review_policy=ReviewPolicy(low_confidence_threshold=50, slow_correct_ms=2000),
    )
    return catalog, attempts, version_ids


def test_review_queue_records_explainable_reasons_and_can_start_review(
    tmp_path: Path,
) -> None:
    _, attempts, _ = _ready_services(tmp_path)
    attempt = attempts.start_attempt(exam_id="exam-review", mode="practice")
    items = attempt["items"]
    correct = {item["position"]: item for item in items}

    # Wrong, high-confidence answer.
    attempts.record_answer(
        attempt["id"],
        position=0,
        selected_option_key="A" if correct[0]["stable_id"] == "review-a1" else "B",
        confidence=90,
        elapsed_ms=500,
        confirmed=True,
    )
    # Correct but low-confidence answer.
    second_key = "A" if correct[1]["stable_id"] == "review-a2" else "B"
    attempts.record_answer(
        attempt["id"],
        position=1,
        selected_option_key=second_key,
        confidence=40,
        elapsed_ms=1000,
        confirmed=True,
    )
    # Correct, slow, and hint-assisted answer.
    attempts.use_hint(attempt["id"], position=2, elapsed_ms=100)
    attempts.record_answer(
        attempt["id"],
        position=2,
        selected_option_key="B",
        confidence=90,
        elapsed_ms=3000,
        confirmed=True,
    )
    attempts.submit_attempt(attempt["id"])

    queue = attempts.list_review_queue()
    reasons_by_stable_id = {item["stable_id"]: set(item["reasons"]) for item in queue}
    assert reasons_by_stable_id["review-a1"] == {"incorrect"}
    assert reasons_by_stable_id["review-a2"] == {"low_confidence"}
    assert reasons_by_stable_id["review-b1"] == {"hint_used", "slow_correct"}
    review = attempts.start_review_attempt(exam_id="exam-review", limit=10)
    assert review["mode"] == "review"
    assert len(review["items"]) == 3
    assert all("correct_option_key" not in item for item in review["items"])


def test_analytics_cover_area_confidence_time_hints_and_trend(tmp_path: Path) -> None:
    catalog, attempts, version_ids = _ready_services(tmp_path)
    attempt = attempts.start_attempt(exam_id="exam-review", mode="practice")
    answers = [(0, "A", 90, 500), (1, "A", 40, 1000), (2, "B", 90, 3000)]
    attempts.use_hint(attempt["id"], position=2, elapsed_ms=100)
    for position, option, confidence, elapsed in answers:
        attempts.record_answer(
            attempt["id"],
            position=position,
            selected_option_key=option,
            confidence=confidence,
            elapsed_ms=elapsed,
            confirmed=True,
        )
    attempts.submit_attempt(attempt["id"])
    catalog.retire_question_version(version_ids[0], actor_id="admin-1")

    analytics = attempts.analytics()

    assert analytics["overall"] == {
        "total": 3,
        "answered": 3,
        "correct": 2,
        "accuracy": 2 / 3,
        "average_elapsed_ms": 1500,
        "hint_use_rate": 1 / 3,
    }
    assert analytics["by_area"]["area-a"]["total"] == 2
    assert analytics["by_area"]["area-a"]["correct"] == 1
    assert analytics["confidence"]["low"]["answered"] == 1
    assert analytics["confidence"]["low"]["correct"] == 1
    assert analytics["confidence"]["high"]["answered"] == 2
    assert analytics["confidence"]["high"]["correct"] == 1
    assert analytics["trend"][0]["attempt_id"] == attempt["id"]


def test_empty_and_partial_history_are_well_defined(tmp_path: Path) -> None:
    _, attempts, _ = _ready_services(tmp_path)
    assert attempts.analytics() == {
        "overall": {
            "total": 0,
            "answered": 0,
            "correct": 0,
            "accuracy": None,
            "average_elapsed_ms": None,
            "hint_use_rate": None,
        },
        "by_area": {},
        "confidence": {
            "low": {"answered": 0, "correct": 0, "accuracy": None},
            "medium": {"answered": 0, "correct": 0, "accuracy": None},
            "high": {"answered": 0, "correct": 0, "accuracy": None},
        },
        "trend": [],
    }

    attempt = attempts.start_attempt(exam_id="exam-review", mode="exam")
    attempts.record_answer(
        attempt["id"],
        position=0,
        selected_option_key="B",
        confidence=50,
        elapsed_ms=1000,
        confirmed=True,
    )
    attempts.submit_attempt(attempt["id"])

    analytics = attempts.analytics()
    assert analytics["overall"]["total"] == 3
    assert analytics["overall"]["answered"] == 1
