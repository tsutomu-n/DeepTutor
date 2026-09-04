#!/usr/bin/env python3
"""Build-independent regressions for implemented Dockerfile.runner boundaries.

This does not claim per-job aggregate cgroup or workspace-storage isolation;
those remain explicit deployment blockers for hostile multi-tenant use.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import urllib.error
import urllib.request

PROFILE = "landlock-v6-fd-v3"
REQUIRED_LANDLOCK_ERRATA = 1 << (3 - 1)


def _docker(*args: str, input_text: str | None = None, check: bool = True) -> str:
    completed = subprocess.run(
        ["docker", *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode:
        raise RuntimeError(
            f"docker {' '.join(args)} failed ({completed.returncode}):\n{completed.stderr}"
        )
    if args and args[0] == "logs":
        return "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
        )
    return completed.stdout.strip()


class _Runner:
    def __init__(self, image: str, require_abi: int) -> None:
        self.image = image
        self.require_abi = require_abi
        self.control_token = secrets.token_urlsafe(48)
        self.name = f"deeptutor-runner-p0-{os.getpid()}-{secrets.token_hex(4)}"
        self._temporary = tempfile.TemporaryDirectory(prefix="deeptutor-runner-p0-")
        self.root = Path(self._temporary.name)
        self.admin = self.root / "admin"
        self.users = self.root / "users"
        self.cli_apps = self.root / "cli-apps"
        self.user_a = self.users / "a" / "workspace"
        self.user_b = self.users / "b" / "workspace"
        for path in (self.admin, self.user_a, self.user_b, self.cli_apps):
            path.mkdir(parents=True)
            path.chmod(0o777)
        (self.user_b / "secret.txt").write_text("principal-b-secret", encoding="utf-8")
        (self.user_b / "sentinel").write_text("sibling", encoding="utf-8")
        self.base_url = ""

    def __enter__(self) -> "_Runner":
        _docker(
            "run",
            "-d",
            "--name",
            self.name,
            "--init",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "FOWNER",
            "--cap-add",
            "KILL",
            "--cap-add",
            "SETGID",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETPCAP",
            "--security-opt",
            "no-new-privileges:true",
            "--security-opt",
            "seccomp=unconfined",
            "--env",
            f"DEEPTUTOR_RUNNER_TOKEN={self.control_token}",
            "--read-only",
            "--memory",
            "192m",
            "--pids-limit",
            "128",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
            "--tmpfs",
            "/home/runner:rw,nosuid,nodev,size=16m,mode=0755",
            "--mount",
            f"type=bind,src={self.admin},dst=/app/data/user/workspace",
            "--mount",
            f"type=bind,src={self.users},dst=/app/data/users",
            "--mount",
            f"type=bind,src={self.cli_apps},dst=/app/data/cli-apps,readonly",
            "-p",
            "127.0.0.1::8900",
            self.image,
        )
        published = _docker("port", self.name, "8900/tcp").splitlines()[0]
        self.base_url = f"http://127.0.0.1:{published.rsplit(':', 1)[1]}"
        deadline = time.monotonic() + 30
        last_error = ""
        while time.monotonic() < deadline:
            try:
                health = self.get_health()
                if health.get("status") == "ok":
                    break
            except Exception as exc:  # noqa: BLE001 - retry startup
                last_error = str(exc)
            time.sleep(0.2)
        else:
            logs = _docker("logs", self.name, check=False)
            raise RuntimeError(f"runner did not become ready: {last_error}\n{logs}")
        return self

    def __exit__(self, *_exc: object) -> None:
        _docker("rm", "-f", self.name, check=False)
        self._temporary.cleanup()

    def request(
        self,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        token: str | None,
        timeout: int = 3,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def get_health(self) -> dict[str, Any]:
        return self.request("/health", token=self.control_token)

    def exec(
        self,
        *,
        argv: list[str],
        workdir: str = "/app/data/users/a/workspace",
        mounts: list[dict[str, Any]] | None = None,
        timeout_s: int = 10,
        memory_mb: int = 128,
        max_output_chars: int = 4096,
    ) -> dict[str, Any]:
        if mounts is None:
            mounts = [
                {
                    "host_path": workdir,
                    "sandbox_path": workdir,
                    "read_only": False,
                }
            ]
        payload = {
            "command": " ".join(argv),
            "argv": argv,
            "workdir": workdir,
            "env": {},
            "mounts": mounts,
            "limits": {
                "timeout_s": timeout_s,
                "memory_mb": memory_mb,
                "cpu_seconds": min(timeout_s, 30),
                "max_output_chars": max_output_chars,
            },
        }
        result = self.request(
            "/v3/exec",
            payload=payload,
            token=self.control_token,
            timeout=timeout_s + 15,
        )
        if result.get("security_profile") != PROFILE:
            raise AssertionError(f"missing worker profile: {result!r}")
        return result


def _assert_ok(result: dict[str, Any]) -> None:
    if result.get("error") or result.get("timed_out") or result.get("exit_code") != 0:
        raise AssertionError(f"runner job failed: {result!r}")


def _test_protocol_and_health(runner: _Runner) -> dict[str, Any]:
    health = runner.get_health()
    assert health["protocol"] == 3
    assert health["security_profile"] == PROFILE
    assert int(health["landlock_abi"]) >= runner.require_abi
    assert int(health["landlock_errata"]) & REQUIRED_LANDLOCK_ERRATA
    assert health["worker_self_test"] is True
    required = {
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
    assert required.issubset(set(health["features"]))

    legacy_payload = json.dumps(
        {
            "command": "touch legacy-ran",
            "workdir": "/app/data/users/a/workspace",
            "mounts": [
                {
                    "host_path": "/app/data/users/a/workspace",
                    "sandbox_path": "/app/data/users/a/workspace",
                    "read_only": False,
                }
            ],
        }
    ).encode("utf-8")
    for legacy_path in ("/exec", "/v2/exec"):
        request = urllib.request.Request(
            f"{runner.base_url}{legacy_path}",
            data=legacy_payload,
            headers={"Authorization": f"Bearer {runner.control_token}"},
        )
        try:
            urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError(f"legacy {legacy_path} endpoint did not fail closed")
    assert not (runner.user_a / "legacy-ran").exists()

    unauthorized_payload = {
        "command": "touch auth-bypass",
        "argv": ["touch", "auth-bypass"],
        "workdir": "/app/data/users/a/workspace",
        "mounts": [
            {
                "host_path": "/app/data/users/a/workspace",
                "sandbox_path": "/app/data/users/a/workspace",
                "read_only": False,
            }
        ],
    }
    for token in (None, "wrong-control-token-" * 3):
        for path, payload in (("/health", None), ("/v3/exec", unauthorized_payload)):
            try:
                runner.request(path, payload=payload, token=token)
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
            else:
                raise AssertionError(f"unauthorized request reached {path}")
    assert not (runner.user_a / "auth-bypass").exists()
    return health


def _test_credentials(runner: _Runner) -> None:
    code = r"""
