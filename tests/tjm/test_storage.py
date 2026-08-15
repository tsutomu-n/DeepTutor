from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

from deeptutor.multi_user import paths
from deeptutor.services.path_service import PathService
from deeptutor.tjm.storage import CatalogStore, LearningStore


def _v2_snapshot(maximum_score: int = 1, duration_seconds: int = 60) -> str:
    return json.dumps(
        {
            "snapshot_schema_version": 2,
            "id": "exam",
            "title": "Exam",
            "description": "",
            "duration_seconds": duration_seconds,
            "question_count": maximum_score,
            "blueprint": {},
            "revision": 1,
            "maximum_score": maximum_score,
            "official_passing_score": None,
            "official_passing_score_source": None,
            "practice_target_score": None,
            "practice_target_origin": None,
            "scoring_policy": {
                "type": "unit_correct",
                "version": 1,
                "points_per_item": 1,
            },
        },
        separators=(",", ":"),
    )


_LEGACY_SNAPSHOT = '{"question_count":1}'


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }


def _migration_versions(db_path: Path) -> list[int]:
    with sqlite3.connect(db_path) as conn:
        return [
            int(row[0])
            for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]


def test_tjm_catalog_and_learning_paths_have_different_scopes(tmp_path: Path, monkeypatch) -> None:
    system_root = tmp_path / "data" / "system"
    monkeypatch.setattr(paths, "SYSTEM_ROOT", system_root)

    admin_service = PathService(workspace_root=tmp_path / "data")
    alice_service = PathService(workspace_root=tmp_path / "data" / "users" / "u_alice")
    bob_service = PathService(workspace_root=tmp_path / "data" / "users" / "u_bob")

    assert paths.get_tjm_catalog_db() == system_root / "tjm" / "catalog.db"
    assert admin_service.get_tjm_learning_db() == tmp_path / "data" / "user" / "tjm_learning.db"
    assert alice_service.get_tjm_learning_db() == (
        tmp_path / "data" / "users" / "u_alice" / "user" / "tjm_learning.db"
    )
    assert bob_service.get_tjm_learning_db() != alice_service.get_tjm_learning_db()


def test_catalog_store_initializes_only_catalog_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "system" / "tjm" / "catalog.db"

    CatalogStore(db_path)
    CatalogStore(db_path)

    assert _table_names(db_path) == {
        "schema_migrations",
        "exam_definitions",
        "questions",
        "question_versions",
        "question_version_revisions",
        "review_events",
        "review_bindings",
        "import_batches",
    }
    assert _migration_versions(db_path) == [1, 2, 3, 4, 5]


def test_learning_store_initializes_only_user_learning_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "users" / "u_alice" / "user" / "tjm_learning.db"

    LearningStore(db_path)
    LearningStore(db_path)

    assert _table_names(db_path) == {
        "schema_migrations",
        "attempts",
        "attempt_items",
        "answer_events",
        "learning_commands",
        "review_queue",
        "review_attempt_queue_links",
        "exam_preferences",
    }
    assert _migration_versions(db_path) == [1, 2, 3, 4, 5, 6]


