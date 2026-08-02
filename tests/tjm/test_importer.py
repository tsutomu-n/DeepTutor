from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from deeptutor.tjm.catalog import CatalogService
from deeptutor.tjm.domain import ExamSpec
from deeptutor.tjm.importer import ImportService
from deeptutor.tjm.storage import CatalogStore


def _services(tmp_path: Path) -> tuple[CatalogService, ImportService]:
    catalog = CatalogService(CatalogStore(tmp_path / "catalog.db"))
    catalog.create_exam(
        ExamSpec(
            id="exam-import",
            title="Import Exam",
            duration_seconds=900,
            question_count=2,
            blueprint={"area-a": 1, "area-b": 1},
        ),
        actor_id="admin-1",
    )
    return catalog, ImportService(catalog)


def _record(stable_id: str, *, area: str = "area-a", correct: str = "B") -> dict:
    return {
        "exam_id": "exam-import",
        "stable_id": stable_id,
        "stem": f"Question {stable_id}",
        "options": [
            {"key": "A", "text": "First"},
            {"key": "B", "text": "Second"},
        ],
        "correct_option_key": correct,
        "area": area,
        "explanation": "Reviewed later.",
        "hints": ["Look closely."],
        "source": {"license": "test-fixture"},
    }


def _counts(catalog: CatalogService) -> tuple[int, int]:
    with catalog.store.connect() as conn:
        versions = conn.execute("SELECT COUNT(*) FROM question_versions").fetchone()[0]
        batches = conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0]
    return int(versions), int(batches)


@pytest.mark.parametrize("import_format", ["json", "jsonl", "csv"])
def test_supported_formats_create_drafts_and_a_completed_batch(
    tmp_path: Path, import_format: str
) -> None:
    catalog, importer = _services(tmp_path)
    records = [_record("q-001", area="area-a"), _record("q-002", area="area-b")]
    if import_format == "json":
        payload = json.dumps(records, ensure_ascii=False).encode()
    elif import_format == "jsonl":
        payload = ("\n".join(json.dumps(item) for item in records) + "\n").encode()
    else:
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "exam_id",
                "stable_id",
                "stem",
                "options_json",
                "correct_option_key",
                "area",
                "explanation",
                "hints_json",
                "source_json",
            ],
        )
        writer.writeheader()
        for item in records:
            writer.writerow(
                {
                    "exam_id": item["exam_id"],
                    "stable_id": item["stable_id"],
                    "stem": item["stem"],
                    "options_json": json.dumps(item["options"]),
                    "correct_option_key": item["correct_option_key"],
                    "area": item["area"],
                    "explanation": item["explanation"],
                    "hints_json": json.dumps(item["hints"]),
                    "source_json": json.dumps(item["source"]),
                }
            )
        payload = output.getvalue().encode()

    result = importer.import_bytes(
        payload,
        import_format=import_format,
        source_name=f"questions.{import_format}",
        actor_id="admin-1",
    )

    assert result.status == "completed"
    assert result.total_rows == 2
    assert result.imported_rows == 2
    assert result.duplicate_rows == 0
    assert result.errors == ()
    with catalog.store.connect() as conn:
        statuses = {
            row[0] for row in conn.execute("SELECT status FROM question_versions").fetchall()
        }
    assert statuses == {"draft"}


def test_one_invalid_row_rolls_back_every_question_but_records_failed_batch(tmp_path: Path) -> None:
    catalog, importer = _services(tmp_path)
    invalid = _record("q-002", area="area-b", correct="missing")
    payload = json.dumps([_record("q-001"), invalid]).encode()

    result = importer.import_bytes(
        payload, import_format="json", source_name="mixed.json", actor_id="admin-1"
    )

    assert result.status == "failed"
    assert result.imported_rows == 0
    assert result.errors[0].row == 2
    assert result.errors[0].field == "correct_option_key"
    assert _counts(catalog) == (0, 1)


def test_duplicate_stable_ids_in_one_batch_fail_closed(tmp_path: Path) -> None:
    catalog, importer = _services(tmp_path)
    payload = json.dumps([_record("q-001"), _record("q-001")]).encode()

    result = importer.import_bytes(
        payload, import_format="json", source_name="duplicate.json", actor_id="admin-1"
    )

    assert result.status == "failed"
    assert result.errors[0].row == 2
    assert result.errors[0].field == "stable_id"
    assert _counts(catalog) == (0, 1)


def test_invalid_utf8_is_a_failed_audited_batch(tmp_path: Path) -> None:
    catalog, importer = _services(tmp_path)

    result = importer.import_bytes(
        b"\xff\xfe", import_format="jsonl", source_name="broken.jsonl", actor_id="admin-1"
    )

    assert result.status == "failed"
    assert result.total_rows == 0
    assert result.errors[0].field == "encoding"
    assert _counts(catalog) == (0, 1)


def test_reimporting_identical_content_is_counted_without_new_version(tmp_path: Path) -> None:
    catalog, importer = _services(tmp_path)
    payload = json.dumps([_record("q-001")]).encode()
    first = importer.import_bytes(
        payload, import_format="json", source_name="first.json", actor_id="admin-1"
    )
    second = importer.import_bytes(
        payload, import_format="json", source_name="second.json", actor_id="admin-1"
    )

    assert first.imported_rows == 1
    assert second.status == "completed"
    assert second.imported_rows == 0
    assert second.duplicate_rows == 1
    assert _counts(catalog) == (1, 2)


@pytest.mark.parametrize(
    ("payload", "import_format", "field"),
    [
        (b"{}", "json", "document"),
        (b"not-json\n", "jsonl", "document"),
        (b"exam_id,stable_id\nexam-import,q-1\n", "csv", "options_json"),
    ],
)
def test_malformed_documents_are_failed_batches(
    tmp_path: Path, payload: bytes, import_format: str, field: str
) -> None:
    catalog, importer = _services(tmp_path)

    result = importer.import_bytes(
        payload, import_format=import_format, source_name="bad-input", actor_id="admin-1"
    )

    assert result.status == "failed"
    assert field in {issue.field for issue in result.errors}
    assert _counts(catalog) == (0, 1)
