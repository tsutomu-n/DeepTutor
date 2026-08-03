from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import time

from .domain import (
    DomainValidationError,
    OfficialPassingScoreSource,
    normalize_attempt_snapshot,
    normalize_exam_id,
)


class UnsupportedSchemaVersion(RuntimeError):
    """Raised instead of opening a database created by newer TJM code."""


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Set persistent WAL mode despite concurrent first-open races."""
    deadline = time.monotonic() + 30
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _valid_official_source_json(value: object) -> int:
    """Expose the domain validator to SQLite without propagating UDF errors."""
    if not isinstance(value, str):
        return 0
    try:
        decoded = json.loads(value, object_pairs_hook=_reject_duplicate_json_keys)
        OfficialPassingScoreSource.from_value(decoded).normalized()
    except (DomainValidationError, TypeError, ValueError):
        return 0
    return 1


def _valid_exam_id(value: object) -> int:
    try:
        return int(isinstance(value, str) and normalize_exam_id(value) == value)
    except DomainValidationError:
        return 0


def _valid_attempt_record(
    snapshot_json: object,
    exam_id: object,
    mode: object,
    status: object,
    started_at: object,
    deadline_at: object,
    submitted_at: object,
    allow_legacy: object,
) -> int:
    if (
        not isinstance(snapshot_json, str)
        or not isinstance(exam_id, str)
        or not exam_id.strip()
        or exam_id != exam_id.strip()
        or _valid_exam_id(exam_id) != 1
        or not isinstance(mode, str)
        or not isinstance(status, str)
        or not isinstance(started_at, str)
        or not started_at.strip()
    ):
        return 0
    try:
        decoded = json.loads(snapshot_json, object_pairs_hook=_reject_duplicate_json_keys)
        snapshot = normalize_attempt_snapshot(
            decoded,
            mode=mode,
            allow_legacy=bool(allow_legacy),
        )
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except (DomainValidationError, TypeError, ValueError):
        return 0
    if started.tzinfo is None:
        return 0
    snapshot_exam_id = snapshot["exam_id"]
    if snapshot_exam_id is not None and snapshot_exam_id != exam_id.strip():
        return 0
    submitted: datetime | None = None
    if status == "in_progress":
        if submitted_at is not None:
            return 0
    elif status in {"submitted", "expired"}:
        if not isinstance(submitted_at, str) or not submitted_at.strip():
            return 0
        try:
            submitted = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
        except ValueError:
            return 0
        if submitted.tzinfo is None or submitted < started:
            return 0
    else:
        return 0
    if mode != "exam":
        return int(mode in {"practice", "review"} and deadline_at is None and status != "expired")
    legacy = bool(snapshot["legacy"])
    if deadline_at is None:
        return int(legacy and status == "submitted")
    if not isinstance(deadline_at, str) or not deadline_at.strip():
        return 0
    try:
        deadline = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if deadline.tzinfo is None or deadline <= started:
        return 0
    duration_seconds = snapshot["duration_seconds"]
    if duration_seconds is not None and deadline - started != timedelta(
        seconds=int(duration_seconds)
    ):
        return 0
    if duration_seconds is None and not legacy:
        return 0
    if status == "submitted":
        return int(submitted is not None and submitted < deadline)
    if status == "expired":
        return int(submitted == deadline)
    return 1


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
        conn.create_function(
            "tjm_valid_exam_id",
            1,
            _valid_exam_id,
            deterministic=True,
        )
        conn.create_function(
            "tjm_valid_official_source_json",
            1,
            _valid_official_source_json,
            deterministic=True,
        )
        conn.create_function(
            "tjm_valid_attempt_record",
            8,
            _valid_attempt_record,
            deterministic=True,
        )
        conn.execute("PRAGMA foreign_keys = ON")
        # SQLite otherwise lets INSERT OR REPLACE bypass BEFORE DELETE audit
        # guards through its implicit delete step.
        conn.execute("PRAGMA recursive_triggers = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        _enable_wal(conn)
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


_CATALOG_V4 = """
ALTER TABLE question_versions ADD COLUMN retirement_reason TEXT CHECK (
    retirement_reason IS NULL OR
    retirement_reason IN ('superseded', 'invalid_content')
);
ALTER TABLE question_versions ADD COLUMN retired_at TEXT;
ALTER TABLE question_versions ADD COLUMN replacement_question_version_id TEXT
    REFERENCES question_versions(id);

CREATE INDEX idx_question_versions_replacement
    ON question_versions(replacement_question_version_id);

