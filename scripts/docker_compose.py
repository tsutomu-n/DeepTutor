#!/usr/bin/env python
"""Run Docker Compose with port mappings rendered from JSON settings.

Docker Compose cannot read ``data/user/settings/system.json`` directly for
host port interpolation. This wrapper renders a tiny compose env file from the
JSON settings and then invokes ``docker compose --env-file``. It intentionally
does not read or migrate the project-root ``.env`` file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_DIR = PROJECT_ROOT / "data" / "user" / "settings"
DOCKER_ENV_PATH = SETTINGS_DIR / "docker.env"
RUNNER_CONTROL_TOKEN_PATH = PROJECT_ROOT / "data" / "system" / "sandbox-runner.token"

RUNNER_CONTROL_TOKEN_ENV = "DEEPTUTOR_SANDBOX_RUNNER_TOKEN"
RUNNER_CONTROL_TOKEN_BYTES = 48
MIN_RUNNER_CONTROL_TOKEN_LENGTH = 64
MAX_RUNNER_CONTROL_TOKEN_LENGTH = 4096

DEFAULT_BACKEND_PORT = 8001
DEFAULT_FRONTEND_PORT = 3782
DEFAULT_POCKETBASE_PORT = 8090


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _coerce_port(value: Any, default: int) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def _ensure_token_parent(path: Path) -> None:
    """Create a missing token directory privately without changing an existing one."""
    try:
        path.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise RuntimeError(f"cannot inspect runner control-token directory: {path}") from exc
        if not stat.S_ISDIR(mode):
            raise RuntimeError(f"runner control-token parent is not a directory: {path}")
    else:
        # mkdir applies the process umask. Tighten only the directory created by
        # this invocation; pre-existing directory permissions belong to the user.
        path.chmod(0o700)


def _validate_control_token(raw: bytes, path: Path) -> str:
    if len(raw) > MAX_RUNNER_CONTROL_TOKEN_LENGTH:
        raise RuntimeError(f"runner control token is unexpectedly large: {path}")
    try:
        token = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"runner control token is not ASCII: {path}") from exc
    if len(token) < MIN_RUNNER_CONTROL_TOKEN_LENGTH:
        raise RuntimeError(f"runner control token is too short: {path}")
    if not all(character.isalnum() or character in "-_" for character in token):
        raise RuntimeError(f"runner control token has invalid characters: {path}")
    return token


def _read_existing_control_token(parent_fd: int, path: Path) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path.name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RuntimeError(f"cannot open runner control token securely: {path}") from exc

    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"runner control token is not a regular file: {path}")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode not in {0o400, 0o600}:
            raise RuntimeError(
                f"runner control token must have owner-only read permissions: {path}"
            )
        if metadata.st_uid != os.geteuid():
            raise RuntimeError(f"runner control token is not owned by the current user: {path}")
        raw = os.read(fd, MAX_RUNNER_CONTROL_TOKEN_LENGTH + 1)
    finally:
        os.close(fd)
    return _validate_control_token(raw, path)


def _load_or_create_runner_control_token(path: Path = RUNNER_CONTROL_TOKEN_PATH) -> str:
    """Return a validated token, creating it once without following symlinks."""
    _ensure_token_parent(path.parent)
    try:
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise RuntimeError(f"cannot open runner control-token directory: {path.parent}") from exc

    try:
        try:
            return _read_existing_control_token(parent_fd, path)
        except FileNotFoundError:
            pass

        token = secrets.token_urlsafe(RUNNER_CONTROL_TOKEN_BYTES)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            # Another process won the first-start race. Its file must satisfy the
            # same fail-closed validation before it can be reused.
            return _read_existing_control_token(parent_fd, path)
        except OSError as exc:
            raise RuntimeError(f"cannot create runner control token securely: {path}") from exc

        try:
            os.fchmod(fd, 0o600)
            encoded = token.encode("ascii")
            offset = 0
            while offset < len(encoded):
                offset += os.write(fd, encoded[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        return token
    finally:
        os.close(parent_fd)


def _write_private_env(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RuntimeError(f"cannot write Docker environment file securely: {path}") from exc
    try:
        os.fchmod(fd, 0o600)
        data = content.encode("utf-8")
        offset = 0
        while offset < len(data):
            offset += os.write(fd, data[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)


def render_docker_env(
    settings_dir: Path = SETTINGS_DIR,
    output_path: Path = DOCKER_ENV_PATH,
    control_token_path: Path | None = None,
) -> dict[str, str]:
    """Render compose interpolation vars from JSON settings only."""
    if control_token_path is None:
        # Keep existing callers that render wholly inside a temporary directory
        # isolated from the real deployment secret. Production's no-argument
        # call always uses the canonical data/system path above.
        if settings_dir != SETTINGS_DIR and output_path != DOCKER_ENV_PATH:
            control_token_path = output_path.with_name("sandbox-runner.token")
        else:
            control_token_path = RUNNER_CONTROL_TOKEN_PATH
    control_token = _load_or_create_runner_control_token(control_token_path)
    system = _read_json_object(settings_dir / "system.json")
    integrations = _read_json_object(settings_dir / "integrations.json")
    values = {
        "DEEPTUTOR_DOCKER_BACKEND_PORT": str(
            _coerce_port(system.get("backend_port"), DEFAULT_BACKEND_PORT)
        ),
        "DEEPTUTOR_DOCKER_FRONTEND_PORT": str(
            _coerce_port(system.get("frontend_port"), DEFAULT_FRONTEND_PORT)
        ),
        "DEEPTUTOR_DOCKER_POCKETBASE_PORT": str(
            _coerce_port(integrations.get("pocketbase_port"), DEFAULT_POCKETBASE_PORT)
        ),
    }
    lines = [
        "# Auto-generated by scripts/docker_compose.py from data/user/settings/*.json.",
        "# Do not edit manually; update system.json/integrations.json instead.",
    ]
    lines.extend(f"{key}={value}" for key, value in values.items())
    lines.append(f"{RUNNER_CONTROL_TOKEN_ENV}={control_token}")
    _write_private_env(output_path, "\n".join(lines) + "\n")
    return values


def _compose_command(args: list[str]) -> list[str]:
    docker = shutil.which("docker")
    if not docker:
        raise SystemExit("docker was not found on PATH")
    return [docker, "compose", "--env-file", str(DOCKER_ENV_PATH), *args]


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        args = ["up", "-d"]

    values = render_docker_env()
    print(
        "Docker settings: "
        f"backend={values['DEEPTUTOR_DOCKER_BACKEND_PORT']} "
        f"frontend={values['DEEPTUTOR_DOCKER_FRONTEND_PORT']} "
        f"pocketbase={values['DEEPTUTOR_DOCKER_POCKETBASE_PORT']}",
        file=sys.stderr,
    )

    env = os.environ.copy()
    # Keep Docker execution detached from host process overrides.
    for key in (
        "BACKEND_PORT",
        "FRONTEND_PORT",
        "POCKETBASE_PORT",
        "AUTH_ENABLED",
        "POCKETBASE_URL",
        "NEXT_PUBLIC_API_BASE",
        "NEXT_PUBLIC_API_BASE_EXTERNAL",
        RUNNER_CONTROL_TOKEN_ENV,
    ):
        env.pop(key, None)

    result = subprocess.run(_compose_command(args), cwd=str(PROJECT_ROOT), env=env, check=False)
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
