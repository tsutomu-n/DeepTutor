from contextlib import ExitStack, contextmanager
import importlib.util
from pathlib import Path
import socket

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "tjm_distribution_smoke.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("tjm_distribution_smoke_under_test", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_free_ports_returns_distinct_bindable_ports() -> None:
    module = _load_module()

    ports = module._free_ports(2)

    assert len(ports) == 2
    assert len(set(ports)) == 2
    assert all(0 < port <= 65_535 for port in ports)
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in ports]
        for sock, port in zip(sockets, ports, strict=True):
            sock.bind(("127.0.0.1", port))


def test_launcher_env_removes_inherited_runtime_overrides(monkeypatch) -> None:
    module = _load_module()
    runtime_override_keys = {
        "BACKEND_PORT",
        "FRONTEND_PORT",
        "NEXT_PUBLIC_API_BASE_EXTERNAL",
        "NEXT_PUBLIC_API_BASE",
        "AUTH_ENABLED",
        "NEXT_PUBLIC_AUTH_ENABLED",
    }
    for key in runtime_override_keys | {"DEEPTUTOR_HOME", "PYTHONPATH"}:
        monkeypatch.setenv(key, "hostile-inherited-value")
    monkeypatch.setenv("PATH", "/test/bin")

    env = module._launcher_env()

    assert "DEEPTUTOR_HOME" not in env
    assert "PYTHONPATH" not in env
    assert env["DEEPTUTOR_IGNORE_PROCESS_ENV_OVERRIDES"] == "true"
    assert all(env[key] == "hostile-inherited-value" for key in runtime_override_keys)
    assert env["PATH"] == "/test/bin"


def test_wait_ready_fails_immediately_when_launcher_exits() -> None:
    module = _load_module()

    class ExitedProcess:
        def poll(self) -> int:
            return 17

    with pytest.raises(RuntimeError, match="launcher exited with status 17"):
        module._wait_ready(
            "http://127.0.0.1:1/",
            timeout=60,
            process=ExitedProcess(),
        )


def test_wait_unavailable_fails_closed_when_timeout_expires() -> None:
    module = _load_module()

    with pytest.raises(RuntimeError, match="service remained available"):
        module._wait_unavailable("http://127.0.0.1:1/", timeout=0)


def test_verify_surfaces_checks_frontend_api_proxy(monkeypatch) -> None:
    module = _load_module()
    json_calls: list[tuple[str, str]] = []

    def fake_json_request(base_url: str, path: str, **_kwargs):
        json_calls.append((base_url, path))
        if path == "/openapi.json":
            return {"paths": {f"/api/v1/tjm/test-{index}": {} for index in range(28)}}
        return {"exams": []}

    def fake_request(_base_url: str, path: str, **_kwargs):
        if path == "/tjm":
            return 200, b"TJM"
        return 200, b"ricky0123 Silero Team Microsoft Corporation"

    monkeypatch.setattr(module, "_json_request", fake_json_request)
    monkeypatch.setattr(module, "_request", fake_request)

    module._verify_surfaces("http://api", "http://frontend")

    assert ("http://frontend", "/api/v1/tjm/exams") in json_calls


def test_docker_teardown_fails_when_container_already_exited(monkeypatch) -> None:
    module = _load_module()
    calls: list[list[str]] = []
    inspect_count = 0

    def fake_run(command, **_kwargs):
        nonlocal inspect_count
        calls.append(command)
        if command[:3] == ["docker", "container", "inspect"]:
            inspect_count += 1
            if inspect_count == 1:
                return module.subprocess.CompletedProcess(
                    command,
                    0,
                    stdout='{"Status":"exited","Running":false,"ExitCode":23}\n',
                    stderr="",
                )
            return module.subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="Error: No such container: smoke-container",
            )
        return module.subprocess.CompletedProcess(command, 0, stdout="smoke-container\n", stderr="")

    unavailable: list[str] = []
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_wait_unavailable", lambda url: unavailable.append(url))

    with pytest.raises(RuntimeError, match=r"exited before teardown.*exit code 23"):
        module._teardown_docker_container(
            "smoke-container",
            "http://127.0.0.1:41001",
            "http://127.0.0.1:41002",
        )

    assert ["docker", "stop", "--time", "15", "smoke-container"] in calls
    assert unavailable == ["http://127.0.0.1:41001/", "http://127.0.0.1:41002/tjm"]


