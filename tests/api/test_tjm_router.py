from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from deeptutor.api.routers import auth as auth_router
from deeptutor.api.routers import tjm
from deeptutor.api.routers.auth import require_admin, require_auth
from deeptutor.multi_user import paths as multi_user_paths
from deeptutor.services.auth import TokenPayload
from deeptutor.services.path_service import PathService
from deeptutor.tjm.attempts import AttemptService
from deeptutor.tjm.catalog import CatalogService
from deeptutor.tjm.domain import Choice, ExamSpec, QuestionVersionDraft
from deeptutor.tjm.storage import CatalogStore, LearningStore


@pytest.fixture
def catalog(tmp_path: Path) -> CatalogService:
    return CatalogService(CatalogStore(tmp_path / "catalog.db"))


@pytest.fixture
def admin_client(catalog: CatalogService) -> TestClient:
    app = FastAPI()
    app.include_router(tjm.router, prefix="/api/v1/tjm")
    app.dependency_overrides[tjm.get_catalog_service] = lambda: catalog
    app.dependency_overrides[require_admin] = lambda: TokenPayload(
        username="admin", role="admin", user_id="admin-1"
    )
    return TestClient(app)


def _exam_payload() -> dict:
    return {
        "id": "exam-api",
        "title": "API Exam",
        "description": "No fixed exam constants.",
        "duration_seconds": 601,
        "question_count": 1,
        "pass_score": 1,
        "blueprint": {"custom-area": 1},
    }


def _question_payload() -> list[dict]:
    return [
        {
            "exam_id": "exam-api",
            "stable_id": "api-q-001",
            "stem": "Choose the valid statement.",
            "options": [
                {"key": "1", "text": "First"},
                {"key": "2", "text": "Second"},
            ],
            "correct_option_key": "2",
            "area": "custom-area",
            "explanation": "Second is valid.",
            "hints": ["Check the condition."],
            "source": {"license": "test-fixture"},
        }
    ]


