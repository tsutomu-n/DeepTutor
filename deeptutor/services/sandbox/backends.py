"""
Sandbox backends: one class per isolation mechanism.

* :class:`RunnerSidecarBackend` — submits the command to a separate runner
  container over HTTP (SYSTEM isolation). The deployment answer for Docker:
  the main app stays least-privileged and never executes untrusted shell.
* :class:`BwrapBackend` — wraps the command in ``bwrap`` mount namespaces on
  Linux bare-metal (SYSTEM isolation).
* :class:`RestrictedSubprocessBackend` — a plain subprocess with cleaned env
  and path-confined cwd (APPLICATION isolation). Degraded fallback for local
  dev (e.g. macOS); admin-opt-in only because it does not OS-isolate.

Every backend is constructed from :class:`SandboxSettings` and reports the
isolation level it actually provides via :attr:`level`.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import os
from pathlib import Path
import shutil

import httpx

from deeptutor.services.sandbox.spec import (
    ExecRequest,
    ExecResult,
    IsolationLevel,
)

_RUNNER_PROTOCOL = 3
_RUNNER_SECURITY_PROFILE = "landlock-v6-fd-v3"
_RUNNER_REQUIRED_LANDLOCK_ERRATA = 1 << (3 - 1)
_RUNNER_REQUIRED_FEATURES = {
    "bounded-output-streaming",
    "bearer-control-auth",
    "credential-drop",
    "fd-pinned-workdir",
    "landlock-abstract-unix-scope",
    "landlock-errata-3",
    "landlock-signal-scope",
    "landlock-tcp-deny",
    "seccomp-fs-notify-deny",
    "seccomp-ipc-and-socket-deny",
    "seccomp-path-metadata-deny",
    "seccomp-cross-process-deny",
    "single-active-job",
}


class SandboxBackend:
    """Abstract execution backend."""

    level: IsolationLevel = IsolationLevel.OFF

    async def exec(self, request: ExecRequest) -> ExecResult:
        raise NotImplementedError

    async def health(self) -> tuple[bool, str]:
        """Return ``(available, detail)`` — whether the backend can run now."""
        return True, ""


class RunnerSidecarBackend(SandboxBackend):
    """Delegate execution to the runner sidecar over HTTP."""

    level = IsolationLevel.SYSTEM

    def __init__(
        self,
        base_url: str,
        *,
        control_token: str = "",
        connect_timeout_s: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._control_token = control_token
        self._connect_timeout_s = connect_timeout_s

    def _auth_headers(self) -> dict[str, str]:
        if len(self._control_token) < 43:
            raise ValueError("runner control token is missing or invalid")
        return {"Authorization": f"Bearer {self._control_token}"}

    async def exec(self, request: ExecRequest) -> ExecResult:
        payload = {
            # Both spellings travel and must agree. argv remains authoritative;
            # command is the shell form used only for model-authored shell jobs.
            "command": request.command,
            "argv": list(request.argv),
            "workdir": request.workdir,
            "env": request.env,
            "mounts": [
                {
                    "host_path": m.host_path,
                    "sandbox_path": m.sandbox_path,
                    "read_only": m.read_only,
                }
                for m in request.mounts
            ],
            "limits": {
                "timeout_s": request.limits.timeout_s,
                "memory_mb": request.limits.memory_mb,
                "cpu_seconds": request.limits.cpu_seconds,
                "max_output_chars": request.limits.max_output_chars,
            },
        }
        # Allow the HTTP call to outlast the command's own timeout a little so
        # the runner can report a clean timeout result instead of us aborting.
        http_timeout = httpx.Timeout(
            request.limits.timeout_s + 15,
            connect=self._connect_timeout_s,
        )
        try:
            async with httpx.AsyncClient(timeout=http_timeout) as client:
                # The authenticated versioned path is fail-closed: a
                # pre-hardening runner cannot execute this request.
                resp = await client.post(
                    f"{self._base_url}/v3/exec",
                    json=payload,
                    headers=self._auth_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("security_profile") != _RUNNER_SECURITY_PROFILE:
                    raise ValueError("runner security profile mismatch")
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            return ExecResult(error=f"runner unavailable: {type(exc).__name__}: {exc}")
        return ExecResult(
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
            exit_code=int(data.get("exit_code", 0)),
            timed_out=bool(data.get("timed_out", False)),
            error=str(data.get("error", "")),
        )

    async def health(self) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=self._connect_timeout_s) as client:
                resp = await client.get(
                    f"{self._base_url}/health",
                    headers=self._auth_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
            features = set(data.get("features") or [])
            if (
                data.get("status") != "ok"
                or data.get("protocol") != _RUNNER_PROTOCOL
                or data.get("security_profile") != _RUNNER_SECURITY_PROFILE
                or data.get("worker_self_test") is not True
                or int(data.get("landlock_abi", 0)) < 6
                or int(data.get("landlock_errata", 0)) & _RUNNER_REQUIRED_LANDLOCK_ERRATA
                != _RUNNER_REQUIRED_LANDLOCK_ERRATA
                or not _RUNNER_REQUIRED_FEATURES.issubset(features)
            ):
                return False, "runner security attestation mismatch"
            return True, (
                f"runner {_RUNNER_SECURITY_PROFILE} ready (Landlock ABI {data['landlock_abi']})"
            )
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            return False, f"runner unavailable: {type(exc).__name__}: {exc}"


class BwrapBackend(SandboxBackend):
    """Bubblewrap mount-namespace isolation (Linux only)."""

    level = IsolationLevel.SYSTEM

    _RO_SYSTEM_DIRS = ("/usr", "/usr/local", "/bin", "/lib", "/lib64", "/etc", "/sbin")

    def __init__(self, bwrap_path: str = "bwrap") -> None:
        self._bwrap = bwrap_path

    def _build_argv(self, request: ExecRequest) -> list[str]:
        argv = [
            self._bwrap,
            "--die-with-parent",
            "--unshare-all",
            "--new-session",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",  # nosec B108 — path inside the bwrap mount namespace, not the host
        ]
        for system_dir in self._RO_SYSTEM_DIRS:
            if Path(system_dir).exists():
                argv += ["--ro-bind", system_dir, system_dir]
        for mount in request.mounts:
            flag = "--ro-bind" if mount.read_only else "--bind"
            argv += [flag, mount.host_path, mount.sandbox_path]
        if request.workdir:
            argv += ["--chdir", request.workdir]
        for key, value in request.env.items():
            argv += ["--setenv", key, value]
        if request.argv:
            # No shell in between: bwrap execs the vector directly.
            argv += ["--", *request.argv]
        else:
            argv += ["/bin/sh", "-c", request.command]
        return argv

    async def exec(self, request: ExecRequest) -> ExecResult:
        argv = self._build_argv(request)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return ExecResult(error="bwrap not found on host")
        return await _communicate(process, request.limits.timeout_s)

    async def health(self) -> tuple[bool, str]:
        if shutil.which(self._bwrap) is None:
            return False, "bwrap not installed"
        # bwrap needs unprivileged user namespaces; a default-seccomp Docker
        # container blocks them, so confirm a trivial sandbox actually runs.
        probe = ExecRequest(command="true")
        result = await self.exec(probe)
        if result.error:
            return False, result.error
        return True, "bwrap functional"


class RestrictedSubprocessBackend(SandboxBackend):
    """Plain subprocess with a scrubbed env and confined cwd (no OS isolation)."""

    level = IsolationLevel.APPLICATION

    _SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")

    async def exec(self, request: ExecRequest) -> ExecResult:
        env = {k: os.environ[k] for k in self._SAFE_ENV_KEYS if k in os.environ}
        env.update(request.env)
        cwd = request.workdir or None
        try:
            if request.argv:
                process = await asyncio.create_subprocess_exec(
                    *request.argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
            else:
                process = await asyncio.create_subprocess_shell(
                    request.command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
        except Exception as exc:
            return ExecResult(error=f"{type(exc).__name__}: {exc}")
        return await _communicate(process, request.limits.timeout_s)


async def _communicate(process: asyncio.subprocess.Process, timeout_s: int) -> ExecResult:
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        process.kill()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5.0)
        return ExecResult(timed_out=True, exit_code=124)
    return ExecResult(
        stdout=stdout.decode("utf-8", errors="replace") if stdout else "",
        stderr=stderr.decode("utf-8", errors="replace") if stderr else "",
        exit_code=process.returncode if process.returncode is not None else 0,
    )


__all__ = [
    "BwrapBackend",
    "RestrictedSubprocessBackend",
    "RunnerSidecarBackend",
    "SandboxBackend",
]
