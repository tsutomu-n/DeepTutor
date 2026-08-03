from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

from deeptutor.multi_user import paths
from deeptutor.services.path_service import PathService
from deeptutor.tjm.storage import CatalogStore, LearningStore


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
    assert _migration_versions(db_path) == [1, 2, 3]


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
    }
    assert _migration_versions(db_path) == [1, 2]


def test_catalog_store_serializes_concurrent_initialization(tmp_path: Path) -> None:
    db_path = tmp_path / "shared" / "catalog.db"
    barrier = Barrier(16)

    def initialize(_: int) -> CatalogStore:
        barrier.wait()
        return CatalogStore(db_path)

    with ThreadPoolExecutor(max_workers=16) as executor:
        stores = list(executor.map(initialize, range(16)))

    assert len(stores) == 16
    assert _migration_versions(db_path) == [1, 2, 3]
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

    assert _migration_versions(db_path) == [1, 2, 3]
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
            ) VALUES ('legacy-attempt', 'exam', 'practice', 'in_progress', '{}',
                      '2026-01-01T00:00:00Z')
            """
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

    assert _migration_versions(db_path) == [1, 2]
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


def test_tjm_stores_enable_foreign_keys_on_every_connection(tmp_path: Path) -> None:
    catalog = CatalogStore(tmp_path / "catalog.db")
    learning = LearningStore(tmp_path / "learning.db")

    with catalog.connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with learning.connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
