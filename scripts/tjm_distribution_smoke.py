#!/usr/bin/env python3
"""Exercise packaged TJM state across restart and backup/restore boundaries."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from typing import Iterator
from urllib import error as urlerror
from urllib import request as urlrequest
import uuid

EXAM_ID = "distribution-smoke-general-choice"
QUESTION_STABLE_ID = f"{EXAM_ID}-q-001"
_LAUNCHER_ENV_KEYS_TO_CLEAR = frozenset({"DEEPTUTOR_HOME", "PYTHONPATH"})
_DOCKER_TEARDOWN_TIMEOUT_SECONDS = 15.0
_DOCKER_MISSING_MARKERS = ("no such container", "no such object")


def _launcher_env() -> dict[str, str]:
    """Return a host environment without settings that can alter this smoke."""
    env = os.environ.copy()
    for key in _LAUNCHER_ENV_KEYS_TO_CLEAR:
        env.pop(key, None)
    env["DEEPTUTOR_IGNORE_PROCESS_ENV_OVERRIDES"] = "true"
    return env


def _free_ports(count: int) -> tuple[int, ...]:
    sockets: list[socket.socket] = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return tuple(int(sock.getsockname()[1]) for sock in sockets)
    finally:
        for sock in sockets:
            sock.close()


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: object | None = None,
    body: bytes | None = None,
    content_type: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, bytes]:
    headers = {"Accept": "application/json", "Origin": base_url}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        content_type = "application/json"
    if content_type:
        headers["Content-Type"] = content_type
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = urlrequest.Request(f"{base_url}{path}", data=body, headers=headers, method=method)
    try:
        with urlrequest.urlopen(request, timeout=15) as response:
            return int(response.status), response.read()
    except urlerror.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed ({exc.code}): {response_body}") from exc


def _json_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: object | None = None,
    idempotency_key: str | None = None,
) -> object:
    status, body = _request(
        base_url,
        path,
        method=method,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    if not 200 <= status < 300:
        raise RuntimeError(f"{method} {path} returned unexpected HTTP {status}")
    return json.loads(body)


def _multipart_import(base_url: str, questions: list[dict[str, object]]) -> object:
    boundary = f"----deeptutor-{uuid.uuid4().hex}"
    chunks = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="import_format"\r\n\r\njson\r\n'.encode(),
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="questions.json"\r\n'
            "Content-Type: application/json\r\n\r\n"
        ).encode(),
        json.dumps(questions, ensure_ascii=False, separators=(",", ":")).encode(),
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    status, response = _request(
        base_url,
        "/api/v1/tjm/imports",
        method="POST",
        body=b"".join(chunks),
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    if not 200 <= status < 300:
        raise RuntimeError(f"question import returned unexpected HTTP {status}")
    return json.loads(response)


def _wait_ready(
    url: str,
    *,
    timeout: float = 120,
    process: subprocess.Popen[str] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process is not None and (return_code := process.poll()) is not None:
            raise RuntimeError(f"launcher exited with status {return_code} before {url} was ready")
        try:
            with urlrequest.urlopen(url, timeout=2) as response:
                if 200 <= response.status < 500:
                    return
        except (OSError, urlerror.URLError) as exc:
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"service did not become ready at {url}: {last_error}")


def _wait_unavailable(url: str, *, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlrequest.urlopen(url, timeout=1):
                pass
        except urlerror.HTTPError:
            pass
        except (OSError, urlerror.URLError):
            return
        time.sleep(0.25)
    raise RuntimeError(f"service remained available after launcher shutdown: {url}")


def _verify_surfaces(api_base: str, frontend_base: str) -> None:
    openapi = _json_request(api_base, "/openapi.json")
    if not isinstance(openapi, dict) or not isinstance(openapi.get("paths"), dict):
        raise RuntimeError("OpenAPI payload has no paths object")
    tjm_paths = [path for path in openapi["paths"] if path.startswith("/api/v1/tjm")]
    if len(tjm_paths) < 28:
        raise RuntimeError(f"expected at least 28 TJM API paths, found {len(tjm_paths)}")

    status, page = _request(frontend_base, "/tjm")
    if status != 200 or b"TJM" not in page:
        raise RuntimeError("packaged /tjm page is unavailable")
    status, notice = _request(frontend_base, "/vad/THIRD_PARTY_NOTICES.md")
    required_notices = (b"ricky0123", b"Silero Team", b"Microsoft Corporation")
    if status != 200 or any(value not in notice for value in required_notices):
        raise RuntimeError("browser voice third-party notices are incomplete")
    proxy_exams = _json_request(frontend_base, "/api/v1/tjm/exams")
    if not isinstance(proxy_exams, dict) or not isinstance(proxy_exams.get("exams"), list):
        raise RuntimeError("frontend TJM API proxy is unavailable")


def _seed_attempt(api_base: str) -> str:
    exam = {
        "id": EXAM_ID,
        "title": "Distribution smoke general-choice exam",
        "description": "Synthetic distribution fixture; not a real examination question.",
        "duration_seconds": 300,
        "question_count": 1,
        "official_passing_score": 1,
        "official_passing_score_source": {
            "title": "Synthetic distribution fixture",
            "publisher": "DeepTutor tests",
            "published_at": "2026-08-03",
        },
        "blueprint": {"general": 1},
    }
    question = {
        "exam_id": EXAM_ID,
        "stable_id": QUESTION_STABLE_ID,
        "stem": "Synthetic question: choose option 2.",
        "options": [
            {"key": "1", "text": "Synthetic incorrect choice"},
            {"key": "2", "text": "Synthetic correct choice"},
            {"key": "3", "text": "Synthetic incorrect choice"},
            {"key": "4", "text": "Synthetic incorrect choice"},
        ],
        "correct_option_key": "2",
        "area": "general",
        "explanation": "The synthetic fixture defines option 2 as correct.",
        "hints": ["This hint belongs only to the synthetic fixture."],
        "source": {"license": "synthetic-test-fixture", "generated": True},
    }
    _json_request(api_base, "/api/v1/tjm/exams", method="POST", payload=exam)
    _multipart_import(api_base, [question])
    drafts = _json_request(api_base, "/api/v1/tjm/review/questions?status=draft")
    if not isinstance(drafts, dict) or not isinstance(drafts.get("questions"), list):
        raise RuntimeError("draft review queue is malformed")
    version_ids = [
        row["id"]
        for row in drafts["questions"]
        if isinstance(row, dict) and row.get("exam_id") == EXAM_ID
    ]
    if len(version_ids) != 1:
        raise RuntimeError(f"expected one imported draft, found {len(version_ids)}")
    version_id = str(version_ids[0])
    _json_request(
        api_base,
        f"/api/v1/tjm/review/questions/{version_id}/review",
        method="POST",
        payload={"note": "Synthetic human-review smoke"},
    )
    _json_request(
        api_base,
        f"/api/v1/tjm/review/questions/{version_id}/publish",
        method="POST",
    )
    _json_request(api_base, f"/api/v1/tjm/exams/{EXAM_ID}/activate", method="POST")
    attempt = _json_request(
        api_base,
        "/api/v1/tjm/attempts",
        method="POST",
        payload={"exam_id": EXAM_ID, "mode": "exam"},
        idempotency_key="distribution-start-1",
    )
    if not isinstance(attempt, dict) or not isinstance(attempt.get("id"), str):
        raise RuntimeError("attempt start response is malformed")
    attempt_id = attempt["id"]
    _json_request(api_base, f"/api/v1/tjm/attempts/{attempt_id}/items/0/open", method="POST")
    _json_request(
        api_base,
        f"/api/v1/tjm/attempts/{attempt_id}/answers",
        method="POST",
        payload={
            "position": 0,
            "selected_option_key": "2",
            "confidence": 80,
            "elapsed_ms": 1000,
            "confirmed": True,
            "client_created_at": "2026-08-03T00:00:00Z",
        },
        idempotency_key="distribution-answer-1",
    )
    submitted = _json_request(
        api_base,
        f"/api/v1/tjm/attempts/{attempt_id}/submit",
        method="POST",
        idempotency_key="distribution-submit-1",
    )
    if not isinstance(submitted, dict) or submitted.get("correct_count") != 1:
        raise RuntimeError("deterministic score did not record one correct answer")
    result = submitted.get("result")
    if not isinstance(result, dict) or result.get("score") != 1:
        raise RuntimeError("submitted result does not contain the deterministic score")
    return attempt_id


def _verify_persisted(api_base: str, attempt_id: str) -> None:
    exams = _json_request(api_base, "/api/v1/tjm/exams")
    if not isinstance(exams, dict) or EXAM_ID not in {
        row.get("id") for row in exams.get("exams", []) if isinstance(row, dict)
    }:
        raise RuntimeError("persisted exam is missing after restart or restore")
    history = _json_request(api_base, "/api/v1/tjm/history")
    rows = history.get("attempts", []) if isinstance(history, dict) else []
    match = next(
        (row for row in rows if isinstance(row, dict) and row.get("id") == attempt_id), None
    )
    if match is None or match.get("status") != "submitted" or match.get("correct_count") != 1:
        raise RuntimeError("submitted attempt history is missing after restart or restore")


def _write_runtime_ports(home: Path, backend_port: int, frontend_port: int) -> None:
    settings = home / "data" / "user" / "settings"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "system.json").write_text(
        json.dumps(
            {"version": 1, "backend_port": backend_port, "frontend_port": frontend_port},
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


@contextmanager
def _launcher_process(
    executable: Path, home: Path, read_only_cwd: Path
) -> Iterator[tuple[str, str]]:
    backend_port, frontend_port = _free_ports(2)
    _write_runtime_ports(home, backend_port, frontend_port)
    log_path = home.parent / f"launcher-{uuid.uuid4().hex}.log"
    env = _launcher_env()
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [str(executable), "start", "--home", str(home)],
            cwd=read_only_cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    api_base = f"http://127.0.0.1:{backend_port}"
    frontend_base = f"http://127.0.0.1:{frontend_port}"
    try:
        _wait_ready(f"{api_base}/", process=process)
        _wait_ready(f"{frontend_base}/tjm", process=process)
        yield api_base, frontend_base
    except Exception as exc:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-8000:]
        raise RuntimeError(f"packaged launcher smoke failed: {exc}\n{tail}") from exc
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
        _wait_unavailable(f"{api_base}/")
        _wait_unavailable(f"{frontend_base}/tjm")


def _run_launcher_smoke(executable: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="deeptutor-wheel-smoke-") as raw:
        root = Path(raw)
        home = root / "runtime-home"
        read_only_cwd = root / "read-only-cwd"
        home.mkdir()
        read_only_cwd.mkdir()
        read_only_cwd.chmod(0o555)
        try:
            print("launcher smoke: initial packaged startup", flush=True)
            with _launcher_process(executable, home, read_only_cwd) as (api, frontend):
                _verify_surfaces(api, frontend)
                attempt_id = _seed_attempt(api)
                _verify_persisted(api, attempt_id)
            if (read_only_cwd / "data").exists():
                raise RuntimeError("launcher wrote runtime data into its read-only cwd")

            backup_data = root / "backup-data"
            shutil.copytree(home / "data", backup_data)
            print("launcher smoke: restart persisted state", flush=True)
            with _launcher_process(executable, home, read_only_cwd) as (api, _):
                _verify_persisted(api, attempt_id)

            restored_home = root / "restored-home"
            restored_home.mkdir()
            shutil.copytree(backup_data, restored_home / "data")
            print("launcher smoke: restore backup into a new home", flush=True)
            with _launcher_process(executable, restored_home, read_only_cwd) as (api, _):
                _verify_persisted(api, attempt_id)
        finally:
            read_only_cwd.chmod(0o755)


def _docker_container_state(name: str) -> dict[str, object] | None:
    result = subprocess.run(
        ["docker", "container", "inspect", "--format", "{{json .State}}", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if any(marker in detail.casefold() for marker in _DOCKER_MISSING_MARKERS):
            return None
        raise RuntimeError(
            f"docker inspect failed for {name} ({result.returncode}): {detail or 'no output'}"
        )
    try:
        state = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"docker inspect returned invalid state for {name}: {result.stdout!r}"
        ) from exc
    if not isinstance(state, dict):
        raise RuntimeError(f"docker inspect returned a non-object state for {name}: {state!r}")
    return state


def _wait_docker_container_removed(name: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_state: dict[str, object] | None = None
    while True:
        last_state = _docker_container_state(name)
        if last_state is None:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    status = last_state.get("Status", "unknown")
    raise RuntimeError(f"container {name} remained inspectable after docker stop (status {status})")


def _teardown_docker_container(name: str, api_base: str, frontend_base: str) -> None:
    failures: list[str] = []
    try:
        state = _docker_container_state(name)
    except Exception as exc:
        failures.append(f"pre-stop state could not be verified: {exc}")
    else:
        if state is None:
            failures.append(f"container {name} disappeared before teardown")
        elif state.get("Running") is not True:
            status = state.get("Status", "unknown")
            exit_code = state.get("ExitCode", "unknown")
            failures.append(
                f"container {name} exited before teardown (status {status}, exit code {exit_code})"
            )

    try:
        stopped = subprocess.run(
            ["docker", "stop", "--time", "15", name],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        failures.append(f"docker stop could not be executed for {name}: {exc}")
    else:
        if stopped.returncode != 0:
            detail = (stopped.stderr or stopped.stdout).strip()
            failures.append(
                f"docker stop failed for {name} ({stopped.returncode}): {detail or 'no output'}"
            )

    try:
        _wait_docker_container_removed(name, timeout=_DOCKER_TEARDOWN_TIMEOUT_SECONDS)
    except Exception as exc:
        failures.append(str(exc))

    for url in (f"{api_base}/", f"{frontend_base}/tjm"):
        try:
            _wait_unavailable(url)
        except Exception as exc:
            failures.append(f"published port remained available: {exc}")

    if failures:
        raise RuntimeError("Docker teardown verification failed:\n- " + "\n- ".join(failures))


@contextmanager
def _docker_container(image: str, data_dir: Path) -> Iterator[tuple[str, str]]:
    backend_port, frontend_port = _free_ports(2)
    name = f"deeptutor-tjm-smoke-{uuid.uuid4().hex[:12]}"
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.chmod(0o777)
    command = [
        "docker",
        "run",
        "--detach",
        "--rm",
        "--name",
        name,
        "--publish",
        f"127.0.0.1:{backend_port}:8001",
        "--publish",
        f"127.0.0.1:{frontend_port}:3782",
        "--volume",
        f"{data_dir.resolve()}:/app/data",
        image,
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    api_base = f"http://127.0.0.1:{backend_port}"
    frontend_base = f"http://127.0.0.1:{frontend_port}"
    try:
        _wait_ready(f"{api_base}/", timeout=180)
        _wait_ready(f"{frontend_base}/tjm", timeout=180)
        yield api_base, frontend_base
    except Exception as exc:
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True, check=False)
        raise RuntimeError(f"Docker TJM smoke failed: {exc}\n{logs.stdout}\n{logs.stderr}") from exc
    finally:
        _teardown_docker_container(name, api_base, frontend_base)


def _assert_docker_web_portable(image: str) -> None:
    script = """