def test_admin_import_review_publish_and_retire_workflow(admin_client: TestClient) -> None:
    created_exam = admin_client.post("/api/v1/tjm/exams", json=_exam_payload())
    assert created_exam.status_code == 201

    updated_exam = admin_client.patch(
        "/api/v1/tjm/exams/exam-api",
        json={**_exam_payload(), "title": "Updated API Exam", "duration_seconds": 777},
    )
    assert updated_exam.status_code == 200
    assert updated_exam.json()["title"] == "Updated API Exam"
    assert updated_exam.json()["duration_seconds"] == 777
    assert updated_exam.json()["revision"] == 2

    imported = admin_client.post(
        "/api/v1/tjm/imports",
        data={"import_format": "json"},
        files={
            "file": (
                "questions.json",
                json.dumps(_question_payload()).encode(),
                "application/json",
            )
        },
    )
    assert imported.status_code == 201
    assert imported.json()["imported_rows"] == 1
    batch_id = imported.json()["batch_id"]
    assert admin_client.get(f"/api/v1/tjm/imports/{batch_id}").json()["status"] == "completed"

    review_list = admin_client.get("/api/v1/tjm/review/questions?status=draft")
    assert review_list.status_code == 200
    question = review_list.json()["questions"][0]
    version_id = question["id"]
    assert question["correct_option_key"] == "2"

    not_reviewed = admin_client.post(f"/api/v1/tjm/review/questions/{version_id}/publish")
    assert not_reviewed.status_code == 409

    reviewed = admin_client.post(
        f"/api/v1/tjm/review/questions/{version_id}/review",
        json={"note": "Source and answer checked."},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["reviewed_by"] == "admin-1"
    assert reviewed.json()["review_note"] == "Source and answer checked."
    assert reviewed.json()["reviewed_at"]
    listed_after_review = admin_client.get("/api/v1/tjm/review/questions?status=draft").json()[
        "questions"
    ][0]
    assert listed_after_review["reviewed_by"] == "admin-1"
    published = admin_client.post(f"/api/v1/tjm/review/questions/{version_id}/publish")
    assert published.status_code == 200
    assert published.json()["status"] == "published"

    edit_published = admin_client.patch(
        f"/api/v1/tjm/review/questions/{version_id}",
        json={**_question_payload()[0], "correct_option_key": "1"},
    )
    assert edit_published.status_code == 409

    missing_reason = admin_client.post(f"/api/v1/tjm/review/questions/{version_id}/retire")
    assert missing_reason.status_code == 422
    inferred_supersession = admin_client.post(
        f"/api/v1/tjm/review/questions/{version_id}/retire",
        json={"reason": "superseded", "note": "No replacement was published."},
    )
    assert inferred_supersession.status_code == 422
    retired = admin_client.post(
        f"/api/v1/tjm/review/questions/{version_id}/retire",
        json={"reason": "invalid_content", "note": "The keyed answer is invalid."},
    )
    assert retired.status_code == 200
    assert retired.json()["status"] == "retired"
    assert retired.json()["retirement_reason"] == "invalid_content"


def test_admin_legacy_retirement_classification_requires_explicit_replacement(
    admin_client: TestClient,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def classify(
        version_id: str,
        *,
        replacement_version_id: str,
        actor_id: str,
        note: str,
    ) -> dict:
        captured.update(
            version_id=version_id,
            replacement_version_id=replacement_version_id,
            actor_id=actor_id,
            note=note,
        )
        return {
            "id": version_id,
            "status": "retired",
            "retirement_reason": "superseded",
            "replacement_question_version_id": replacement_version_id,
        }

    monkeypatch.setattr(catalog, "classify_legacy_retirement", classify)

    missing = admin_client.post(
        "/api/v1/tjm/review/questions/legacy-v1/classify-retirement",
        json={"reason": "superseded", "note": "verified"},
    )
    assert missing.status_code == 422
    invalid_reason = admin_client.post(
        "/api/v1/tjm/review/questions/legacy-v1/classify-retirement",
        json={
            "reason": "invalid_content",
            "replacement_question_version_id": "replacement-v2",
        },
    )
    assert invalid_reason.status_code == 422

    response = admin_client.post(
        "/api/v1/tjm/review/questions/legacy-v1/classify-retirement",
        json={
            "reason": "superseded",
            "replacement_question_version_id": "replacement-v2",
            "note": "verified from the publication ledger",
        },
    )
    assert response.status_code == 200
    assert response.json()["retirement_reason"] == "superseded"
    assert captured == {
        "version_id": "legacy-v1",
        "replacement_version_id": "replacement-v2",
        "actor_id": "admin-1",
        "note": "verified from the publication ledger",
    }


def test_admin_must_rereview_current_revision_after_edit(admin_client: TestClient) -> None:
    assert admin_client.post("/api/v1/tjm/exams", json=_exam_payload()).status_code == 201
    imported = admin_client.post(
        "/api/v1/tjm/imports",
        data={"import_format": "json"},
        files={
            "file": (
                "questions.json",
                json.dumps(_question_payload()).encode(),
                "application/json",
            )
        },
    )
    assert imported.status_code == 201
    version_id = admin_client.get("/api/v1/tjm/review/questions?status=draft").json()["questions"][
        0
    ]["id"]

    reviewed = admin_client.post(
        f"/api/v1/tjm/review/questions/{version_id}/review",
        json={"note": "reviewed revision one"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["content_revision"] == 1
    assert reviewed.json()["reviewed_revision"] == 1

    edited_payload = {
        **_question_payload()[0],
        "stem": "Edited after review.",
        "correct_option_key": "1",
    }
    edited = admin_client.patch(f"/api/v1/tjm/review/questions/{version_id}", json=edited_payload)
    assert edited.status_code == 200
    assert edited.json()["content_revision"] == 2
    assert edited.json()["reviewed_by"] is None
    assert edited.json()["reviewed_revision"] is None
    assert edited.json()["review_binding_state"] == "stale"

    stale_publish = admin_client.post(f"/api/v1/tjm/review/questions/{version_id}/publish")
    assert stale_publish.status_code == 409
    assert "current revision must be reviewed" in stale_publish.json()["detail"]

    rereviewed = admin_client.post(
        f"/api/v1/tjm/review/questions/{version_id}/review",
        json={"note": "reviewed revision two"},
    )
    assert rereviewed.status_code == 200
    assert rereviewed.json()["reviewed_revision"] == 2
    published = admin_client.post(f"/api/v1/tjm/review/questions/{version_id}/publish")
    assert published.status_code == 200
    assert published.json()["correct_option_key"] == "1"


def test_invalid_import_returns_422_with_auditable_batch(admin_client: TestClient) -> None:
    assert admin_client.post("/api/v1/tjm/exams", json=_exam_payload()).status_code == 201
    invalid = _question_payload()
    invalid[0]["correct_option_key"] = "missing"

    response = admin_client.post(
        "/api/v1/tjm/imports",
        data={"import_format": "json"},
        files={"file": ("bad.json", json.dumps(invalid).encode(), "application/json")},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "failed"
    assert body["errors"][0]["field"] == "correct_option_key"
    batch = admin_client.get(f"/api/v1/tjm/imports/{body['batch_id']}").json()
    assert batch["status"] == "failed"


def test_reject_keeps_candidate_out_of_published_list(admin_client: TestClient) -> None:
    admin_client.post("/api/v1/tjm/exams", json=_exam_payload())
    admin_client.post(
        "/api/v1/tjm/imports",
        data={"import_format": "json"},
        files={"file": ("q.json", json.dumps(_question_payload()).encode(), "application/json")},
    )
    version_id = admin_client.get("/api/v1/tjm/review/questions?status=draft").json()["questions"][
        0
    ]["id"]

    response = admin_client.post(
        f"/api/v1/tjm/review/questions/{version_id}/reject",
        json={"note": "Citation is insufficient."},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert admin_client.get("/api/v1/tjm/review/questions?status=published").json() == {
        "questions": [],
        "total": 0,
    }


def test_admin_dependency_blocks_catalog_mutation(catalog: CatalogService) -> None:
    app = FastAPI()
    app.include_router(tjm.router, prefix="/api/v1/tjm")
    app.dependency_overrides[tjm.get_catalog_service] = lambda: catalog

    def deny() -> None:
        raise HTTPException(status_code=403, detail="Admin access required")

    app.dependency_overrides[require_admin] = deny
    client = TestClient(app)

    response = client.post("/api/v1/tjm/exams", json=_exam_payload())

    assert response.status_code == 403
    with catalog.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM exam_definitions").fetchone()[0] == 0


def test_main_app_registers_tjm_router() -> None:
    from deeptutor.api.main import app

    paths = set(app.openapi()["paths"])
    assert "/api/v1/tjm/exams" in paths
    assert "/api/v1/tjm/imports" in paths


def _active_catalog(catalog: CatalogService) -> None:
    catalog.create_exam(
        ExamSpec(
            id="exam-learn",
            title="Learning Exam",
            duration_seconds=300,
            question_count=1,
            blueprint={"area": 1},
        ),
        actor_id="admin-1",
    )
    version = catalog.create_question_version(
        QuestionVersionDraft(
            exam_id="exam-learn",
            stable_id="learn-001",
            stem="Pick B.",
            choices=(Choice("A", "Wrong"), Choice("B", "Right")),
            correct_option_key="B",
            area="area",
            explanation="B is the official answer.",
            hints=("The second option is relevant.",),
            source={"license": "test-fixture"},
        ),
        actor_id="admin-1",
    )
    catalog.review_question_version(version["id"], actor_id="reviewer-1")
    catalog.publish_question_version(version["id"], actor_id="admin-1")
    catalog.activate_exam("exam-learn", actor_id="admin-1")


@pytest.fixture
def learner_client(catalog: CatalogService, tmp_path: Path) -> TestClient:
    _active_catalog(catalog)
    attempts = AttemptService(catalog, LearningStore(tmp_path / "learner.db"), owner_id="u_learner")
    app = FastAPI()
    app.include_router(tjm.router, prefix="/api/v1/tjm")
    token = TokenPayload(username="learner", role="user", user_id="u_learner")
    app.dependency_overrides[require_auth] = lambda: token
    app.dependency_overrides[tjm.get_catalog_service] = lambda: catalog
    app.dependency_overrides[tjm.get_attempt_service] = lambda: attempts
    return TestClient(app)


def test_tjm_user_path_failure_never_falls_back_to_admin_learning_db(
    catalog: CatalogService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_paths = PathService(workspace_root=tmp_path / "admin-data")
    admin_learning_db = admin_paths.get_tjm_learning_db()
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", True)
    monkeypatch.setattr(
        auth_router,
        "decode_token",
        lambda _token: TokenPayload(username="alice", role="user", user_id="u_alice"),
    )
    monkeypatch.setattr(
        multi_user_paths,
        "get_current_path_service",
        lambda: (_ for _ in ()).throw(OSError("user workspace is unavailable")),
    )
    monkeypatch.setattr(
        PathService,
        "get_instance",
        classmethod(lambda cls: admin_paths),
    )

    app = FastAPI()
    app.include_router(tjm.router, prefix="/api/v1/tjm")
    app.dependency_overrides[tjm.get_catalog_service] = lambda: catalog
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/tjm/history",
            headers={"Authorization": "Bearer valid-test-token"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "TJM user workspace is unavailable"}
    assert not admin_learning_db.exists()


def test_tjm_attempt_service_rejects_missing_request_user_context(
    catalog: CatalogService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_paths = PathService(workspace_root=tmp_path / "admin-data")
    admin_learning_db = admin_paths.get_tjm_learning_db()
    monkeypatch.setattr(
        PathService,
        "get_instance",
        classmethod(lambda cls: admin_paths),
    )

    app = FastAPI()
    app.include_router(tjm.router, prefix="/api/v1/tjm")
    app.dependency_overrides[require_auth] = lambda: TokenPayload(
        username="alice", role="user", user_id="u_alice"
    )
    app.dependency_overrides[tjm.get_catalog_service] = lambda: catalog
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/tjm/history")

    assert response.status_code == 500
    assert response.json() == {"detail": "TJM user context is unavailable"}
    assert not admin_learning_db.exists()


def test_tjm_attempt_service_uses_authenticated_users_learning_db(
    catalog: CatalogService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users_root = tmp_path / "data" / "users"
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", True)
    monkeypatch.setattr(multi_user_paths, "USERS_ROOT", users_root)
    monkeypatch.setattr(multi_user_paths, "_path_services", {})
    monkeypatch.setattr(
        auth_router,
        "decode_token",
        lambda _token: TokenPayload(username="alice", role="user", user_id="u_alice"),
    )

    app = FastAPI()
    app.include_router(tjm.router, prefix="/api/v1/tjm")
    app.dependency_overrides[tjm.get_catalog_service] = lambda: catalog
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/tjm/history",
            headers={"Authorization": "Bearer valid-test-token"},
        )

    assert response.status_code == 200
    assert (users_root / "u_alice" / "user" / "tjm_learning.db").is_file()


def test_tjm_attempt_service_keeps_auth_disabled_local_admin_mode(
    catalog: CatalogService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_root = tmp_path / "data"
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", False)
    monkeypatch.setattr(multi_user_paths, "ADMIN_WORKSPACE_ROOT", admin_root)
    monkeypatch.setattr(multi_user_paths, "_path_services", {})

    app = FastAPI()
    app.include_router(tjm.router, prefix="/api/v1/tjm")
    app.dependency_overrides[tjm.get_catalog_service] = lambda: catalog
    with TestClient(app) as client:
        response = client.get("/api/v1/tjm/history")

    assert response.status_code == 200
    assert (admin_root / "user" / "tjm_learning.db").is_file()


def test_practice_attempt_api_returns_immediate_deterministic_feedback(
    learner_client: TestClient,
) -> None:
    started = learner_client.post(
        "/api/v1/tjm/attempts", json={"exam_id": "exam-learn", "mode": "practice"}
    )
    assert started.status_code == 201
    attempt_id = started.json()["id"]
    assert learner_client.post(f"/api/v1/tjm/attempts/{attempt_id}/items/0/open").status_code == 200

    hint = learner_client.post(
        f"/api/v1/tjm/attempts/{attempt_id}/items/0/hint", json={"elapsed_ms": 100}
    )
    assert hint.status_code == 200
    answered = learner_client.post(
        f"/api/v1/tjm/attempts/{attempt_id}/answers",
        json={
            "position": 0,
            "selected_option_key": "B",
            "confidence": 90,
            "elapsed_ms": 850,
            "confirmed": True,
        },
    )
    assert answered.status_code == 200
    assert answered.json()["is_correct"] is True
    assert answered.json()["correct_option_key"] == "B"

    submitted = learner_client.post(f"/api/v1/tjm/attempts/{attempt_id}/submit")
    assert submitted.status_code == 200
    history = learner_client.get("/api/v1/tjm/history")
    assert history.status_code == 200
    assert history.json()["attempts"][0]["id"] == attempt_id


def test_start_attempt_api_retry_does_not_create_a_second_attempt(
    learner_client: TestClient,
) -> None:
    payload = {"exam_id": "exam-learn", "mode": "exam"}
    headers = {"Idempotency-Key": "start-attempt-1"}

    first = learner_client.post("/api/v1/tjm/attempts", json=payload, headers=headers)
    replay = learner_client.post("/api/v1/tjm/attempts", json=payload, headers=headers)
    conflict = learner_client.post(
        "/api/v1/tjm/attempts",
        json={**payload, "mode": "practice"},
        headers=headers,
    )

    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    history = learner_client.get("/api/v1/tjm/history").json()["attempts"]
    assert [attempt["id"] for attempt in history] == [first.json()["id"]]


def test_exam_attempt_api_has_no_answer_leak_before_submit(learner_client: TestClient) -> None:
    started = learner_client.post(
        "/api/v1/tjm/attempts", json={"exam_id": "exam-learn", "mode": "exam"}
    )
    assert started.status_code == 201
    assert "correct_option_key" not in json.dumps(started.json())
    assert "official answer" not in json.dumps(started.json())
    attempt_id = started.json()["id"]
    assert learner_client.post(f"/api/v1/tjm/attempts/{attempt_id}/items/0/open").status_code == 200

    answered = learner_client.post(
        f"/api/v1/tjm/attempts/{attempt_id}/answers",
        json={
            "position": 0,
            "selected_option_key": "A",
            "confidence": 40,
            "elapsed_ms": 700,
            "confirmed": True,
        },
    )
    assert answered.status_code == 200
    assert "correct_option_key" not in answered.json()
    assert "explanation" not in answered.json()

    submitted = learner_client.post(f"/api/v1/tjm/attempts/{attempt_id}/submit")
    assert submitted.status_code == 200
    assert submitted.json()["items"][0]["correct_option_key"] == "B"
    assert submitted.json()["items"][0]["is_correct"] is False


def test_active_exam_closes_same_exam_feedback_routes_until_submit(
    learner_client: TestClient,
) -> None:
    practice = learner_client.post(
        "/api/v1/tjm/attempts",
        json={"exam_id": "exam-learn", "mode": "practice"},
    ).json()
    practice_id = practice["id"]
    assert (
        learner_client.post(f"/api/v1/tjm/attempts/{practice_id}/items/0/open").status_code == 200
    )
    feedback = learner_client.post(
        f"/api/v1/tjm/attempts/{practice_id}/answers",
        json={
            "position": 0,
            "selected_option_key": "B",
            "confidence": 90,
            "elapsed_ms": 100,
            "confirmed": True,
        },
    )
    assert feedback.status_code == 200
    assert feedback.json()["correct_option_key"] == "B"

    exam = learner_client.post(
        "/api/v1/tjm/attempts",
        json={"exam_id": "exam-learn", "mode": "exam"},
    )
    assert exam.status_code == 201
    exam_id = exam.json()["id"]

    assert learner_client.get(f"/api/v1/tjm/attempts/{practice_id}").status_code == 409
    assert (
        learner_client.post(
            "/api/v1/tjm/attempts",
            json={"exam_id": "exam-learn", "mode": "practice"},
        ).status_code
        == 409
    )
    history = learner_client.get("/api/v1/tjm/history")
    assert history.status_code == 200
    assert [attempt["id"] for attempt in history.json()["attempts"]] == [exam_id]
    assert "correct_option_key" not in json.dumps(history.json())

    assert learner_client.post(f"/api/v1/tjm/attempts/{exam_id}/submit").status_code == 200
    resumed = learner_client.get(f"/api/v1/tjm/attempts/{practice_id}")
    assert resumed.status_code == 200
    assert resumed.json()["items"][0]["correct_option_key"] == "B"


def test_answer_and_submit_api_retries_are_idempotent_and_conflicts_fail_closed(
    learner_client: TestClient,
) -> None:
    started = learner_client.post(
        "/api/v1/tjm/attempts", json={"exam_id": "exam-learn", "mode": "exam"}
    ).json()
    attempt_id = started["id"]
    opened = learner_client.post(f"/api/v1/tjm/attempts/{attempt_id}/items/0/open")
    assert opened.status_code == 200
    assert opened.json()["first_presented_at"]
    answer_payload = {
        "position": 0,
        "selected_option_key": "A",
        "confidence": 40,
        "elapsed_ms": 700,
        "confirmed": True,
        "client_created_at": "2026-01-01T00:00:00Z",
    }
    headers = {"Idempotency-Key": "api-answer-1"}

    first = learner_client.post(
        f"/api/v1/tjm/attempts/{attempt_id}/answers", json=answer_payload, headers=headers
    )
    replay = learner_client.post(
        f"/api/v1/tjm/attempts/{attempt_id}/answers", json=answer_payload, headers=headers
    )
    conflict = learner_client.post(
        f"/api/v1/tjm/attempts/{attempt_id}/answers",
        json={**answer_payload, "selected_option_key": "B"},
        headers=headers,
    )

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    submitted = learner_client.post(
        f"/api/v1/tjm/attempts/{attempt_id}/submit",
        headers={"Idempotency-Key": "api-submit-1"},
    )
    submit_replay = learner_client.post(
        f"/api/v1/tjm/attempts/{attempt_id}/submit",
        headers={"Idempotency-Key": "api-submit-1"},
    )
    answer_after_submit = learner_client.post(
        f"/api/v1/tjm/attempts/{attempt_id}/answers", json=answer_payload, headers=headers
    )
    assert submitted.status_code == submit_replay.status_code == 200
    assert submit_replay.json() == submitted.json()
    assert answer_after_submit.json() == first.json()
    assert "correct_option_key" not in answer_after_submit.json()


def test_elapsed_value_outside_sqlite_integer_range_is_a_400(
    learner_client: TestClient,
) -> None:
    started = learner_client.post(
        "/api/v1/tjm/attempts", json={"exam_id": "exam-learn", "mode": "practice"}
    ).json()
    learner_client.post(f"/api/v1/tjm/attempts/{started['id']}/items/0/open")

    response = learner_client.post(
        f"/api/v1/tjm/attempts/{started['id']}/answers",
        json={
            "position": 0,
            "selected_option_key": "A",
            "confidence": 50,
            "elapsed_ms": 9_223_372_036_854_775_808,
            "confirmed": True,
        },
    )

    assert response.status_code == 400


def test_voice_answer_api_saves_only_after_candidate_confirmation(
    learner_client: TestClient,
) -> None:
    started = learner_client.post(
        "/api/v1/tjm/attempts", json={"exam_id": "exam-learn", "mode": "exam"}
    ).json()
    assert (
        learner_client.post(f"/api/v1/tjm/attempts/{started['id']}/items/0/open").status_code == 200
    )
    candidate = learner_client.post(
        f"/api/v1/tjm/attempts/{started['id']}/items/0/voice-candidate",
        json={"transcript": "2番", "elapsed_ms": 700},
    )
    assert candidate.status_code == 201
    assert candidate.json()["proposed_option_key"] == "B"
    assert "correct_option_key" not in candidate.json()
    before_confirm = learner_client.get(f"/api/v1/tjm/attempts/{started['id']}")
    assert before_confirm.json()["items"][0]["confirmed_option_key"] is None

    confirmed = learner_client.post(
        f"/api/v1/tjm/attempts/{started['id']}/items/0/voice-candidates/"
        f"{candidate.json()['candidate_id']}/confirm",
        json={"confidence": 80, "elapsed_ms": 900},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["confirmed_option_key"] == "B"
    assert "correct_option_key" not in confirmed.json()

    repeated = learner_client.post(
        f"/api/v1/tjm/attempts/{started['id']}/items/0/voice-candidates/"
        f"{candidate.json()['candidate_id']}/confirm",
        json={"confidence": 80, "elapsed_ms": 950},
    )
    assert repeated.status_code == 409


def test_invalidated_question_rejects_answer_and_never_returns_official_grade(
    learner_client: TestClient, catalog: CatalogService
) -> None:
    started = learner_client.post(
        "/api/v1/tjm/attempts", json={"exam_id": "exam-learn", "mode": "exam"}
    ).json()
    version_id = started["items"][0]["question_version_id"]
    assert (
        learner_client.post(f"/api/v1/tjm/attempts/{started['id']}/items/0/open").status_code == 200
    )
    catalog.retire_question_version(
        version_id,
        actor_id="admin-1",
        reason="invalid_content",
        note="Fixture invalidation.",
    )

    answer = learner_client.post(
        f"/api/v1/tjm/attempts/{started['id']}/answers",
        json={
            "position": 0,
            "selected_option_key": "B",
            "confidence": 80,
            "elapsed_ms": 900,
            "confirmed": True,
        },
    )
    assert answer.status_code == 409

    submitted = learner_client.post(f"/api/v1/tjm/attempts/{started['id']}/submit")
    assert submitted.status_code == 200
    assert submitted.json()["total_count"] == 0
    invalid_item = submitted.json()["items"][0]
    assert invalid_item["grading_status"] == "content_invalidated"
    assert "correct_option_key" not in invalid_item
    assert "is_correct" not in invalid_item


def test_review_queue_review_attempt_and_analytics_api(learner_client: TestClient) -> None:
    started = learner_client.post(
        "/api/v1/tjm/attempts", json={"exam_id": "exam-learn", "mode": "practice"}
    ).json()
    assert (
        learner_client.post(f"/api/v1/tjm/attempts/{started['id']}/items/0/open").status_code == 200
    )
    learner_client.post(
        f"/api/v1/tjm/attempts/{started['id']}/items/0/hint", json={"elapsed_ms": 100}
    )
    learner_client.post(
        f"/api/v1/tjm/attempts/{started['id']}/answers",
        json={
            "position": 0,
            "selected_option_key": "A",
            "confidence": 20,
            "elapsed_ms": 900,
            "confirmed": True,
        },
    )
    learner_client.post(f"/api/v1/tjm/attempts/{started['id']}/submit")

    queue = learner_client.get("/api/v1/tjm/review/queue")
    assert queue.status_code == 200
    assert set(queue.json()["items"][0]["reasons"]) == {
        "incorrect",
        "low_confidence",
        "hint_used",
    }
    review = learner_client.post(
        "/api/v1/tjm/review/attempts", json={"exam_id": "exam-learn", "limit": 10}
    )
    assert review.status_code == 201
    assert review.json()["mode"] == "review"
    assert "correct_option_key" not in review.json()["items"][0]

    analytics = learner_client.get("/api/v1/tjm/analytics")
    assert analytics.status_code == 200
    assert analytics.json()["overall"]["total"] == 1
    assert analytics.json()["overall"]["answered"] == 1
    assert analytics.json()["overall"]["correct"] == 0


def test_regular_attempt_endpoint_rejects_review_mode(learner_client: TestClient) -> None:
    response = learner_client.post(
        "/api/v1/tjm/attempts", json={"exam_id": "exam-learn", "mode": "review"}
    )

    assert response.status_code == 422


def test_learner_exam_listing_excludes_inactive_definitions(
    learner_client: TestClient, catalog: CatalogService
) -> None:
    catalog.create_exam(
        ExamSpec(
            id="draft-hidden",
            title="Draft Hidden",
            duration_seconds=60,
            question_count=1,
        ),
        actor_id="admin-1",
    )

    response = learner_client.get("/api/v1/tjm/exams")

    assert response.status_code == 200
    assert [exam["id"] for exam in response.json()["exams"]] == ["exam-learn"]
