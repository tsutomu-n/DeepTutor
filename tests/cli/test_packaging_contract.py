"""Contracts shared by the source-install dependency layers."""

from __future__ import annotations

from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[2]
CRONITER = "croniter>=6.0.0,<7.0.0"
PYTHON_RANGE = ">=3.11,<3.14"


def _pyproject(relative_path: str) -> dict[str, object]:
    return tomllib.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def _requirements(relative_path: str) -> list[str]:
    return [
        line.strip()
        for line in (ROOT / relative_path).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_supported_python_range_matches_both_source_packages() -> None:
    root_project = _pyproject("pyproject.toml")["project"]
    cli_project = _pyproject("packaging/deeptutor-cli/pyproject.toml")["project"]

    assert root_project["requires-python"] == PYTHON_RANGE
    assert cli_project["requires-python"] == PYTHON_RANGE


def test_croniter_is_available_to_every_cli_source_install() -> None:
    root_project = _pyproject("pyproject.toml")["project"]
    root_dependencies = root_project["dependencies"]
    extras = root_project["optional-dependencies"]
    cli_project = _pyproject("packaging/deeptutor-cli/pyproject.toml")["project"]

    assert root_dependencies.count(CRONITER) == 1
    assert extras["cli"].count(CRONITER) == 1
    assert CRONITER not in extras["server"]
    assert cli_project["dependencies"].count(CRONITER) == 1


def test_server_requirements_inherit_cli_cron_dependency() -> None:
    cli_requirements = _requirements("requirements/cli.txt")
    server_requirements = _requirements("requirements/server.txt")

    assert cli_requirements.count(CRONITER) == 1
    assert "-r cli.txt" in server_requirements
    assert CRONITER not in server_requirements


def test_packaging_guidance_is_private_source_only() -> None:
    for relative_path in (
        "pyproject.toml",
        "requirements.txt",
        "requirements/cli.txt",
        "requirements/server.txt",
    ):
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "pip install deeptutor" not in text, relative_path