from pathlib import Path
import re
root = Path('/app/web')
native = re.compile(r'(?:\\.node|\\.dll|\\.dylib|\\.exe|\\.so(?:\\.\\d+)*)$', re.I)
bad = [str(path) for path in root.rglob('*') if path.is_file() and native.search(path.name)]
if bad:
    raise SystemExit('native Web payloads: ' + ', '.join(bad[:20]))
sharp = []
for path in root.rglob('*'):
    parts = [part.casefold() for part in path.parts]
    for index, part in enumerate(parts):
        if part != 'node_modules' or index + 1 >= len(parts):
            continue
        package = parts[index + 1]
        if package == 'sharp':
            sharp.append(str(path))
            break
        if package == '@img' and index + 2 < len(parts) and parts[index + 2].startswith('sharp'):
            sharp.append(str(path))
            break
if sharp:
    raise SystemExit('Sharp Web payloads: ' + ', '.join(sharp[:20]))
for relative in (
    'public/vad/vad.worklet.bundle.min.js',
    'public/vad/silero_vad_v5.onnx',
    'public/vad/ort-wasm-simd-threaded.mjs',
    'public/vad/ort-wasm-simd-threaded.wasm',
):
    asset = root / relative
    if not asset.is_file() or asset.stat().st_size == 0:
        raise SystemExit(f'missing or empty VAD runtime asset: {relative}')