import ctypes, errno, json, os
libc = ctypes.CDLL(None, use_errno=True)
status = {}
for line in open("/proc/self/status", encoding="utf-8"):
    key, _, value = line.partition(":")
    if key in {"Uid", "Gid", "Groups", "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs"}:
        status[key] = value.strip()

def denied(call):
    try:
        call()
    except PermissionError:
        return True
    return False

class Header(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int32)]
class Data(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]
header = Header(0x20080522, 0)
data = (Data * 2)()
data[0].effective = data[0].permitted = 1
ctypes.set_errno(0)
capset_denied = (
    libc.capset(ctypes.byref(header), ctypes.byref(data)) == -1
    and ctypes.get_errno() == errno.EPERM
)
ctypes.set_errno(0)
ambient_denied = libc.prctl(47, 2, 0, 0, 0) == -1 and ctypes.get_errno() == errno.EPERM
print(json.dumps({
    "resuid": os.getresuid(), "resgid": os.getresgid(), "groups": os.getgroups(),
    "status": status,
    "setuid_denied": denied(lambda: os.setuid(0)),
    "setgid_denied": denied(lambda: os.setgid(0)),
    "setgroups_denied": denied(lambda: os.setgroups([0])),
    "capset_denied": capset_denied,
    "ambient_denied": ambient_denied,
}))
"""
    result = runner.exec(argv=["python", "-c", code])
    _assert_ok(result)
    evidence = json.loads(result["stdout"])
    assert evidence["resuid"] == [1000, 1000, 1000]
    assert evidence["resgid"] == [1000, 1000, 1000]
    assert evidence["groups"] == []
    assert evidence["status"]["Uid"].split() == ["1000"] * 4
    assert evidence["status"]["Gid"].split() == ["1000"] * 4
    assert evidence["status"]["Groups"] == ""
    for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        assert int(evidence["status"][key], 16) == 0
    assert evidence["status"]["NoNewPrivs"] == "1"
    assert all(
        evidence[key]
        for key in (
            "setuid_denied",
            "setgid_denied",
            "setgroups_denied",
            "capset_denied",
            "ambient_denied",
        )
    )


def _test_filesystem_and_control_plane(runner: _Runner) -> None:
    code = r"""
