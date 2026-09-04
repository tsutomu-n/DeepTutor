from __future__ import annotations

import os
from pathlib import Path
import subprocess


def _run_hook(
    tmp_path: Path, *, hygiene_exit: int
) -> tuple[subprocess.CompletedProcess[str], Path]:
    repo_root = Path(__file__).resolve().parents[2]
    calls = tmp_path / "calls"
    fake_python = tmp_path / "python3"
    fake_python.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$1" >> "{calls}"\nexit {hygiene_exit}\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        ["sh", "scripts/hooks/pre-commit"],
        cwd=repo_root,
        env=env,
        check=False,
        text=True,
    )
    return result, calls


def test_hook_runs_only_repo_hygiene(tmp_path: Path) -> None:
    result, calls = _run_hook(tmp_path, hygiene_exit=0)

    assert result.returncode == 0
    assert calls.read_text(encoding="utf-8").splitlines() == ["scripts/check_repo_hygiene.py"]


def test_hook_propagates_repo_hygiene_failure(tmp_path: Path) -> None:
    result, calls = _run_hook(tmp_path, hygiene_exit=1)

    assert result.returncode != 0
    assert calls.read_text(encoding="utf-8").splitlines() == ["scripts/check_repo_hygiene.py"]