def test_docker_teardown_fails_when_stop_command_fails(monkeypatch) -> None:
    module = _load_module()
    inspect_count = 0

    def fake_run(command, **_kwargs):
        nonlocal inspect_count
        if command[:3] == ["docker", "container", "inspect"]:
            inspect_count += 1
            if inspect_count == 1:
                return module.subprocess.CompletedProcess(
                    command,
                    0,
                    stdout='{"Status":"running","Running":true,"ExitCode":0}\n',
                    stderr="",
                )
            return module.subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="Error: No such container: smoke-container",
            )
        return module.subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="daemon refused to stop the container",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_wait_unavailable", lambda _url: None)

    with pytest.raises(
        RuntimeError,
        match=r"docker stop failed.*daemon refused to stop the container",
    ):
        module._teardown_docker_container(
            "smoke-container",
            "http://127.0.0.1:41001",
            "http://127.0.0.1:41002",
        )


def test_docker_teardown_checks_both_published_ports(monkeypatch) -> None:
    module = _load_module()
    inspect_count = 0

    def fake_run(command, **_kwargs):
        nonlocal inspect_count
        if command[:3] == ["docker", "container", "inspect"]:
            inspect_count += 1
            if inspect_count == 1:
                return module.subprocess.CompletedProcess(
                    command,
                    0,
                    stdout='{"Status":"running","Running":true,"ExitCode":0}\n',
                    stderr="",
                )
            return module.subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="Error: No such container: smoke-container",
            )
        return module.subprocess.CompletedProcess(command, 0, stdout="smoke-container\n", stderr="")

    unavailable: list[str] = []

    def fake_wait_unavailable(url: str) -> None:
        unavailable.append(url)
        if url.endswith(":41002/tjm"):
            raise RuntimeError(f"service remained available after launcher shutdown: {url}")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_wait_unavailable", fake_wait_unavailable)

    with pytest.raises(RuntimeError, match=r"published port remained available.*41002/tjm"):
        module._teardown_docker_container(
            "smoke-container",
            "http://127.0.0.1:41001",
            "http://127.0.0.1:41002",
        )

    assert unavailable == ["http://127.0.0.1:41001/", "http://127.0.0.1:41002/tjm"]


def test_docker_teardown_fails_when_container_remains_inspectable(monkeypatch) -> None:
    module = _load_module()

    def fake_run(command, **_kwargs):
        if command[:3] == ["docker", "container", "inspect"]:
            return module.subprocess.CompletedProcess(
                command,
                0,
                stdout='{"Status":"running","Running":true,"ExitCode":0}\n',
                stderr="",
            )
        return module.subprocess.CompletedProcess(command, 0, stdout="smoke-container\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_wait_unavailable", lambda _url: None)
    monkeypatch.setattr(module, "_DOCKER_TEARDOWN_TIMEOUT_SECONDS", 0)

    with pytest.raises(RuntimeError, match=r"remained inspectable after docker stop"):
        module._teardown_docker_container(
            "smoke-container",
            "http://127.0.0.1:41001",
            "http://127.0.0.1:41002",
        )


def test_docker_container_always_runs_verified_teardown(monkeypatch, tmp_path) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_free_ports", lambda _count: (41001, 41002))
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: module.subprocess.CompletedProcess(
            command, 0, stdout="container-id\n", stderr=""
        ),
    )
    monkeypatch.setattr(module, "_wait_ready", lambda *_args, **_kwargs: None)
    teardown_calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        module,
        "_teardown_docker_container",
        lambda name, api, frontend: teardown_calls.append((name, api, frontend)),
    )

    with module._docker_container("deeptutor:test", tmp_path / "data") as endpoints:
        assert endpoints == ("http://127.0.0.1:41001", "http://127.0.0.1:41002")

    assert len(teardown_calls) == 1
    name, api, frontend = teardown_calls[0]
    assert name.startswith("deeptutor-tjm-smoke-")
    assert api == "http://127.0.0.1:41001"
    assert frontend == "http://127.0.0.1:41002"