import ctypes, errno, json, os, socket
from pathlib import Path
evidence = {}
for name, path in {
    "other_principal": "/app/data/users/b/workspace/secret.txt",
    "broker_source": "/app/server.py",
    "worker_source": "/app/worker.py",
    "broker_environment": "/proc/1/environ",
    "control_token_file": "/app/data/system/sandbox-runner.token",
}.items():
    try:
        Path(path).read_text()
    except OSError:
        evidence[name] = "denied"
    else:
        evidence[name] = "reachable"
try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(("127.0.0.1", 8900))
except PermissionError:
    evidence["broker_tcp"] = "denied"
else:
    evidence["broker_tcp"] = "reachable"
try:
    socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
except PermissionError:
    evidence["ip_datagram"] = "denied"
else:
    evidence["ip_datagram"] = "reachable"
try:
    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
except PermissionError:
    evidence["unix_socket"] = "denied"
else:
    evidence["unix_socket"] = "reachable"
try:
    os.mkfifo("job-fifo")
except PermissionError:
    evidence["fifo_create"] = "denied"
else:
    evidence["fifo_create"] = "reachable"
libc = ctypes.CDLL(None, use_errno=True)
ctypes.set_errno(0)
shmid = libc.shmget(0, 4096, 0o600)
if shmid == -1 and ctypes.get_errno() == errno.EPERM:
    evidence["sysv_shm"] = "denied"
else:
    if shmid != -1:
        libc.shmctl(shmid, 0, 0)
    evidence["sysv_shm"] = "reachable"
ctypes.set_errno(0)
mq = libc.mq_open(b"/deeptutor-p0-mq", os.O_CREAT | os.O_RDWR, 0o600, 0)
if mq == -1 and ctypes.get_errno() == errno.EPERM:
    evidence["posix_mqueue"] = "denied"
else:
    if mq != -1:
        libc.mq_close(mq)
        libc.mq_unlink(b"/deeptutor-p0-mq")
    evidence["posix_mqueue"] = "reachable"
ctypes.set_errno(0)
uring = libc.syscall(425, 1, 0)
evidence["io_uring"] = (
    "denied" if uring == -1 and ctypes.get_errno() == errno.EPERM else "reachable"
)
ctypes.set_errno(0)
notify_fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
if notify_fd == -1 and ctypes.get_errno() == errno.EPERM:
    evidence["filesystem_notify"] = "denied"
else:
    if notify_fd != -1:
        os.close(notify_fd)
    evidence["filesystem_notify"] = "reachable"
