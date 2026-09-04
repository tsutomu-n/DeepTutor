from __future__ import annotations

import os

import pytest

from deeptutor.services.cli_apps import runner as cli_apps_runner
from deeptutor.services.sandbox.backends import RunnerSidecarBackend
from deeptutor.services.sandbox.runner import server, worker

CONTROL_TOKEN = "test-runner-control-token-" * 2


@pytest.mark.parametrize(
    ("machine", "required"),
    [
        (
            "x86_64",
            {
                41,
                53,
                89,
                90,
                132,
                188,
                235,
                248,
                249,
                250,
                253,
                256,
                267,
                268,
                274,
                279,
                280,
                294,
                298,
                300,
                302,
                425,
                426,
                427,
                440,
                448,
                452,
            },
        ),
        (
            "aarch64",
            {
                5,
                26,
                52,
                53,
                78,
                88,
                100,
                140,
                198,
                199,
                217,
                218,
                219,
                238,
                239,
                241,
                261,
                262,
                425,
                426,
                427,
                440,
                448,
                452,
            },
        ),
    ],
)
def test_job_seccomp_denies_socket_process_control_and_io_uring_bypasses(
    monkeypatch: pytest.MonkeyPatch,
    machine: str,
    required: set[int],
) -> None:
    monkeypatch.setattr(worker.platform, "machine", lambda: machine)

    _, denied = worker._seccomp_syscalls()

    assert required <= denied


def test_workdir_fd_survives_atomic_path_replacement(tmp_path) -> None:
    root = tmp_path / "users"
    original = root / "active"
    sibling = root / "sibling"
    original.mkdir(parents=True)
    sibling.mkdir()
    (original / "sentinel").write_text("original", encoding="utf-8")
    (sibling / "sentinel").write_text("sibling", encoding="utf-8")

    fd, _ = server._pin_directory(str(original), [str(root)])
    before = os.fstat(fd)
    try:
        original.rename(root / "held")
        original.symlink_to(sibling, target_is_directory=True)
        after = os.fstat(fd)
        sentinel_fd = os.open("sentinel", os.O_RDONLY, dir_fd=fd)
        try:
            assert os.read(sentinel_fd, 32) == b"original"
        finally:
            os.close(sentinel_fd)
    finally:
        os.close(fd)

    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)


def test_openat2_rejects_nested_symlink(tmp_path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        server._pin_directory(str(root / "link"), [str(root)])


def test_runner_hard_clamps_caller_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _capture(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
            "error": "",
            "security_profile": server.SECURITY_PROFILE,
        }

    monkeypatch.setattr(server, "_run_worker", _capture)
    result = server.execute(
        {
            "command": "true",
            "limits": {
                "timeout_s": 10**9,
                "memory_mb": 10**9,
                "cpu_seconds": 10**9,
                "max_output_chars": 10**9,
            },
        }
    )

    assert result["error"] == ""
    assert captured["timeout_s"] == server._MAX_TIMEOUT_S
    assert captured["memory_mb"] == server._MAX_MEMORY_MB
    assert captured["cpu_seconds"] == server._MAX_CPU_SECONDS
    assert captured["max_output_chars"] == server._MAX_OUTPUT_CHARS


def test_runner_wall_timeout_matches_cli_app_contract() -> None:
    assert server._MAX_TIMEOUT_S == cli_apps_runner.MAX_TIMEOUT_S


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _Client:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def get(self, _url: str, *, headers: dict[str, str]) -> _Response:
        assert headers == {"Authorization": f"Bearer {CONTROL_TOKEN}"}
        return _Response(self._payload)


@pytest.mark.asyncio
async def test_runner_health_rejects_reachable_legacy_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deeptutor.services.sandbox.backends as backends

    monkeypatch.setattr(backends.httpx, "AsyncClient", lambda **_kwargs: _Client({}))
    healthy, detail = await RunnerSidecarBackend(
        "http://runner:8900", control_token=CONTROL_TOKEN
    ).health()

    assert healthy is False
    assert "attestation mismatch" in detail


@pytest.mark.asyncio
async def test_runner_health_accepts_complete_security_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deeptutor.services.sandbox.backends as backends

    payload: dict[str, object] = {
        "status": "ok",
        "protocol": 3,
        "security_profile": "landlock-v6-fd-v3",
        "landlock_abi": 7,
        "landlock_errata": 7,
        "worker_self_test": True,
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
    monkeypatch.setattr(backends.httpx, "AsyncClient", lambda **_kwargs: _Client(payload))
    healthy, detail = await RunnerSidecarBackend(
        "http://runner:8900", control_token=CONTROL_TOKEN
    ).health()

    assert healthy is True
    assert "Landlock ABI 7" in detail


@pytest.mark.asyncio
async def test_runner_health_rejects_missing_control_token() -> None:
    healthy, detail = await RunnerSidecarBackend("http://runner:8900").health()

    assert healthy is False
    assert "control token" in detail
