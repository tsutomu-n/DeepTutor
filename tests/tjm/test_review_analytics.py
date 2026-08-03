from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import deeptutor.tjm.attempts as attempts_module
from deeptutor.tjm.attempts import AttemptService, ReviewPolicy
from deeptutor.tjm.catalog import CatalogService
from deeptutor.tjm.domain import Choice, ExamSpec, InvalidTransitionError, QuestionVersionDraft
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


def _seed_review_queue(attempts: AttemptService) -> dict:
    attempt = attempts.start_attempt(exam_id="exam-review", mode="practice")
    attempts.submit_attempt(attempt["id"])
    return attempt


def test_review_queue_records_explainable_reasons_and_can_start_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, attempts, _ = _ready_services(tmp_path)
    attempt = attempts.start_attempt(exam_id="exam-review", mode="practice")
    items = attempt["items"]
    correct = {item["position"]: item for item in items}
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    monkeypatch.setattr(attempts_module, "_now_datetime", lambda: clock["now"])

    # Wrong, high-confidence answer.
    attempts.present_item(attempt["id"], position=0)
    clock["now"] += timedelta(milliseconds=500)
    attempts.record_answer(
        attempt["id"],
        position=0,
        selected_option_key="A" if correct[0]["stable_id"] == "review-a1" else "B",
        confidence=90,
        elapsed_ms=500,
        confirmed=True,
    )
    # Correct but low-confidence answer.
    clock["now"] += timedelta(seconds=10)
    attempts.present_item(attempt["id"], position=1)
    second_key = "A" if correct[1]["stable_id"] == "review-a2" else "B"
    clock["now"] += timedelta(milliseconds=1000)
    attempts.record_answer(
        attempt["id"],
        position=1,
        selected_option_key=second_key,
        confidence=40,
        elapsed_ms=1000,
        confirmed=True,
    )
    # Correct, slow, and hint-assisted answer.
    clock["now"] += timedelta(seconds=10)
    attempts.present_item(attempt["id"], position=2)
    attempts.use_hint(attempt["id"], position=2, elapsed_ms=100)
    clock["now"] += timedelta(milliseconds=3000)
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


def test_review_attempt_resolves_only_queue_rows_linked_at_start(tmp_path: Path) -> None:
    _, attempts, _ = _ready_services(tmp_path)
    _seed_review_queue(attempts)
    review = attempts.start_review_attempt(
        exam_id="exam-review", limit=1, idempotency_key="review-start-snapshot"
    )
    version_id = review["items"][0]["question_version_id"]
    with attempts.learning.connect() as conn:
        linked_ids = {
            int(row[0])
            for row in conn.execute(
                """
                SELECT queue_row_id FROM review_attempt_queue_links
                WHERE attempt_id = ?
                """,
                (review["id"],),
            )
        }
        assert linked_ids
        cursor = conn.execute(
            """
            INSERT INTO review_queue (
                question_version_id, reason, priority, due_at, status,
                source_attempt_id, created_at
            ) VALUES (?, 'late_reason', 200, NULL, 'pending', NULL,
                      '2026-01-02T00:00:00Z')
            """,
            (version_id,),
        )
        late_row_id = int(cursor.lastrowid)

    attempts.submit_attempt(review["id"], idempotency_key="review-submit-snapshot")

    with attempts.learning.connect() as conn:
        linked = conn.execute(
            f"""
            SELECT id, status, resolution_reason, resolution_attempt_id
            FROM review_queue WHERE id IN ({",".join("?" for _ in linked_ids)})
            """,
            tuple(sorted(linked_ids)),
        ).fetchall()
        late = conn.execute(
            "SELECT status, resolution_reason FROM review_queue WHERE id = ?",
            (late_row_id,),
        ).fetchone()
        assert all(row["status"] == "completed" for row in linked)
        assert all(row["resolution_reason"] == "review_completed" for row in linked)
        assert all(row["resolution_attempt_id"] == review["id"] for row in linked)
        assert dict(late) == {"status": "pending", "resolution_reason": None}


