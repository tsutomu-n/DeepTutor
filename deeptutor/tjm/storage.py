from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sqlite3


class UnsupportedSchemaVersion(RuntimeError):
    """Raised instead of opening a database created by newer TJM code."""


def _execute_migration(conn: sqlite3.Connection, sql: str) -> None:
    """Execute a multi-statement migration without sqlite3's implicit commit."""
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if not sqlite3.complete_statement(buffer):
            continue
        statement = buffer.strip()
        if statement:
            conn.execute(statement)
        buffer = ""
    if buffer.strip():
        raise sqlite3.OperationalError("incomplete TJM migration statement")


class _SQLiteStore:
    migrations: tuple[str, ...] = ()

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _migrate(self) -> None:
        with self.connect() as conn:
            # Every constructor may run from a separate FastAPI worker thread.
            # Keep the version read, DDL, and ledger insert under one writer
            # lock; sqlite3.executescript() cannot be used because it commits
            # any pending transaction before executing its script.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            current = int(row[0] or 0)
            if current > len(self.migrations):
                raise UnsupportedSchemaVersion(
                    f"database schema {current} is newer than supported {len(self.migrations)}"
                )
            for version, sql in enumerate(self.migrations, start=1):
                if version <= current:
                    continue
                _execute_migration(conn, sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) "
                    "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                    (version,),
                )


_CATALOG_V1 = """
CREATE TABLE exam_definitions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0),
    question_count INTEGER NOT NULL CHECK (question_count > 0),
    pass_score INTEGER CHECK (pass_score IS NULL OR pass_score >= 0),
    blueprint_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'active', 'retired')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE questions (
    id TEXT PRIMARY KEY,
    exam_id TEXT NOT NULL REFERENCES exam_definitions(id),
    stable_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (exam_id, stable_id)
);

CREATE TABLE question_versions (
    id TEXT PRIMARY KEY,
    question_id TEXT NOT NULL REFERENCES questions(id),
    version INTEGER NOT NULL CHECK (version > 0),
    stem TEXT NOT NULL CHECK (length(trim(stem)) > 0),
    options_json TEXT NOT NULL,
    correct_option_key TEXT NOT NULL CHECK (length(trim(correct_option_key)) > 0),
    area TEXT NOT NULL CHECK (length(trim(area)) > 0),
    explanation TEXT NOT NULL DEFAULT '',
    hints_json TEXT NOT NULL DEFAULT '[]',
    source_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'rejected', 'published', 'retired')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT,
    UNIQUE (question_id, version),
    UNIQUE (question_id, content_hash)
);

CREATE INDEX idx_question_versions_status
    ON question_versions(status, question_id, version);

CREATE TABLE review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_version_id TEXT NOT NULL REFERENCES question_versions(id),
    action TEXT NOT NULL
        CHECK (action IN ('reviewed', 'published', 'rejected', 'retired')),
    actor_id TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_review_events_version
    ON review_events(question_version_id, created_at, id);

CREATE TABLE import_batches (
    id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    format TEXT NOT NULL CHECK (format IN ('json', 'jsonl', 'csv')),
    status TEXT NOT NULL CHECK (status IN ('validating', 'failed', 'completed')),
    actor_id TEXT NOT NULL,
    total_rows INTEGER NOT NULL DEFAULT 0 CHECK (total_rows >= 0),
    imported_rows INTEGER NOT NULL DEFAULT 0 CHECK (imported_rows >= 0),
    duplicate_rows INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_rows >= 0),
    errors_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    completed_at TEXT
);
"""