CREATE TRIGGER prevent_retirement_metadata_insert
BEFORE INSERT ON question_versions
WHEN
    NEW.status = 'retired' OR
    NEW.retirement_reason IS NOT NULL OR
    NEW.retired_at IS NOT NULL OR
    NEW.replacement_question_version_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'question version must transition to retired');
END;

CREATE TRIGGER validate_new_question_retirement
BEFORE UPDATE ON question_versions
WHEN OLD.status = 'published' AND NEW.status = 'retired' AND (
    NEW.retirement_reason IS NULL OR
    NEW.retired_at IS NULL OR
    (NEW.retirement_reason = 'superseded' AND (
        NEW.replacement_question_version_id IS NULL OR
        NOT EXISTS (
            SELECT 1 FROM question_versions AS replacement
            WHERE replacement.id = NEW.replacement_question_version_id
              AND replacement.question_id = OLD.question_id
              AND replacement.id != OLD.id
              AND replacement.version > OLD.version
        )
    )) OR
    (NEW.retirement_reason = 'invalid_content' AND
        NEW.replacement_question_version_id IS NOT NULL)
)
BEGIN
    SELECT RAISE(ABORT, 'invalid question retirement metadata');
END;

CREATE TRIGGER prevent_retirement_metadata_on_active_version
BEFORE UPDATE ON question_versions
WHEN NEW.status != 'retired' AND (
    NEW.retirement_reason IS NOT NULL OR
    NEW.retired_at IS NOT NULL OR
    NEW.replacement_question_version_id IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'retirement metadata requires retired status');
END;