def test_two_review_tabs_and_double_submit_do_not_corrupt_queue_resolution(
    tmp_path: Path,
) -> None:
    _, attempts, _ = _ready_services(tmp_path)
    _seed_review_queue(attempts)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            attempts.start_review_attempt,
            exam_id="exam-review",
            limit=1,
            idempotency_key="tab-one-start",
        )
        second_future = executor.submit(
            attempts.start_review_attempt,
            exam_id="exam-review",
            limit=1,
            idempotency_key="tab-two-start",
        )
        first = first_future.result(timeout=5)
        second = second_future.result(timeout=5)
    assert first["items"][0]["question_version_id"] == second["items"][0]["question_version_id"]
    with attempts.learning.connect() as conn:
        shared = conn.execute(
            """
            SELECT queue_row_id, COUNT(DISTINCT attempt_id) AS attempt_count
            FROM review_attempt_queue_links
            WHERE attempt_id IN (?, ?)
            GROUP BY queue_row_id HAVING attempt_count = 2
            """,
            (first["id"], second["id"]),
        ).fetchall()
        assert shared

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_submit = executor.submit(
            attempts.submit_attempt, first["id"], idempotency_key="tab-one-submit"
        )
        second_submit = executor.submit(
            attempts.submit_attempt, second["id"], idempotency_key="tab-two-submit"
        )
        first_result = first_submit.result(timeout=5)
        second_submit.result(timeout=5)
    assert attempts.submit_attempt(first["id"], idempotency_key="tab-one-submit") == first_result

    with attempts.learning.connect() as conn:
        for row in shared:
            resolution = conn.execute(
                """
                SELECT status, resolution_attempt_id FROM review_queue WHERE id = ?
                """,
                (row["queue_row_id"],),
            ).fetchone()
            assert resolution["status"] == "completed"
            assert resolution["resolution_attempt_id"] in {first["id"], second["id"]}


def test_queue_row_dismissed_during_review_is_not_recompleted(tmp_path: Path) -> None:
    _, attempts, _ = _ready_services(tmp_path)
    _seed_review_queue(attempts)
    review = attempts.start_review_attempt(exam_id="exam-review", limit=1)
    with attempts.learning.connect() as conn:
        queue_row_id = int(
            conn.execute(
                """
                SELECT queue_row_id FROM review_attempt_queue_links
                WHERE attempt_id = ? ORDER BY queue_row_id LIMIT 1
                """,
                (review["id"],),
            ).fetchone()[0]
        )
        conn.execute(
            """
            UPDATE review_queue
            SET status = 'dismissed', resolved_at = '2026-01-02T00:00:00Z',
                resolution_reason = 'manual_test_dismissal'
            WHERE id = ?
            """,
            (queue_row_id,),
        )

    attempts.submit_attempt(review["id"])

    with attempts.learning.connect() as conn:
        row = conn.execute(
            """
            SELECT status, resolution_reason, resolution_attempt_id
            FROM review_queue WHERE id = ?
            """,
            (queue_row_id,),
        ).fetchone()
        assert dict(row) == {
            "status": "dismissed",
            "resolution_reason": "manual_test_dismissal",
            "resolution_attempt_id": None,
        }


def test_analytics_cover_area_confidence_time_hints_and_trend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, attempts, version_ids = _ready_services(tmp_path)
    attempt = attempts.start_attempt(exam_id="exam-review", mode="practice")
    answers = [(0, "A", 90, 500), (1, "A", 40, 1000), (2, "B", 90, 3000)]
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    monkeypatch.setattr(attempts_module, "_now_datetime", lambda: clock["now"])
    for position, option, confidence, elapsed in answers:
        clock["now"] += timedelta(seconds=10)
        attempts.present_item(attempt["id"], position=position)
        if position == 2:
            attempts.use_hint(attempt["id"], position=2, elapsed_ms=100)
        clock["now"] += timedelta(milliseconds=elapsed)
        attempts.record_answer(
            attempt["id"],
            position=position,
            selected_option_key=option,
            confidence=confidence,
            elapsed_ms=elapsed,
            confirmed=True,
        )
    attempts.submit_attempt(attempt["id"])
    old = catalog.get_question_version(version_ids[0])
    replacement = catalog.create_question_version(
        QuestionVersionDraft(
            exam_id="exam-review",
            stable_id=old["stable_id"],
            stem="Corrected replacement question",
            choices=(Choice("A", "First"), Choice("B", "Second")),
            correct_option_key="B",
            area=old["area"],
            explanation="Corrected explanation",
            hints=("Corrected hint",),
            source={"license": "test-fixture", "revision": 2},
        ),
        actor_id="admin-1",
    )
    catalog.review_question_version(replacement["id"], actor_id="reviewer-1")
    catalog.publish_question_version(replacement["id"], actor_id="admin-1")

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


