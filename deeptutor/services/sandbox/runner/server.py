"""Fail-closed HTTP broker for the Linux sandbox-runner container.

The broker never executes an untrusted command in its own process. It pins the
requested directories with ``openat2(2)``, starts a fresh launcher process, and
streams bounded stdout/stderr while the launcher irreversibly drops credentials
and enters a per-job Landlock domain.

Private wire protocol v3:

* ``GET /health`` returns a JSON security attestation.
* ``POST /v3/exec`` runs one authenticated job and returns the usual
  ``ExecResult`` fields.

Older unversioned endpoints are intentionally rejected. An old broker cannot
accidentally execute a v2 request during a rolling deployment.
"""

from __future__ import annotations

import codecs
import ctypes
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from typing import Any

try:
    from . import worker
except ImportError:  # Direct-file execution in Dockerfile.runner.
    import worker  # type: ignore[no-redef]


DEFAULT_PORT = 8900
PROTOCOL_VERSION = 3
SECURITY_PROFILE = worker.SECURITY_PROFILE

_MAX_REQUEST_BYTES = 4 * 1024 * 1024
_DEFAULT_TIMEOUT_S = 30
_DEFAULT_MEMORY_MB = 512
_DEFAULT_CPU_SECONDS = 30
_DEFAULT_MAX_OUTPUT_CHARS = 10_000
_MAX_TIMEOUT_S = 600
_MAX_MEMORY_MB = 768
_MAX_CPU_SECONDS = 120
_MAX_OUTPUT_CHARS = 1_000_000
_RLIMIT_NOFILE = 4096
_MAX_WORKER_STATUS_BYTES = 64 * 1024
_MIN_CONTROL_TOKEN_CHARS = 43

_SYS_OPENAT2 = 437
_RESOLVE_NO_XDEV = 0x01
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08

# CAP_DAC_OVERRIDE and CAP_FOWNER let the trusted broker pin mode-restricted
# workdirs and reliably remove adversarial job-owned scratch trees. Every job
# drops them with all other capabilities before exec.
_EXPECTED_BROKER_CAPABILITIES = {1, 3, 5, 6, 7, 8}

_ALLOWED_WORKDIR_ROOTS = [
    root
    for root in os.environ.get(
        "DEEPTUTOR_RUNNER_ALLOWED_WORKDIRS",
        "/app/data/user/workspace:/app/data/users",
    ).split(":")
    if root
]
_ALLOWED_READ_ONLY_ROOTS = [
    root
    for root in os.environ.get(
        "DEEPTUTOR_RUNNER_ALLOWED_READ_ONLY_DIRS",
        "/app/data/cli-apps",
    ).split(":")
    if root
]
_JOB_UID = int(os.environ.get("DEEPTUTOR_RUNNER_JOB_UID", "1000"))
_JOB_GID = int(os.environ.get("DEEPTUTOR_RUNNER_JOB_GID", "1000"))

_libc = ctypes.CDLL(None, use_errno=True)
_HEALTH_ATTESTATION: dict[str, Any] | None = None
_HEALTH_LOCK = threading.Lock()
_EXECUTION_LOCK = threading.Lock()


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


class _BoundedText:
    """Incrementally decode a byte stream into a fixed-size head/tail buffer."""

    def __init__(self, max_chars: int) -> None:
        self._max_chars = max_chars
        self._head_limit = max_chars // 2
        self._tail_limit = max_chars - self._head_limit
        self._head = ""
        self._tail = ""
        self._total = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, data: bytes) -> None:
        self._feed_text(self._decoder.decode(data, final=False))

    def finish(self) -> None:
        self._feed_text(self._decoder.decode(b"", final=True))

    def _feed_text(self, text: str) -> None:
        if not text:
            return
        self._total += len(text)
        head_room = self._head_limit - len(self._head)
        if head_room > 0:
            self._head += text[:head_room]
            text = text[head_room:]
        if text and self._tail_limit:
            self._tail = (self._tail + text)[-self._tail_limit :]

    def render(self) -> str:
        kept = len(self._head) + len(self._tail)
        if self._total <= self._max_chars:
            return self._head + self._tail
        dropped = self._total - kept
        return self._head + f"\n\n... ({dropped:,} chars truncated) ...\n\n" + self._tail


def _normalize_absolute(path: str) -> str:
    if not path or not os.path.isabs(path) or "\x00" in path:
        raise ValueError(f"path must be a non-empty absolute path: {path!r}")
    return os.path.normpath(path)


