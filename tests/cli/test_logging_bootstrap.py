from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from deeptutor_cli import init_cmd


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions are required")
def test_importing_cli_from_read_only_cwd_has_no_logging_side_effect(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    read_only_cwd = tmp_path / "read-only"
    read_only_cwd.mkdir()
    read_only_cwd.chmod(0o555)
    env = os.environ.copy()
    env.pop("DEEPTUTOR_HOME", None)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(repository_root), env.get("PYTHONPATH", "")) if part
    )

    try:
        result = subprocess.run(
            [sys.executable, "-c", "import deeptutor_cli.main"],
            cwd=read_only_cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        read_only_cwd.chmod(0o755)

    assert result.returncode == 0, result.stderr
    assert not (read_only_cwd / "data").exists()


def test_init_configures_logging_only_after_explicit_home_is_active(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_home = tmp_path / "runtime-home"
    calls: list[bool] = []

    class LoggingConfigured(Exception):
        pass

    def fake_configure_logging(*, force: bool = False) -> None:
        assert force is True
        assert Path(os.environ["DEEPTUTOR_HOME"]) == runtime_home
        assert (runtime_home / "data" / "user").is_dir()
        calls.append(force)
        raise LoggingConfigured

    monkeypatch.setattr("deeptutor.logging.configure_logging", fake_configure_logging)

    with pytest.raises(LoggingConfigured):
        init_cmd.run_init(home=runtime_home)

    assert calls == [True]