_CATALOG_V2 = """
CREATE UNIQUE INDEX idx_one_published_version_per_question
    ON question_versions(question_id) WHERE status = 'published';

CREATE TRIGGER prevent_published_version_content_update
BEFORE UPDATE ON question_versions
WHEN OLD.status IN ('published', 'retired') AND (
    NEW.question_id IS NOT OLD.question_id OR
    NEW.version IS NOT OLD.version OR
    NEW.stem IS NOT OLD.stem OR
    NEW.options_json IS NOT OLD.options_json OR
    NEW.correct_option_key IS NOT OLD.correct_option_key OR
    NEW.area IS NOT OLD.area OR
    NEW.explanation IS NOT OLD.explanation OR
    NEW.hints_json IS NOT OLD.hints_json OR
    NEW.source_json IS NOT OLD.source_json OR
    NEW.content_hash IS NOT OLD.content_hash OR
    NEW.created_by IS NOT OLD.created_by OR
    NEW.created_at IS NOT OLD.created_at OR
    NEW.published_at IS NOT OLD.published_at
)
BEGIN
    SELECT RAISE(ABORT, 'published question version content is immutable');
END;

CREATE TRIGGER prevent_published_version_delete
BEFORE DELETE ON question_versions
WHEN OLD.status IN ('published', 'retired')
BEGIN
    SELECT RAISE(ABORT, 'published question version is immutable');
END;

CREATE TRIGGER validate_question_version_transition
BEFORE UPDATE OF status ON question_versions
WHEN NOT (
    NEW.status = OLD.status OR
    (OLD.status = 'draft' AND NEW.status IN ('published', 'rejected')) OR
    (OLD.status = 'published' AND NEW.status = 'retired')
)
BEGIN
    SELECT RAISE(ABORT, 'invalid question version status transition');
END;

CREATE TRIGGER prevent_direct_published_insert
BEFORE INSERT ON question_versions
WHEN NEW.status = 'published'
BEGIN
    SELECT RAISE(ABORT, 'question version must be reviewed before publication');
END;

CREATE TRIGGER require_review_before_publish
BEFORE UPDATE OF status ON question_versions
WHEN NEW.status = 'published' AND OLD.status != 'published' AND NOT EXISTS (
    SELECT 1 FROM review_events
    WHERE question_version_id = OLD.id AND action = 'reviewed'
)
BEGIN
    SELECT RAISE(ABORT, 'question version must be reviewed before publication');
END;
"""