evidence["control_token_env"] = (
    "reachable" if "DEEPTUTOR_RUNNER_TOKEN" in os.environ else "denied"
)
print(json.dumps(evidence))
"""
    result = runner.exec(argv=["python", "-c", code])
    _assert_ok(result)
    evidence = json.loads(result["stdout"])
    assert set(evidence.values()) == {"denied"}

    victim_file = runner.user_b / "metadata-victim.txt"
    victim_dir = runner.user_b / "metadata-victim-dir"
    victim_link = runner.user_b / "metadata-victim-link"
    # Create the victim as the actual job UID. Creating it from the host could
    # make a CI runner's different UID cause a false-positive chmod denial.
    _docker(
        "exec",
        "--user",
        "1000:1000",
        "-w",
        "/app/data/users/b/workspace",
        runner.name,
        "python",
        "-c",
        "import os; from pathlib import Path; "
        "p=Path('metadata-victim.txt'); p.write_text('unchanged'); p.chmod(0o666); "
        "d=Path('metadata-victim-dir'); d.mkdir(); d.chmod(0o777); "
        "Path('metadata-victim-link').symlink_to('private-target-name'); "
        "os.setxattr(p, b'user.deeptutor-p0', b'unchanged')",
    )
    original_file = victim_file.stat()
    original_dir = victim_dir.stat()
    metadata_code = r"""
import json, os
from pathlib import Path
victim_file = Path("/app/data/users/b/workspace/metadata-victim.txt")
victim_dir = Path("/app/data/users/b/workspace/metadata-victim-dir")

def denied(call):
    try:
        call()
    except PermissionError:
        return True
    return False

print(json.dumps({
    "chmod_file": denied(lambda: os.chmod(victim_file, 0)),
    "chmod_dir": denied(lambda: os.chmod(victim_dir, 0)),
    "chown": denied(lambda: os.chown(victim_file, os.getuid(), os.getgid())),
    "utime": denied(lambda: os.utime(victim_file, (1, 1))),
    "setxattr": denied(lambda: os.setxattr(victim_file, b"user.deeptutor-p0", b"changed")),
    "getxattr": denied(lambda: os.getxattr(victim_file, b"user.deeptutor-p0")),
    "listxattr": denied(lambda: os.listxattr(victim_file)),
    "removexattr": denied(lambda: os.removexattr(victim_file, b"user.deeptutor-p0")),
    "readlink": denied(lambda: os.readlink("/app/data/users/b/workspace/metadata-victim-link")),
}))
"""
    metadata = runner.exec(argv=["python", "-c", metadata_code])
    _assert_ok(metadata)
    assert set(json.loads(metadata["stdout"]).values()) == {True}
    after_file = victim_file.stat()
    after_dir = victim_dir.stat()
    assert victim_file.read_text(encoding="utf-8") == "unchanged"
    assert (after_file.st_mode, after_file.st_mtime_ns) == (
        original_file.st_mode,
        original_file.st_mtime_ns,
    )
    assert after_dir.st_mode == original_dir.st_mode
    assert os.getxattr(victim_file, b"user.deeptutor-p0") == b"unchanged"
    assert os.readlink(victim_link) == "private-target-name"

    tool_root = runner.cli_apps / "example"
    tool_root.mkdir()
    tool_root.chmod(0o755)
    (tool_root / "asset.txt").write_text("trusted-cli-asset", encoding="utf-8")
    mount_path = "/app/data/cli-apps/example"
    ro_mount = {
        "host_path": mount_path,
        "sandbox_path": mount_path,
        "read_only": True,
    }
    code = f"""
from pathlib import Path
path = Path({str(mount_path + "/asset.txt")!r})
print(path.read_text())
try:
    path.write_text("changed")
except OSError:
    print("write-denied")
"""
    result = runner.exec(
        argv=["python", "-c", code],
        mounts=[
            {
                "host_path": "/app/data/users/a/workspace",
                "sandbox_path": "/app/data/users/a/workspace",
                "read_only": False,
            },
            ro_mount,
        ],
    )
    _assert_ok(result)
    assert result["stdout"].splitlines() == ["trusted-cli-asset", "write-denied"]


def _test_directory_swap(runner: _Runner) -> None:
    active = runner.users / "swap-active"
    # Move the pinned inode completely outside the bind-mounted users root.
    # This exercises Landlock erratum 3's disconnected-directory fix instead
    # of merely renaming a directory inside the same visible mount.
    held = runner.root / "swap-held-outside-mount"
    active.mkdir()
    active.chmod(0o777)
    (active / "sentinel").write_text("original-inode", encoding="utf-8")
    before = active.stat()
    code = r"""