def test_docker_copy_tree_uses_offline_root_helper(monkeypatch, tmp_path) -> None:
    module = _load_module()
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return module.subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module._docker_copy_tree("deeptutor:test", source, destination)

    assert destination.is_dir()
    assert calls == [
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--entrypoint",
            "/bin/sh",
            "--volume",
            f"{source.resolve()}:/source:ro",
            "--volume",
            f"{destination.resolve()}:/destination",
            "deeptutor:test",
            "-ceu",
            "cp -a /source/. /destination/",
        ]
    ]


def test_docker_copy_tree_fails_closed_when_helper_fails(monkeypatch, tmp_path) -> None:
    module = _load_module()
    source = tmp_path / "source"
    source.mkdir()

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: module.subprocess.CompletedProcess(
            command,
            23,
            stdout="",
            stderr="permission denied",
        ),
    )

    with pytest.raises(RuntimeError, match=r"Docker filesystem helper failed.*permission denied"):
        module._docker_copy_tree("deeptutor:test", source, tmp_path / "destination")


def test_docker_smoke_uses_container_copy_and_reclaims_temp_tree(monkeypatch, tmp_path) -> None:
    module = _load_module()
    copy_calls: list[tuple[str, Path, Path]] = []
    reclaim_calls: list[tuple[str, Path]] = []

    class FakeTemporaryDirectory:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self) -> str:
            return str(tmp_path)

        def __exit__(self, *_args) -> None:
            return None

    @contextmanager
    def fake_docker_container(_image: str, _data_dir: Path):
        yield "http://api", "http://frontend"

    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", FakeTemporaryDirectory)
    monkeypatch.setattr(module, "_assert_docker_web_portable", lambda _image: None)
    monkeypatch.setattr(module, "_docker_container", fake_docker_container)
    monkeypatch.setattr(module, "_verify_surfaces", lambda *_args: None)
    monkeypatch.setattr(module, "_seed_attempt", lambda _api: "attempt-1")
    monkeypatch.setattr(module, "_verify_persisted", lambda *_args: None)
    monkeypatch.setattr(
        module,
        "_docker_copy_tree",
        lambda image, source, destination: copy_calls.append((image, source, destination)),
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "_reclaim_docker_tree",
        lambda image, root: reclaim_calls.append((image, root)),
        raising=False,
    )
    monkeypatch.setattr(
        module.shutil,
        "copytree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("host-side copytree must not read container-owned data")
        ),
    )

    module._run_docker_smoke("deeptutor:test")

    assert copy_calls == [
        (
            "deeptutor:test",
            tmp_path / "original-data",
            tmp_path / "backup-data",
        ),
        (
            "deeptutor:test",
            tmp_path / "backup-data",
            tmp_path / "restored-data",
        ),
    ]
    assert reclaim_calls == [("deeptutor:test", tmp_path)]


def test_docker_smoke_preserves_primary_and_reclaim_failures(monkeypatch, tmp_path) -> None:
    module = _load_module()

    class FakeTemporaryDirectory:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self) -> str:
            return str(tmp_path)

        def __exit__(self, *_args) -> None:
            return None

    @contextmanager
    def fake_docker_container(_image: str, _data_dir: Path):
        yield "http://api", "http://frontend"

    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", FakeTemporaryDirectory)
    monkeypatch.setattr(module, "_assert_docker_web_portable", lambda _image: None)
    monkeypatch.setattr(module, "_docker_container", fake_docker_container)
    monkeypatch.setattr(module, "_verify_surfaces", lambda *_args: None)
    monkeypatch.setattr(
        module,
        "_seed_attempt",
        lambda _api: (_ for _ in ()).throw(RuntimeError("seed failed")),
    )
    monkeypatch.setattr(
        module,
        "_reclaim_docker_tree",
        lambda _image, _root: (_ for _ in ()).throw(RuntimeError("reclaim failed")),
    )

    with pytest.raises(BaseExceptionGroup) as exc_info:
        module._run_docker_smoke("deeptutor:test")

    assert [str(exc) for exc in exc_info.value.exceptions] == [
        "seed failed",
        "reclaim failed",
    ]
