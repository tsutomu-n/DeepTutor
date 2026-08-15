from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from deeptutor.api.routers import auth as auth_router
from deeptutor.api.routers import tjm
from deeptutor.api.routers.auth import (
    require_admin,
    require_admin_same_origin,
    require_auth,
)
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
    app.dependency_overrides[require_admin_same_origin] = lambda: TokenPayload(
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
        "official_passing_score": None,
        "official_passing_score_source": None,
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


def test_exam_api_rejects_legacy_pass_score_field(admin_client: TestClient) -> None:
    payload = _exam_payload()
    payload["pass_score"] = 1

    response = admin_client.post("/api/v1/tjm/exams", json=payload)

    assert response.status_code == 422


def test_exam_api_rejects_ids_that_cannot_roundtrip_through_path_routes(
    admin_client: TestClient,
) -> None:
    response = admin_client.post(
        "/api/v1/tjm/exams",
        json={**_exam_payload(), "id": "exam/a"},
    )

    assert response.status_code == 400
    assert "URL-safe ASCII path segment" in response.json()["detail"]
    assert admin_client.get("/api/v1/tjm/exams").json()["exams"] == []


def test_score_apis_reject_boolean_coercion_and_ambiguous_source(
    admin_client: TestClient,
) -> None:
    source = {"title": "Standard", "publisher": "Test board"}

    create_bool = admin_client.post(
        "/api/v1/tjm/exams",
        json={
            **_exam_payload(),
            "official_passing_score": True,
            "official_passing_score_source": source,
        },
    )
    put_bool = admin_client.put(
        "/api/v1/tjm/exams/exam-api/official-passing-score",
        json={
            "official_passing_score": True,
            "official_passing_score_source": source,
        },
    )
    source_without_score = admin_client.put(
        "/api/v1/tjm/exams/exam-api/official-passing-score",
        json={
            "official_passing_score": None,
            "official_passing_score_source": source,
        },
    )
    unsafe_source = admin_client.put(
        "/api/v1/tjm/exams/exam-api/official-passing-score",
        json={
            "official_passing_score": 1,
            "official_passing_score_source": {
                "title": "Standard",
                "publisher": "Test board",
                "url": "javascript:alert(1)",
            },
        },
    )
    empty_official_update = admin_client.put(
        "/api/v1/tjm/exams/exam-api/official-passing-score",
        json={},
    )

    assert create_bool.status_code == 422
    assert put_bool.status_code == 422
    assert source_without_score.status_code == 422
    assert unsafe_source.status_code == 422
    assert empty_official_update.status_code == 422


@pytest.mark.parametrize(
    "override",
    [
        {"duration_seconds": True},
        {"question_count": True},
        {"blueprint": {"custom-area": True}},
    ],
)
def test_exam_api_rejects_boolean_numeric_fields(
    admin_client: TestClient,
    override: dict,
) -> None:
    response = admin_client.post(
        "/api/v1/tjm/exams",
        json={**_exam_payload(), **override},
    )

    assert response.status_code == 422


def test_admin_official_passing_score_route_preserves_source_contract(
    admin_client: TestClient,
    catalog: CatalogService,
) -> None:
    assert admin_client.post("/api/v1/tjm/exams", json=_exam_payload()).status_code == 201
    source = {
        "title": "Published examination standard",
        "publisher": "Test authority",
        "url": "https://example.test/standards/score",
        "published_at": "2026-08-03",
    }

    response = admin_client.put(
        "/api/v1/tjm/exams/exam-api/official-passing-score",
        json={
            "official_passing_score": 1,
            "official_passing_score_source": source,
        },
    )

    assert response.status_code == 200
    assert response.json()["official_passing_score"] == 1
    assert response.json()["official_passing_score_source"] == source
    assert response.json()["revision"] == 2

    with catalog.store.connect() as conn:
        conn.execute("UPDATE exam_definitions SET status = 'active' WHERE id = 'exam-api'")
    active_update = admin_client.put(
        "/api/v1/tjm/exams/exam-api/official-passing-score",
        json={
            "official_passing_score": 0,
            "official_passing_score_source": {
                "title": "Updated standard",
                "publisher": "Test authority",
            },
        },
    )
    assert active_update.status_code == 200
    assert active_update.json()["official_passing_score"] == 0

    with catalog.store.connect() as conn:
        conn.execute("UPDATE exam_definitions SET status = 'retired' WHERE id = 'exam-api'")
    retired_update = admin_client.put(
        "/api/v1/tjm/exams/exam-api/official-passing-score",
        json={
            "official_passing_score": 1,
            "official_passing_score_source": source,
        },
    )
    assert retired_update.status_code == 409


def test_exam_request_persists_official_score_atomically_on_create_and_replace(
    admin_client: TestClient,
) -> None:
    source = {"title": "Standard", "publisher": "Test authority"}
    payload = {
        **_exam_payload(),
        "official_passing_score": 1,
        "official_passing_score_source": source,
    }

    created = admin_client.post("/api/v1/tjm/exams", json=payload)
    replaced = admin_client.patch(
        "/api/v1/tjm/exams/exam-api",
        json={**payload, "title": "Updated title"},
    )

    assert created.status_code == 201
    assert created.json()["revision"] == 1
    assert created.json()["official_passing_score"] == 1
    assert created.json()["official_passing_score_source"] == source
    assert replaced.status_code == 200
    assert replaced.json()["revision"] == 2
    assert replaced.json()["official_passing_score"] == 1


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


def test_exam_preference_api_distinguishes_explicit_null_from_missing_default(
    learner_client: TestClient,
) -> None:
    initial = learner_client.get("/api/v1/tjm/exam-preferences")
    assert initial.status_code == 200
    assert initial.json() == {
        "preferences": [
            {
                "exam_id": "exam-learn",
                "practice_target_score": None,
                "origin": None,
                "updated_at": None,
            }
        ],
        "total": 1,
    }

    configured = learner_client.put(
        "/api/v1/tjm/exam-preferences/exam-learn",
        json={"practice_target_score": 1},
    )
    assert configured.status_code == 200
    assert configured.json()["practice_target_score"] == 1
    assert configured.json()["origin"] == "user"
    assert configured.json()["updated_at"]

    cleared = learner_client.put(
        "/api/v1/tjm/exam-preferences/exam-learn",
        json={"practice_target_score": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["practice_target_score"] is None
    assert cleared.json()["origin"] == "user"
    assert (
        learner_client.get("/api/v1/tjm/exam-preferences").json()["preferences"][0]["origin"]
        == "user"
    )

    too_high = learner_client.put(
        "/api/v1/tjm/exam-preferences/exam-learn",
        json={"practice_target_score": 2},
    )
    assert too_high.status_code == 400
    boolean = learner_client.put(
        "/api/v1/tjm/exam-preferences/exam-learn",
        json={"practice_target_score": True},
    )
    assert boolean.status_code == 422
    missing_field = learner_client.put(
        "/api/v1/tjm/exam-preferences/exam-learn",
        json={},
    )
    assert missing_field.status_code == 422


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
    assert started.json()["result"] is None
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


def test_tjm_admin_cookie_mutations_require_one_allowed_origin(
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed_origin = "https://learn.example.com"
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", True)
    monkeypatch.setattr(
        auth_router,
        "decode_token",
        lambda token: (
            TokenPayload(username="admin", role="admin", user_id="admin-1")
            if token == "cookie-admin"
            else None
        ),
    )
    monkeypatch.setattr(
        auth_router,
        "load_system_settings",
        lambda: {
            "frontend_port": 3782,
            "cors_origin": "",
            "cors_origins": ["*", allowed_origin],
        },
        raising=False,
    )
    app = FastAPI()
    app.include_router(tjm.router, prefix="/api/v1/tjm")
    app.dependency_overrides[tjm.get_catalog_service] = lambda: catalog

    with TestClient(app) as client:
        client.cookies.set("dt_token", "cookie-admin")
        for headers in (
            None,
            {"Origin": "null"},
            {"Origin": "https://evil.example.com"},
            {"Origin": "https://learn.example.com.evil"},
            {"Origin": "https://learn.example.com/path"},
            {"Origin": "https://learn.example.com/"},
            {"Origin": "http://learn.example.com"},
        ):
            response = client.post(
                "/api/v1/tjm/exams",
                json=_exam_payload(),
                headers=headers,
            )
            assert response.status_code == 403
        duplicate = client.post(
            "/api/v1/tjm/exams",
            json=_exam_payload(),
            headers=[("Origin", allowed_origin), ("Origin", allowed_origin)],
        )
        assert duplicate.status_code == 403
        assert catalog.list_exams() == []
        admin_get = client.get("/api/v1/tjm/review/questions")
        assert admin_get.status_code == 200

        created = client.post(
            "/api/v1/tjm/exams",
            json=_exam_payload(),
            headers={"Origin": allowed_origin},
        )
        assert created.status_code == 201
        patched = client.patch(
            "/api/v1/tjm/exams/exam-api",
            json={**_exam_payload(), "title": "Origin checked"},
            headers={"Origin": allowed_origin},
        )
        assert patched.status_code == 200
        imported = client.post(
            "/api/v1/tjm/imports",
            data={"import_format": "json"},
            files={
                "file": (
                    "questions.json",
                    json.dumps(_question_payload()).encode(),
                    "application/json",
                )
            },
            headers={"Origin": allowed_origin},
        )
        assert imported.status_code == 201
        version_id = catalog.list_question_versions(status="draft")[0]["id"]
        reviewed = client.post(
            f"/api/v1/tjm/review/questions/{version_id}/review",
            json={"note": "origin checked"},
            headers={"Origin": allowed_origin},
        )
        assert reviewed.status_code == 200
        published = client.post(
            f"/api/v1/tjm/review/questions/{version_id}/publish",
            headers={"Origin": allowed_origin},
        )
        assert published.status_code == 200
        activated = client.post(
            "/api/v1/tjm/exams/exam-api/activate",
            headers={"Origin": allowed_origin},
        )
        assert activated.status_code == 200


def test_tjm_admin_bearer_and_auth_disabled_clients_keep_origin_compatibility(
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def decode(token: str) -> TokenPayload | None:
        if token in {"cookie-admin", "bearer-admin"}:
            return TokenPayload(username="admin", role="admin", user_id="admin-1")
        if token == "bearer-user":
            return TokenPayload(username="alice", role="user", user_id="u_alice")
        return None

    monkeypatch.setattr(auth_router, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth_router, "decode_token", decode)
    monkeypatch.setattr(
        auth_router,
        "load_system_settings",
        lambda: {"frontend_port": 3782, "cors_origin": "", "cors_origins": []},
        raising=False,
    )
    app = FastAPI()
    app.include_router(tjm.router, prefix="/api/v1/tjm")
    app.dependency_overrides[tjm.get_catalog_service] = lambda: catalog

    with TestClient(app) as client:
        client.cookies.set("dt_token", "cookie-admin")
        invalid_bearer = client.post(
            "/api/v1/tjm/exams",
            json=_exam_payload(),
            headers={"Authorization": "Bearer invalid"},
        )
        assert invalid_bearer.status_code == 401
        non_admin = client.post(
            "/api/v1/tjm/exams",
            json=_exam_payload(),
            headers={"Authorization": "Bearer bearer-user"},
        )
        assert non_admin.status_code == 403
        client.cookies.set("dt_token", "bearer-user")
        non_admin_cookie = client.post(
            "/api/v1/tjm/exams",
            json=_exam_payload(),
            headers={"Origin": "http://localhost:3782"},
        )
        assert non_admin_cookie.status_code == 403
        client.cookies.set("dt_token", "cookie-admin")
        bearer = client.post(
            "/api/v1/tjm/exams",
            json=_exam_payload(),
            headers={
                "Authorization": "Bearer bearer-admin",
                "Origin": "https://evil.example.com",
            },
        )
        assert bearer.status_code == 201

    auth_disabled_catalog = CatalogService(CatalogStore(catalog.store.db_path.parent / "local.db"))
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", False)
    local_app = FastAPI()
    local_app.include_router(tjm.router, prefix="/api/v1/tjm")
    local_app.dependency_overrides[tjm.get_catalog_service] = lambda: auth_disabled_catalog
    with TestClient(local_app) as client:
        local = client.post("/api/v1/tjm/exams", json={**_exam_payload(), "id": "local-exam"})
    assert local.status_code == 201


def test_tjm_learner_cookie_mutations_require_allowed_origin(
    catalog: CatalogService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed_origin = "https://learn.example.com"
    _active_catalog(catalog)
    attempts = AttemptService(
        catalog,
        LearningStore(tmp_path / "csrf-learner.db"),
        owner_id="u_learner",
    )
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", True)
    monkeypatch.setattr(
        auth_router,
        "decode_token",
        lambda token: (
            TokenPayload(username="learner", role="user", user_id="u_learner")
            if token in {"cookie-user", "bearer-user"}
            else None
        ),
    )
    monkeypatch.setattr(
        auth_router,
        "load_system_settings",
        lambda: {
            "frontend_port": 3782,
            "cors_origin": allowed_origin,
            "cors_origins": [],
        },
        raising=False,
    )
    app = FastAPI()
    app.include_router(tjm.router, prefix="/api/v1/tjm")
    app.dependency_overrides[tjm.get_catalog_service] = lambda: catalog
    app.dependency_overrides[tjm.get_attempt_service] = lambda: attempts

    with TestClient(app) as client:
        client.cookies.set("dt_token", "cookie-user")
        missing = client.post(
            "/api/v1/tjm/attempts",
            json={"exam_id": "exam-learn", "mode": "practice"},
        )
        evil = client.post(
            "/api/v1/tjm/attempts",
            json={"exam_id": "exam-learn", "mode": "practice"},
            headers={"Origin": "https://evil.example.com"},
        )
        allowed = client.post(
            "/api/v1/tjm/attempts",
            json={"exam_id": "exam-learn", "mode": "practice"},
            headers={"Origin": allowed_origin},
        )
        attempt_id = allowed.json()["id"]
        open_evil = client.post(
            f"/api/v1/tjm/attempts/{attempt_id}/items/0/open",
            headers={"Origin": "https://evil.example.com"},
        )
        open_allowed = client.post(
            f"/api/v1/tjm/attempts/{attempt_id}/items/0/open",
            headers={"Origin": allowed_origin},
        )
        bearer = client.post(
            f"/api/v1/tjm/attempts/{attempt_id}/items/0/hint",
            json={"elapsed_ms": 0},
            headers={
                "Authorization": "Bearer bearer-user",
                "Origin": "https://evil.example.com",
            },
        )

    assert missing.status_code == 403
    assert evil.status_code == 403
    assert allowed.status_code == 201
    assert open_evil.status_code == 403
    assert open_allowed.status_code == 200
    assert bearer.status_code == 200


def test_exact_tjm_admin_mutation_routes_use_same_origin_guard() -> None:
    admin_guard = auth_router.require_admin_same_origin
    learner_guard = auth_router.require_authenticated_write_same_origin
    admin_guarded = {
        (method, route.path)
        for route in tjm.router.routes
        for method in route.methods
        if any(dependency.call is admin_guard for dependency in route.dependant.dependencies)
    }

    assert admin_guarded == {
        ("POST", "/exams"),
        ("PATCH", "/exams/{exam_id}"),
        ("PUT", "/exams/{exam_id}/official-passing-score"),
        ("POST", "/exams/{exam_id}/activate"),
        ("POST", "/imports"),
        ("PATCH", "/review/questions/{version_id}"),
        ("POST", "/review/questions/{version_id}/review"),
        ("POST", "/review/questions/{version_id}/publish"),
        ("POST", "/review/questions/{version_id}/reject"),
        ("POST", "/review/questions/{version_id}/retire"),
        ("POST", "/review/questions/{version_id}/classify-retirement"),
    }
    learner_guarded = {
        (method, route.path)
        for route in tjm.router.routes
        for method in route.methods
        if any(dependency.call is learner_guard for dependency in route.dependant.dependencies)
    }
    assert learner_guarded == {
        ("PUT", "/exam-preferences/{exam_id}"),
        ("POST", "/attempts"),
        ("POST", "/attempts/{attempt_id}/items/{position}/open"),
        ("POST", "/attempts/{attempt_id}/answers"),
        ("POST", "/attempts/{attempt_id}/items/{position}/hint"),
        ("POST", "/attempts/{attempt_id}/items/{position}/voice-candidate"),
        (
            "POST",
            "/attempts/{attempt_id}/items/{position}/voice-candidates/{candidate_id}/confirm",
        ),
        (
            "POST",
            "/attempts/{attempt_id}/items/{position}/voice-candidates/{candidate_id}/cancel",
        ),
        ("POST", "/attempts/{attempt_id}/submit"),
        ("POST", "/review/attempts"),
    }
    mutating_routes = {
        (method, route.path)
        for route in tjm.router.routes
        for method in route.methods
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert mutating_routes == admin_guarded | learner_guarded


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


def test_attempt_api_rejects_boolean_metric_coercion(
    learner_client: TestClient,
) -> None:
    started = learner_client.post(
        "/api/v1/tjm/attempts", json={"exam_id": "exam-learn", "mode": "practice"}
    ).json()
    attempt_id = started["id"]
    assert learner_client.post(f"/api/v1/tjm/attempts/{attempt_id}/items/0/open").status_code == 200

    valid_answer = {
        "position": 0,
        "selected_option_key": "A",
        "confidence": 50,
        "elapsed_ms": 0,
        "confirmed": True,
    }
    invalid_answers = [
        learner_client.post(
            f"/api/v1/tjm/attempts/{attempt_id}/answers",
            json={**valid_answer, **override},
        )
        for override in (
            {"position": False},
            {"confidence": True},
            {"elapsed_ms": False},
            {"confirmed": 1},
        )
    ]
    hint = learner_client.post(
        f"/api/v1/tjm/attempts/{attempt_id}/items/0/hint",
        json={"elapsed_ms": True},
    )
    voice = learner_client.post(
        f"/api/v1/tjm/attempts/{attempt_id}/items/0/voice-candidate",
        json={"transcript": "1番", "elapsed_ms": True},
    )
    valid_candidate = learner_client.post(
        f"/api/v1/tjm/attempts/{attempt_id}/items/0/voice-candidate",
        json={"transcript": "1番", "elapsed_ms": 1},
    ).json()
    voice_confirm = learner_client.post(
        f"/api/v1/tjm/attempts/{attempt_id}/items/0/voice-candidates/"
        f"{valid_candidate['candidate_id']}/confirm",
        json={"confidence": False, "elapsed_ms": True},
    )
    review = learner_client.post(
        "/api/v1/tjm/review/attempts",
        json={"exam_id": "exam-learn", "limit": True},
    )

    assert all(answer.status_code == 422 for answer in invalid_answers)
    assert hint.status_code == 422
    assert voice.status_code == 422
    assert voice_confirm.status_code == 422
    assert review.status_code == 422


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


def test_voice_answer_api_rejects_identifiers_outside_sqlite_integer_range(
    learner_client: TestClient,
) -> None:
    started = learner_client.post(
        "/api/v1/tjm/attempts", json={"exam_id": "exam-learn", "mode": "exam"}
    ).json()
    attempt_id = started["id"]
    too_large = 2**63

    position = learner_client.post(
        f"/api/v1/tjm/attempts/{attempt_id}/items/{too_large}/hint",
        json={"elapsed_ms": 0},
    )
    candidate = learner_client.post(
        f"/api/v1/tjm/attempts/{attempt_id}/items/0/voice-candidates/{too_large}/confirm",
        json={"confidence": 50, "elapsed_ms": 0},
    )

    assert position.status_code == 400
    assert candidate.status_code == 400
    assert "SQLite integer range" in position.json()["detail"]
    assert "SQLite integer range" in candidate.json()["detail"]


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
    preferences = learner_client.get("/api/v1/tjm/exam-preferences")
    draft_preference = learner_client.put(
        "/api/v1/tjm/exam-preferences/draft-hidden",
        json={"practice_target_score": 1},
    )
    unknown_preference = learner_client.put(
        "/api/v1/tjm/exam-preferences/not-present",
        json={"practice_target_score": 1},
    )

    assert response.status_code == 200
    assert [exam["id"] for exam in response.json()["exams"]] == ["exam-learn"]
    assert [item["exam_id"] for item in preferences.json()["preferences"]] == ["exam-learn"]
    assert draft_preference.status_code == unknown_preference.status_code == 400
    assert (
        draft_preference.json()
        == unknown_preference.json()
        == {"detail": "exam is not available for preferences"}
    )