def test_invalid_content_preserves_raw_history_but_is_excluded_from_analytics_and_review(
    tmp_path: Path,
) -> None:
    catalog, attempts, version_ids = _ready_services(tmp_path)
    attempt = attempts.start_attempt(exam_id="exam-review", mode="practice")
    for item in attempt["items"]:
        attempts.present_item(attempt["id"], position=item["position"])
        version = catalog.get_question_version(item["question_version_id"])
        attempts.record_answer(
            attempt["id"],
            position=item["position"],
            selected_option_key=version["correct_option_key"],
            confidence=20 if item["question_version_id"] == version_ids[0] else 90,
            elapsed_ms=1000,
            confirmed=True,
        )
    submitted = attempts.submit_attempt(attempt["id"])
    assert (submitted["correct_count"], submitted["total_count"]) == (3, 3)
    with attempts.learning.connect() as conn:
        answer_event_count = conn.execute(
            "SELECT COUNT(*) FROM answer_events WHERE attempt_id = ?",
            (attempt["id"],),
        ).fetchone()[0]

    catalog.retire_question_version(
        version_ids[0],
        actor_id="admin-1",
        reason="invalid_content",
        note="Answer key is invalid.",
    )

    history = attempts.get_attempt(attempt["id"])
    invalid_item = next(
        item for item in history["items"] if item["question_version_id"] == version_ids[0]
    )
    assert invalid_item["catalog_disposition"] == "invalid_content"
    assert invalid_item["grading_status"] == "content_invalidated"
    assert invalid_item["content_invalidated_at"]
    assert "correct_option_key" not in invalid_item
    assert history["content_invalidated_count"] == 1
    assert (history["correct_count"], history["total_count"]) == (3, 3)
    assert all(
        item["question_version_id"] != version_ids[0] for item in attempts.list_review_queue()
    )

    analytics = attempts.analytics()
    assert analytics["overall"]["total"] == 2
    assert analytics["overall"]["correct"] == 2
    assert analytics["trend"] == [
        {
            "attempt_id": attempt["id"],
            "mode": "practice",
            "submitted_at": submitted["submitted_at"],
            "correct": 2,
            "total": 2,
        }
    ]
    with attempts.learning.connect() as conn:
        raw = conn.execute(
            "SELECT correct_count, total_count FROM attempts WHERE id = ?",
            (attempt["id"],),
        ).fetchone()
        dismissed = conn.execute(
            """
            SELECT status, resolution_reason FROM review_queue
            WHERE question_version_id = ?
            """,
            (version_ids[0],),
        ).fetchall()
        assert tuple(raw) == (3, 3)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM answer_events WHERE attempt_id = ?",
                (attempt["id"],),
            ).fetchone()[0]
            == answer_event_count
        )
        assert dismissed
        assert all(row["status"] == "dismissed" for row in dismissed)
        assert all(row["resolution_reason"] == "question_invalid_content" for row in dismissed)


def test_invalid_content_sanitizes_idempotent_answer_and_submit_replays(tmp_path: Path) -> None:
    catalog, attempts, version_ids = _ready_services(tmp_path)
    attempt = attempts.start_attempt(
        exam_id="exam-review",
        mode="practice",
        idempotency_key="invalid-replay-start",
    )
    position = next(
        item["position"]
        for item in attempt["items"]
        if item["question_version_id"] == version_ids[0]
    )
    version = catalog.get_question_version(version_ids[0])
    attempts.present_item(attempt["id"], position=position)
    answer_request = {
        "position": position,
        "selected_option_key": version["correct_option_key"],
        "confidence": 80,
        "elapsed_ms": 1000,
        "confirmed": True,
        "idempotency_key": "invalid-replay-answer",
    }
    first_answer = attempts.record_answer(attempt["id"], **answer_request)
    assert first_answer["grading_status"] == "eligible"
    assert first_answer["correct_option_key"] == version["correct_option_key"]
    first_submit = attempts.submit_attempt(attempt["id"], idempotency_key="invalid-replay-submit")
    assert first_submit["content_invalidated_count"] == 0

    catalog.retire_question_version(
        version_ids[0],
        actor_id="admin-1",
        reason="invalid_content",
        note="The answer key is no longer trustworthy.",
    )

    answer_replay = attempts.record_answer(attempt["id"], **answer_request)
    assert answer_replay["catalog_disposition"] == "invalid_content"
    assert answer_replay["grading_status"] == "content_invalidated"
    assert "correct_option_key" not in answer_replay
    assert "explanation" not in answer_replay
    assert "is_correct" not in answer_replay

    submit_replay = attempts.submit_attempt(attempt["id"], idempotency_key="invalid-replay-submit")
    assert submit_replay["content_invalidated_count"] == 1
    invalid_item = submit_replay["items"][position]
    assert invalid_item["grading_status"] == "content_invalidated"
    assert "correct_option_key" not in invalid_item
    assert "explanation" not in invalid_item
    assert "is_correct" not in invalid_item

    start_replay = attempts.start_attempt(
        exam_id="exam-review",
        mode="practice",
        idempotency_key="invalid-replay-start",
    )
    assert start_replay["content_invalidated_count"] == 1
    assert start_replay["items"][position]["grading_status"] == "content_invalidated"