def test_learning_v6_links_legacy_voice_resolutions_to_their_candidate(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-voice.db"

    class LegacyLearningStore(LearningStore):
        migrations = LearningStore.migrations[:5]

    legacy = LegacyLearningStore(db_path)
    with legacy.connect() as conn:
        conn.execute(
            """
            INSERT INTO attempts (
                id, exam_id, mode, status, exam_snapshot_json, started_at
            ) VALUES ('attempt', 'exam', 'practice', 'in_progress', ?,
                      '2026-01-01T00:00:00Z')
            """,
            (_v2_snapshot(),),
        )
        conn.execute(
            """
            INSERT INTO attempt_items (
                attempt_id, position, question_version_id, area,
                first_presented_at, catalog_disposition
            ) VALUES ('attempt', 0, 'qv-1', 'area',
                      '2026-01-01T00:00:01Z', 'current')
            """
        )
        candidate_id = int(
            conn.execute(
                """
                INSERT INTO answer_events (
                    attempt_id, position, event_type, option_key, transcript, created_at
                ) VALUES ('attempt', 0, 'voice_candidate', 'A', '1番',
                          '2026-01-01T00:00:02Z')
                """
            ).lastrowid
        )
        resolution_id = int(
            conn.execute(
                """
                INSERT INTO answer_events (
                    attempt_id, position, event_type, option_key, transcript, created_at
                ) VALUES ('attempt', 0, 'voice_cancelled', 'A', '1番',
                          '2026-01-01T00:00:03Z')
                """
            ).lastrowid
        )

    migrated = LearningStore(db_path)

    assert _migration_versions(db_path) == [1, 2, 3, 4, 5, 6]
    with migrated.connect() as conn:
        resolution = conn.execute(
            "SELECT voice_candidate_id FROM answer_events WHERE id = ?",
            (resolution_id,),
        ).fetchone()
        assert resolution["voice_candidate_id"] == candidate_id
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                """
                INSERT INTO answer_events (
                    attempt_id, position, event_type, option_key, transcript,
                    created_at, voice_candidate_id
                ) VALUES ('attempt', 0, 'voice_confirmed', 'A', '1番',
                          '2026-01-01T00:00:04Z', ?)
                """,
                (candidate_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE answer_events SET transcript = '改変' WHERE id = ?",
                (candidate_id,),
            )


def test_learning_v6_rejects_orphaned_legacy_voice_resolution(tmp_path: Path) -> None:
    db_path = tmp_path / "orphaned-voice.db"

    class LegacyLearningStore(LearningStore):
        migrations = LearningStore.migrations[:5]

    legacy = LegacyLearningStore(db_path)
    with legacy.connect() as conn:
        conn.execute(
            """
            INSERT INTO attempts (
                id, exam_id, mode, status, exam_snapshot_json, started_at
            ) VALUES ('attempt', 'exam', 'practice', 'in_progress', ?,
                      '2026-01-01T00:00:00Z')
            """,
            (_v2_snapshot(),),
        )
        conn.execute(
            """
            INSERT INTO attempt_items (
                attempt_id, position, question_version_id, area,
                first_presented_at, catalog_disposition
            ) VALUES ('attempt', 0, 'qv-1', 'area',
                      '2026-01-01T00:00:01Z', 'current')
            """
        )
        conn.execute(
            """
            INSERT INTO answer_events (
                attempt_id, position, event_type, option_key, transcript, created_at
            ) VALUES ('attempt', 0, 'voice_confirmed', 'A', '1番',
                      '2026-01-01T00:00:02Z')
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="valid_learning_v6_voice_history"):
        LearningStore(db_path)
    assert _migration_versions(db_path) == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    (
        "candidate_option",
        "candidate_transcript",
        "resolution_type",
        "resolution_option",
        "resolution_transcript",
    ),
    [
        ("A", "1番", "voice_confirmed", "B", "1番"),
        ("A", "1番", "voice_confirmed", "A", "別の認識結果"),
        (None, "曖昧", "voice_confirmed", None, "曖昧"),
        ("\t", "1番", "voice_confirmed", "\t", "1番"),
        ("A", None, "voice_cancelled", "A", None),
        ("A", "  ", "voice_cancelled", "A", "  "),
        ("A", "\t", "voice_cancelled", "A", "\t"),
        ("A", "\n", "voice_cancelled", "A", "\n"),
        ("A", "　", "voice_cancelled", "A", "　"),
    ],
)
def test_learning_v6_rejects_invalid_legacy_voice_resolution(
    tmp_path: Path,
    candidate_option: str | None,
    candidate_transcript: str | None,
    resolution_type: str,
    resolution_option: str | None,
    resolution_transcript: str | None,
) -> None:
    db_path = tmp_path / "invalid-legacy-voice.db"

    class LegacyLearningStore(LearningStore):
        migrations = LearningStore.migrations[:5]

    legacy = LegacyLearningStore(db_path)
    with legacy.connect() as conn:
        conn.execute(
            """
            INSERT INTO attempts (
                id, exam_id, mode, status, exam_snapshot_json, started_at
            ) VALUES ('attempt', 'exam', 'practice', 'in_progress', ?,
                      '2026-01-01T00:00:00Z')
            """,
            (_v2_snapshot(),),
        )
        conn.execute(
            """
            INSERT INTO attempt_items (
                attempt_id, position, question_version_id, area,
                first_presented_at, catalog_disposition
            ) VALUES ('attempt', 0, 'qv-1', 'area',
                      '2026-01-01T00:00:01Z', 'current')
            """
        )
        conn.execute(
            """
            INSERT INTO answer_events (
                attempt_id, position, event_type, option_key, transcript, created_at
            ) VALUES ('attempt', 0, 'voice_candidate', ?, ?,
                      '2026-01-01T00:00:02Z')
            """,
            (candidate_option, candidate_transcript),
        )
        conn.execute(
            """
            INSERT INTO answer_events (
                attempt_id, position, event_type, option_key, transcript, created_at
            ) VALUES ('attempt', 0, ?, ?, ?, '2026-01-01T00:00:03Z')
            """,
            (resolution_type, resolution_option, resolution_transcript),
        )

    with pytest.raises(sqlite3.IntegrityError, match="valid_learning_v6_voice_history"):
        LearningStore(db_path)
    assert _migration_versions(db_path) == [1, 2, 3, 4, 5]
    with sqlite3.connect(db_path) as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(answer_events)")}
    assert "voice_candidate_id" not in columns


def test_learning_v6_rejects_invalid_new_voice_events(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "invalid-new-voice.db")
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO attempts (
                id, exam_id, mode, status, exam_snapshot_json, started_at
            ) VALUES ('attempt', 'exam', 'practice', 'in_progress', ?,
                      '2026-01-01T00:00:00Z')
            """,
            (_v2_snapshot(),),
        )
        conn.execute(
            """
            INSERT INTO attempt_items (
                attempt_id, position, question_version_id, area,
                first_presented_at, catalog_disposition
            ) VALUES ('attempt', 0, 'qv-1', 'area',
                      '2026-01-01T00:00:01Z', 'current')
            """
        )
        for invalid_transcript in (None, "", "  ", "\t", "\n", "　"):
            with pytest.raises(sqlite3.IntegrityError, match="invalid voice candidate event"):
                conn.execute(
                    """
                    INSERT INTO answer_events (
                        attempt_id, position, event_type, option_key, transcript, created_at
                    ) VALUES ('attempt', 0, 'voice_candidate', 'A', ?,
                              '2026-01-01T00:00:02Z')
                    """,
                    (invalid_transcript,),
                )
        valid_candidate_id = int(
            conn.execute(
                """
                INSERT INTO answer_events (
                    attempt_id, position, event_type, option_key, transcript, created_at
                ) VALUES ('attempt', 0, 'voice_candidate', 'A', '1番',
                          '2026-01-01T00:00:03Z')
                """
            ).lastrowid
        )
        for option_key, transcript in (("B", "1番"), ("A", "別の認識結果")):
            with pytest.raises(sqlite3.IntegrityError, match="invalid voice candidate event"):
                conn.execute(
                    """
                    INSERT INTO answer_events (
                        attempt_id, position, event_type, option_key, transcript,
                        created_at, voice_candidate_id
                    ) VALUES ('attempt', 0, 'voice_confirmed', ?, ?,
                              '2026-01-01T00:00:04Z', ?)
                    """,
                    (option_key, transcript, valid_candidate_id),
                )
        ambiguous_candidate_id = int(
            conn.execute(
                """
                INSERT INTO answer_events (
                    attempt_id, position, event_type, option_key, transcript, created_at
                ) VALUES ('attempt', 0, 'voice_candidate', NULL, '一番か二番',
                          '2026-01-01T00:00:05Z')
                """
            ).lastrowid
        )
        with pytest.raises(sqlite3.IntegrityError, match="invalid voice candidate event"):
            conn.execute(
                """
                INSERT INTO answer_events (
                    attempt_id, position, event_type, option_key, transcript,
                    created_at, voice_candidate_id
                ) VALUES ('attempt', 0, 'voice_confirmed', NULL, '一番か二番',
                          '2026-01-01T00:00:06Z', ?)
                """,
                (ambiguous_candidate_id,),
            )
        whitespace_option_candidate_id = int(
            conn.execute(
                """
                INSERT INTO answer_events (
                    attempt_id, position, event_type, option_key, transcript, created_at
                ) VALUES ('attempt', 0, 'voice_candidate', '\t', '候補',
                          '2026-01-01T00:00:07Z')
                """
            ).lastrowid
        )
        with pytest.raises(sqlite3.IntegrityError, match="invalid voice candidate event"):
            conn.execute(
                """
                INSERT INTO answer_events (
                    attempt_id, position, event_type, option_key, transcript,
                    created_at, voice_candidate_id
                ) VALUES ('attempt', 0, 'voice_confirmed', '\t', '候補',
                          '2026-01-01T00:00:08Z', ?)
                """,
                (whitespace_option_candidate_id,),
            )


def test_catalog_store_serializes_concurrent_initialization(tmp_path: Path) -> None:
    db_path = tmp_path / "shared" / "catalog.db"
    barrier = Barrier(16)

    def initialize(_: int) -> CatalogStore:
        barrier.wait()
        return CatalogStore(db_path)

    with ThreadPoolExecutor(max_workers=16) as executor:
        stores = list(executor.map(initialize, range(16)))

    assert len(stores) == 16
    assert _migration_versions(db_path) == [1, 2, 3, 4, 5]
    assert "exam_definitions" in _table_names(db_path)


def test_catalog_v3_migrates_legacy_content_without_binding_old_review(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-catalog.db"
    version_id = "legacy-qv-1"
    content_hash = "legacy-content-hash"

    class LegacyCatalogStore(CatalogStore):
        migrations = CatalogStore.migrations[:2]

    legacy = LegacyCatalogStore(db_path)
    with legacy.connect() as conn:
        conn.execute(
            """
            INSERT INTO exam_definitions (
                id, title, duration_seconds, question_count, blueprint_json,
                status, revision, created_by, created_at, updated_at
            ) VALUES ('legacy-exam', 'Legacy', 60, 1, '{}', 'draft', 1,
                      'admin', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO questions (id, exam_id, stable_id, created_at)
            VALUES ('legacy-q1', 'legacy-exam', 'legacy-q1', '2026-01-01T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO question_versions (
                id, question_id, version, stem, options_json, correct_option_key,
                area, explanation, hints_json, source_json, content_hash, status,
                created_by, created_at, updated_at
            ) VALUES (?, 'legacy-q1', 1, 'Legacy question',
                      '[{"key":"A","text":"First"},{"key":"B","text":"Second"}]',
                      'B', 'legacy', '', '[]', '{}', ?, 'draft', 'author',
                      '2026-01-01T00:00:00Z', '2026-01-01T00:02:00Z')
            """,
            (version_id, content_hash),
        )
        conn.execute(
            """
            INSERT INTO review_events (
                question_version_id, action, actor_id, note, created_at
            ) VALUES (?, 'reviewed', 'reviewer', 'old review', '2026-01-01T00:01:00Z')
            """,
            (version_id,),
        )

    migrated = CatalogStore(db_path)

    assert _migration_versions(db_path) == [1, 2, 3, 4, 5]
    with migrated.connect() as conn:
        current = conn.execute(
            "SELECT content_revision, content_hash FROM question_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        revision = conn.execute(
            """
            SELECT content_revision, stem, correct_option_key, content_hash,
                   created_by, created_at
            FROM question_version_revisions
            WHERE question_version_id = ?
            """,
            (version_id,),
        ).fetchone()
        assert dict(current) == {"content_revision": 1, "content_hash": content_hash}
        assert dict(revision) == {
            "content_revision": 1,
            "stem": "Legacy question",
            "correct_option_key": "B",
            "content_hash": content_hash,
            "created_by": "legacy-unknown",
            "created_at": "2026-01-01T00:02:00Z",
        }
        assert conn.execute("SELECT COUNT(*) FROM review_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM review_bindings").fetchone()[0] == 0
        legacy_event_id = conn.execute("SELECT id FROM review_events").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE review_events SET actor_id = 'attacker' WHERE id = ?",
                (legacy_event_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM review_events WHERE id = ?", (legacy_event_id,))


def test_learning_v2_preserves_client_metrics_without_inventing_server_time(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-learning.db"

    class LegacyLearningStore(LearningStore):
        migrations = LearningStore.migrations[:1]

    legacy = LegacyLearningStore(db_path)
    with legacy.connect() as conn:
        conn.execute(
            """
            INSERT INTO attempts (
                id, exam_id, mode, status, exam_snapshot_json, started_at
            ) VALUES ('legacy-attempt', 'exam', 'practice', 'in_progress', ?,
                      '2026-01-01T00:00:00Z')
            """,
            (_LEGACY_SNAPSHOT,),
        )
        conn.execute(
            """
            INSERT INTO attempt_items (
                attempt_id, position, question_version_id, area, opened_at,
                answered_at, confirmed_option_key, elapsed_ms
            ) VALUES ('legacy-attempt', 0, 'qv-1', 'area',
                      '2026-01-01T00:00:01Z', '2026-01-01T00:00:02Z', 'A', 9000)
            """
        )
        conn.execute(
            """
            INSERT INTO answer_events (
                attempt_id, position, event_type, option_key, elapsed_ms, created_at
            ) VALUES ('legacy-attempt', 0, 'confirmed', 'A', 9000,
                      '2026-01-01T00:00:02Z')
            """
        )

    migrated = LearningStore(db_path)

    assert _migration_versions(db_path) == [1, 2, 3, 4, 5, 6]
    with migrated.connect() as conn:
        item = conn.execute(
            """
            SELECT first_presented_at, first_answered_at, final_answered_at,
                   server_elapsed_ms, client_active_elapsed_ms
            FROM attempt_items WHERE attempt_id = 'legacy-attempt'
            """
        ).fetchone()
        event = conn.execute(
            """
            SELECT server_elapsed_ms, client_active_elapsed_ms
            FROM answer_events WHERE attempt_id = 'legacy-attempt'
            """
        ).fetchone()
        assert dict(item) == {
            "first_presented_at": None,
            "first_answered_at": None,
            "final_answered_at": "2026-01-01T00:00:02Z",
            "server_elapsed_ms": None,
            "client_active_elapsed_ms": 9000,
        }
        assert dict(event) == {
            "server_elapsed_ms": None,
            "client_active_elapsed_ms": 9000,
        }
        assert conn.execute("SELECT COUNT(*) FROM learning_commands").fetchone()[0] == 0
        event_id = conn.execute("SELECT id FROM answer_events").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE answer_events SET option_key = 'B' WHERE id = ?", (event_id,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM answer_events WHERE id = ?", (event_id,))


def test_catalog_v4_does_not_guess_legacy_retirement_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-retired-catalog.db"

    class LegacyCatalogStore(CatalogStore):
        migrations = CatalogStore.migrations[:3]

    legacy = LegacyCatalogStore(db_path)
    with legacy.connect() as conn:
        conn.execute(
            """
            INSERT INTO exam_definitions (
                id, title, duration_seconds, question_count, blueprint_json,
                status, revision, created_by, created_at, updated_at
            ) VALUES ('legacy-exam', 'Legacy', 60, 1, '{}', 'draft', 1,
                      'admin', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO questions (id, exam_id, stable_id, created_at)
            VALUES ('legacy-q1', 'legacy-exam', 'legacy-q1', '2026-01-01T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO question_versions (
                id, question_id, version, stem, options_json, correct_option_key,
                area, content_hash, status, created_by, created_at, updated_at, updated_by
            ) VALUES ('legacy-qv-1', 'legacy-q1', 1, 'Legacy question',
                      '[{"key":"A","text":"First"},{"key":"B","text":"Second"}]',
                      'B', 'legacy', 'legacy-hash', 'retired', 'author',
                      '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z', 'author')
            """
        )

    migrated = CatalogStore(db_path)

    with migrated.connect() as conn:
        row = conn.execute(
            """
            SELECT retirement_reason, retired_at, replacement_question_version_id
            FROM question_versions WHERE id = 'legacy-qv-1'
            """
        ).fetchone()
        assert dict(row) == {
            "retirement_reason": None,
            "retired_at": None,
            "replacement_question_version_id": None,
        }


def test_learning_v3_preserves_queue_and_adds_immutable_review_links(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-review-queue.db"

    class LegacyLearningStore(LearningStore):
        migrations = LearningStore.migrations[:2]

    legacy = LegacyLearningStore(db_path)
    with legacy.connect() as conn:
        conn.execute(
            """
            INSERT INTO attempts (
                id, exam_id, mode, status, exam_snapshot_json, started_at
            ) VALUES ('review-attempt', 'exam', 'review', 'in_progress', ?,
                      '2026-01-01T00:00:00Z')
            """,
            (_LEGACY_SNAPSHOT,),
        )
        conn.execute(
            """
            INSERT INTO attempt_items (
                attempt_id, position, question_version_id, area
            ) VALUES ('review-attempt', 0, 'qv-1', 'area')
            """
        )
        cursor = conn.execute(
            """
            INSERT INTO review_queue (
                question_version_id, reason, priority, status, created_at
            ) VALUES ('qv-1', 'incorrect', 100, 'pending', '2026-01-01T00:00:00Z')
            """
        )
        queue_row_id = int(cursor.lastrowid)

    migrated = LearningStore(db_path)

    with migrated.connect() as conn:
        item = conn.execute(
            """
            SELECT catalog_disposition, content_invalidated_at
            FROM attempt_items WHERE attempt_id = 'review-attempt'
            """
        ).fetchone()
        queue = conn.execute(
            """
            SELECT resolution_reason, resolution_attempt_id
            FROM review_queue WHERE id = ?
            """,
            (queue_row_id,),
        ).fetchone()
        assert dict(item) == {
            "catalog_disposition": "unchecked",
            "content_invalidated_at": None,
        }
        assert dict(queue) == {"resolution_reason": None, "resolution_attempt_id": None}
        assert conn.execute("SELECT COUNT(*) FROM review_attempt_queue_links").fetchone()[0] == 0
        conn.execute(
            """
            INSERT INTO review_attempt_queue_links (attempt_id, queue_row_id, linked_at)
            VALUES ('review-attempt', ?, '2026-01-01T00:00:01Z')
            """,
            (queue_row_id,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE review_attempt_queue_links SET linked_at = 'later' WHERE queue_row_id = ?",
                (queue_row_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "DELETE FROM review_attempt_queue_links WHERE queue_row_id = ?",
                (queue_row_id,),
            )
        unlinked = conn.execute(
            """
            INSERT INTO review_queue (
                question_version_id, reason, priority, status, created_at
            ) VALUES ('qv-1', 'late', 50, 'pending', '2026-01-01T00:00:02Z')
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="resolution transition"):
            conn.execute(
                """
                UPDATE review_queue SET
                    status = 'completed', resolved_at = '2026-01-01T00:00:03Z',
                    resolution_reason = 'review_completed',
                    resolution_attempt_id = 'review-attempt'
                WHERE id = ?
                """,
                (unlinked.lastrowid,),
            )
        other_queue = conn.execute(
            """
            INSERT INTO review_queue (
                question_version_id, reason, priority, status, created_at
            ) VALUES ('qv-2', 'incorrect', 100, 'pending', '2026-01-01T00:00:02Z')
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="match an active review item"):
            conn.execute(
                """
                INSERT INTO review_attempt_queue_links (attempt_id, queue_row_id, linked_at)
                VALUES ('review-attempt', ?, '2026-01-01T00:00:03Z')
                """,
                (other_queue.lastrowid,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="initial catalog disposition"):
            conn.execute(
                """
                INSERT INTO attempt_items (
                    attempt_id, position, question_version_id, area,
                    catalog_disposition
                ) VALUES ('review-attempt', 1, 'qv-2', 'area', 'invalid_content')
                """
            )


def test_tjm_stores_enable_foreign_keys_on_every_connection(tmp_path: Path) -> None:
    catalog = CatalogStore(tmp_path / "catalog.db")
    learning = LearningStore(tmp_path / "learning.db")

    with catalog.connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with learning.connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_learning_v4_rejects_new_attempt_while_exam_is_active_at_database_edge(
    tmp_path: Path,
) -> None:
    store = LearningStore(tmp_path / "learning.db")
    snapshot = _v2_snapshot(duration_seconds=3600)

    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO attempts (
                id, exam_id, mode, status, exam_snapshot_json, started_at, deadline_at
            ) VALUES ('exam-1', 'exam', 'exam', 'in_progress', ?,
                      '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z')
            """,
            (snapshot,),
        )
        conn.execute(
            """
            INSERT INTO attempt_items (
                attempt_id, position, question_version_id, area,
                catalog_disposition
            ) VALUES ('exam-1', 0, 'version-1', 'area', 'current')
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="already in progress"):
            conn.execute(
                """
                INSERT INTO attempts (
                    id, exam_id, mode, status, exam_snapshot_json, started_at
                ) VALUES ('practice-1', 'exam', 'practice', 'in_progress', ?,
                          '2026-01-01T00:00:01Z')
                """,
                (snapshot,),
            )
        conn.execute(
            """
            UPDATE attempts SET status = 'submitted', submitted_at = ?,
                                correct_count = 0, total_count = 1
            WHERE id = 'exam-1'
            """,
            ("2026-01-01T00:10:00Z",),
        )
        conn.execute(
            """
            INSERT INTO attempts (
                id, exam_id, mode, status, exam_snapshot_json, started_at
            ) VALUES ('practice-1', 'exam', 'practice', 'in_progress', ?,
                      '2026-01-01T00:10:01Z')
            """,
            (snapshot,),
        )
        conn.execute(
            """
            INSERT INTO attempts (
                id, exam_id, mode, status, exam_snapshot_json, started_at, deadline_at
            ) VALUES ('exam-2', 'exam', 'exam', 'in_progress', ?,
                      '2026-01-01T00:10:02Z', '2026-01-01T01:10:02Z')
            """,
            (snapshot,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="already in progress"):
            conn.execute(
                """
                INSERT INTO attempts (
                    id, exam_id, mode, status, exam_snapshot_json, started_at, deadline_at
                ) VALUES ('exam-3', 'exam', 'exam', 'in_progress', ?,
                          '2026-01-01T00:10:03Z', '2026-01-01T01:10:03Z')
                """,
                (snapshot,),
            )


def test_learning_v4_preserves_legacy_active_exam_conflicts_but_blocks_new_ones(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-active-exams.db"

    class LegacyLearningStore(LearningStore):
        migrations = LearningStore.migrations[:3]

    legacy = LegacyLearningStore(db_path)
    with legacy.connect() as conn:
        for attempt_id in ("legacy-exam-1", "legacy-exam-2"):
            conn.execute(
                """
                INSERT INTO attempts (
                    id, exam_id, mode, status, exam_snapshot_json,
                    started_at, deadline_at
                ) VALUES (?, 'exam', 'exam', 'in_progress', ?,
                          '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z')
                """,
                (attempt_id, _LEGACY_SNAPSHOT),
            )

    migrated = LearningStore(db_path)

    assert _migration_versions(db_path) == [1, 2, 3, 4, 5, 6]
    with migrated.connect() as conn:
        assert (
            conn.execute(
                """
            SELECT COUNT(*) FROM attempts
            WHERE exam_id = 'exam' AND mode = 'exam' AND status = 'in_progress'
            """
            ).fetchone()[0]
            == 2
        )
        with pytest.raises(sqlite3.IntegrityError, match="already in progress"):
            conn.execute(
                """
                INSERT INTO attempts (
                    id, exam_id, mode, status, exam_snapshot_json, started_at
                ) VALUES ('new-practice', 'exam', 'practice', 'in_progress', ?,
                          '2026-01-01T00:00:01Z')
                """,
                (_v2_snapshot(),),
            )


def test_catalog_v5_preserves_legacy_pass_score_without_promoting_it(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-score-catalog.db"

    class LegacyCatalogStore(CatalogStore):
        migrations = CatalogStore.migrations[:4]

    legacy = LegacyCatalogStore(db_path)
    with legacy.connect() as conn:
        conn.execute(
            """
            INSERT INTO exam_definitions (
                id, title, duration_seconds, question_count, pass_score, blueprint_json,
                status, revision, created_by, created_at, updated_at
            ) VALUES ('legacy-exam', 'Legacy', 60, 3, 2, '{}', 'active', 1,
                      'admin', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )

    migrated = CatalogStore(db_path)

    assert _migration_versions(db_path) == [1, 2, 3, 4, 5]
    with migrated.connect() as conn:
        row = conn.execute(
            """
            SELECT pass_score, official_passing_score,
                   official_passing_score_source_json
            FROM exam_definitions WHERE id = 'legacy-exam'
            """
        ).fetchone()
        assert dict(row) == {
            "pass_score": 2,
            "official_passing_score": None,
            "official_passing_score_source_json": None,
        }
        with pytest.raises(sqlite3.IntegrityError, match="legacy pass_score is immutable"):
            conn.execute("UPDATE exam_definitions SET pass_score = 1 WHERE id = 'legacy-exam'")
        with pytest.raises(sqlite3.IntegrityError, match="legacy pass_score cannot be set"):
            conn.execute(
                """
                INSERT INTO exam_definitions (
                    id, title, duration_seconds, question_count, pass_score,
                    blueprint_json, status, revision, created_by, created_at, updated_at
                ) VALUES ('new-legacy', 'Forbidden', 60, 1, 1, '{}', 'draft', 1,
                          'admin', 'now', 'now')
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="official passing score"):
            conn.execute(
                """
                UPDATE exam_definitions SET
                    official_passing_score = 1,
                    official_passing_score_source_json =
                        '{"title":"Standard","publisher":"Board","url":42}'
                WHERE id = 'legacy-exam'
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="official passing score"):
            conn.execute(
                """
                UPDATE exam_definitions SET
                    official_passing_score = 1,
                    official_passing_score_source_json =
                        '{"title":"Standard","publisher":"Board","url":"http://"}',
                    revision = revision + 1
                WHERE id = 'legacy-exam'
                """
            )
        conn.execute(
            """
            UPDATE exam_definitions SET status = 'retired'
            WHERE id = 'legacy-exam'
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="retired exam"):
            conn.execute(
                """
                UPDATE exam_definitions SET
                    official_passing_score = 1,
                    official_passing_score_source_json =
                        '{"title":"Standard","publisher":"Board"}',
                    revision = revision + 1
                WHERE id = 'legacy-exam'
                """
            )


def test_catalog_v5_requires_revision_for_direct_official_score_updates(
    tmp_path: Path,
) -> None:
    store = CatalogStore(tmp_path / "catalog.db")
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO exam_definitions (
                id, title, duration_seconds, question_count, blueprint_json,
                status, revision, created_by, created_at, updated_at
            ) VALUES ('exam', 'Exam', 60, 1, '{}', 'draft', 1,
                      'admin', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="revision"):
            conn.execute(
                """
                UPDATE exam_definitions SET
                    official_passing_score = 1,
                    official_passing_score_source_json =
                        '{"title":"Standard","publisher":"Board"}'
                WHERE id = 'exam'
                """
            )


def test_catalog_v5_enforces_path_safe_immutable_exam_ids(tmp_path: Path) -> None:
    store = CatalogStore(tmp_path / "catalog.db")
    with store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="URL-safe ASCII path segment"):
            conn.execute(
                """
                INSERT INTO exam_definitions (
                    id, title, duration_seconds, question_count, blueprint_json,
                    status, revision, created_by, created_at, updated_at
                ) VALUES ('exam/a', 'Exam', 60, 1, '{}', 'draft', 1,
                          'admin', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """
            )
        conn.execute(
            """
            INSERT INTO exam_definitions (
                id, title, duration_seconds, question_count, blueprint_json,
                status, revision, created_by, created_at, updated_at
            ) VALUES ('exam', 'Exam', 60, 1, '{}', 'draft', 1,
                      'admin', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="exam id is immutable"):
            conn.execute("UPDATE exam_definitions SET id = 'other' WHERE id = 'exam'")


def test_catalog_v5_aborts_on_unrouteable_v4_exam_ids(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-invalid-id.db"

    class LegacyCatalogStore(CatalogStore):
        migrations = CatalogStore.migrations[:4]

    legacy = LegacyCatalogStore(db_path)
    with legacy.connect() as conn:
        conn.execute(
            """
            INSERT INTO exam_definitions (
                id, title, duration_seconds, question_count, blueprint_json,
                status, revision, created_by, created_at, updated_at
            ) VALUES ('exam/a', 'Exam', 60, 1, '{}', 'draft', 1,
                      'admin', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="valid_catalog_v5_exam_ids"):
        CatalogStore(db_path)
    assert _migration_versions(db_path) == [1, 2, 3, 4]


@pytest.mark.parametrize(
    "source_json",
    [
        '{"title":"Standard","publisher":"Board","url":"https://user:secret@example.test/notice"}',
        '{"title":"Standard","publisher":"Board","url":"https://example.test/\\tbad"}',
        '{"title":"Standard","publisher":"Board","url":"https://example.test:99999/notice"}',
        '{"title":"Standard","title":42,"publisher":"Board"}',
    ],
)
def test_catalog_v5_uses_domain_source_validation_at_database_edge(
    tmp_path: Path,
    source_json: str,
) -> None:
    store = CatalogStore(tmp_path / "catalog.db")
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO exam_definitions (
                id, title, duration_seconds, question_count, blueprint_json,
                status, revision, created_by, created_at, updated_at
            ) VALUES ('exam', 'Exam', 60, 1, '{}', 'draft', 1,
                      'admin', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="official passing score"):
            conn.execute(
                """
                UPDATE exam_definitions SET
                    official_passing_score = 1,
                    official_passing_score_source_json = ?,
                    revision = revision + 1
                WHERE id = 'exam'
                """,
                (source_json,),
            )


def test_learning_v5_distinguishes_absent_and_explicitly_cleared_target(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning.db")

    with store.connect() as conn:
        assert (
            conn.execute(
                "SELECT practice_target_score FROM exam_preferences WHERE exam_id = 'exam'"
            ).fetchone()
            is None
        )
        conn.execute(
            """
            INSERT INTO exam_preferences (
                exam_id, practice_target_score, origin, created_at, updated_at
            ) VALUES ('exam', NULL, 'user', '2026-01-01T00:00:00Z',
                      '2026-01-01T00:00:00Z')
            """
        )
        row = conn.execute(
            "SELECT practice_target_score, origin FROM exam_preferences WHERE exam_id = 'exam'"
        ).fetchone()
        assert dict(row) == {"practice_target_score": None, "origin": "user"}
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            conn.execute("DELETE FROM exam_preferences WHERE exam_id = 'exam'")
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            conn.execute(
                """
                INSERT OR REPLACE INTO exam_preferences (
                    exam_id, practice_target_score, origin, created_at, updated_at
                ) VALUES ('exam', 1, 'legacy_pass_score', 'now', 'now')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO exam_preferences (
                    exam_id, practice_target_score, origin, created_at, updated_at
                ) VALUES ('bad', -1, 'user', 'now', 'now')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO exam_preferences (
                    exam_id, practice_target_score, origin, created_at, updated_at
                ) VALUES ('ambiguous', NULL, 'legacy_pass_score', 'now', 'now')
                """
            )


def test_learning_v5_makes_attempt_snapshot_and_final_score_immutable(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning.db")
    snapshot = _v2_snapshot(maximum_score=3)

    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO attempts (
                id, exam_id, mode, status, exam_snapshot_json, started_at
            ) VALUES ('attempt', 'exam', 'practice', 'in_progress', ?,
                      '2026-01-01T00:00:00Z')
            """,
            (snapshot,),
        )
        conn.executemany(
            """
            INSERT INTO attempt_items (
                attempt_id, position, question_version_id, area,
                catalog_disposition
            ) VALUES ('attempt', ?, ?, 'area', 'current')
            """,
            [(position, f"version-{position}") for position in range(3)],
        )
        with pytest.raises(sqlite3.IntegrityError, match="snapshot is immutable"):
            conn.execute("UPDATE attempts SET exam_snapshot_json = '{}' WHERE id = 'attempt'")
        conn.execute(
            """
            UPDATE attempts SET status = 'submitted', submitted_at = ?,
                                correct_count = 2, total_count = 3
            WHERE id = 'attempt'
            """,
            ("2026-01-01T00:01:00Z",),
        )
        with pytest.raises(sqlite3.IntegrityError, match="final score is immutable"):
            conn.execute("UPDATE attempts SET correct_count = 3 WHERE id = 'attempt'")
        with pytest.raises(sqlite3.IntegrityError, match="finalized attempt is immutable"):
            conn.execute("UPDATE attempts SET status = 'in_progress' WHERE id = 'attempt'")
        with pytest.raises(sqlite3.IntegrityError, match="finalized attempt is immutable"):
            conn.execute("UPDATE attempts SET mode = 'exam' WHERE id = 'attempt'")
        with pytest.raises(sqlite3.IntegrityError, match="finalized attempt is immutable"):
            conn.execute(
                "UPDATE attempts SET submitted_at = '2026-02-01T00:00:00Z' WHERE id = 'attempt'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="attempt is immutable"):
            conn.execute("DELETE FROM attempts WHERE id = 'attempt'")
        with pytest.raises(sqlite3.IntegrityError, match="attempt identity is immutable"):
            conn.execute(
                """
                INSERT OR REPLACE INTO attempts (
                    id, exam_id, mode, status, exam_snapshot_json,
                    started_at, submitted_at, correct_count, total_count
                ) VALUES ('attempt', 'other', 'exam', 'submitted', '{}',
                          '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z', 1, 1)
                """
            )


@pytest.mark.parametrize(
    ("correct_count", "total_count"),
    [(None, None), (2, 1), (1.5, 2), (1, 2.5), (1, 1)],
)
def test_learning_v5_rejects_invalid_final_score_transitions(
    tmp_path: Path,
    correct_count: object,
    total_count: object,
) -> None:
    store = LearningStore(tmp_path / "learning.db")
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO attempts (
                id, exam_id, mode, status, exam_snapshot_json, started_at
            ) VALUES ('attempt', 'exam', 'practice', 'in_progress', ?,
                      '2026-01-01T00:00:00Z')
            """,
            (_v2_snapshot(),),
        )
        with pytest.raises(sqlite3.IntegrityError, match="invalid finalized attempt score"):
            conn.execute(
                """
                UPDATE attempts SET status = 'submitted', submitted_at = ?,
                                    correct_count = ?, total_count = ?
                WHERE id = 'attempt'
                """,
                ("2026-01-01T00:01:00Z", correct_count, total_count),
            )


def test_learning_v5_closes_finalized_items_and_answer_history(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning.db")
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO attempts (
                id, exam_id, mode, status, exam_snapshot_json, started_at
            ) VALUES ('attempt', 'exam', 'practice', 'in_progress', ?,
                      '2026-01-01T00:00:00Z')
            """,
            (_v2_snapshot(),),
        )
        conn.execute(
            """
            INSERT INTO attempt_items (
                attempt_id, position, question_version_id, area,
                confirmed_option_key, catalog_disposition
            ) VALUES ('attempt', 0, 'version-1', 'area', 'A', 'current')
            """
        )
        conn.execute(
            """
            INSERT INTO answer_events (
                attempt_id, position, event_type, option_key, created_at
            ) VALUES ('attempt', 0, 'confirmed', 'A', '2026-01-01T00:00:30Z')
            """
        )
        conn.execute(
            """
            UPDATE attempts SET status = 'submitted', submitted_at = ?,
                                correct_count = 1, total_count = 1
            WHERE id = 'attempt'
            """,
            ("2026-01-01T00:01:00Z",),
        )

        with pytest.raises(sqlite3.IntegrityError, match="finalized attempt items"):
            conn.execute(
                "UPDATE attempt_items SET confirmed_option_key = 'B' "
                "WHERE attempt_id = 'attempt' AND position = 0"
            )
        with pytest.raises(sqlite3.IntegrityError, match="finalized attempt items"):
            conn.execute(
                """
                INSERT INTO attempt_items (
                    attempt_id, position, question_version_id, area
                ) VALUES ('attempt', 1, 'version-2', 'area')
                """
            )
        conn.execute(
            """
            INSERT INTO attempts (
                id, exam_id, mode, status, exam_snapshot_json, started_at
            ) VALUES ('active-attempt', 'exam', 'practice', 'in_progress', ?,
                      '2026-01-02T00:00:00Z')
            """,
            (_v2_snapshot(),),
        )
        conn.execute(
            """
            INSERT INTO attempt_items (
                attempt_id, position, question_version_id, area,
                catalog_disposition
            ) VALUES ('active-attempt', 1, 'version-active', 'area', 'current')
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="finalized attempt items"):
            conn.execute(
                """
                UPDATE attempt_items SET attempt_id = 'attempt'
                WHERE attempt_id = 'active-attempt' AND position = 1
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="finalized attempt items"):
            conn.execute("DELETE FROM attempt_items WHERE attempt_id = 'attempt' AND position = 0")
        with pytest.raises(sqlite3.IntegrityError, match="answer history"):
            conn.execute(
                """
                INSERT INTO answer_events (
                    attempt_id, position, event_type, option_key, created_at
                ) VALUES ('attempt', 0, 'selected', 'B', '2026-01-01T00:02:00Z')
                """
            )

        conn.execute(
            """
            UPDATE attempt_items SET
                catalog_disposition = 'invalid_content',
                content_invalidated_at = '2026-01-02T00:00:00Z'
            WHERE attempt_id = 'attempt' AND position = 0
            """
        )
        row = conn.execute(
            """
            SELECT confirmed_option_key, catalog_disposition
            FROM attempt_items WHERE attempt_id = 'attempt' AND position = 0
            """
        ).fetchone()
        assert dict(row) == {
            "confirmed_option_key": "A",
            "catalog_disposition": "invalid_content",
        }
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM answer_events WHERE attempt_id = 'attempt'"
            ).fetchone()[0]
            == 1
        )


def test_learning_v5_requires_valid_new_attempts_and_immutable_schedule(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning.db")
    with store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="valid in-progress attempt"):
            conn.execute(
                """
                INSERT INTO attempts (
                    id, exam_id, mode, status, exam_snapshot_json,
                    started_at, submitted_at, correct_count, total_count
                ) VALUES ('forged', 'exam', 'practice', 'submitted', '{}',
                          '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z', 1, 1)
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="valid in-progress attempt"):
            conn.execute(
                """
                INSERT INTO attempts (
                    id, exam_id, mode, status, exam_snapshot_json, started_at
                ) VALUES ('bad-json', 'exam', 'practice', 'in_progress', 'not-json',
                          '2026-01-01T00:00:00Z')
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="valid in-progress attempt"):
            conn.execute(
                """
                INSERT INTO attempts (
                    id, exam_id, mode, status, exam_snapshot_json, started_at
                ) VALUES ('wrong-exam', 'other', 'practice', 'in_progress', ?,
                          '2026-01-01T00:00:00Z')
                """,
                (_v2_snapshot(),),
            )
        with pytest.raises(sqlite3.IntegrityError, match="valid in-progress attempt"):
            conn.execute(
                """
                INSERT INTO attempts (
                    id, exam_id, mode, status, exam_snapshot_json, started_at
                ) VALUES ('spaced-exam', ' exam ', 'practice', 'in_progress', ?,
                          '2026-01-01T00:00:00Z')
                """,
                (_v2_snapshot(),),
            )
        with pytest.raises(sqlite3.IntegrityError, match="valid in-progress attempt"):
            conn.execute(
                """
                INSERT INTO attempts (
                    id, exam_id, mode, status, exam_snapshot_json,
                    started_at, deadline_at
                ) VALUES ('bad-deadline', 'exam', 'exam', 'in_progress', ?,
                          '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z')
                """,
                (_v2_snapshot(duration_seconds=60),),
            )
        conn.execute(
            """
            INSERT INTO attempts (
                id, exam_id, mode, status, exam_snapshot_json, started_at
            ) VALUES ('attempt', 'exam', 'practice', 'in_progress', ?,
                      '2026-01-01T00:00:00Z')
            """,
            (_v2_snapshot(),),
        )
        for mutation in (
            "exam_id = 'other'",
            "mode = 'exam'",
            "started_at = '2026-02-01T00:00:00Z'",
            "deadline_at = '2026-02-01T01:00:00Z'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="identity and schedule"):
                conn.execute(f"UPDATE attempts SET {mutation} WHERE id = 'attempt'")


def test_learning_v5_validates_submission_and_expiry_timestamps(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning.db")
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO attempts (
                id, exam_id, mode, status, exam_snapshot_json,
                started_at, deadline_at
            ) VALUES ('attempt', 'exam', 'exam', 'in_progress', ?,
                      '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z')
            """,
            (_v2_snapshot(duration_seconds=60),),
        )
        conn.execute(
            """
            INSERT INTO attempt_items (
                attempt_id, position, question_version_id, area,
                catalog_disposition
            ) VALUES ('attempt', 0, 'version-1', 'area', 'current')
            """
        )
        for final_status, submitted_at in (
            ("submitted", "not-a-time"),
            ("submitted", "2026-01-01T00:01:00Z"),
            ("expired", "2026-01-01T00:00:30Z"),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="invalid finalized attempt score"):
                conn.execute(
                    """
                    UPDATE attempts SET status = ?, submitted_at = ?,
                                        correct_count = 0, total_count = 1
                    WHERE id = 'attempt'
                    """,
                    (final_status, submitted_at),
                )
        conn.execute(
            """
            UPDATE attempts SET status = 'expired', submitted_at = ?,
                                correct_count = 0, total_count = 1
            WHERE id = 'attempt'
            """,
            ("2026-01-01T00:01:00Z",),
        )


@pytest.mark.parametrize(
    ("snapshot_json", "correct_count", "total_count", "submitted_at"),
    [
        ("not-json", 0, 0, "2026-01-01T00:01:00Z"),
        (_LEGACY_SNAPSHOT, None, None, "2026-01-01T00:01:00Z"),
        (_LEGACY_SNAPSHOT, 1.5, 2, "2026-01-01T00:01:00Z"),
        (_LEGACY_SNAPSHOT, 0, 0, "not-a-time"),
        (_v2_snapshot(), 1, 1, "2026-01-01T00:01:00Z"),
    ],
)
def test_learning_v5_aborts_before_freezing_invalid_v4_history(
    tmp_path: Path,
    snapshot_json: str,
    correct_count: object,
    total_count: object,
    submitted_at: str,
) -> None:
    db_path = tmp_path / "legacy-invalid.db"

    class LegacyLearningStore(LearningStore):
        migrations = LearningStore.migrations[:4]

    legacy = LegacyLearningStore(db_path)
    with legacy.connect() as conn:
        conn.execute(
            """
            INSERT INTO attempts (
                id, exam_id, mode, status, exam_snapshot_json,
                started_at, submitted_at, correct_count, total_count
            ) VALUES ('legacy', 'exam', 'practice', 'submitted', ?,
                      '2026-01-01T00:00:00Z', ?, ?, ?)
            """,
            (snapshot_json, submitted_at, correct_count, total_count),
        )

    with pytest.raises(sqlite3.IntegrityError, match="valid_learning_v5_history"):
        LearningStore(db_path)
    assert _migration_versions(db_path) == [1, 2, 3, 4]
