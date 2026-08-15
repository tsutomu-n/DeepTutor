"""Contracts that keep the public docs aligned with the CLI surface."""

from __future__ import annotations

from pathlib import Path
import re
import shlex

ROOT = Path(__file__).resolve().parents[2]
DOCS_ROOT = ROOT / "site" / "src" / "content" / "docs"
PUBLIC_DOCS = (
    ROOT / "README.md",
    ROOT / "deeptutor_cli" / "README.md",
    ROOT / "SKILL.md",
)
HISTORICAL_UPSTREAM_TRANSLATIONS = {
    f"assets/README/README_{language}.md"
    for language in ("AR", "CN", "ES", "FR", "HI", "PL", "PT", "RU", "TH")
}


def _command_doc_paths() -> list[Path]:
    paths: list[Path] = []
    if DOCS_ROOT.exists():
        paths.extend(DOCS_ROOT.rglob("*.md"))
    paths.extend(path for path in PUBLIC_DOCS if path.exists())
    return paths


def _docs_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in _command_doc_paths())


def _doc_ids() -> set[str]:
    ids: set[str] = set()
    for path in DOCS_ROOT.rglob("*.md"):
        slug = path.relative_to(DOCS_ROOT).with_suffix("").as_posix()
        ids.add(f"/{slug}")
        ids.add(f"/{slug}/")
        if slug.endswith("/index"):
            base = slug[: -len("/index")]
            ids.add(f"/{base}")
            ids.add(f"/{base}/")
    return ids


def _deeptutor_commands() -> list[str]:
    commands: list[str] = []
    pending = ""
    for path in _command_doc_paths():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not pending and not stripped.startswith("deeptutor "):
                continue
            continued = stripped.endswith("\\")
            line_part = stripped[:-1].strip() if continued else stripped
            pending = f"{pending} {line_part}".strip()
            if continued:
                continue
            commands.append(pending)
            pending = ""
    return commands


def test_internal_docs_links_point_to_existing_pages() -> None:
    ids = _doc_ids()
    missing: list[tuple[str, str]] = []

    for path in DOCS_ROOT.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\[[^\]]+\]\((/docs/[^)\s#]+)(?:#[^)]+)?\)", text):
            href = match.group(1)
            if href not in ids:
                missing.append((str(path.relative_to(ROOT)), href))

    assert missing == []


def test_documented_deeptutor_subcommands_exist() -> None:
    top_level = {
        "book",
        "chat",
        "config",
        "init",
        "kb",
        "memory",
        "notebook",
        "partner",
        "plugin",
        "provider",
        "run",
        "serve",
        "session",
        "skill",
        "start",
    }
    provider_subcommands = {"login"}

    for command in _deeptutor_commands():
        first_segment = command.split("|", 1)[0].split("#", 1)[0].strip()
        if "<" in first_segment or "[" in first_segment:
            continue
        tokens = shlex.split(first_segment)
        if len(tokens) < 2:
            continue
        assert tokens[1] in top_level, command
        if tokens[1] == "provider" and len(tokens) >= 3:
            assert tokens[2] in provider_subcommands, command


def test_deep_research_examples_include_required_config() -> None:
    examples = [
        command for command in _deeptutor_commands() if "deeptutor run deep_research" in command
    ]

    assert examples, "docs should include at least one deep_research example"
    for command in examples:
        has_json_config = "--config-json" in command
        has_pair_config = "--config mode=" in command and "--config depth=" in command
        assert has_json_config or has_pair_config, command


def test_docs_do_not_advertise_removed_cli_forms() -> None:
    text = _docs_text()

    assert "deeptutor provider logout" not in text
    assert "deeptutor memory show summary" not in text
    assert "WS /api/v1/turns" not in text


def test_tjm_readme_only_links_synchronized_translations() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_ja = (ROOT / "assets/README/README_JA.md").read_text(encoding="utf-8")
    linked_translations = set(re.findall(r'href="(assets/README/README_[A-Z]+\.md)"', readme))
    linked_translations_ja = set(re.findall(r'href="(README_[A-Z]+\.md)"', readme_ja))

    assert linked_translations == {"assets/README/README_JA.md"}
    assert linked_translations.isdisjoint(HISTORICAL_UPSTREAM_TRANSLATIONS)
    assert linked_translations_ja == {"README_JA.md"}
    assert linked_translations_ja.isdisjoint(
        {Path(path).name for path in HISTORICAL_UPSTREAM_TRANSLATIONS}
    )
    assert "Only the English and Japanese guides are synchronized with the TJM fork" in readme
    assert "TJMフォークと同期しているガイドは英語版と日本語版だけです" in readme_ja


def test_containerization_links_to_root_readme() -> None:
    guide = (ROOT / "CONTAINERIZATION.md").read_text(encoding="utf-8")

    assert "[README.md](README.md)" in guide
    assert "[README.md](../README.md)" not in guide