def test_started_attempt_can_finish_superseded_version_without_mastery_transfer(
    tmp_path: Path,
) -> None:
    catalog, attempts, version_ids = _ready_services(tmp_path)
    attempt = attempts.start_attempt(exam_id="exam-review", mode="practice")
    old_version_id = version_ids[0]
    old = catalog.get_question_version(old_version_id)
    old_position = next(
        item["position"]
        for item in attempt["items"]
        if item["question_version_id"] == old_version_id
    )
    replacement = catalog.create_question_version(
        QuestionVersionDraft(
            exam_id="exam-review",
            stable_id=old["stable_id"],
            stem="Superseding correction",
            choices=(Choice("A", "First"), Choice("B", "Second")),
            correct_option_key="B",
            area=old["area"],
            explanation="Superseding explanation",
            hints=("Superseding hint",),
            source={"license": "test-fixture", "revision": 2},
        ),
        actor_id="admin-1",
    )
    catalog.review_question_version(replacement["id"], actor_id="reviewer-1")
    catalog.publish_question_version(replacement["id"], actor_id="admin-1")

    attempts.present_item(attempt["id"], position=old_position)
    attempts.record_answer(
        attempt["id"],
        position=old_position,
        selected_option_key=old["correct_option_key"],
        confidence=20,
        elapsed_ms=1000,
        confirmed=True,
    )
    submitted = attempts.submit_attempt(attempt["id"])

    old_item = next(
        item for item in submitted["items"] if item["question_version_id"] == old_version_id
    )
    assert old_item["catalog_disposition"] == "superseded"
    assert old_item["grading_status"] == "eligible"
    assert old_item["is_correct"] is True
    assert submitted["total_count"] == 3
    assert all(
        item["question_version_id"] != old_version_id for item in attempts.list_review_queue()
    )
    with attempts.learning.connect() as conn:
        assert (
            conn.execute(
                """
                SELECT COUNT(*) FROM review_queue
                WHERE question_version_id = ? AND status = 'pending'
                """,
                (replacement["id"],),
            ).fetchone()[0]
            == 0
        )


def test_started_attempt_excludes_invalid_content_from_scoring(tmp_path: Path) -> None:
    catalog, attempts, version_ids = _ready_services(tmp_path)
    attempt = attempts.start_attempt(exam_id="exam-review", mode="exam")
    invalid_version_id = version_ids[0]
    position = next(
        item["position"]
        for item in attempt["items"]
        if item["question_version_id"] == invalid_version_id
    )
    attempts.present_item(attempt["id"], position=position)
    catalog.retire_question_version(
        invalid_version_id, actor_id="admin-1", reason="invalid_content"
    )

    with pytest.raises(InvalidTransitionError, match="invalid content"):
        attempts.record_answer(
            attempt["id"],
            position=position,
            selected_option_key="B",
            confidence=80,
            elapsed_ms=1000,
            confirmed=True,
        )

    submitted = attempts.submit_attempt(attempt["id"])
    invalid_item = submitted["items"][position]
    assert invalid_item["catalog_disposition"] == "invalid_content"
    assert invalid_item["grading_status"] == "content_invalidated"
    assert "correct_option_key" not in invalid_item
    assert submitted["total_count"] == 2


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
    attempts.present_item(attempt["id"], position=0)
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
