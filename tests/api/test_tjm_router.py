from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from deeptutor.api.routers import tjm
from deeptutor.api.routers.auth import require_admin, require_auth
from deeptutor.services.auth import TokenPayload
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

    retired = admin_client.post(f"/api/v1/tjm/review/questions/{version_id}/retire")
    assert retired.status_code == 200
    assert retired.json()["status"] == "retired"


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


def test_practice_attempt_api_returns_immediate_deterministic_feedback(
    learner_client: TestClient,
) -> None:
    started = learner_client.post(
        "/api/v1/tjm/attempts", json={"exam_id": "exam-learn", "mode": "practice"}
    )
    assert started.status_code == 201
    attempt_id = started.json()["id"]

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


def test_exam_attempt_api_has_no_answer_leak_before_submit(learner_client: TestClient) -> None:
    started = learner_client.post(
        "/api/v1/tjm/attempts", json={"exam_id": "exam-learn", "mode": "exam"}
    )
    assert started.status_code == 201
    assert "correct_option_key" not in json.dumps(started.json())
    assert "official answer" not in json.dumps(started.json())
    attempt_id = started.json()["id"]

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


def test_voice_answer_api_saves_only_after_candidate_confirmation(
    learner_client: TestClient,
) -> None:
    started = learner_client.post(
        "/api/v1/tjm/attempts", json={"exam_id": "exam-learn", "mode": "exam"}
    ).json()
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


def test_review_queue_review_attempt_and_analytics_api(learner_client: TestClient) -> None:
    started = learner_client.post(
        "/api/v1/tjm/attempts", json={"exam_id": "exam-learn", "mode": "practice"}
    ).json()
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