_CATALOG_V3 = """
ALTER TABLE question_versions
    ADD COLUMN content_revision INTEGER NOT NULL DEFAULT 1 CHECK (content_revision > 0);
ALTER TABLE question_versions
    ADD COLUMN updated_by TEXT NOT NULL DEFAULT '';
UPDATE question_versions
SET updated_by = CASE
    WHEN updated_at = created_at THEN created_by
    ELSE 'legacy-unknown'
END
WHERE updated_by = '';

CREATE TABLE question_version_revisions (
    question_version_id TEXT NOT NULL REFERENCES question_versions(id),
    content_revision INTEGER NOT NULL CHECK (content_revision > 0),
    stem TEXT NOT NULL CHECK (length(trim(stem)) > 0),
    options_json TEXT NOT NULL,
    correct_option_key TEXT NOT NULL CHECK (length(trim(correct_option_key)) > 0),
    area TEXT NOT NULL CHECK (length(trim(area)) > 0),
    explanation TEXT NOT NULL DEFAULT '',
    hints_json TEXT NOT NULL DEFAULT '[]',
    source_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (question_version_id, content_revision)
);

INSERT INTO question_version_revisions (
    question_version_id, content_revision, stem, options_json, correct_option_key,
    area, explanation, hints_json, source_json, content_hash, created_by, created_at
)
SELECT
    id, content_revision, stem, options_json, correct_option_key, area, explanation,
    hints_json, source_json, content_hash, updated_by, updated_at
FROM question_versions;

CREATE TABLE review_bindings (
    review_event_id INTEGER PRIMARY KEY REFERENCES review_events(id),
    question_version_id TEXT NOT NULL,
    content_revision INTEGER NOT NULL CHECK (content_revision > 0),
    content_hash TEXT NOT NULL,
    FOREIGN KEY (question_version_id, content_revision)
        REFERENCES question_version_revisions(question_version_id, content_revision)
);

CREATE INDEX idx_review_bindings_revision
    ON review_bindings(question_version_id, content_revision, review_event_id);

DROP TRIGGER require_review_before_publish;
DROP TRIGGER prevent_published_version_content_update;

CREATE TRIGGER prevent_question_identity_update
BEFORE UPDATE ON questions
WHEN
    NEW.id IS NOT OLD.id OR
    NEW.exam_id IS NOT OLD.exam_id OR
    NEW.stable_id IS NOT OLD.stable_id OR
    NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'question identity is immutable');
END;

CREATE TRIGGER prevent_question_version_identity_update
BEFORE UPDATE ON question_versions
WHEN
    NEW.id IS NOT OLD.id OR
    NEW.question_id IS NOT OLD.question_id OR
    NEW.version IS NOT OLD.version OR
    NEW.created_by IS NOT OLD.created_by OR
    NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'question version identity is immutable');
END;

CREATE TRIGGER prevent_published_version_content_update
BEFORE UPDATE ON question_versions
WHEN OLD.status != 'draft' AND (
    NEW.question_id IS NOT OLD.question_id OR
    NEW.version IS NOT OLD.version OR
    NEW.stem IS NOT OLD.stem OR
    NEW.options_json IS NOT OLD.options_json OR
    NEW.correct_option_key IS NOT OLD.correct_option_key OR
    NEW.area IS NOT OLD.area OR
    NEW.explanation IS NOT OLD.explanation OR
    NEW.hints_json IS NOT OLD.hints_json OR
    NEW.source_json IS NOT OLD.source_json OR
    NEW.content_hash IS NOT OLD.content_hash OR
    NEW.content_revision IS NOT OLD.content_revision OR
    NEW.updated_by IS NOT OLD.updated_by OR
    NEW.created_by IS NOT OLD.created_by OR
    NEW.created_at IS NOT OLD.created_at OR
    NEW.published_at IS NOT OLD.published_at
)
BEGIN
    SELECT RAISE(ABORT, 'non-draft question version content is immutable');
END;

CREATE TRIGGER validate_draft_content_revision
BEFORE UPDATE ON question_versions
WHEN OLD.status = 'draft' AND (
    NEW.stem IS NOT OLD.stem OR
    NEW.options_json IS NOT OLD.options_json OR
    NEW.correct_option_key IS NOT OLD.correct_option_key OR
    NEW.area IS NOT OLD.area OR
    NEW.explanation IS NOT OLD.explanation OR
    NEW.hints_json IS NOT OLD.hints_json OR
    NEW.source_json IS NOT OLD.source_json OR
    NEW.content_hash IS NOT OLD.content_hash
) AND NOT (
    OLD.status = 'draft' AND
    NEW.status = 'draft' AND
    NEW.content_revision = OLD.content_revision + 1
)
BEGIN
    SELECT RAISE(ABORT, 'draft content update must create the next revision');
END;

CREATE TRIGGER prevent_empty_content_revision
BEFORE UPDATE ON question_versions
WHEN NEW.content_revision IS NOT OLD.content_revision AND NOT (
    NEW.stem IS NOT OLD.stem OR
    NEW.options_json IS NOT OLD.options_json OR
    NEW.correct_option_key IS NOT OLD.correct_option_key OR
    NEW.area IS NOT OLD.area OR
    NEW.explanation IS NOT OLD.explanation OR
    NEW.hints_json IS NOT OLD.hints_json OR
    NEW.source_json IS NOT OLD.source_json OR
    NEW.content_hash IS NOT OLD.content_hash
)
BEGIN
    SELECT RAISE(ABORT, 'content revision requires a content change');
END;

CREATE TRIGGER snapshot_question_version_insert
AFTER INSERT ON question_versions
BEGIN
    INSERT INTO question_version_revisions (
        question_version_id, content_revision, stem, options_json, correct_option_key,
        area, explanation, hints_json, source_json, content_hash, created_by, created_at
    ) VALUES (
        NEW.id, NEW.content_revision, NEW.stem, NEW.options_json, NEW.correct_option_key,
        NEW.area, NEW.explanation, NEW.hints_json, NEW.source_json, NEW.content_hash,
        NEW.updated_by, NEW.created_at
    );
END;

CREATE TRIGGER snapshot_question_version_update
AFTER UPDATE OF content_revision ON question_versions
WHEN NEW.content_revision = OLD.content_revision + 1
BEGIN
    INSERT INTO question_version_revisions (
        question_version_id, content_revision, stem, options_json, correct_option_key,
        area, explanation, hints_json, source_json, content_hash, created_by, created_at
    ) VALUES (
        NEW.id, NEW.content_revision, NEW.stem, NEW.options_json, NEW.correct_option_key,
        NEW.area, NEW.explanation, NEW.hints_json, NEW.source_json, NEW.content_hash,
        NEW.updated_by, NEW.updated_at
    );
END;

CREATE TRIGGER prevent_question_revision_update
BEFORE UPDATE ON question_version_revisions
BEGIN
    SELECT RAISE(ABORT, 'question content revision is immutable');
END;

CREATE TRIGGER prevent_question_revision_delete
BEFORE DELETE ON question_version_revisions
BEGIN
    SELECT RAISE(ABORT, 'question content revision is immutable');
END;

CREATE TRIGGER prevent_review_event_update
BEFORE UPDATE ON review_events
BEGIN
    SELECT RAISE(ABORT, 'question review event is immutable');
END;

CREATE TRIGGER prevent_review_event_delete
BEFORE DELETE ON review_events
BEGIN
    SELECT RAISE(ABORT, 'question review event is immutable');
END;

CREATE TRIGGER validate_review_binding
BEFORE INSERT ON review_bindings
WHEN NOT EXISTS (
    SELECT 1
    FROM review_events AS event
    JOIN question_version_revisions AS revision
      ON revision.question_version_id = NEW.question_version_id
     AND revision.content_revision = NEW.content_revision
    WHERE event.id = NEW.review_event_id
      AND event.question_version_id = NEW.question_version_id
      AND event.action = 'reviewed'
      AND revision.content_hash = NEW.content_hash
)
BEGIN
    SELECT RAISE(ABORT, 'review binding must match a reviewed content revision');
END;

CREATE TRIGGER prevent_review_binding_update
BEFORE UPDATE ON review_bindings
BEGIN
    SELECT RAISE(ABORT, 'review binding is immutable');
END;

CREATE TRIGGER prevent_review_binding_delete
BEFORE DELETE ON review_bindings
BEGIN
    SELECT RAISE(ABORT, 'review binding is immutable');
END;

CREATE TRIGGER require_review_before_publish
BEFORE UPDATE OF status ON question_versions
WHEN NEW.status = 'published' AND OLD.status != 'published' AND NOT EXISTS (
    SELECT 1
    FROM review_bindings AS binding
    JOIN review_events AS event ON event.id = binding.review_event_id
    WHERE binding.question_version_id = OLD.id
      AND binding.content_revision = OLD.content_revision
      AND binding.content_hash = OLD.content_hash
      AND event.action = 'reviewed'
)
BEGIN
    SELECT RAISE(ABORT, 'current question revision must be reviewed before publication');
END;
"""


