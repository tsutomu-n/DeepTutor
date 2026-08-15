from __future__ import annotations

from collections.abc import Mapping
import csv
from dataclasses import asdict, dataclass
import io
import json
import sqlite3
from typing import Any, Literal
import uuid

from .catalog import CatalogService, _now
from .domain import (
    Choice,
    DomainValidationError,
    DuplicateRecordError,
    InvalidTransitionError,
    QuestionVersionDraft,
)

ImportFormat = Literal["json", "jsonl", "csv"]


@dataclass(frozen=True, slots=True)
class ImportIssue:
    row: int
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class ImportResult:
    batch_id: str
    status: Literal["failed", "completed"]
    total_rows: int
    imported_rows: int
    duplicate_rows: int
    errors: tuple[ImportIssue, ...]


class _RowImportFailure(RuntimeError):
    def __init__(self, issue: ImportIssue) -> None:
        super().__init__(issue.message)
        self.issue = issue


class ImportService:
    """Strict question candidate ingestion; successful rows remain drafts."""

    def __init__(self, catalog: CatalogService) -> None:
        self.catalog = catalog

    def import_bytes(
        self,
        payload: bytes,
        *,
        import_format: str,
        source_name: str,
        actor_id: str,
    ) -> ImportResult:
        batch_id = f"imp_{uuid.uuid4().hex}"
        source = source_name.strip()
        actor = actor_id.strip()
        if not source:
            return self._failed(
                batch_id,
                import_format,
                source_name,
                actor,
                0,
                (ImportIssue(0, "source_name", "source_name is required"),),
            )
        if not actor:
            return self._failed(
                batch_id,
                import_format,
                source,
                actor_id,
                0,
                (ImportIssue(0, "actor_id", "actor_id is required"),),
            )
        if import_format not in {"json", "jsonl", "csv"}:
            return self._failed(
                batch_id,
                "json",
                source,
                actor,
                0,
                (ImportIssue(0, "format", f"unsupported import format: {import_format}"),),
            )

        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            return self._failed(
                batch_id,
                import_format,
                source,
                actor,
                0,
                (ImportIssue(0, "encoding", f"input must be valid UTF-8: {exc}"),),
            )

        drafts, issues, total_rows = self._parse(text, import_format)
        if issues:
            return self._failed(batch_id, import_format, source, actor, total_rows, tuple(issues))

        imported = 0
        duplicates = 0
        now = _now()
        try:
            with self.catalog.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO import_batches (
                        id, source_name, format, status, actor_id, total_rows,
                        imported_rows, duplicate_rows, errors_json, created_at
                    ) VALUES (?, ?, ?, 'validating', ?, ?, 0, 0, '[]', ?)
                    """,
                    (batch_id, source, import_format, actor, total_rows, now),
                )
                for row_number, draft in drafts:
                    try:
                        self.catalog._insert_question_version(conn, draft, actor=actor, now=now)
                    except DuplicateRecordError:
                        duplicates += 1
                    except (DomainValidationError, InvalidTransitionError) as exc:
                        raise _RowImportFailure(
                            ImportIssue(row_number, "document", str(exc))
                        ) from exc
                    else:
                        imported += 1
                conn.execute(
                    """
                    UPDATE import_batches SET
                        status = 'completed', imported_rows = ?, duplicate_rows = ?,
                        completed_at = ?
                    WHERE id = ?
                    """,
                    (imported, duplicates, _now(), batch_id),
                )
        except _RowImportFailure as exc:
            return self._failed(
                batch_id,
                import_format,
                source,
                actor,
                total_rows,
                (exc.issue,),
            )
        except sqlite3.IntegrityError as exc:
            return self._failed(
                batch_id,
                import_format,
                source,
                actor,
                total_rows,
                (ImportIssue(0, "document", f"database rejected import: {exc}"),),
            )

        return ImportResult(
            batch_id=batch_id,
            status="completed",
            total_rows=total_rows,
            imported_rows=imported,
            duplicate_rows=duplicates,
            errors=(),
        )

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        with self.catalog.store.connect() as conn:
            row = conn.execute("SELECT * FROM import_batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            raise DomainValidationError(f"unknown import batch: {batch_id}")
        result = dict(row)
        result["errors"] = json.loads(result.pop("errors_json"))
        return result

    def _failed(
        self,
        batch_id: str,
        import_format: str,
        source_name: str,
        actor_id: str,
        total_rows: int,
        issues: tuple[ImportIssue, ...],
    ) -> ImportResult:
        safe_format = import_format if import_format in {"json", "jsonl", "csv"} else "json"
        now = _now()
        with self.catalog.store.connect() as conn:
            conn.execute(
                """
                INSERT INTO import_batches (
                    id, source_name, format, status, actor_id, total_rows,
                    imported_rows, duplicate_rows, errors_json, created_at, completed_at
                ) VALUES (?, ?, ?, 'failed', ?, ?, 0, 0, ?, ?, ?)
                """,
                (
                    batch_id,
                    source_name.strip() or "unnamed",
                    safe_format,
                    actor_id.strip() or "unknown",
                    total_rows,
                    json.dumps([asdict(issue) for issue in issues], ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return ImportResult(
            batch_id=batch_id,
            status="failed",
            total_rows=total_rows,
            imported_rows=0,
            duplicate_rows=0,
            errors=issues,
        )

    def _parse(
        self, text: str, import_format: str
    ) -> tuple[list[tuple[int, QuestionVersionDraft]], list[ImportIssue], int]:
        if import_format == "json":
            return self._parse_json(text)
        if import_format == "jsonl":
            return self._parse_jsonl(text)
        return self._parse_csv(text)

    def _parse_json(
        self, text: str
    ) -> tuple[list[tuple[int, QuestionVersionDraft]], list[ImportIssue], int]:
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            return [], [ImportIssue(0, "document", f"invalid JSON: {exc.msg}")], 0
        if not isinstance(document, list):
            return [], [ImportIssue(0, "document", "JSON document must be an array")], 0
        if not document:
            return [], [ImportIssue(0, "document", "import contains no rows")], 0
        return self._validate_records([(index, item) for index, item in enumerate(document, 1)])

    def _parse_jsonl(
        self, text: str
    ) -> tuple[list[tuple[int, QuestionVersionDraft]], list[ImportIssue], int]:
        records: list[tuple[int, Any]] = []
        issues: list[ImportIssue] = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append((line_number, json.loads(line)))
            except json.JSONDecodeError as exc:
                issues.append(ImportIssue(line_number, "document", f"invalid JSON: {exc.msg}"))
        if issues:
            return [], issues, len(records) + len(issues)
        if not records:
            return [], [ImportIssue(0, "document", "import contains no rows")], 0
        return self._validate_records(records)

    def _parse_csv(
        self, text: str
    ) -> tuple[list[tuple[int, QuestionVersionDraft]], list[ImportIssue], int]:
        try:
            reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
            rows = list(reader)
        except csv.Error as exc:
            return [], [ImportIssue(0, "document", f"invalid CSV: {exc}")], 0
        if not reader.fieldnames:
            return [], [ImportIssue(0, "document", "CSV header is required")], 0
        if not rows:
            return [], [ImportIssue(0, "document", "import contains no rows")], 0

        records: list[tuple[int, Any]] = []
        issues: list[ImportIssue] = []
        for row_number, row in enumerate(rows, 2):
            converted, row_issues = self._convert_csv_row(row_number, row)
            if row_issues:
                issues.extend(row_issues)
            else:
                records.append((row_number, converted))
        if issues:
            return [], issues, len(rows)
        drafts, validation_issues, _ = self._validate_records(records)
        return drafts, validation_issues, len(rows)

    @staticmethod
    def _convert_csv_row(
        row_number: int, row: Mapping[str | None, str | None]
    ) -> tuple[dict[str, Any], list[ImportIssue]]:
        issues: list[ImportIssue] = []
        for field_name in (
            "exam_id",
            "stable_id",
            "stem",
            "options_json",
            "correct_option_key",
            "area",
        ):
            if not str(row.get(field_name) or "").strip():
                issues.append(ImportIssue(row_number, field_name, f"{field_name} is required"))
        if issues:
            return {}, issues
        converted: dict[str, Any] = {
            "exam_id": row["exam_id"],
            "stable_id": row["stable_id"],
            "stem": row["stem"],
            "correct_option_key": row["correct_option_key"],
            "area": row["area"],
            "explanation": row.get("explanation") or "",
        }
        for csv_name, target_name, default in (
            ("options_json", "options", None),
            ("hints_json", "hints", []),
            ("source_json", "source", {}),
        ):
            raw = row.get(csv_name)
            if not raw and default is not None:
                converted[target_name] = default
                continue
            try:
                converted[target_name] = json.loads(str(raw))
            except json.JSONDecodeError as exc:
                return {}, [ImportIssue(row_number, csv_name, f"invalid JSON: {exc.msg}")]
        return converted, []

    def _validate_records(
        self, records: list[tuple[int, Any]]
    ) -> tuple[list[tuple[int, QuestionVersionDraft]], list[ImportIssue], int]:
        drafts: list[tuple[int, QuestionVersionDraft]] = []
        issues: list[ImportIssue] = []
        seen: set[tuple[str, str]] = set()
        for row_number, record in records:
            draft, issue = self._record_to_draft(row_number, record)
            if issue is not None:
                issues.append(issue)
                continue
            assert draft is not None
            key = (draft.exam_id, draft.stable_id)
            if key in seen:
                issues.append(
                    ImportIssue(
                        row_number,
                        "stable_id",
                        "duplicate exam_id and stable_id in one import batch",
                    )
                )
                continue
            seen.add(key)
            drafts.append((row_number, draft))
        return drafts, issues, len(records)

    @staticmethod
    def _record_to_draft(
        row_number: int, record: Any
    ) -> tuple[QuestionVersionDraft | None, ImportIssue | None]:
        if not isinstance(record, Mapping):
            return None, ImportIssue(row_number, "document", "row must be an object")
        for field_name in (
            "exam_id",
            "stable_id",
            "stem",
            "options",
            "correct_option_key",
            "area",
        ):
            if field_name not in record:
                return None, ImportIssue(row_number, field_name, f"{field_name} is required")
        raw_options = record["options"]
        if not isinstance(raw_options, list):
            return None, ImportIssue(row_number, "options", "options must be an array")
        choices: list[Choice] = []
        for item in raw_options:
            if not isinstance(item, Mapping) or "key" not in item or "text" not in item:
                return None, ImportIssue(row_number, "options", "each option requires key and text")
            choices.append(Choice(key=str(item["key"]), text=str(item["text"])))
        hints = record.get("hints", [])
        source = record.get("source", {})
        if not isinstance(hints, list):
            return None, ImportIssue(row_number, "hints", "hints must be an array")
        if not isinstance(source, Mapping):
            return None, ImportIssue(row_number, "source", "source must be an object")
        try:
            draft = QuestionVersionDraft(
                exam_id=str(record["exam_id"]),
                stable_id=str(record["stable_id"]),
                stem=str(record["stem"]),
                choices=tuple(choices),
                correct_option_key=str(record["correct_option_key"]),
                area=str(record["area"]),
                explanation=str(record.get("explanation", "")),
                hints=tuple(str(item) for item in hints),
                source=dict(source),
            ).normalized()
        except DomainValidationError as exc:
            message = str(exc)
            if "correct_option_key" in message:
                field = "correct_option_key"
            elif "choice" in message:
                field = "options"
            elif "stem" in message:
                field = "stem"
            elif "area" in message:
                field = "area"
            elif "source" in message:
                field = "source"
            elif "hint" in message:
                field = "hints"
            else:
                field = "document"
            return None, ImportIssue(row_number, field, message)
        return draft, None


__all__ = ["ImportFormat", "ImportIssue", "ImportResult", "ImportService"]
