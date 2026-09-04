from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_runner_image_and_compose_keep_broker_worker_boundary() -> None:
    dockerfile = (ROOT / "Dockerfile.runner").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "COPY deeptutor/services/sandbox/runner/worker.py /app/worker.py" in dockerfile
    assert "USER root" in dockerfile
    for capability in ("DAC_OVERRIDE", "FOWNER", "KILL", "SETGID", "SETUID", "SETPCAP"):
        assert f"      - {capability}" in compose
    assert "cap_drop:\n      - ALL" in compose
    assert "no-new-privileges:true" in compose
    assert "landlock-v6-fd-v3" in compose
    assert "landlock_errata" in compose
    assert "seccomp-cross-process-deny" in compose
    assert "seccomp-fs-notify-deny" in compose
    assert "seccomp-path-metadata-deny" in compose
    assert "init: true" in compose
    assert "deeptutor-runner-network:" in compose
    assert "internal: true" in compose
    assert "DEEPTUTOR_RUNNER_TOKEN" in compose
    assert "DEEPTUTOR_SANDBOX_RUNNER_TOKEN" in compose

    verifier = (ROOT / "scripts/verify_runner_p0.py").read_text(encoding="utf-8")
    assert '"seccomp=unconfined"' in verifier

    backend = (ROOT / "deeptutor/services/sandbox/backends.py").read_text(encoding="utf-8")
    assert 'f"{self._base_url}/v3/exec"' in backend
    assert '"Authorization": f"Bearer {self._control_token}"' in backend


def test_ci_runs_the_real_runner_container_regression() -> None:
    workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")

    assert "runner-p0-amd64:" in workflow
    assert "Sandbox Runner Isolation Regression (linux/amd64)" in workflow
    assert "file: ./Dockerfile.runner" in workflow
    assert "scripts/verify_runner_p0.py" in workflow
    assert "needs.runner-p0-amd64.result != 'success'" in workflow
