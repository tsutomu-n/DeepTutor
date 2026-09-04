"""Contracts for the repository-wide CI secret gate."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_secret_scan_has_no_workflow_path_bypass() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )

    triggers = workflow["on"]
    assert "paths" not in triggers["push"]
    assert "paths" not in triggers["pull_request"]


def test_secret_scan_is_pinned_and_gates_the_summary() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    jobs = workflow["jobs"]
    secret_steps = jobs["secret-scan"]["steps"]

    assert any(
        step.get("run") == "python -m pip install pre-commit==4.6.1" for step in secret_steps
    )
    assert any(
        step.get("run") == "pre-commit run detect-secrets --all-files" for step in secret_steps
    )
    assert "secret-scan" in jobs["test-summary"]["needs"]
    fail_step = next(
        step for step in jobs["test-summary"]["steps"] if step.get("name") == "Fail if tests failed"
    )
    assert "needs.secret-scan.result != 'success'" in fail_step["if"]


def test_detect_secrets_receives_the_all_files_filename_list() -> None:
    config = yaml.load(
        (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    detect_secrets = next(
        hook
        for repository in config["repos"]
        for hook in repository["hooks"]
        if hook["id"] == "detect-secrets"
    )

    assert detect_secrets["args"] == ["--baseline", ".secrets.baseline"]
    assert "pass_filenames" not in detect_secrets