_LEARNING_V1 = """
CREATE TABLE attempts (
    id TEXT PRIMARY KEY,
    exam_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('practice', 'exam', 'review')),
    status TEXT NOT NULL DEFAULT 'in_progress'
        CHECK (status IN ('in_progress', 'submitted', 'expired')),
    exam_snapshot_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    deadline_at TEXT,
    submitted_at TEXT,
    correct_count INTEGER CHECK (correct_count IS NULL OR correct_count >= 0),
    total_count INTEGER CHECK (total_count IS NULL OR total_count >= 0)
);

CREATE TABLE attempt_items (
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    question_version_id TEXT NOT NULL,
    area TEXT NOT NULL,
    opened_at TEXT,
    answered_at TEXT,
    confirmed_option_key TEXT,
    confidence INTEGER CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 100),
    elapsed_ms INTEGER CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    hint_count INTEGER NOT NULL DEFAULT 0 CHECK (hint_count >= 0),
    PRIMARY KEY (attempt_id, position),
    UNIQUE (attempt_id, question_version_id)
);

CREATE TABLE answer_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'selected', 'confirmed', 'confidence', 'hint',
            'voice_candidate', 'voice_confirmed', 'voice_cancelled'
        )
    ),
    option_key TEXT,
    confidence INTEGER CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 100),
    elapsed_ms INTEGER CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    transcript TEXT,
    client_created_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (attempt_id, position)
        REFERENCES attempt_items(attempt_id, position) ON DELETE CASCADE
);

CREATE INDEX idx_answer_events_attempt_item
    ON answer_events(attempt_id, position, id);

CREATE TABLE review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_version_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    priority REAL NOT NULL DEFAULT 0,
    due_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'completed', 'dismissed')),
    source_attempt_id TEXT REFERENCES attempts(id),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX idx_review_queue_pending
    ON review_queue(status, due_at, priority DESC);
"""


