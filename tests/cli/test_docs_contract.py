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
PRIVATE_SOURCE_DOCS = (
    ROOT / "AGENTS.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "README.md",
    ROOT / "assets" / "README" / "README_JA.md",
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


def test_agent_architecture_doc_matches_runtime_contracts() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    normalized_agents = " ".join(agents.split())

    assert "Seven user-toggleable tools" in agents
    for tool_name in (
        "brainstorm",
        "web_search",
        "paper_search",
        "reason",
        "geogebra_analysis",
        "imagegen",
        "videogen",
    ):
        assert f"`{tool_name}`" in agents
    assert "`COMING_SOON_TOOL_TYPES` is currently empty." in normalized_agents
    assert "remain mountable when enabled" in normalized_agents
    assert "mounted only when their model is configured" not in normalized_agents
    assert "deeptutor/agents/_shared/tool_composition.py" in agents
    assert "deeptutor/agents/_shared/capability_result.py" in agents
    assert "deeptutor/services/prompt/manager.py" in agents
    assert "deeptutor/app/facade.py" in agents
    assert "does not parse a project-root" in agents
    assert "Raw Compose or Podman Compose may still consume that file" in agents
    assert "data/user/settings/docker.env" in agents


def test_private_install_docs_are_source_only() -> None:
    public_wheel_commands = (
        "pip install deeptutor",
        "pip install deeptutor-cli",
        "pip install -U deeptutor",
    )

    for path in PRIVATE_SOURCE_DOCS:
        text = path.read_text(encoding="utf-8")
        assert "JustPass" in text, f"{path.relative_to(ROOT)} omits the private project name"
        for command in public_wheel_commands:
            assert command not in text, f"{path.relative_to(ROOT)} advertises {command!r}"

    install_guides = (
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / "assets" / "README" / "README_JA.md",
        ROOT / "deeptutor_cli" / "README.md",
        ROOT / "SKILL.md",
    )
    for path in install_guides:
        text = path.read_text(encoding="utf-8")
        assert "python -m pip install -e ." in text
        assert "python -m pip install -e ./packaging/deeptutor-cli" in text
    assert "Python `>=3.11,<3.14`" in (ROOT / "SKILL.md").read_text(encoding="utf-8")


def test_root_skill_and_readmes_require_manual_handover() -> None:
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_ja = (ROOT / "assets/README/README_JA.md").read_text(encoding="utf-8")

    assert "manual handover" in skill.lower()
    assert "automatic skill discovery is not assumed" in skill
    assert "a bare reference defaults to `eduhub`" in skill
    assert "do not assume automatic discovery" in readme
    assert "自動検出を想定しないでください" in readme_ja
    assert "pick up `SKILL.md` automatically" not in readme
    assert "`SKILL.md`を自動的に取得します" not in readme_ja


def test_runner_docs_describe_the_fail_closed_container_gate() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_ja = (ROOT / "assets/README/README_JA.md").read_text(encoding="utf-8")

    for contract in ("Landlock ABI", "openat2", "scripts/verify_runner_p0.py"):
        assert contract in readme
        assert contract in readme_ja
    assert "Outbound IP sockets (including TCP and UDP) are intentionally unavailable" in readme
    assert "TCP・UDPを含むoutbound IP socketは意図的に利用できません" in readme_ja
    assert "authenticated v3 endpoint" in readme
    assert "認証付きv3 endpoint" in readme_ja
    assert "data/system/sandbox-runner.token" in readme_ja
    assert "does not yet put each job in a" in readme
    assert "jobごとの独立cgroup" in readme_ja
    assert "Do not treat the runner as accepted" in readme
    assert "敵対的multi-tenant production向けに受入済みとは扱えません" in readme_ja
    assert "hardened, least-privileged" not in readme
    assert "strongest posture" not in readme
    assert "ハードニングされた最小権限" not in readme_ja
    assert "最も強固な姿勢" not in readme_ja


def test_private_solo_contribution_contract() -> None:
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    normalized_contributing = " ".join(contributing.split())
    pull_request = (ROOT / ".github/pull_request_template.md").read_text(encoding="utf-8")

    assert "`main` as the integration branch" in contributing
    assert "A feature branch is optional." in contributing
    assert "A pull request is optional." in contributing
    assert "are human decisions" in contributing
    assert "git config core.hooksPath scripts/hooks" in contributing
    assert "does not restrict direct commits to `main`" in normalized_contributing
    assert "Never regenerate or wholesale overwrite `.secrets.baseline`." in contributing
    assert "detect-secrets scan > .secrets.baseline" not in contributing
    assert "intended for `main`" in pull_request
    assert "github.com/HKUDS/DeepTutor/blob/dev" not in pull_request


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
