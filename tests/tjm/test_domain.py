from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3
from threading import Event

import pytest

from deeptutor.tjm.catalog import CatalogService
from deeptutor.tjm.domain import (
    Choice,
    DomainValidationError,
    DuplicateRecordError,
    ExamSpec,
    ImmutableVersionError,
    InvalidTransitionError,
    OfficialPassingScoreSource,
    QuestionVersionDraft,
    evaluate_attempt_result,
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


def _seed_legacy_reviewed_version(db_path: Path, *, published: bool = False) -> str:
    version_id = "legacy-qv-1"

    class LegacyCatalogStore(CatalogStore):
        migrations = CatalogStore.migrations[:2]

    store = LegacyCatalogStore(db_path)
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO exam_definitions (
                id, title, duration_seconds, question_count, pass_score, blueprint_json,
                status, revision, created_by, created_at, updated_at
            ) VALUES ('license-alpha', 'Legacy', 413, 1, 1,
                      '{"rules":1}', 'draft', 1, 'admin-1',
                      '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO questions (id, exam_id, stable_id, created_at)
            VALUES ('legacy-q1', 'license-alpha', 'alpha-001', '2026-01-01T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO question_versions (
                id, question_id, version, stem, options_json, correct_option_key,
                area, explanation, hints_json, source_json, content_hash, status,
                created_by, created_at, updated_at
            ) VALUES (?, 'legacy-q1', 1, 'Which statement is correct?',
                      '[{"key":"A","text":"First"},{"key":"B","text":"Second"},
                       {"key":"C","text":"Third"}]',
                      'B', 'rules', 'Legacy explanation', '[]', '{}',
                      'legacy-hash', 'draft', 'author',
                      '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """,
            (version_id,),
        )
        conn.execute(
            """
            INSERT INTO review_events (
                question_version_id, action, actor_id, note, created_at
            ) VALUES (?, 'reviewed', 'legacy-reviewer', 'legacy',
                      '2026-01-01T00:01:00Z')
            """,
            (version_id,),
        )
        if published:
            conn.execute(
                "UPDATE question_versions SET status = 'published' WHERE id = ?",
                (version_id,),
            )
    return version_id


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
        ExamSpec(id="exam/a", title="X", duration_seconds=60, question_count=1),
        ExamSpec(id="..", title="X", duration_seconds=60, question_count=1),
        ExamSpec(id="試験", title="X", duration_seconds=60, question_count=1),
        ExamSpec(id="x", title="X", duration_seconds=0, question_count=1),
        ExamSpec(id="x", title="X", duration_seconds=60, question_count=0),
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


def test_rejected_version_content_is_immutable(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.create_exam(_exam(), actor_id="admin-1")
    version = catalog.create_question_version(_question(), actor_id="author")
    catalog.reject_question_version(version["id"], actor_id="reviewer", note="incorrect")

    with catalog.store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE question_versions SET stem = 'Changed after rejection' WHERE id = ?",
                (version["id"],),
            )


def test_review_is_bound_to_immutable_content_revision(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.create_exam(_exam(), actor_id="admin-1")
    version = catalog.create_question_version(_question(), actor_id="author")

    assert version["content_revision"] == 1
    reviewed = catalog.review_question_version(
        version["id"], actor_id="reviewer-1", note="reviewed revision one"
    )
    assert reviewed["reviewed_revision"] == 1
    assert reviewed["review_binding_state"] == "current"

    edited = catalog.replace_draft(
        version["id"],
        _question(stem="Revised after review", correct="A"),
        actor_id="editor",
    )

    assert edited["content_revision"] == 2
    assert edited["reviewed_by"] is None
    assert edited["reviewed_revision"] is None
    assert edited["review_binding_state"] == "stale"
    with pytest.raises(InvalidTransitionError, match="current revision must be reviewed"):
        catalog.publish_question_version(version["id"], actor_id="publisher")

    with catalog.store.connect() as conn:
        revisions = conn.execute(
            """
            SELECT content_revision, stem, correct_option_key, created_by
            FROM question_version_revisions
            WHERE question_version_id = ?
            ORDER BY content_revision
            """,
            (version["id"],),
        ).fetchall()
        assert [tuple(row) for row in revisions] == [
            (1, "Which statement is correct?", "B", "author"),
            (2, "Revised after review", "A", "editor"),
        ]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM review_events WHERE question_version_id = ?",
                (version["id"],),
            ).fetchone()[0]
            == 1
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                """
                UPDATE question_version_revisions SET correct_option_key = 'C'
                WHERE question_version_id = ? AND content_revision = 1
                """,
                (version["id"],),
            )
        review_event_id = conn.execute(
            "SELECT review_event_id FROM review_bindings WHERE question_version_id = ?",
            (version["id"],),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE review_events SET actor_id = 'attacker' WHERE id = ?",
                (review_event_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM review_events WHERE id = ?", (review_event_id,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                """
                DELETE FROM question_version_revisions
                WHERE question_version_id = ? AND content_revision = 1
                """,
                (version["id"],),
            )

    rereviewed = catalog.review_question_version(
        version["id"], actor_id="reviewer-2", note="reviewed revision two"
    )
    assert rereviewed["reviewed_revision"] == 2
    published = catalog.publish_question_version(version["id"], actor_id="publisher")
    assert published["status"] == "published"
    assert published["correct_option_key"] == "A"
    assert published["reviewed_revision"] == 2


@pytest.mark.parametrize(
    "changed_field",
    ["choice_order", "area", "explanation", "hints", "source"],
)
def test_every_content_field_change_invalidates_review(tmp_path: Path, changed_field: str) -> None:
    catalog = _catalog(tmp_path)
    catalog.create_exam(_exam(), actor_id="admin")
    original = _question()
    version = catalog.create_question_version(original, actor_id="author")
    catalog.review_question_version(version["id"], actor_id="reviewer")
    changes = {
        "choice_order": {
            "choices": (original.choices[1], original.choices[0], original.choices[2])
        },
        "area": {"area": "practice"},
        "explanation": {"explanation": "A revised explanation."},
        "hints": {"hints": ("A revised hint.",)},
        "source": {"source": {"kind": "licensed_import", "reference": "fixture-2"}},
    }

    edited = catalog.replace_draft(
        version["id"], replace(original, **changes[changed_field]), actor_id="editor"
    )

    assert edited["content_revision"] == 2
    assert edited["review_binding_state"] == "stale"
    with pytest.raises(InvalidTransitionError, match="current revision must be reviewed"):
        catalog.publish_question_version(version["id"], actor_id="publisher")


def test_reviewed_question_identity_is_immutable(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.create_exam(_exam(), actor_id="admin")
    version = catalog.create_question_version(_question(), actor_id="author")
    catalog.review_question_version(version["id"], actor_id="reviewer")

    with catalog.store.connect() as conn:
        question_id = conn.execute(
            "SELECT question_id FROM question_versions WHERE id = ?", (version["id"],)
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO questions (id, exam_id, stable_id, created_at)
            VALUES ('other-question', 'license-alpha', 'alpha-002', '2026-01-01T00:00:00Z')
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            conn.execute(
                "UPDATE question_versions SET question_id = 'other-question' WHERE id = ?",
                (version["id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            conn.execute(
                "UPDATE questions SET stable_id = 'tampered' WHERE id = ?",
                (question_id,),
            )


def test_publish_rechecks_review_after_concurrent_edit_transaction(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.create_exam(_exam(), actor_id="admin-1")
    version = catalog.create_question_version(_question(), actor_id="author")
    catalog.review_question_version(version["id"], actor_id="reviewer")
    publish_started = Event()

    def publish() -> dict:
        publish_started.set()
        return catalog.publish_question_version(version["id"], actor_id="publisher")

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with catalog.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE question_versions
                SET stem = 'Concurrent edit', correct_option_key = 'A',
                    content_hash = 'concurrent-edit-hash',
                    content_revision = content_revision + 1,
                    updated_by = 'editor', updated_at = '2026-01-01T00:02:00Z'
                WHERE id = ?
                """,
                (version["id"],),
            )
            future = executor.submit(publish)
            assert publish_started.wait(timeout=2)

        with pytest.raises(InvalidTransitionError, match="current revision must be reviewed"):
            future.result(timeout=5)
    finally:
        executor.shutdown(wait=True)

    current = catalog.get_question_version(version["id"])
    assert current["status"] == "draft"
    assert current["content_revision"] == 2
    assert current["correct_option_key"] == "A"
    assert current["review_binding_state"] == "stale"


def test_legacy_review_is_audit_only_until_current_revision_is_reviewed(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    version_id = _seed_legacy_reviewed_version(db_path)

    catalog = CatalogService(CatalogStore(db_path))
    migrated = catalog.get_question_version(version_id)

    assert migrated["content_revision"] == 1
    assert migrated["reviewed_by"] is None
    assert migrated["review_binding_state"] == "legacy_unverified"
    exam = catalog.get_exam("license-alpha")
    assert "pass_score" not in exam
    assert exam["official_passing_score"] is None
    assert catalog.get_legacy_practice_target("license-alpha") == 1
    with pytest.raises(InvalidTransitionError, match="current revision must be reviewed"):
        catalog.publish_question_version(version_id, actor_id="publisher")

    catalog.review_question_version(version_id, actor_id="new-reviewer")
    assert catalog.publish_question_version(version_id, actor_id="publisher")["status"] == (
        "published"
    )


def test_legacy_published_version_is_not_selectable_until_rereviewed(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-published.db"
    version_id = _seed_legacy_reviewed_version(db_path, published=True)
    catalog = CatalogService(CatalogStore(db_path))

    migrated = catalog.get_question_version(version_id)
    assert migrated["status"] == "published"
    assert migrated["review_binding_state"] == "legacy_unverified"
    with pytest.raises(InvalidTransitionError, match="blueprint is incomplete"):
        catalog.activate_exam("license-alpha", actor_id="admin")

    catalog.review_question_version(version_id, actor_id="new-reviewer")
    catalog.activate_exam("license-alpha", actor_id="admin")
    assert [item["id"] for item in catalog.selected_published_versions("license-alpha")] == [
        version_id
    ]


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

    retired = catalog.get_question_version(first["id"])
    assert retired["status"] == "retired"
    assert retired["correct_option_key"] == "B"
    assert retired["retirement_reason"] == "superseded"
    assert retired["replacement_question_version_id"] == second["id"]
    assert retired["retired_at"]
    assert catalog.get_question_version(second["id"])["status"] == "published"
    assert catalog.get_question_version(second["id"])["correct_option_key"] == "C"

    with pytest.raises(ImmutableVersionError):
        catalog.replace_draft(second["id"], _question(correct="A"), actor_id="admin-1")


def test_manual_retirement_is_explicitly_invalid_content_and_audited(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.create_exam(_exam(), actor_id="admin-1")
    version = catalog.create_question_version(_question(), actor_id="author")
    catalog.review_question_version(version["id"], actor_id="reviewer")
    catalog.publish_question_version(version["id"], actor_id="publisher")

    with pytest.raises(DomainValidationError, match="invalid_content"):
        catalog.retire_question_version(version["id"], actor_id="admin-1", reason="superseded")

    retired = catalog.retire_question_version(
        version["id"],
        actor_id="admin-1",
        reason="invalid_content",
        note="The source proves that the keyed answer is invalid.",
    )

    assert retired["status"] == "retired"
    assert retired["retirement_reason"] == "invalid_content"
    assert retired["replacement_question_version_id"] is None
    assert retired["retired_at"]
    with catalog.store.connect() as conn:
        event = conn.execute(
            """
            SELECT actor_id, note FROM review_events
            WHERE question_version_id = ? AND action = 'retired'
            ORDER BY id DESC LIMIT 1
            """,
            (version["id"],),
        ).fetchone()
        assert dict(event) == {
            "actor_id": "admin-1",
            "note": "The source proves that the keyed answer is invalid.",
        }
        with pytest.raises(sqlite3.IntegrityError, match="invalid retirement transition"):
            conn.execute(
                """
                UPDATE question_versions SET retirement_reason = 'superseded'
                WHERE id = ?
                """,
                (version["id"],),
            )


def test_superseded_retirement_can_only_escalate_to_invalid_content(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.create_exam(_exam(), actor_id="admin")
    first = catalog.create_question_version(_question(), actor_id="author")
    catalog.review_question_version(first["id"], actor_id="reviewer")
    catalog.publish_question_version(first["id"], actor_id="publisher")
    replacement = catalog.create_question_version(
        _question(stem="Replacement question", correct="C"), actor_id="author"
    )
    catalog.review_question_version(replacement["id"], actor_id="reviewer")
    catalog.publish_question_version(replacement["id"], actor_id="publisher")

    escalated = catalog.retire_question_version(
        first["id"],
        actor_id="admin",
        reason="invalid_content",
        note="The historical item was later proven invalid.",
    )

    assert escalated["retirement_reason"] == "invalid_content"
    assert escalated["replacement_question_version_id"] == replacement["id"]
    with catalog.store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="invalid retirement transition"):
            conn.execute(
                """
                UPDATE question_versions SET retirement_reason = 'superseded'
                WHERE id = ?
                """,
                (first["id"],),
            )


def test_legacy_retirement_can_be_explicitly_classified_as_superseded(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-retirement.db"

    class LegacyCatalogStore(CatalogStore):
        migrations = CatalogStore.migrations[:3]

    legacy = CatalogService(LegacyCatalogStore(db_path))
    legacy.create_exam(_exam(), actor_id="admin")
    first = legacy.create_question_version(_question(), actor_id="author")
    legacy.review_question_version(first["id"], actor_id="reviewer")
    legacy.publish_question_version(first["id"], actor_id="publisher")
    replacement = legacy.create_question_version(
        _question(stem="Historical replacement", correct="C"), actor_id="author"
    )
    legacy.review_question_version(replacement["id"], actor_id="reviewer")
    with legacy.store.connect() as conn:
        conn.execute(
            "UPDATE question_versions SET status = 'retired' WHERE id = ?",
            (first["id"],),
        )
        conn.execute(
            "UPDATE question_versions SET status = 'published' WHERE id = ?",
            (replacement["id"],),
        )

    catalog = CatalogService(CatalogStore(db_path))
    assert catalog.get_question_version(first["id"])["retirement_reason"] is None
    unverified_draft = catalog.create_question_version(
        _question(stem="Unpublished later draft", correct="A"), actor_id="author"
    )
    with catalog.store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="invalid retirement transition"):
            conn.execute(
                """
                UPDATE question_versions SET
                    retirement_reason = 'superseded',
                    replacement_question_version_id = ?
                WHERE id = ?
                """,
                (unverified_draft["id"], first["id"]),
            )

    classified = catalog.classify_legacy_retirement(
        first["id"],
        replacement_version_id=replacement["id"],
        actor_id="admin",
        note="Verified from the historical publication log.",
    )

    assert classified["retirement_reason"] == "superseded"
    assert classified["replacement_question_version_id"] == replacement["id"]
    with catalog.store.connect() as conn:
        event = conn.execute(
            """
            SELECT actor_id, note FROM review_events
            WHERE question_version_id = ? AND action = 'retired'
            ORDER BY id DESC LIMIT 1
            """,
            (first["id"],),
        ).fetchone()
        assert event["actor_id"] == "admin"
        assert event["note"].startswith(f"classified_superseded_by:{replacement['id']}")

    with pytest.raises(InvalidTransitionError, match="already classified"):
        catalog.classify_legacy_retirement(
            first["id"],
            replacement_version_id=replacement["id"],
            actor_id="admin",
        )


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


def test_official_passing_score_is_separate_from_exam_shape_and_requires_safe_source(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    created = catalog.create_exam(_exam(), actor_id="admin")

    assert "pass_score" not in created
    assert created["official_passing_score"] is None
    assert created["official_passing_score_source"] is None

    source = OfficialPassingScoreSource(
        title="  Published examination standard  ",
        publisher="  Licensing Board  ",
        url=" https://example.test/standards/alpha?year=2026 ",
        published_at=" 2026-01-15 ",
    )
    updated = catalog.set_official_passing_score(
        "license-alpha", score=2, source=source, actor_id="admin"
    )

    assert updated["official_passing_score"] == 2
    assert updated["official_passing_score_source"] == {
        "title": "Published examination standard",
        "publisher": "Licensing Board",
        "url": "https://example.test/standards/alpha?year=2026",
        "published_at": "2026-01-15",
    }
    assert updated["revision"] == 2

    unchanged = catalog.set_official_passing_score(
        "license-alpha",
        score=2,
        source={
            "publisher": "Licensing Board",
            "title": "Published examination standard",
            "published_at": "2026-01-15",
            "url": "https://example.test/standards/alpha?year=2026",
        },
        actor_id="admin",
    )
    assert unchanged["revision"] == 2


def test_exam_create_and_draft_replace_persist_official_score_atomically(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    original = replace(
        _exam(),
        official_passing_score=2,
        official_passing_score_source=OfficialPassingScoreSource(
            title="Initial standard", publisher="Board"
        ),
    )

    created = catalog.create_exam(original, actor_id="admin")
    assert created["official_passing_score"] == 2
    assert created["official_passing_score_source"] == {
        "title": "Initial standard",
        "publisher": "Board",
    }
    assert created["revision"] == 1

    updated = catalog.replace_exam(
        "license-alpha",
        replace(
            original,
            title="Revised exam",
            official_passing_score=1,
            official_passing_score_source={
                "title": "Revised standard",
                "publisher": "Board",
            },
        ),
        actor_id="admin",
    )
    assert updated["title"] == "Revised exam"
    assert updated["official_passing_score"] == 1
    assert updated["official_passing_score_source"]["title"] == "Revised standard"
    assert updated["revision"] == 2


@pytest.mark.parametrize(
    ("score", "source"),
    [
        (True, {"title": "Standard", "publisher": "Board"}),
        (-1, {"title": "Standard", "publisher": "Board"}),
        (4, {"title": "Standard", "publisher": "Board"}),
        (1, None),
        (None, {"title": "Standard", "publisher": "Board"}),
        (1, {"title": "", "publisher": "Board"}),
        (1, {"title": "Standard", "publisher": ""}),
        (1, {"title": "Standard", "publisher": "Board", "url": "javascript:alert(1)"}),
        (1, {"title": "Standard", "publisher": "Board", "url": "data:text/html,x"}),
        (1, {"title": "Standard", "publisher": "Board", "url": "file:///tmp/x"}),
        (1, {"title": "Standard", "publisher": "Board", "url": "/relative"}),
        (1, {"title": "Standard", "publisher": "Board", "url": "https://exa mple.test"}),
        (1, {"title": "Standard", "publisher": "Board", "url": "https://u:p@example.test"}),
        (1, {"title": "Standard", "publisher": "Board", "published_at": " "}),
        (1, {"title": "Standard", "publisher": "Board", "extra": "guess"}),
    ],
)
def test_official_passing_score_rejects_unsafe_or_ambiguous_values(
    tmp_path: Path, score: object, source: object
) -> None:
    catalog = _catalog(tmp_path)
    catalog.create_exam(_exam(), actor_id="admin")

    with pytest.raises(DomainValidationError):
        catalog.set_official_passing_score(
            "license-alpha", score=score, source=source, actor_id="admin"
        )


def test_official_passing_score_can_change_for_active_exam_but_not_retired(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    catalog.create_exam(_exam(), actor_id="admin")
    with catalog.store.connect() as conn:
        conn.execute(
            "UPDATE exam_definitions SET status = 'active' WHERE id = ?", ("license-alpha",)
        )

    active = catalog.set_official_passing_score(
        "license-alpha",
        score=0,
        source={"title": "Standard", "publisher": "Board"},
        actor_id="admin",
    )
    assert active["official_passing_score"] == 0
    assert active["revision"] == 2

    with catalog.store.connect() as conn:
        conn.execute(
            "UPDATE exam_definitions SET status = 'retired' WHERE id = ?", ("license-alpha",)
        )
    with pytest.raises(InvalidTransitionError, match="retired"):
        catalog.set_official_passing_score(
            "license-alpha",
            score=1,
            source={"title": "Updated", "publisher": "Board"},
            actor_id="admin",
        )


def _score_snapshot(*, official: int | None = 2, target: int | None = 1) -> dict[str, object]:
    return {
        "snapshot_schema_version": 2,
        "id": "exam",
        "title": "Exam",
        "description": "",
        "duration_seconds": 60,
        "question_count": 3,
        "blueprint": {},
        "revision": 1,
        "maximum_score": 3,
        "official_passing_score": official,
        "official_passing_score_source": (
            {"title": "Standard", "publisher": "Board"} if official is not None else None
        ),
        "practice_target_score": target,
        "practice_target_origin": "user" if target is not None else None,
        "scoring_policy": {
            "type": "unit_correct",
            "version": 1,
            "points_per_item": 1,
        },
    }


def test_attempt_result_is_none_until_finalized_and_threshold_zero_is_evaluated() -> None:
    assert (
        evaluate_attempt_result(
            mode="exam",
            status="in_progress",
            correct_count=None,
            total_count=None,
            content_invalidated_count=0,
            exam_snapshot=_score_snapshot(official=0, target=0),
        )
        is None
    )

    result = evaluate_attempt_result(
        mode="exam",
        status="submitted",
        correct_count=0,
        total_count=3,
        content_invalidated_count=0,
        exam_snapshot=_score_snapshot(official=0, target=0),
    )

    assert result is not None
    assert result["score"] == 0
    assert result["maximum_score"] == 3
    assert result["validity"] == "eligible"
    assert result["official"]["status"] == "passed"
    assert result["practice_target"]["status"] == "achieved"


@pytest.mark.parametrize(
    ("mode", "official_status", "target_status"),
    [
        ("exam", "passed", "achieved"),
        ("practice", "not_evaluated", "achieved"),
        ("review", "not_evaluated", "not_evaluated"),
    ],
)
def test_attempt_result_respects_official_and_target_mode_boundaries(
    mode: str, official_status: str, target_status: str
) -> None:
    result = evaluate_attempt_result(
        mode=mode,
        status="expired",
        correct_count=2,
        total_count=3,
        content_invalidated_count=0,
        exam_snapshot=_score_snapshot(),
    )

    assert result is not None
    assert result["official"]["status"] == official_status
    assert result["practice_target"]["status"] == target_status


@pytest.mark.parametrize(
    ("invalidated", "total", "reason"),
    [(1, 2, "content_invalidated"), (0, 2, "incomplete_score_scope")],
)
def test_attempt_result_preserves_raw_score_but_withholds_invalid_judgements(
    invalidated: int, total: int, reason: str
) -> None:
    result = evaluate_attempt_result(
        mode="exam",
        status="submitted",
        correct_count=2,
        total_count=total,
        content_invalidated_count=invalidated,
        exam_snapshot=_score_snapshot(),
    )

    assert result is not None
    assert result["score"] == 2
    assert result["maximum_score"] == 3
    assert result["validity"] == ("content_invalidated" if invalidated else "eligible")
    assert result["official"] == {
        "status": "not_evaluated",
        "threshold": 2,
        "source": {"title": "Standard", "publisher": "Board"},
        "not_evaluated_reason": reason,
    }
    assert result["practice_target"] == {
        "status": "not_evaluated",
        "threshold": 1,
        "not_evaluated_reason": reason,
    }


def test_legacy_snapshot_is_never_guessed_as_an_official_standard() -> None:
    result = evaluate_attempt_result(
        mode="exam",
        status="submitted",
        correct_count=3,
        total_count=3,
        content_invalidated_count=0,
        exam_snapshot={"question_count": 3, "pass_score": 2},
    )

    assert result is not None
    assert result["score"] == 3
    assert result["maximum_score"] == 3
    assert result["official"] == {
        "status": "not_evaluated",
        "threshold": None,
        "source": None,
        "not_evaluated_reason": "legacy_score_ambiguous",
    }
    assert result["practice_target"] == {
        "status": "achieved",
        "threshold": 2,
        "not_evaluated_reason": None,
    }