import time
from pathlib import Path
Path("ready").write_text("ready", encoding="utf-8")
while not Path("go").exists():
    time.sleep(0.01)
print(Path("sentinel").read_text())
try:
    Path("/app/data/users/swap-active/sentinel").read_text()
except OSError:
    print("replacement-denied")
"""
    result: dict[str, Any] = {}

    def _run() -> None:
        result.update(
            runner.exec(
                argv=["python", "-c", code],
                workdir="/app/data/users/swap-active",
                timeout_s=10,
            )
        )

    request_thread = threading.Thread(target=_run, daemon=True)
    request_thread.start()
    ready = active / "ready"
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not ready.exists():
        raise AssertionError("public runner request did not reach the pinned directory")

    active.rename(held)
    active.symlink_to(runner.user_b, target_is_directory=True)
    (held / "go").write_text("go", encoding="utf-8")
    request_thread.join(timeout=15)
    assert not request_thread.is_alive()
    after = held.stat()
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    _assert_ok(result)
    assert result["stdout"].splitlines() == [
        "original-inode",
        "replacement-denied",
    ]


def _test_peer_ipc(runner: _Runner) -> None:
    peer_dir = runner.users / "a" / "peer"
    peer_dir.mkdir()
    peer_dir.chmod(0o777)
    socket_name = f"deeptutor-p0-{secrets.token_hex(8)}"
    peer_code = f"""
import json, os, socket, time
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.bind("\\0{socket_name}")
sock.listen(1)
open("peer.json", "w", encoding="utf-8").write(json.dumps({{"pid": os.getpid()}}))
time.sleep(15)
"""
    _docker(
        "exec",
        "-d",
        "--user",
        "1000:1000",
        "-w",
        "/app/data/users/a/peer",
        runner.name,
        "python",
        "-c",
        peer_code,
    )
    ready = peer_dir / "peer.json"
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not ready.exists():
        raise AssertionError("peer job did not publish its PID")
    peer_pid = int(json.loads(ready.read_text(encoding="utf-8"))["pid"])

    try:
        attack_code = f"""
import ctypes, errno, json, os, resource, signal, socket
evidence = {{}}
try:
    os.kill({peer_pid}, signal.SIGTERM)
except PermissionError:
    evidence["signal"] = "denied"
else:
    evidence["signal"] = "delivered"
try:
    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
except PermissionError:
    evidence["abstract_socket"] = "denied"
else:
    evidence["abstract_socket"] = "reachable"
libc = ctypes.CDLL(None, use_errno=True)
def record_seccomp(name, syscall_nr, *args):
    ctypes.set_errno(0)
    result = libc.syscall(syscall_nr, *args)
    evidence[name] = (
        "denied"
        if result == -1 and ctypes.get_errno() == errno.EPERM
        else f"reachable:{{result}}:{{ctypes.get_errno()}}"
    )
ctypes.set_errno(0)
result = libc.ptrace(16, {peer_pid}, 0, 0)
evidence["ptrace"] = (
    "denied" if result == -1 and ctypes.get_errno() == errno.EPERM else "reachable"
)
try:
    resource.prlimit({peer_pid}, resource.RLIMIT_NOFILE, (32, 32))
except PermissionError:
    evidence["prlimit"] = "denied"
else:
    evidence["prlimit"] = "changed"
try:
    os.setpriority(os.PRIO_PROCESS, {peer_pid}, 19)
except PermissionError:
    evidence["setpriority"] = "denied"
else:
    evidence["setpriority"] = "changed"