def _is_within(path: str, root: str) -> bool:
    try:
        normalized_root = _normalize_absolute(root)
        return os.path.commonpath((_normalize_absolute(path), normalized_root)) == normalized_root
    except ValueError:
        return False


def _select_root(path: str, roots: list[str]) -> tuple[str, str]:
    normalized = _normalize_absolute(path)
    candidates = [_normalize_absolute(root) for root in roots if _is_within(normalized, root)]
    if not candidates:
        raise ValueError(f"path {path!r} is outside allowed roots ({':'.join(roots)})")
    root = max(candidates, key=len)
    return normalized, root


def _openat2_directory(root_fd: int, relative: str) -> int:
    how = _OpenHow(
        os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC,
        0,
        _RESOLVE_NO_XDEV | _RESOLVE_NO_MAGICLINKS | _RESOLVE_NO_SYMLINKS | _RESOLVE_BENEATH,
    )
    fd = _libc.syscall(
        _SYS_OPENAT2,
        root_fd,
        os.fsencode(relative),
        ctypes.byref(how),
        ctypes.sizeof(how),
    )
    if fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, f"openat2({relative!r}): {os.strerror(error)}")
    return int(fd)


def _pin_directory(path: str, roots: list[str]) -> tuple[int, str]:
    """Open an immutable reference to *path* without following nested symlinks."""
    normalized, root = _select_root(path, roots)
    root_fd = os.open(root, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        relative = os.path.relpath(normalized, root)
        fd = os.dup(root_fd) if relative == "." else _openat2_directory(root_fd, relative)
    finally:
        os.close(root_fd)
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise NotADirectoryError(normalized)
    return fd, normalized


def _workdir_violation(workdir: str) -> str:
    """Return a lexical rejection reason, or ``''`` for an allowed root."""
    try:
        _select_root(workdir, _ALLOWED_WORKDIR_ROOTS)
    except (OSError, ValueError) as exc:
        return f"workdir rejected: {exc}"
    return ""


def _probe_openat2() -> None:
    probe_path = tempfile.mkdtemp(prefix="deeptutor-openat2-probe-", dir="/tmp")
    root_fd = os.open("/tmp", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fd = _openat2_directory(root_fd, os.path.basename(probe_path))
    finally:
        os.close(root_fd)
        os.rmdir(probe_path)
    os.close(fd)


def _control_token() -> str:
    token = os.environ.get("DEEPTUTOR_RUNNER_TOKEN", "")
    if (
        len(token) < _MIN_CONTROL_TOKEN_CHARS
        or not token.isascii()
        or any(character.isspace() for character in token)
    ):
        raise RuntimeError("runner control token is missing or invalid")
    return token


def _probe_broker() -> dict[str, Any]:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("sandbox runner requires Linux")
    if os.getresuid() != (0, 0, 0) or os.getresgid() != (0, 0, 0):
        raise RuntimeError("broker must start with real/effective/saved UID and GID 0")
    if worker.no_new_privs() != 1:
        raise RuntimeError("broker requires container no-new-privileges")
    _control_token()

    capabilities = worker.capability_state()
    for name in ("effective", "permitted", "bounding"):
        actual = set(capabilities[name])
        if actual != _EXPECTED_BROKER_CAPABILITIES:
            raise RuntimeError(
                f"broker {name} capabilities must be exactly "
                f"{sorted(_EXPECTED_BROKER_CAPABILITIES)}, got {sorted(actual)}"
            )
    if capabilities["inheritable"] or capabilities["ambient"]:
        raise RuntimeError("broker inheritable and ambient capabilities must be empty")

    abi = worker.landlock_abi()
    if abi < worker.MIN_LANDLOCK_ABI:
        raise RuntimeError(f"Landlock ABI {abi} is below required ABI {worker.MIN_LANDLOCK_ABI}")
    errata = worker.landlock_errata()
    if errata & worker.REQUIRED_LANDLOCK_ERRATA != worker.REQUIRED_LANDLOCK_ERRATA:
        raise RuntimeError("Landlock erratum 3 is not fixed by this kernel")
    _probe_openat2()

    missing_roots = [
        root
        for root in (*_ALLOWED_WORKDIR_ROOTS, *_ALLOWED_READ_ONLY_ROOTS)
        if not os.path.isdir(root)
    ]
    if missing_roots:
        raise RuntimeError(f"configured sandbox roots are missing: {missing_roots!r}")

    return {
        "status": "ok",
        "protocol": PROTOCOL_VERSION,
        "security_profile": SECURITY_PROFILE,
        "landlock_abi": abi,
        "landlock_errata": errata,
        "features": [
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
        ],
    }


def _require_broker_ready() -> dict[str, Any]:
    global _HEALTH_ATTESTATION
    if _HEALTH_ATTESTATION is None:
        with _HEALTH_LOCK:
            if _HEALTH_ATTESTATION is None:
                _HEALTH_ATTESTATION = _probe_broker()
    return dict(_HEALTH_ATTESTATION)


def _bounded_int(value: Any, default: int, ceiling: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, ceiling))


def _prepare_mounts(raw_mounts: Any, workdir: str | None) -> list[int]:
    if not isinstance(raw_mounts, list):
        raise ValueError("'mounts' must be a list")
    read_only_fds: list[int] = []
    try:
        for mount in raw_mounts:
            if not isinstance(mount, dict):
                raise ValueError("every mount must be an object")
            host_path = mount.get("host_path")
            sandbox_path = mount.get("sandbox_path")
            read_only = mount.get("read_only", True)
            if not isinstance(host_path, str) or not isinstance(sandbox_path, str):
                raise ValueError("mount paths must be strings")
            if host_path != sandbox_path:
                raise ValueError("runner mounts require host_path == sandbox_path")
            if not isinstance(read_only, bool):
                raise ValueError("mount read_only must be a boolean")

            if read_only:
                fd, _ = _pin_directory(host_path, _ALLOWED_READ_ONLY_ROOTS)
                read_only_fds.append(fd)
            else:
                if workdir is None or not _is_within(workdir, host_path):
                    raise ValueError("a writable mount must contain the pinned workdir")
                fd, _ = _pin_directory(host_path, _ALLOWED_WORKDIR_ROOTS)
                os.close(fd)
    except Exception:
        for fd in read_only_fds:
            os.close(fd)
        raise
    return read_only_fds


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _reap_process_group(pgid: int) -> None:
    """Reap orphaned descendants after their session leader has been waited."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            pid, _ = os.waitpid(-pgid, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            time.sleep(0.01)


def _drain_process(
    process: subprocess.Popen[bytes],
    status_fd: int,
    *,
    timeout_s: int,
    max_output_chars: int,
) -> tuple[str, str, bytes, bool]:
    stdout = _BoundedText(max_output_chars)
    stderr = _BoundedText(max_output_chars)
    status_data = bytearray()
    status_overflow = False
    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, ("stdout", stdout))
    selector.register(process.stderr, selectors.EVENT_READ, ("stderr", stderr))
    selector.register(status_fd, selectors.EVENT_READ, ("status", None))

    deadline = time.monotonic() + timeout_s
    drain_deadline: float | None = None
    timed_out = False
    try:
        while selector.get_map():
            now = time.monotonic()
            if not timed_out and now >= deadline:
                timed_out = True
                _kill_process_group(process)
                drain_deadline = now + 5
            if drain_deadline is not None and now >= drain_deadline:
                break

            for key, _ in selector.select(0.1):
                try:
                    chunk = os.read(key.fd, 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                stream_name, collector = key.data
                if stream_name == "status":
                    remaining = _MAX_WORKER_STATUS_BYTES - len(status_data)
                    if remaining > 0:
                        status_data.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        status_overflow = True
                else:
                    collector.feed(chunk)
    finally:
        selector.close()
        stdout.finish()
        stderr.finish()

    if process.poll() is None:
        _kill_process_group(process)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    _kill_process_group(process)
    _reap_process_group(process.pid)
    if status_overflow:
        status_data = bytearray(b'{"ok":false,"error":"worker status overflow"}\n')
    return stdout.render(), stderr.render(), bytes(status_data), timed_out


def _parse_worker_status(raw: bytes) -> tuple[dict[str, Any] | None, str]:
    records: list[dict[str, Any]] = []
    try:
        for line in raw.decode("utf-8").splitlines():
            record = json.loads(line)
            if isinstance(record, dict):
                records.append(record)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"invalid worker attestation: {exc}"
    if not records:
        return None, "worker produced no security attestation"
    failures = [record for record in records if record.get("ok") is not True]
    if failures:
        return None, str(failures[-1].get("error") or "worker setup failed")
    attestation = records[-1]
    if attestation.get("security_profile") != SECURITY_PROFILE:
        return None, "worker security profile mismatch"
    if int(attestation.get("landlock_abi", 0)) < worker.MIN_LANDLOCK_ABI:
        return None, "worker Landlock ABI is below the required version"
    if (
        int(attestation.get("landlock_errata", 0)) & worker.REQUIRED_LANDLOCK_ERRATA
        != worker.REQUIRED_LANDLOCK_ERRATA
    ):
        return None, "worker Landlock erratum 3 is not fixed"
    if (
        attestation.get("uid") != _JOB_UID
        or attestation.get("gid") != _JOB_GID
        or attestation.get("groups") != []
        or attestation.get("no_new_privs") != 1
    ):
        return None, "worker credential attestation mismatch"
    capabilities = attestation.get("capabilities")
    if not isinstance(capabilities, dict) or any(capabilities.values()):
        return None, "worker retained one or more capabilities"
    if attestation.get("seccomp_policy") != worker.SECCOMP_POLICY:
        return None, "worker seccomp policy mismatch"
    return attestation, ""


def _run_worker(
    *,
    command: str,
    argv: list[str],
    env: dict[str, str],
    workdir_fd: int | None,
    read_only_fds: list[int],
    timeout_s: int,
    memory_mb: int,
    cpu_seconds: int,
    max_output_chars: int,
) -> dict[str, Any]:
    _require_broker_ready()
    scratch_path = tempfile.mkdtemp(prefix="deeptutor-job-", dir="/tmp")
    # Keep the root owned by the broker so the job cannot chmod/rename it. The
    # worker gets write/search access but Landlock does not grant the /tmp
    # parent, so it cannot replace the entry itself.
    os.chmod(scratch_path, 0o777)
    scratch_fd = os.open(
        scratch_path,
        os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    owned_workdir_fd: int | None = None
    if workdir_fd is None:
        owned_workdir_fd = os.dup(scratch_fd)
        workdir_fd = owned_workdir_fd

    status_r, status_w = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    process: subprocess.Popen[bytes] | None = None
    try:
        config = {
            "command": command,
            "argv": argv,
            "env": env,
            "uid": _JOB_UID,
            "gid": _JOB_GID,
            "broker_pid": os.getpid(),
            "workdir_fd": workdir_fd,
            "scratch_fd": scratch_fd,
            "scratch_path": scratch_path,
            "read_only_fds": read_only_fds,
            "memory_mb": memory_mb,
            "cpu_seconds": cpu_seconds,
            "nofile": _RLIMIT_NOFILE,
        }
        worker_env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONUNBUFFERED": "1",
            "DEEPTUTOR_RUNNER_STATUS_FD": str(status_w),
        }
        pass_fds = tuple(sorted({status_w, workdir_fd, scratch_fd, *read_only_fds}))
        process = subprocess.Popen(
            [sys.executable, str(Path(worker.__file__).resolve())],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=worker_env,
            close_fds=True,
            pass_fds=pass_fds,
            bufsize=0,
        )
        os.close(status_w)
        status_w = -1
        assert process.stdin is not None
        process.stdin.write(json.dumps(config).encode("utf-8"))
        process.stdin.close()
        process.stdin = None

        stdout, stderr, raw_status, timed_out = _drain_process(
            process,
            status_r,
            timeout_s=timeout_s,
            max_output_chars=max_output_chars,
        )
        os.close(status_r)
        status_r = -1
        _, status_error = _parse_worker_status(raw_status)
        if status_error:
            return _error_result(status_error, stderr=stderr)
        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": 124 if timed_out else int(process.returncode or 0),
            "timed_out": timed_out,
            "error": "",
            "security_profile": SECURITY_PROFILE,
        }
    except (OSError, ValueError) as exc:
        if process is not None:
            _kill_process_group(process)
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        return _error_result(f"{type(exc).__name__}: {exc}")
    finally:
        if status_w >= 0:
            os.close(status_w)
        if status_r >= 0:
            os.close(status_r)
        if owned_workdir_fd is not None:
            os.close(owned_workdir_fd)
        os.close(scratch_fd)
        shutil.rmtree(scratch_path)


def execute(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and execute one authenticated private protocol v3 request."""
    command = payload.get("command")
    if not isinstance(command, str) or not command:
        return _error_result("missing or empty 'command'")

    raw_argv = payload.get("argv") or []
    if not isinstance(raw_argv, list) or any(not isinstance(item, str) for item in raw_argv):
        return _error_result("'argv' must be a list of strings")
    argv = list(raw_argv)

    workdir = payload.get("workdir") or None
    if workdir is not None and not isinstance(workdir, str):
        return _error_result("'workdir' must be a string or null")

    raw_env = payload.get("env") or {}
    if not isinstance(raw_env, dict):
        return _error_result("'env' must be an object")
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    for key, value in raw_env.items():
        env[str(key)] = str(value)

    limits = payload.get("limits") or {}
    if not isinstance(limits, dict):
        return _error_result("'limits' must be an object")
    timeout_s = _bounded_int(limits.get("timeout_s"), _DEFAULT_TIMEOUT_S, _MAX_TIMEOUT_S)
    memory_mb = _bounded_int(limits.get("memory_mb"), _DEFAULT_MEMORY_MB, _MAX_MEMORY_MB)
    cpu_seconds = _bounded_int(limits.get("cpu_seconds"), _DEFAULT_CPU_SECONDS, _MAX_CPU_SECONDS)
    max_output_chars = _bounded_int(
        limits.get("max_output_chars"), _DEFAULT_MAX_OUTPUT_CHARS, _MAX_OUTPUT_CHARS
    )

    workdir_fd: int | None = None
    read_only_fds: list[int] = []
    try:
        if workdir is not None:
            workdir_fd, workdir = _pin_directory(workdir, _ALLOWED_WORKDIR_ROOTS)
        read_only_fds = _prepare_mounts(payload.get("mounts") or [], workdir)
        return _run_worker(
            command=command,
            argv=argv,
            env=env,
            workdir_fd=workdir_fd,
            read_only_fds=read_only_fds,
            timeout_s=timeout_s,
            memory_mb=memory_mb,
            cpu_seconds=cpu_seconds,
            max_output_chars=max_output_chars,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        return _error_result(f"{type(exc).__name__}: {exc}")
    finally:
        if workdir_fd is not None:
            os.close(workdir_fd)
        for fd in read_only_fds:
            os.close(fd)


def _error_result(message: str, *, stderr: str = "") -> dict[str, Any]:
    return {
        "stdout": "",
        "stderr": stderr,
        "exit_code": 0,
        "timed_out": False,
        "error": message,
        "security_profile": SECURITY_PROFILE,
    }


def _self_test_worker() -> None:
    """Prove the complete worker profile before the broker becomes healthy."""
    global _HEALTH_ATTESTATION
    result = _run_worker(
        command="/bin/true",
        argv=["/bin/true"],
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        workdir_fd=None,
        read_only_fds=[],
        timeout_s=5,
        memory_mb=128,
        cpu_seconds=5,
        max_output_chars=1024,
    )
    if result.get("error") or result.get("timed_out") or result.get("exit_code") != 0:
        raise RuntimeError(f"worker security self-test failed: {result!r}")
    with _HEALTH_LOCK:
        if _HEALTH_ATTESTATION is None:
            raise RuntimeError("broker preflight disappeared during worker self-test")
        _HEALTH_ATTESTATION["worker_self_test"] = True


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        sys.stdout.write("runner: " + (format % args) + "\n")

    def _send_json(
        self,
        status_code: int,
        body: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {_control_token()}"
        return hmac.compare_digest(supplied, expected)

    def _reject_unauthorized(self) -> None:
        self._send_json(
            401,
            _error_result("runner authentication failed"),
            headers={"WWW-Authenticate": "Bearer"},
        )

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            if not self._authorized():
                self._reject_unauthorized()
                return
            try:
                self._send_json(200, _require_broker_ready())
            except Exception as exc:  # noqa: BLE001
                self._send_json(503, _error_result(f"runner unavailable: {exc}"))
            return
        self._send_json(404, _error_result("not found"))

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v3/exec":
            self._send_json(404, _error_result("unsupported runner protocol"))
            return
        if not self._authorized():
            self._reject_unauthorized()
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, _error_result("invalid Content-Length"))
            return
        if length < 0 or length > _MAX_REQUEST_BYTES:
            self._send_json(413, _error_result("request body too large"))
            return
        try:
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_json(400, _error_result(f"invalid JSON: {exc}"))
            return
        if not _EXECUTION_LOCK.acquire(blocking=False):
            self._send_json(429, _error_result("another runner job is active"))
            return
        try:
            try:
                result = execute(payload)
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                result = _error_result(f"runner crashed: {type(exc).__name__}: {exc}")
            self._send_json(200, result)
        finally:
            _EXECUTION_LOCK.release()


def main() -> None:
    try:
        _require_broker_ready()
        _self_test_worker()
        attestation = _require_broker_ready()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"runner: security preflight failed: {exc}") from exc
    try:
        port = int(os.environ.get("RUNNER_PORT", "") or DEFAULT_PORT)
    except ValueError:
        port = DEFAULT_PORT
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    sys.stdout.write(
        f"runner: listening on 0.0.0.0:{port} "
        f"profile={attestation['security_profile']} abi={attestation['landlock_abi']} "
        f"errata={attestation['landlock_errata']}\n"
    )
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