_LEARNING_V2 = """
ALTER TABLE attempt_items ADD COLUMN first_presented_at TEXT;
ALTER TABLE attempt_items ADD COLUMN first_answered_at TEXT;
ALTER TABLE attempt_items ADD COLUMN final_answered_at TEXT;
ALTER TABLE attempt_items ADD COLUMN server_elapsed_ms INTEGER
    CHECK (server_elapsed_ms IS NULL OR server_elapsed_ms >= 0);
ALTER TABLE attempt_items ADD COLUMN client_active_elapsed_ms INTEGER
    CHECK (client_active_elapsed_ms IS NULL OR client_active_elapsed_ms >= 0);

UPDATE attempt_items
SET final_answered_at = answered_at,
    client_active_elapsed_ms = elapsed_ms;

ALTER TABLE answer_events ADD COLUMN client_event_id TEXT;
ALTER TABLE answer_events ADD COLUMN server_elapsed_ms INTEGER
    CHECK (server_elapsed_ms IS NULL OR server_elapsed_ms >= 0);
ALTER TABLE answer_events ADD COLUMN client_active_elapsed_ms INTEGER
    CHECK (client_active_elapsed_ms IS NULL OR client_active_elapsed_ms >= 0);

UPDATE answer_events SET client_active_elapsed_ms = elapsed_ms;

CREATE TABLE learning_commands (
    idempotency_key TEXT PRIMARY KEY CHECK (
        length(trim(idempotency_key)) BETWEEN 1 AND 200
    ),
    command_type TEXT NOT NULL CHECK (length(trim(command_type)) > 0),
    target_id TEXT NOT NULL CHECK (length(trim(target_id)) > 0),
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER prevent_learning_command_update
BEFORE UPDATE ON learning_commands
BEGIN
    SELECT RAISE(ABORT, 'learning command is immutable');
END;

CREATE TRIGGER prevent_learning_command_delete
BEFORE DELETE ON learning_commands
BEGIN
    SELECT RAISE(ABORT, 'learning command is immutable');
END;

CREATE TRIGGER prevent_answer_event_update
BEFORE UPDATE ON answer_events
BEGIN
    SELECT RAISE(ABORT, 'answer event is immutable');
END;

CREATE TRIGGER prevent_answer_event_delete
BEFORE DELETE ON answer_events
BEGIN
    SELECT RAISE(ABORT, 'answer event is immutable');
END;
"""


class CatalogStore(_SQLiteStore):
    """Authoritative deployment-wide exam and immutable question catalog."""

    migrations = (_CATALOG_V1, _CATALOG_V2, _CATALOG_V3)


class LearningStore(_SQLiteStore):
    """Request-owner-scoped attempts and append-only answer history."""

    migrations = (_LEARNING_V1, _LEARNING_V2)


__all__ = ["CatalogStore", "LearningStore", "UnsupportedSchemaVersion"]