robust_head = ctypes.c_void_p()
robust_size = ctypes.c_size_t()
record_seccomp("add_key", 248, 0, 0, 0, 0, 0)
record_seccomp("request_key", 249, 0, 0, 0, 0)
record_seccomp("keyctl", 250, 0, 0, 0, 0, 0)
record_seccomp(
    "get_robust_list",
    274,
    {peer_pid},
    ctypes.byref(robust_head),
    ctypes.byref(robust_size),
)
record_seccomp("migrate_pages", 256, {peer_pid}, 0, 0, 0)
record_seccomp("move_pages", 279, {peer_pid}, 0, 0, 0, 0, 0)
record_seccomp("perf_event_open", 298, 0, {peer_pid}, -1, -1, 0)
record_seccomp("process_madvise", 440, -1, 0, 0, 0, 0)
record_seccomp("process_mrelease", 448, -1, 0)
print(json.dumps(evidence))
"""
        attack = runner.exec(
            argv=["python", "-c", attack_code],
            workdir="/app/data/users/b/workspace",
        )
        _assert_ok(attack)
        assert json.loads(attack["stdout"]) == {
            "signal": "denied",
            "abstract_socket": "denied",
            "ptrace": "denied",
            "prlimit": "denied",
            "setpriority": "denied",
            "add_key": "denied",
            "request_key": "denied",
            "keyctl": "denied",
            "get_robust_list": "denied",
            "migrate_pages": "denied",
            "move_pages": "denied",
            "perf_event_open": "denied",
            "process_madvise": "denied",
            "process_mrelease": "denied",
        }
        _docker(
            "exec",
            runner.name,
            "python",
            "-c",
            f"import os; os.kill({peer_pid}, 0)",
        )
    finally:
        _docker(
            "exec",
            runner.name,
            "python",
            "-c",
            f"import os,signal; os.kill({peer_pid}, signal.SIGTERM)",
            check=False,
        )

    busy_dir = runner.users / "a" / "busy"
    busy_dir.mkdir()
    busy_dir.chmod(0o777)
    busy_result: dict[str, Any] = {}

    def _run_busy_job() -> None:
        busy_result.update(
            runner.exec(
                argv=[
                    "python",
                    "-c",
                    "from pathlib import Path; import time; "
                    "Path('ready').write_text('1'); time.sleep(2)",
                ],
                workdir="/app/data/users/a/busy",
                timeout_s=5,
            )
        )

    thread = threading.Thread(target=_run_busy_job, daemon=True)
    thread.start()
    busy_ready = busy_dir / "ready"
    deadline = time.monotonic() + 3
    while not busy_ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not busy_ready.exists():
        raise AssertionError("busy job did not start")
    try:
        runner.exec(argv=["/bin/true"])
    except urllib.error.HTTPError as exc:
        assert exc.code == 429
    else:
        raise AssertionError("runner accepted two simultaneous public jobs")
    thread.join(timeout=8)
    assert not thread.is_alive()
    _assert_ok(busy_result)


def _test_bounded_output(runner: _Runner) -> dict[str, Any]:
    code = r"""
import os
chunk_out = b"O" * 65536
chunk_err = b"E" * 65536
for _ in range(4096):
    os.write(1, chunk_out)
    os.write(2, chunk_err)
