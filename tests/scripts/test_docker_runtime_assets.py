"""Contracts for Markdown assets copied into the production image."""

from __future__ import annotations

from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_MARKDOWN_ASSETS = (
    Path("deeptutor/skills/builtin/pdf/SKILL.md"),
    Path("deeptutor/services/persona/presets/teacher/PERSONA.md"),
    Path("deeptutor/agents/vision_solver/prompts/geogebra.md"),
)


def test_docker_context_reincludes_package_markdown() -> None:
    patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    markdown_ignore = patterns.index("*.md")
    assert patterns[markdown_ignore + 1] == "!deeptutor/**/*.md"


def test_reincluded_markdown_is_declared_as_package_data() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]["deeptutor"]

    assert "**/*.md" in package_data
    for relative_path in RUNTIME_MARKDOWN_ASSETS:
        assert (ROOT / relative_path).is_file(), relative_path
