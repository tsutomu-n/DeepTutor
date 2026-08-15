import importlib
import json
import logging
from pathlib import Path

import pytest

from deeptutor.logging import LoggingConfig, bind_log_context


@pytest.fixture(autouse=True)
def _clean_logging_handlers():
    configure_module = importlib.import_module("deeptutor.logging.configure")
    configure_module._remove_managed_handlers(logging.getLogger())
    yield
    configure_module._remove_managed_handlers(logging.getLogger())


def _flush_root_handlers() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_logging_config_follows_runtime_home_selected_after_import(
    monkeypatch, tmp_path: Path
) -> None:
    config_module = importlib.import_module("deeptutor.logging.config")
    services_config = importlib.import_module("deeptutor.services.config")
    runtime_home = tmp_path / "runtime-home"
    assert services_config.PROJECT_ROOT != runtime_home
    settings_dir = runtime_home / "data" / "user" / "settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / "main.yaml").write_text(
        "logging:\n  level: ERROR\n  console_output: false\n  save_to_file: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPTUTOR_HOME", str(runtime_home))

    loaded = config_module.load_logging_config()

    assert loaded.level == "ERROR"
    assert loaded.console_output is False
    assert loaded.file_output is False


def test_configure_logging_writes_jsonl_and_respects_level(monkeypatch, tmp_path: Path):
    configure_module = importlib.import_module("deeptutor.logging.configure")
    monkeypatch.setattr(
        configure_module,
        "load_logging_config",
        lambda: LoggingConfig(
            level="WARNING",
            console_output=False,
            file_output=True,
            log_dir=str(tmp_path),
            max_bytes=1024 * 1024,
            backup_count=1,
        ),
    )

    configure_module.configure_logging(force=True)
    logger = logging.getLogger("deeptutor.tests.config")
    with bind_log_context(request_id="req-1", task_id="task-1"):
        logger.info("filtered")
        logger.warning("written")
    _flush_root_handlers()

    lines = (tmp_path / "deeptutor.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["level"] == "WARNING"
    assert entry["logger"] == "deeptutor.tests.config"
    assert entry["message"] == "written"
    assert entry["context"] == {"request_id": "req-1", "task_id": "task-1"}


def test_configure_logging_uses_rotation_settings(monkeypatch, tmp_path: Path):
    configure_module = importlib.import_module("deeptutor.logging.configure")
    monkeypatch.setattr(
        configure_module,
        "load_logging_config",
        lambda: LoggingConfig(
            level="INFO",
            console_output=False,
            file_output=True,
            log_dir=str(tmp_path),
            max_bytes=220,
            backup_count=1,
        ),
    )

    configure_module.configure_logging(force=True)
    logger = logging.getLogger("deeptutor.tests.rotation")
    for index in range(20):
        logger.info("rotation line %02d %s", index, "x" * 40)
    _flush_root_handlers()

    assert (tmp_path / "deeptutor.jsonl").exists()
    assert (tmp_path / "deeptutor.jsonl.1").exists()