"""
    before_events = json.loads(
        _docker(
            "exec",
            runner.name,
            "python",
            "-c",
            "import json; from pathlib import Path; "
            "print(json.dumps(dict(line.split() for line in "
            "Path('/sys/fs/cgroup/memory.events').read_text().splitlines())))",
        )
    )
    for _ in range(2):
        result = runner.exec(
            argv=["python", "-c", code],
            timeout_s=30,
            memory_mb=128,
            max_output_chars=2048,
        )
        _assert_ok(result)
        assert len(result["stdout"]) < 2200
        assert len(result["stderr"]) < 2200
        assert "truncated" in result["stdout"] and "truncated" in result["stderr"]
    health = runner.get_health()
    state = json.loads(_docker("inspect", runner.name, "--format", "{{json .State}}"))
    assert state["Running"] is True
    assert state["OOMKilled"] is False
    memory_peak = _docker(
        "exec",
        runner.name,
        "python",
        "-c",
        "from pathlib import Path; print(Path('/sys/fs/cgroup/memory.peak').read_text().strip())",
        check=False,
    )
    after_events = json.loads(
        _docker(
            "exec",
            runner.name,
            "python",
            "-c",
            "import json; from pathlib import Path; "
            "print(json.dumps(dict(line.split() for line in "
            "Path('/sys/fs/cgroup/memory.events').read_text().splitlines())))",
        )
    )
    assert int(after_events["oom"]) == int(before_events["oom"])
    assert int(after_events["oom_kill"]) == int(before_events["oom_kill"])
    peak = int(memory_peak or 0)
    assert 0 < peak < 96 * 1024 * 1024
    return {"health": health["status"], "memory_peak_bytes": peak}


def _test_process_and_scratch_cleanup(runner: _Runner) -> None:
    sticky = runner.exec(
        argv=[
            "python",
            "-c",
            "import os; from pathlib import Path; "
            "p=Path(os.environ['HOME'])/'sealed'; p.mkdir(); "
            "(p/'payload').write_text('x'); "
            "\ntry: p.chmod(0)\nexcept PermissionError: print('metadata-denied')",
        ]
    )
    _assert_ok(sticky)
    assert sticky["stdout"].strip() == "metadata-denied"
    leftovers = json.loads(
        _docker(
            "exec",
            runner.name,
            "python",
            "-c",
            "import json; from pathlib import Path; "
            "print(json.dumps([p.name for p in Path('/tmp').glob('deeptutor-job-*')]))",
        )
    )
    assert leftovers == []

    background_code = r"""
import os, time
from pathlib import Path
pid = os.fork()
if pid == 0:
    Path("background.pid").write_text(str(os.getpid()), encoding="utf-8")
    os.close(0)
    os.close(1)
    os.close(2)
    time.sleep(60)
    os._exit(0)
while not Path("background.pid").exists():
    time.sleep(0.01)
print(pid)
"""
    result = runner.exec(argv=["python", "-c", background_code])
    _assert_ok(result)
    child_pid = int((runner.user_a / "background.pid").read_text(encoding="utf-8"))
    _docker(
        "exec",
        runner.name,
        "python",
        "-c",
        f"import os,sys; sys.exit(1 if os.path.exists('/proc/{child_pid}') else 0)",
    )
    zombie_count = int(
        _docker(
            "exec",
            runner.name,
            "python",
            "-c",
            "from pathlib import Path; "
            "print(sum('State:\\tZ' in p.read_text(errors='ignore') "
            "for p in Path('/proc').glob('[0-9]*/status')))",
        )
    )
    assert zombie_count == 0


def verify(image: str, require_abi: int) -> dict[str, Any]:
    with _Runner(image, require_abi) as runner:
        health = _test_protocol_and_health(runner)
        _test_credentials(runner)
        _test_filesystem_and_control_plane(runner)
        _test_directory_swap(runner)
        _test_peer_ipc(runner)
        _test_process_and_scratch_cleanup(runner)
        output = _test_bounded_output(runner)
        return {
            "image": image,
            "security_profile": health["security_profile"],
            "landlock_abi": health["landlock_abi"],
            "landlock_errata": health["landlock_errata"],
            "gates": {
                "directory_fd_pin": "passed",
                "credential_drop": "passed",
                "landlock_tcp_abstract_signal": "passed",
                "bounded_streaming": "passed",
                "authenticated_broker_and_peer_isolation": "passed",
                "sibling_content_xattr_and_metadata_mutation": "passed",
            },
            "not_covered": [
                "per-job aggregate memory/cpu/pid isolation",
                "per-job or per-workspace storage quota",
                "native arm64 runtime",
            ],
            "memory_peak_bytes": output["memory_peak_bytes"],
        }


def main() -> int:
    if not __debug__:
        print("runner isolation verification requires assertions", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--require-landlock-abi", type=int, default=6)
    args = parser.parse_args()
    try:
        result = verify(args.image, args.require_landlock_abi)
    except Exception as exc:  # noqa: BLE001 - command-line gate
        print(f"runner isolation verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
