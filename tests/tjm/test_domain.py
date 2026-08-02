from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from deeptutor.tjm.catalog import CatalogService
from deeptutor.tjm.domain import (
    Choice,
    DomainValidationError,
    DuplicateRecordError,
    ExamSpec,
    ImmutableVersionError,
    InvalidTransitionError,
    QuestionVersionDraft,
    grade_responses,
)
from deeptutor.tjm.storage import CatalogStore


def _catalog(tmp_path: Path) -> CatalogService:
    return CatalogService(CatalogStore(tmp_path / "catalog.db"))


def _exam() -> ExamSpec:
    return ExamSpec(
        id="license-alpha",
        title="License Alpha",
        duration_seconds=413,
        question_count=3,
        pass_score=2,
        blueprint={"rules": 2, "practice": 1},
    )


def _question(*, stem: str = "Which statement is correct?", correct: str = "B"):
    return QuestionVersionDraft(
        exam_id="license-alpha",
        stable_id="alpha-001",
        stem=stem,
        choices=(Choice("A", "First"), Choice("B", "Second"), Choice("C", "Third")),
        correct_option_key=correct,
        area="rules",
        explanation="Because the second rule applies.",
        hints=("Read every qualifier.",),
        source={"kind": "licensed_import", "reference": "fixture-1"},
    )


def test_exam_definition_is_data_driven_and_duplicate_ids_fail_closed(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)

    created = catalog.create_exam(_exam(), actor_id="admin-1")

    assert created["duration_seconds"] == 413
    assert created["question_count"] == 3
    assert created["blueprint"] == {"rules": 2, "practice": 1}
    with pytest.raises(DuplicateRecordError):
        catalog.create_exam(_exam(), actor_id="admin-1")


def test_draft_exam_definition_can_change_but_active_definition_is_immutable(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    catalog.create_exam(_exam(), actor_id="admin-1")
    replacement = ExamSpec(
        id="license-alpha",
        title="License Alpha Revised",
        description="Configured outside core code.",
        duration_seconds=509,
        question_count=4,
        pass_score=3,
        blueprint={"rules": 3, "practice": 1},
    )

    updated = catalog.replace_exam("license-alpha", replacement, actor_id="admin-2")

    assert updated["title"] == "License Alpha Revised"
    assert updated["duration_seconds"] == 509
    assert updated["question_count"] == 4
    assert updated["blueprint"] == {"rules": 3, "practice": 1}
    assert updated["revision"] == 2

    with catalog.store.connect() as conn:
        conn.execute(
            "UPDATE exam_definitions SET status = 'active' WHERE id = ?", ("license-alpha",)
        )
    with pytest.raises(InvalidTransitionError, match="only draft"):
        catalog.replace_exam("license-alpha", replacement, actor_id="admin-2")

    with pytest.raises(DomainValidationError, match="cannot be changed"):
        catalog.replace_exam(
            "license-alpha",
            ExamSpec(
                id="other-id",
                title="Other",
                duration_seconds=60,
                question_count=1,
            ),
            actor_id="admin-2",
        )


@pytest.mark.parametrize(
    "spec",
    [
        ExamSpec(id="x", title="", duration_seconds=60, question_count=1),
        ExamSpec(id="x", title="X", duration_seconds=0, question_count=1),
        ExamSpec(id="x", title="X", duration_seconds=60, question_count=0),
        ExamSpec(id="x", title="X", duration_seconds=60, question_count=2, pass_score=3),
        ExamSpec(
            id="x",
            title="X",
            duration_seconds=60,
            question_count=2,
            blueprint={"area": 1},
        ),
    ],
)
def test_invalid_exam_definitions_are_rejected(tmp_path: Path, spec: ExamSpec) -> None:
    with pytest.raises(DomainValidationError):
        _catalog(tmp_path).create_exam(spec, actor_id="admin-1")


@pytest.mark.parametrize(
    "draft",
    [
        QuestionVersionDraft(
            exam_id="license-alpha",
            stable_id="alpha-001",
            stem="Question",
            choices=(Choice("A", "Only one"),),
            correct_option_key="A",
            area="rules",
        ),
        QuestionVersionDraft(
            exam_id="license-alpha",
            stable_id="alpha-001",
            stem="Question",
            choices=(Choice("A", "First"), Choice("A", "Duplicate")),
            correct_option_key="A",
            area="rules",
        ),
        QuestionVersionDraft(
            exam_id="license-alpha",
            stable_id="alpha-001",
            stem="Question",
            choices=(Choice("A", "First"), Choice("B", "Second")),
            correct_option_key="C",
            area="rules",
        ),
    ],
)
def test_invalid_choice_and_answer_contracts_are_rejected(
    tmp_path: Path, draft: QuestionVersionDraft
) -> None:
    catalog = _catalog(tmp_path)
    catalog.create_exam(_exam(), actor_id="admin-1")

    with pytest.raises(DomainValidationError):
        catalog.create_question_version(draft, actor_id="admin-1")


def test_published_versions_require_review_and_are_content_immutable(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.create_exam(_exam(), actor_id="admin-1")
    version = catalog.create_question_version(_question(), actor_id="admin-1")

    with pytest.raises(InvalidTransitionError):
        catalog.publish_question_version(version["id"], actor_id="admin-1")

    catalog.review_question_version(version["id"], actor_id="reviewer-1", note="checked")
    published = catalog.publish_question_version(version["id"], actor_id="admin-1")
    assert published["status"] == "published"

    with catalog.store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE question_versions SET correct_option_key = 'A' WHERE id = ?",
                (version["id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM question_versions WHERE id = ?", (version["id"],))


def test_correction_creates_new_version_and_retires_previous_publication(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.create_exam(_exam(), actor_id="admin-1")
    first = catalog.create_question_version(_question(), actor_id="admin-1")
    catalog.review_question_version(first["id"], actor_id="reviewer-1")
    catalog.publish_question_version(first["id"], actor_id="admin-1")

    second = catalog.create_question_version(
        _question(stem="Which revised statement is correct?", correct="C"),
        actor_id="admin-1",
    )
    assert second["version"] == 2
    catalog.review_question_version(second["id"], actor_id="reviewer-2")
    catalog.publish_question_version(second["id"], actor_id="admin-1")

    assert catalog.get_question_version(first["id"])["status"] == "retired"
    assert catalog.get_question_version(first["id"])["correct_option_key"] == "B"
    assert catalog.get_question_version(second["id"])["status"] == "published"
    assert catalog.get_question_version(second["id"])["correct_option_key"] == "C"

    with pytest.raises(ImmutableVersionError):
        catalog.replace_draft(second["id"], _question(correct="A"), actor_id="admin-1")


def test_deterministic_grading_uses_only_answer_key_and_confirmed_responses() -> None:
    result = grade_responses(
        answer_key={"version-1": "B", "version-2": "C", "version-3": "A"},
        responses={"version-1": "B", "version-2": "A"},
    )

    assert result.total == 3
    assert result.answered == 2
    assert result.correct == 1
    assert result.items == {
        "version-1": True,
        "version-2": False,
        "version-3": False,
    }
    assert grade_responses(
        answer_key={"version-1": "B"}, responses={"version-1": "B"}
    ) == grade_responses(answer_key={"version-1": "B"}, responses={"version-1": "B"})

    with pytest.raises(DomainValidationError, match="unknown question version"):
        grade_responses(answer_key={"version-1": "B"}, responses={"other": "B"})
