from __future__ import annotations

import importlib.util
from pathlib import Path
import stat
import subprocess
import sys

import pytest


def _load_module():
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "docker_compose.py"
    spec = importlib.util.spec_from_file_location("docker_compose_token_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    return settings_dir, settings_dir / "docker.env", tmp_path / "system" / "sandbox-runner.token"


def test_control_token_is_created_once_reused_and_not_returned_or_printed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    settings_dir, output_path, token_path = _paths(tmp_path)
    generated = "A" * module.MIN_RUNNER_CONTROL_TOKEN_LENGTH
    calls: list[int] = []

    def _generate(byte_count: int) -> str:
        calls.append(byte_count)
        return generated

    monkeypatch.setattr(module.secrets, "token_urlsafe", _generate)

    first = module.render_docker_env(settings_dir, output_path, token_path)
    second = module.render_docker_env(settings_dir, output_path, token_path)

    assert calls == [48]
    assert token_path.read_text(encoding="ascii") == generated
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    assert f"DEEPTUTOR_SANDBOX_RUNNER_TOKEN={generated}\n" in output_path.read_text(
        encoding="utf-8"
    )
    expected_keys = {
        "DEEPTUTOR_DOCKER_BACKEND_PORT",
        "DEEPTUTOR_DOCKER_FRONTEND_PORT",
        "DEEPTUTOR_DOCKER_POCKETBASE_PORT",
    }
    assert set(first) == expected_keys
    assert second == first
    assert generated not in repr(first)
    captured = capsys.readouterr()
    assert generated not in captured.out
    assert generated not in captured.err


def test_existing_token_parent_permissions_are_not_changed(tmp_path: Path) -> None:
    module = _load_module()
    settings_dir, output_path, token_path = _paths(tmp_path)
    token_path.parent.mkdir(mode=0o750)
    token_path.parent.chmod(0o750)
    token_path.write_text("B" * module.MIN_RUNNER_CONTROL_TOKEN_LENGTH, encoding="ascii")
    token_path.chmod(0o600)

    module.render_docker_env(settings_dir, output_path, token_path)

    assert stat.S_IMODE(token_path.parent.stat().st_mode) == 0o750


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o700])
def test_existing_token_with_unsafe_permissions_is_rejected(tmp_path: Path, mode: int) -> None:
    module = _load_module()
    settings_dir, output_path, token_path = _paths(tmp_path)
    token_path.parent.mkdir()
    token_path.write_text("C" * module.MIN_RUNNER_CONTROL_TOKEN_LENGTH, encoding="ascii")
    token_path.chmod(mode)

    with pytest.raises(RuntimeError, match="owner-only read permissions"):
        module.render_docker_env(settings_dir, output_path, token_path)

    assert not output_path.exists()


def test_existing_token_symlink_is_rejected(tmp_path: Path) -> None:
    module = _load_module()
    settings_dir, output_path, token_path = _paths(tmp_path)
    token_path.parent.mkdir()
    target = tmp_path / "real-token"
    target.write_text("D" * module.MIN_RUNNER_CONTROL_TOKEN_LENGTH, encoding="ascii")
    target.chmod(0o600)
    token_path.symlink_to(target)

    with pytest.raises(RuntimeError, match="open runner control token securely"):
        module.render_docker_env(settings_dir, output_path, token_path)

    assert not output_path.exists()


def test_existing_short_token_is_rejected(tmp_path: Path) -> None:
    module = _load_module()
    settings_dir, output_path, token_path = _paths(tmp_path)
    token_path.parent.mkdir()
    token_path.write_text("too-short", encoding="ascii")
    token_path.chmod(0o600)

    with pytest.raises(RuntimeError, match="too short"):
        module.render_docker_env(settings_dir, output_path, token_path)

    assert not output_path.exists()


def test_compose_subprocess_cannot_override_generated_token_from_caller_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    values = {
        "DEEPTUTOR_DOCKER_BACKEND_PORT": "8001",
        "DEEPTUTOR_DOCKER_FRONTEND_PORT": "3782",
        "DEEPTUTOR_DOCKER_POCKETBASE_PORT": "8090",
    }
    captured: dict[str, object] = {}

    monkeypatch.setenv(module.RUNNER_CONTROL_TOKEN_ENV, "caller-controlled-token")
    monkeypatch.setattr(module, "render_docker_env", lambda: values)
    monkeypatch.setattr(module, "_compose_command", lambda args: ["docker", *args])

    def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr(module.subprocess, "run", _run)

    assert module.main(["config"]) == 0
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert module.RUNNER_CONTROL_TOKEN_ENV not in child_env