notice = root / 'public' / 'vad' / 'THIRD_PARTY_NOTICES.md'
text = notice.read_text(encoding='utf-8')
for value in ('ricky0123', 'Silero Team', 'Microsoft Corporation'):
    if value not in text:
        raise SystemExit(f'missing notice: {value}')
"""
    subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python", image, "-c", script],
        check=True,
    )


def _run_docker_filesystem_helper(image: str, volumes: list[str], script: str) -> None:
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "0:0",
        "--entrypoint",
        "/bin/sh",
    ]
    for volume in volumes:
        command.extend(("--volume", volume))
    command.extend((image, "-ceu", script))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"Docker filesystem helper failed ({result.returncode}): {detail or 'no output'}"
        )


def _docker_copy_tree(image: str, source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    _run_docker_filesystem_helper(
        image,
        [
            f"{source.resolve()}:/source:ro",
            f"{destination.resolve()}:/destination",
        ],
        "cp -a /source/. /destination/",
    )


def _reclaim_docker_tree(image: str, root: Path) -> None:
    if not root.exists():
        return
    _run_docker_filesystem_helper(
        image,
        [f"{root.resolve()}:/smoke"],
        f"chown -R {os.getuid()}:{os.getgid()} /smoke && chmod -R u+rwX /smoke",
    )


def _run_docker_smoke(image: str) -> None:
    _assert_docker_web_portable(image)
    with tempfile.TemporaryDirectory(
        prefix="deeptutor-docker-smoke-", ignore_cleanup_errors=True
    ) as raw:
        root = Path(raw)
        try:
            original_data = root / "original-data"
            print("docker smoke: initial container startup", flush=True)
            with _docker_container(image, original_data) as (api, frontend):
                _verify_surfaces(api, frontend)
                attempt_id = _seed_attempt(api)
                _verify_persisted(api, attempt_id)

            backup_data = root / "backup-data"
            _docker_copy_tree(image, original_data, backup_data)
            print("docker smoke: restart persisted state", flush=True)
            with _docker_container(image, original_data) as (api, _):
                _verify_persisted(api, attempt_id)

            restored_data = root / "restored-data"
            _docker_copy_tree(image, backup_data, restored_data)
            print("docker smoke: restore backup into a new volume", flush=True)
            with _docker_container(image, restored_data) as (api, _):
                _verify_persisted(api, attempt_id)
        except BaseException as smoke_error:
            try:
                _reclaim_docker_tree(image, root)
            except BaseException as reclaim_error:
                raise BaseExceptionGroup(
                    "Docker smoke and temporary-data cleanup both failed",
                    [smoke_error, reclaim_error],
                ) from None
            raise
        else:
            _reclaim_docker_tree(image, root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    launcher = subparsers.add_parser("launcher", help="Smoke an installed deeptutor CLI")
    launcher.add_argument("--deeptutor", type=Path, required=True)
    docker = subparsers.add_parser("docker", help="Smoke a local Docker image")
    docker.add_argument("--image", required=True)
    args = parser.parse_args()
    if args.mode == "launcher":
        _run_launcher_smoke(args.deeptutor.resolve())
    else:
        _run_docker_smoke(args.image)
    print(f"TJM {args.mode} distribution smoke passed.")


if __name__ == "__main__":
    main()
