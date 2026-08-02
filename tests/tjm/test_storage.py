from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from threading import Barrier

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
        "review_events",
        "import_batches",
    }
    assert _migration_versions(db_path) == [1, 2]


def test_learning_store_initializes_only_user_learning_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "users" / "u_alice" / "user" / "tjm_learning.db"

    LearningStore(db_path)
    LearningStore(db_path)

    assert _table_names(db_path) == {
        "schema_migrations",
        "attempts",
        "attempt_items",
        "answer_events",
        "review_queue",
    }
    assert _migration_versions(db_path) == [1]


def test_catalog_store_serializes_concurrent_initialization(tmp_path: Path) -> None:
    db_path = tmp_path / "shared" / "catalog.db"
    barrier = Barrier(16)

    def initialize(_: int) -> CatalogStore:
        barrier.wait()
        return CatalogStore(db_path)

    with ThreadPoolExecutor(max_workers=16) as executor:
        stores = list(executor.map(initialize, range(16)))

    assert len(stores) == 16
    assert _migration_versions(db_path) == [1, 2]
    assert "exam_definitions" in _table_names(db_path)


def test_tjm_stores_enable_foreign_keys_on_every_connection(tmp_path: Path) -> None:
    catalog = CatalogStore(tmp_path / "catalog.db")
    learning = LearningStore(tmp_path / "learning.db")

    with catalog.connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with learning.connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