CREATE TRIGGER validate_retired_metadata_transition
BEFORE UPDATE ON question_versions
WHEN OLD.status = 'retired' AND NOT (
    NEW.status = 'retired' AND
    NEW.retired_at IS OLD.retired_at AND (
        (
            NEW.retirement_reason IS OLD.retirement_reason AND
            NEW.replacement_question_version_id IS OLD.replacement_question_version_id
        ) OR
        (
            OLD.retirement_reason IS NULL AND
            NEW.retirement_reason = 'invalid_content' AND
            NEW.replacement_question_version_id IS OLD.replacement_question_version_id
        ) OR
        (
            OLD.retirement_reason IS NULL AND
            NEW.retirement_reason = 'superseded' AND
            NEW.replacement_question_version_id IS NOT NULL AND
            EXISTS (
                SELECT 1 FROM question_versions AS replacement
                WHERE replacement.id = NEW.replacement_question_version_id
                  AND replacement.question_id = OLD.question_id
                  AND replacement.id != OLD.id
                  AND replacement.version > OLD.version
                  AND (
                      replacement.status = 'published' OR
                      (
                          replacement.status = 'retired' AND
                          replacement.retirement_reason = 'superseded'
                      )
                  )
            )
        ) OR
        (
            OLD.retirement_reason = 'superseded' AND
            NEW.retirement_reason = 'invalid_content' AND
            NEW.replacement_question_version_id IS OLD.replacement_question_version_id
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid retirement transition');
END;
"""


_CATALOG_V5 = """
CREATE TABLE catalog_v5_preflight (
    valid INTEGER NOT NULL,
    CONSTRAINT valid_catalog_v5_exam_ids CHECK (valid = 1)
);

INSERT INTO catalog_v5_preflight (valid)
SELECT 0 FROM exam_definitions
WHERE tjm_valid_exam_id(id) != 1
LIMIT 1;

DROP TABLE catalog_v5_preflight;

ALTER TABLE exam_definitions ADD COLUMN official_passing_score INTEGER CHECK (
    official_passing_score IS NULL OR (
        typeof(official_passing_score) = 'integer' AND official_passing_score >= 0
    )
);
ALTER TABLE exam_definitions ADD COLUMN official_passing_score_source_json TEXT;

CREATE TRIGGER validate_exam_id_insert
BEFORE INSERT ON exam_definitions
WHEN tjm_valid_exam_id(NEW.id) != 1
BEGIN
    SELECT RAISE(ABORT, 'exam id must be one URL-safe ASCII path segment');
END;

CREATE TRIGGER prevent_exam_id_update
BEFORE UPDATE OF id ON exam_definitions
WHEN NEW.id IS NOT OLD.id
BEGIN
    SELECT RAISE(ABORT, 'exam id is immutable');
END;

CREATE TRIGGER validate_official_passing_score_insert
BEFORE INSERT ON exam_definitions
WHEN
    (NEW.official_passing_score IS NULL) !=
        (NEW.official_passing_score_source_json IS NULL) OR
    NEW.official_passing_score > NEW.question_count OR
    CASE
        WHEN NEW.official_passing_score_source_json IS NULL THEN 0
        WHEN NOT json_valid(NEW.official_passing_score_source_json) THEN 1
        WHEN json_type(NEW.official_passing_score_source_json) != 'object' THEN 1
        WHEN tjm_valid_official_source_json(
            NEW.official_passing_score_source_json
        ) != 1 THEN 1
        WHEN json_type(NEW.official_passing_score_source_json, '$.title') != 'text' THEN 1
        WHEN length(trim(json_extract(
            NEW.official_passing_score_source_json, '$.title'
        ))) = 0 THEN 1
        WHEN json_type(NEW.official_passing_score_source_json, '$.publisher') != 'text' THEN 1
        WHEN length(trim(json_extract(
            NEW.official_passing_score_source_json, '$.publisher'
        ))) = 0 THEN 1
        WHEN json_type(NEW.official_passing_score_source_json, '$.url') IS NOT NULL
             AND json_type(NEW.official_passing_score_source_json, '$.url') != 'text'
            THEN 1
        WHEN json_type(NEW.official_passing_score_source_json, '$.url') = 'text'
             AND (
                 length(trim(json_extract(
                     NEW.official_passing_score_source_json, '$.url'
                 ))) = 0 OR (
                     lower(trim(json_extract(
                         NEW.official_passing_score_source_json, '$.url'
                     ))) NOT LIKE 'http://%' AND
                     lower(trim(json_extract(
                         NEW.official_passing_score_source_json, '$.url'
                     ))) NOT LIKE 'https://%'
                 ) OR lower(trim(json_extract(
                     NEW.official_passing_score_source_json, '$.url'
                 ))) IN ('http://', 'https://') OR (
                     lower(trim(json_extract(
                         NEW.official_passing_score_source_json, '$.url'
                     ))) LIKE 'http://%' AND substr(trim(json_extract(
                         NEW.official_passing_score_source_json, '$.url'
                     )), 8, 1) IN ('/', '?', '#', '@')
                 ) OR (
                     lower(trim(json_extract(
                         NEW.official_passing_score_source_json, '$.url'
                     ))) LIKE 'https://%' AND substr(trim(json_extract(
                         NEW.official_passing_score_source_json, '$.url'
                     )), 9, 1) IN ('/', '?', '#', '@')
                 ) OR instr(json_extract(
                     NEW.official_passing_score_source_json, '$.url'
                 ), ' ') > 0
             ) THEN 1
        WHEN json_type(
            NEW.official_passing_score_source_json, '$.published_at'
        ) IS NOT NULL AND json_type(
            NEW.official_passing_score_source_json, '$.published_at'
        ) != 'text' THEN 1
        WHEN json_type(
            NEW.official_passing_score_source_json, '$.published_at'
        ) = 'text' AND length(trim(json_extract(
            NEW.official_passing_score_source_json, '$.published_at'
        ))) = 0 THEN 1
        WHEN EXISTS (
            SELECT 1 FROM json_each(NEW.official_passing_score_source_json)
            WHERE key NOT IN ('title', 'publisher', 'url', 'published_at')
        ) THEN 1
        ELSE 0
    END
BEGIN
    SELECT RAISE(ABORT, 'invalid official passing score');
END;

CREATE TRIGGER prevent_new_legacy_pass_score
BEFORE INSERT ON exam_definitions
WHEN NEW.pass_score IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'legacy pass_score cannot be set');
END;

CREATE TRIGGER prevent_legacy_pass_score_update
BEFORE UPDATE OF pass_score ON exam_definitions
WHEN NEW.pass_score IS NOT OLD.pass_score
BEGIN
    SELECT RAISE(ABORT, 'legacy pass_score is immutable');
END;

CREATE TRIGGER validate_official_passing_score_update
BEFORE UPDATE OF official_passing_score, official_passing_score_source_json,
                 question_count ON exam_definitions
WHEN
    (NEW.official_passing_score IS NULL) !=
        (NEW.official_passing_score_source_json IS NULL) OR
    NEW.official_passing_score > NEW.question_count OR
    CASE
        WHEN NEW.official_passing_score_source_json IS NULL THEN 0
        WHEN NOT json_valid(NEW.official_passing_score_source_json) THEN 1
        WHEN json_type(NEW.official_passing_score_source_json) != 'object' THEN 1
        WHEN tjm_valid_official_source_json(
            NEW.official_passing_score_source_json
        ) != 1 THEN 1
        WHEN json_type(NEW.official_passing_score_source_json, '$.title') != 'text' THEN 1
        WHEN length(trim(json_extract(
            NEW.official_passing_score_source_json, '$.title'
        ))) = 0 THEN 1
        WHEN json_type(NEW.official_passing_score_source_json, '$.publisher') != 'text' THEN 1
        WHEN length(trim(json_extract(
            NEW.official_passing_score_source_json, '$.publisher'
        ))) = 0 THEN 1
        WHEN json_type(NEW.official_passing_score_source_json, '$.url') IS NOT NULL
             AND json_type(NEW.official_passing_score_source_json, '$.url') != 'text'
            THEN 1
        WHEN json_type(NEW.official_passing_score_source_json, '$.url') = 'text'
             AND (
                 length(trim(json_extract(
                     NEW.official_passing_score_source_json, '$.url'
                 ))) = 0 OR (
                     lower(trim(json_extract(
                         NEW.official_passing_score_source_json, '$.url'
                     ))) NOT LIKE 'http://%' AND
                     lower(trim(json_extract(
                         NEW.official_passing_score_source_json, '$.url'
                     ))) NOT LIKE 'https://%'
                 ) OR lower(trim(json_extract(
                     NEW.official_passing_score_source_json, '$.url'
                 ))) IN ('http://', 'https://') OR (
                     lower(trim(json_extract(
                         NEW.official_passing_score_source_json, '$.url'
                     ))) LIKE 'http://%' AND substr(trim(json_extract(
                         NEW.official_passing_score_source_json, '$.url'
                     )), 8, 1) IN ('/', '?', '#', '@')
                 ) OR (
                     lower(trim(json_extract(
                         NEW.official_passing_score_source_json, '$.url'
                     ))) LIKE 'https://%' AND substr(trim(json_extract(
                         NEW.official_passing_score_source_json, '$.url'
                     )), 9, 1) IN ('/', '?', '#', '@')
                 ) OR instr(json_extract(
                     NEW.official_passing_score_source_json, '$.url'
                 ), ' ') > 0
             ) THEN 1
        WHEN json_type(
            NEW.official_passing_score_source_json, '$.published_at'
        ) IS NOT NULL AND json_type(
            NEW.official_passing_score_source_json, '$.published_at'
        ) != 'text' THEN 1
        WHEN json_type(
            NEW.official_passing_score_source_json, '$.published_at'
        ) = 'text' AND length(trim(json_extract(
            NEW.official_passing_score_source_json, '$.published_at'
        ))) = 0 THEN 1
        WHEN EXISTS (
            SELECT 1 FROM json_each(NEW.official_passing_score_source_json)
            WHERE key NOT IN ('title', 'publisher', 'url', 'published_at')
        ) THEN 1
        ELSE 0
    END
BEGIN
    SELECT RAISE(ABORT, 'invalid official passing score');
END;

CREATE TRIGGER prevent_retired_official_passing_score_update
BEFORE UPDATE OF official_passing_score, official_passing_score_source_json
ON exam_definitions
WHEN (
    NEW.official_passing_score IS NOT OLD.official_passing_score OR
    NEW.official_passing_score_source_json IS NOT
        OLD.official_passing_score_source_json
) AND (OLD.status = 'retired' OR NEW.status = 'retired')
BEGIN
    SELECT RAISE(ABORT, 'retired exam official passing score is immutable');
END;

CREATE TRIGGER require_official_passing_score_revision
BEFORE UPDATE OF official_passing_score, official_passing_score_source_json
ON exam_definitions
WHEN (
    NEW.official_passing_score IS NOT OLD.official_passing_score OR
    NEW.official_passing_score_source_json IS NOT
        OLD.official_passing_score_source_json
) AND NEW.revision != OLD.revision + 1
BEGIN
    SELECT RAISE(ABORT, 'official passing score update requires a new revision');
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


_LEARNING_V3 = """
ALTER TABLE attempt_items ADD COLUMN catalog_disposition TEXT NOT NULL
    DEFAULT 'unchecked' CHECK (
        catalog_disposition IN (
            'unchecked', 'current', 'superseded',
            'invalid_content', 'retired_unclassified'
        )
    );
ALTER TABLE attempt_items ADD COLUMN content_invalidated_at TEXT;

ALTER TABLE review_queue ADD COLUMN resolution_reason TEXT;
ALTER TABLE review_queue ADD COLUMN resolution_attempt_id TEXT
    REFERENCES attempts(id);

CREATE TABLE review_attempt_queue_links (
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    queue_row_id INTEGER NOT NULL REFERENCES review_queue(id),
    linked_at TEXT NOT NULL,
    PRIMARY KEY (attempt_id, queue_row_id)
);

CREATE INDEX idx_review_attempt_queue_links_queue
    ON review_attempt_queue_links(queue_row_id);

CREATE TRIGGER validate_review_attempt_queue_link
BEFORE INSERT ON review_attempt_queue_links
WHEN NOT EXISTS (
    SELECT 1
    FROM attempts AS attempt
    JOIN attempt_items AS item ON item.attempt_id = attempt.id
    JOIN review_queue AS queue ON queue.id = NEW.queue_row_id
    WHERE attempt.id = NEW.attempt_id
      AND attempt.mode = 'review'
      AND attempt.status = 'in_progress'
      AND item.question_version_id = queue.question_version_id
)
BEGIN
    SELECT RAISE(ABORT, 'review queue link must match an active review item');
END;

CREATE TRIGGER prevent_review_attempt_queue_link_update
BEFORE UPDATE ON review_attempt_queue_links
BEGIN
    SELECT RAISE(ABORT, 'review attempt queue link is immutable');
END;

CREATE TRIGGER prevent_review_attempt_queue_link_delete
BEFORE DELETE ON review_attempt_queue_links
BEGIN
    SELECT RAISE(ABORT, 'review attempt queue link is immutable');
END;

CREATE TRIGGER prevent_review_queue_delete
BEFORE DELETE ON review_queue
BEGIN
    SELECT RAISE(ABORT, 'review queue history is immutable');
END;

CREATE TRIGGER prevent_review_queue_identity_update
BEFORE UPDATE ON review_queue
WHEN
    NEW.id IS NOT OLD.id OR
    NEW.question_version_id IS NOT OLD.question_version_id OR
    NEW.reason IS NOT OLD.reason OR
    NEW.priority IS NOT OLD.priority OR
    NEW.due_at IS NOT OLD.due_at OR
    NEW.source_attempt_id IS NOT OLD.source_attempt_id OR
    NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'review queue identity is immutable');
END;

CREATE TRIGGER validate_review_queue_insert
BEFORE INSERT ON review_queue
WHEN NOT (
    NEW.status = 'pending' AND
    NEW.resolved_at IS NULL AND
    NEW.resolution_reason IS NULL AND
    NEW.resolution_attempt_id IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'new review queue row must be pending');
END;

CREATE TRIGGER validate_review_queue_resolution
BEFORE UPDATE ON review_queue
WHEN NOT (
    (
        OLD.status = 'pending' AND NEW.status = 'pending' AND
        NEW.resolved_at IS NULL AND NEW.resolution_reason IS NULL AND
        NEW.resolution_attempt_id IS NULL
    ) OR
    (
        OLD.status = 'pending' AND NEW.status = 'completed' AND
        NEW.resolved_at IS NOT NULL AND NEW.resolution_reason IS NOT NULL AND
        NEW.resolution_attempt_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM review_attempt_queue_links AS link
            WHERE link.queue_row_id = OLD.id
              AND link.attempt_id = NEW.resolution_attempt_id
        )
    ) OR
    (
        OLD.status = 'pending' AND NEW.status = 'dismissed' AND
        NEW.resolved_at IS NOT NULL AND NEW.resolution_reason IS NOT NULL AND
        NEW.resolution_attempt_id IS NULL
    ) OR
    (
        OLD.status IN ('completed', 'dismissed') AND
        NEW.status = OLD.status AND
        NEW.resolved_at IS OLD.resolved_at AND
        NEW.resolution_reason IS OLD.resolution_reason AND
        NEW.resolution_attempt_id IS OLD.resolution_attempt_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid review queue resolution transition');
END;


CREATE TRIGGER validate_attempt_item_catalog_disposition_insert
BEFORE INSERT ON attempt_items
WHEN NOT (
    (
        NEW.catalog_disposition IN ('unchecked', 'current', 'superseded') AND
        NEW.content_invalidated_at IS NULL
    ) OR
    (
        NEW.catalog_disposition IN ('invalid_content', 'retired_unclassified') AND
        NEW.content_invalidated_at IS NOT NULL
    )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid initial catalog disposition');
END;

CREATE TRIGGER validate_attempt_item_catalog_disposition
BEFORE UPDATE OF catalog_disposition, content_invalidated_at ON attempt_items
WHEN NOT (
    (
        NEW.catalog_disposition = OLD.catalog_disposition AND
        NEW.content_invalidated_at IS OLD.content_invalidated_at
    ) OR
    (
        OLD.catalog_disposition = 'unchecked' AND
        NEW.catalog_disposition IN ('current', 'superseded') AND
        NEW.content_invalidated_at IS NULL
    ) OR
    (
        OLD.catalog_disposition IN ('unchecked', 'current', 'superseded',
                                    'retired_unclassified') AND
        NEW.catalog_disposition = 'invalid_content' AND
        NEW.content_invalidated_at IS NOT NULL
    ) OR
    (
        OLD.catalog_disposition IN ('unchecked', 'current') AND
        NEW.catalog_disposition = 'superseded' AND
        NEW.content_invalidated_at IS NULL
    ) OR
    (
        OLD.catalog_disposition IN ('unchecked', 'current') AND
        NEW.catalog_disposition = 'retired_unclassified' AND
        NEW.content_invalidated_at IS NOT NULL
    ) OR
    (
        OLD.catalog_disposition = 'retired_unclassified' AND
        NEW.catalog_disposition = 'superseded' AND
        NEW.content_invalidated_at IS NULL
    )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid catalog disposition transition');
END;
"""


_LEARNING_V4 = """
CREATE TRIGGER prevent_attempt_start_while_exam_active
BEFORE INSERT ON attempts
WHEN NEW.status = 'in_progress' AND EXISTS (
    SELECT 1 FROM attempts AS active_exam
    WHERE active_exam.exam_id = NEW.exam_id
      AND active_exam.mode = 'exam'
      AND active_exam.status = 'in_progress'
)
BEGIN
    SELECT RAISE(ABORT, 'an exam attempt is already in progress for this exam');
END;

CREATE TRIGGER prevent_attempt_reactivation_while_exam_active
BEFORE UPDATE OF exam_id, mode, status ON attempts
WHEN NEW.status = 'in_progress' AND EXISTS (
    SELECT 1 FROM attempts AS active_exam
    WHERE active_exam.exam_id = NEW.exam_id
      AND active_exam.mode = 'exam'
      AND active_exam.status = 'in_progress'
      AND active_exam.id != NEW.id
)
BEGIN
    SELECT RAISE(ABORT, 'an exam attempt is already in progress for this exam');
END;
"""


_LEARNING_V5 = """
CREATE TABLE learning_v5_preflight (
    valid INTEGER NOT NULL,
    CONSTRAINT valid_learning_v5_history CHECK (valid = 1)
);

INSERT INTO learning_v5_preflight (valid)
SELECT 0 FROM attempts
WHERE
    tjm_valid_attempt_record(
        exam_snapshot_json, exam_id, mode, status, started_at, deadline_at,
        submitted_at, 1
    ) != 1 OR
    (
        status = 'in_progress' AND (
            submitted_at IS NOT NULL OR
            correct_count IS NOT NULL OR
            total_count IS NOT NULL
        )
    ) OR (
        status IN ('submitted', 'expired') AND (
            typeof(submitted_at) != 'text' OR
            length(trim(submitted_at)) = 0 OR
            typeof(correct_count) != 'integer' OR
            typeof(total_count) != 'integer' OR
            correct_count < 0 OR total_count < 0 OR
            correct_count > total_count
        )
    ) OR CASE
        WHEN status IN ('submitted', 'expired') AND
             tjm_valid_attempt_record(
                 exam_snapshot_json, exam_id, mode, status, started_at,
                 deadline_at, submitted_at, 1
             ) = 1
        THEN (
            (
                SELECT COUNT(*) FROM attempt_items
                WHERE attempt_id = attempts.id
            ) != CASE
                WHEN json_extract(
                    exam_snapshot_json, '$.snapshot_schema_version'
                ) = 2 THEN json_extract(
                    exam_snapshot_json, '$.maximum_score'
                )
                ELSE json_extract(exam_snapshot_json, '$.question_count')
            END OR
            total_count > (
                SELECT COUNT(*) FROM attempt_items
                WHERE attempt_id = attempts.id
            ) OR (
                NOT EXISTS (
                    SELECT 1 FROM attempt_items
                    WHERE attempt_id = attempts.id
                      AND catalog_disposition = 'unchecked'
                ) AND total_count != (
                    SELECT COUNT(*) FROM attempt_items
                    WHERE attempt_id = attempts.id
                      AND catalog_disposition IN ('current', 'superseded')
                )
            )
        )
        ELSE 0
    END
LIMIT 1;

DROP TABLE learning_v5_preflight;

CREATE TABLE exam_preferences (
    exam_id TEXT PRIMARY KEY CHECK (
        length(trim(exam_id)) > 0 AND tjm_valid_exam_id(exam_id) = 1
    ),
    practice_target_score INTEGER CHECK (
        practice_target_score IS NULL OR (
            typeof(practice_target_score) = 'integer' AND practice_target_score >= 0
        )
    ),
    origin TEXT NOT NULL CHECK (origin IN ('user', 'legacy_pass_score')),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    CHECK (origin != 'legacy_pass_score' OR practice_target_score IS NOT NULL)
);

CREATE TRIGGER prevent_exam_preference_identity_update
BEFORE UPDATE ON exam_preferences
WHEN NEW.exam_id IS NOT OLD.exam_id OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'exam preference identity is immutable');
END;

CREATE TRIGGER prevent_exam_preference_delete
BEFORE DELETE ON exam_preferences
BEGIN
    SELECT RAISE(ABORT, 'exam preference cannot be deleted; clear the target explicitly');
END;

CREATE TRIGGER prevent_attempt_snapshot_update
BEFORE UPDATE OF exam_snapshot_json ON attempts
WHEN NEW.exam_snapshot_json IS NOT OLD.exam_snapshot_json
BEGIN
    SELECT RAISE(ABORT, 'attempt snapshot is immutable');
END;

CREATE TRIGGER prevent_finalized_attempt_score_update
BEFORE UPDATE OF correct_count, total_count ON attempts
WHEN OLD.status IN ('submitted', 'expired') AND (
    NEW.correct_count IS NOT OLD.correct_count OR
    NEW.total_count IS NOT OLD.total_count
)
BEGIN
    SELECT RAISE(ABORT, 'attempt final score is immutable');
END;

CREATE TRIGGER prevent_finalized_attempt_reactivation
BEFORE UPDATE OF status ON attempts
WHEN OLD.status IN ('submitted', 'expired') AND NEW.status IS NOT OLD.status
BEGIN
    SELECT RAISE(ABORT, 'finalized attempt is immutable');
END;

CREATE TRIGGER validate_attempt_state_insert
BEFORE INSERT ON attempts
WHEN NEW.status != 'in_progress' OR
    NEW.submitted_at IS NOT NULL OR
    NEW.correct_count IS NOT NULL OR
    NEW.total_count IS NOT NULL OR
    tjm_valid_attempt_record(
        NEW.exam_snapshot_json, NEW.exam_id, NEW.mode, NEW.status,
        NEW.started_at, NEW.deadline_at, NEW.submitted_at, 0
    ) != 1
BEGIN
    SELECT RAISE(ABORT, 'new attempt must be a valid in-progress attempt');
END;

CREATE TRIGGER validate_attempt_state_update
BEFORE UPDATE ON attempts
WHEN OLD.status = 'in_progress' AND (
    (
        NEW.status = 'in_progress' AND (
            NEW.submitted_at IS NOT NULL OR
            NEW.correct_count IS NOT NULL OR
            NEW.total_count IS NOT NULL
        )
    ) OR (
        NEW.status IN ('submitted', 'expired') AND (
            tjm_valid_attempt_record(
                NEW.exam_snapshot_json, NEW.exam_id, NEW.mode, NEW.status,
                NEW.started_at, NEW.deadline_at, NEW.submitted_at, 1
            ) != 1 OR
            typeof(NEW.submitted_at) != 'text' OR
            length(trim(NEW.submitted_at)) = 0 OR
            typeof(NEW.correct_count) != 'integer' OR
            typeof(NEW.total_count) != 'integer' OR
            NEW.correct_count < 0 OR NEW.total_count < 0 OR
            NEW.correct_count > NEW.total_count OR
            NOT EXISTS (
                SELECT 1 FROM attempt_items
                WHERE attempt_id = NEW.id
            ) OR
            (
                SELECT COUNT(*) FROM attempt_items
                WHERE attempt_id = NEW.id
            ) != CASE
                WHEN json_extract(
                    NEW.exam_snapshot_json, '$.snapshot_schema_version'
                ) = 2 THEN json_extract(
                    NEW.exam_snapshot_json, '$.maximum_score'
                )
                ELSE json_extract(NEW.exam_snapshot_json, '$.question_count')
            END OR
            NEW.total_count != (
                SELECT COUNT(*) FROM attempt_items
                WHERE attempt_id = NEW.id
                  AND catalog_disposition IN ('current', 'superseded')
            )
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid finalized attempt score');
END;

CREATE TRIGGER prevent_in_progress_attempt_identity_update
BEFORE UPDATE ON attempts
WHEN OLD.status = 'in_progress' AND (
    NEW.id IS NOT OLD.id OR
    NEW.exam_id IS NOT OLD.exam_id OR
    NEW.mode IS NOT OLD.mode OR
    NEW.started_at IS NOT OLD.started_at OR
    NEW.deadline_at IS NOT OLD.deadline_at
)
BEGIN
    SELECT RAISE(ABORT, 'attempt identity and schedule are immutable');
END;

CREATE TRIGGER prevent_finalized_attempt_identity_update
BEFORE UPDATE ON attempts
WHEN OLD.status IN ('submitted', 'expired') AND (
    NEW.id IS NOT OLD.id OR
    NEW.exam_id IS NOT OLD.exam_id OR
    NEW.mode IS NOT OLD.mode OR
    NEW.started_at IS NOT OLD.started_at OR
    NEW.deadline_at IS NOT OLD.deadline_at OR
    NEW.submitted_at IS NOT OLD.submitted_at
)
BEGIN
    SELECT RAISE(ABORT, 'finalized attempt is immutable');
END;

CREATE TRIGGER prevent_attempt_delete
BEFORE DELETE ON attempts
BEGIN
    SELECT RAISE(ABORT, 'attempt is immutable');
END;

CREATE TRIGGER prevent_duplicate_attempt_insert
BEFORE INSERT ON attempts
WHEN EXISTS (SELECT 1 FROM attempts WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'attempt identity is immutable');
END;

CREATE TRIGGER prevent_finalized_attempt_item_insert
BEFORE INSERT ON attempt_items
WHEN EXISTS (
    SELECT 1 FROM attempts
    WHERE id = NEW.attempt_id AND status IN ('submitted', 'expired')
)
BEGIN
    SELECT RAISE(ABORT, 'finalized attempt items are immutable');
END;

CREATE TRIGGER prevent_finalized_attempt_item_update
BEFORE UPDATE ON attempt_items
WHEN EXISTS (
    SELECT 1 FROM attempts
    WHERE id IN (OLD.attempt_id, NEW.attempt_id)
      AND status IN ('submitted', 'expired')
) AND (
    NEW.attempt_id IS NOT OLD.attempt_id OR
    NEW.position IS NOT OLD.position OR
    NEW.question_version_id IS NOT OLD.question_version_id OR
    NEW.area IS NOT OLD.area OR
    NEW.opened_at IS NOT OLD.opened_at OR
    NEW.answered_at IS NOT OLD.answered_at OR
    NEW.confirmed_option_key IS NOT OLD.confirmed_option_key OR
    NEW.confidence IS NOT OLD.confidence OR
    NEW.elapsed_ms IS NOT OLD.elapsed_ms OR
    NEW.hint_count IS NOT OLD.hint_count OR
    NEW.first_presented_at IS NOT OLD.first_presented_at OR
    NEW.first_answered_at IS NOT OLD.first_answered_at OR
    NEW.final_answered_at IS NOT OLD.final_answered_at OR
    NEW.server_elapsed_ms IS NOT OLD.server_elapsed_ms OR
    NEW.client_active_elapsed_ms IS NOT OLD.client_active_elapsed_ms
)
BEGIN
    SELECT RAISE(ABORT, 'finalized attempt items are immutable');
END;

CREATE TRIGGER prevent_finalized_attempt_item_delete
BEFORE DELETE ON attempt_items
WHEN EXISTS (
    SELECT 1 FROM attempts
    WHERE id = OLD.attempt_id AND status IN ('submitted', 'expired')
)
BEGIN
    SELECT RAISE(ABORT, 'finalized attempt items are immutable');
END;

CREATE TRIGGER prevent_finalized_answer_event_insert
BEFORE INSERT ON answer_events
WHEN EXISTS (
    SELECT 1 FROM attempts
    WHERE id = NEW.attempt_id AND status IN ('submitted', 'expired')
)
BEGIN
    SELECT RAISE(ABORT, 'finalized attempt answer history is immutable');
END;
"""


class CatalogStore(_SQLiteStore):
    """Authoritative deployment-wide exam and immutable question catalog."""

    migrations = (_CATALOG_V1, _CATALOG_V2, _CATALOG_V3, _CATALOG_V4, _CATALOG_V5)


class LearningStore(_SQLiteStore):
    """Request-owner-scoped attempts and append-only answer history."""

    migrations = (_LEARNING_V1, _LEARNING_V2, _LEARNING_V3, _LEARNING_V4, _LEARNING_V5)


__all__ = ["CatalogStore", "LearningStore", "UnsupportedSchemaVersion"]
