from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal, Never

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from deeptutor.api.routers.auth import require_admin, require_auth
from deeptutor.multi_user.context import get_current_user
from deeptutor.multi_user.paths import get_tjm_catalog_db
from deeptutor.services.auth import TokenPayload
from deeptutor.services.path_service import get_path_service
from deeptutor.tjm.attempts import AttemptNotFoundError, AttemptService
from deeptutor.tjm.catalog import CatalogService
from deeptutor.tjm.domain import (
    Choice,
    DomainValidationError,
    DuplicateRecordError,
    ExamSpec,
    ImmutableVersionError,
    InvalidTransitionError,
    QuestionVersionDraft,
)
from deeptutor.tjm.importer import ImportService
from deeptutor.tjm.storage import CatalogStore, LearningStore

router = APIRouter()
MAX_IMPORT_BYTES = 10 * 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExamRequest(_StrictModel):
    id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    duration_seconds: int = Field(gt=0)
    question_count: int = Field(gt=0)
    pass_score: int | None = Field(default=None, ge=0)
    blueprint: dict[str, int] = Field(default_factory=dict)

    def to_domain(self) -> ExamSpec:
        return ExamSpec(**self.model_dump())


class ChoiceRequest(_StrictModel):
    key: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1)


class QuestionRequest(_StrictModel):
    exam_id: str = Field(min_length=1, max_length=200)
    stable_id: str = Field(min_length=1, max_length=300)
    stem: str = Field(min_length=1)
    options: list[ChoiceRequest] = Field(min_length=2)
    correct_option_key: str = Field(min_length=1, max_length=100)
    area: str = Field(min_length=1, max_length=300)
    explanation: str = ""
    hints: list[str] = Field(default_factory=list)
    source: dict[str, Any] = Field(default_factory=dict)

    def to_domain(self) -> QuestionVersionDraft:
        return QuestionVersionDraft(
            exam_id=self.exam_id,
            stable_id=self.stable_id,
            stem=self.stem,
            choices=tuple(Choice(key=item.key, text=item.text) for item in self.options),
            correct_option_key=self.correct_option_key,
            area=self.area,
            explanation=self.explanation,
            hints=tuple(self.hints),
            source=self.source,
        )


class ReviewNote(_StrictModel):
    note: str = ""


class StartAttemptRequest(_StrictModel):
    exam_id: str = Field(min_length=1, max_length=200)
    mode: Literal["practice", "exam"]


class AnswerRequest(_StrictModel):
    position: int = Field(ge=0)
    selected_option_key: str = Field(min_length=1, max_length=100)
    confidence: int | None = Field(default=None, ge=0, le=100)
    elapsed_ms: int = Field(ge=0)
    confirmed: bool = False
    client_created_at: str | None = None


class HintRequest(_StrictModel):
    elapsed_ms: int = Field(ge=0)


class VoiceCandidateRequest(_StrictModel):
    transcript: str = Field(min_length=1, max_length=2000)
    elapsed_ms: int = Field(ge=0)


class VoiceConfirmRequest(_StrictModel):
    confidence: int | None = Field(default=None, ge=0, le=100)
    elapsed_ms: int = Field(ge=0)


class ReviewAttemptRequest(_StrictModel):
    exam_id: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=100)


def get_catalog_service() -> CatalogService:
    return CatalogService(CatalogStore(get_tjm_catalog_db()))


def get_attempt_service(
    catalog: CatalogService = Depends(get_catalog_service),
) -> AttemptService:
    user = get_current_user()
    learning = LearningStore(get_path_service().get_tjm_learning_db())
    return AttemptService(catalog, learning, owner_id=user.id)


def _actor(payload: TokenPayload) -> str:
    return payload.user_id or payload.username


def _raise_http(exc: Exception) -> Never:
    if isinstance(exc, AttemptNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(
        exc,
        (DuplicateRecordError, InvalidTransitionError, ImmutableVersionError),
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/exams")
def list_exams(
    payload: TokenPayload | None = Depends(require_auth),
    catalog: CatalogService = Depends(get_catalog_service),
) -> dict[str, Any]:
    exams = catalog.list_exams(
        status=None if payload is None or payload.role == "admin" else "active"
    )
    return {"exams": exams, "total": len(exams)}


@router.post("/exams", status_code=status.HTTP_201_CREATED)
def create_exam(
    request: ExamRequest,
    admin: TokenPayload = Depends(require_admin),
    catalog: CatalogService = Depends(get_catalog_service),
) -> dict[str, Any]:
    try:
        return catalog.create_exam(request.to_domain(), actor_id=_actor(admin))
    except (DomainValidationError, DuplicateRecordError) as exc:
        _raise_http(exc)


@router.patch("/exams/{exam_id}")
def replace_exam(
    exam_id: str,
    request: ExamRequest,
    admin: TokenPayload = Depends(require_admin),
    catalog: CatalogService = Depends(get_catalog_service),
) -> dict[str, Any]:
    try:
        return catalog.replace_exam(exam_id, request.to_domain(), actor_id=_actor(admin))
    except (DomainValidationError, InvalidTransitionError) as exc:
        _raise_http(exc)


@router.post("/exams/{exam_id}/activate")
def activate_exam(
    exam_id: str,
    admin: TokenPayload = Depends(require_admin),
    catalog: CatalogService = Depends(get_catalog_service),
) -> dict[str, Any]:
    try:
        return catalog.activate_exam(exam_id, actor_id=_actor(admin))
    except (DomainValidationError, InvalidTransitionError) as exc:
        _raise_http(exc)


@router.post("/imports", status_code=status.HTTP_201_CREATED, response_model=None)
async def import_questions(
    import_format: Literal["json", "jsonl", "csv"] = Form(...),
    file: UploadFile = File(...),
    admin: TokenPayload = Depends(require_admin),
    catalog: CatalogService = Depends(get_catalog_service),
) -> dict[str, Any] | JSONResponse:
    payload = await file.read(MAX_IMPORT_BYTES + 1)
    if len(payload) > MAX_IMPORT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"import file exceeds {MAX_IMPORT_BYTES} bytes",
        )
    result = ImportService(catalog).import_bytes(
        payload,
        import_format=import_format,
        source_name=file.filename or "upload",
        actor_id=_actor(admin),
    )
    body = asdict(result)
    if result.status == "failed":
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=body)
    return body


@router.get("/imports/{batch_id}")
def get_import_batch(
    batch_id: str,
    _: TokenPayload = Depends(require_admin),
    catalog: CatalogService = Depends(get_catalog_service),
) -> dict[str, Any]:
    try:
        return ImportService(catalog).get_batch(batch_id)
    except DomainValidationError as exc:
        _raise_http(exc)


@router.get("/review/questions")
def list_review_questions(
    status_filter: Literal["draft", "rejected", "published", "retired"] = Query(
        "draft", alias="status"
    ),
    _: TokenPayload = Depends(require_admin),
    catalog: CatalogService = Depends(get_catalog_service),
) -> dict[str, Any]:
    questions = catalog.list_question_versions(status=status_filter)
    return {"questions": questions, "total": len(questions)}


@router.patch("/review/questions/{version_id}")
def replace_draft(
    version_id: str,
    request: QuestionRequest,
    admin: TokenPayload = Depends(require_admin),
    catalog: CatalogService = Depends(get_catalog_service),
) -> dict[str, Any]:
    try:
        return catalog.replace_draft(version_id, request.to_domain(), actor_id=_actor(admin))
    except (DomainValidationError, DuplicateRecordError, ImmutableVersionError) as exc:
        _raise_http(exc)


@router.post("/review/questions/{version_id}/review")
def review_question(
    version_id: str,
    request: ReviewNote,
    admin: TokenPayload = Depends(require_admin),
    catalog: CatalogService = Depends(get_catalog_service),
) -> dict[str, Any]:
    try:
        return catalog.review_question_version(
            version_id, actor_id=_actor(admin), note=request.note
        )
    except (DomainValidationError, InvalidTransitionError) as exc:
        _raise_http(exc)


@router.post("/review/questions/{version_id}/publish")
def publish_question(
    version_id: str,
    admin: TokenPayload = Depends(require_admin),
    catalog: CatalogService = Depends(get_catalog_service),
) -> dict[str, Any]:
    try:
        return catalog.publish_question_version(version_id, actor_id=_actor(admin))
    except (DomainValidationError, InvalidTransitionError) as exc:
        _raise_http(exc)


@router.post("/review/questions/{version_id}/reject")
def reject_question(
    version_id: str,
    request: ReviewNote,
    admin: TokenPayload = Depends(require_admin),
    catalog: CatalogService = Depends(get_catalog_service),
) -> dict[str, Any]:
    try:
        return catalog.reject_question_version(
            version_id, actor_id=_actor(admin), note=request.note
        )
    except (DomainValidationError, InvalidTransitionError) as exc:
        _raise_http(exc)


@router.post("/review/questions/{version_id}/retire")
def retire_question(
    version_id: str,
    admin: TokenPayload = Depends(require_admin),
    catalog: CatalogService = Depends(get_catalog_service),
) -> dict[str, Any]:
    try:
        return catalog.retire_question_version(version_id, actor_id=_actor(admin))
    except (DomainValidationError, InvalidTransitionError) as exc:
        _raise_http(exc)


@router.post("/attempts", status_code=status.HTTP_201_CREATED)
def start_attempt(
    request: StartAttemptRequest,
    _: TokenPayload | None = Depends(require_auth),
    attempts: AttemptService = Depends(get_attempt_service),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=200
    ),
) -> dict[str, Any]:
    try:
        return attempts.start_attempt(
            exam_id=request.exam_id,
            mode=request.mode,
            idempotency_key=idempotency_key,
        )
    except (DomainValidationError, InvalidTransitionError) as exc:
        _raise_http(exc)


@router.get("/attempts/{attempt_id}")
def get_attempt(
    attempt_id: str,
    _: TokenPayload | None = Depends(require_auth),
    attempts: AttemptService = Depends(get_attempt_service),
) -> dict[str, Any]:
    try:
        return attempts.get_attempt(attempt_id)
    except DomainValidationError as exc:
        _raise_http(exc)


@router.post("/attempts/{attempt_id}/items/{position}/open")
def present_attempt_item(
    attempt_id: str,
    position: int,
    _: TokenPayload | None = Depends(require_auth),
    attempts: AttemptService = Depends(get_attempt_service),
) -> dict[str, Any]:
    try:
        return attempts.present_item(attempt_id, position=position)
    except (DomainValidationError, InvalidTransitionError) as exc:
        _raise_http(exc)


@router.post("/attempts/{attempt_id}/answers")
def answer_attempt(
    attempt_id: str,
    request: AnswerRequest,
    _: TokenPayload | None = Depends(require_auth),
    attempts: AttemptService = Depends(get_attempt_service),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=200
    ),
) -> dict[str, Any]:
    try:
        return attempts.record_answer(
            attempt_id,
            position=request.position,
            selected_option_key=request.selected_option_key,
            confidence=request.confidence,
            elapsed_ms=request.elapsed_ms,
            confirmed=request.confirmed,
            client_created_at=request.client_created_at,
            idempotency_key=idempotency_key,
        )
    except (DomainValidationError, InvalidTransitionError) as exc:
        _raise_http(exc)


@router.post("/attempts/{attempt_id}/items/{position}/hint")
def use_hint(
    attempt_id: str,
    position: int,
    request: HintRequest,
    _: TokenPayload | None = Depends(require_auth),
    attempts: AttemptService = Depends(get_attempt_service),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=200
    ),
) -> dict[str, Any]:
    try:
        return attempts.use_hint(
            attempt_id,
            position=position,
            elapsed_ms=request.elapsed_ms,
            idempotency_key=idempotency_key,
        )
    except (DomainValidationError, InvalidTransitionError) as exc:
        _raise_http(exc)


@router.post(
    "/attempts/{attempt_id}/items/{position}/voice-candidate",
    status_code=status.HTTP_201_CREATED,
)
def record_voice_candidate(
    attempt_id: str,
    position: int,
    request: VoiceCandidateRequest,
    _: TokenPayload | None = Depends(require_auth),
    attempts: AttemptService = Depends(get_attempt_service),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=200
    ),
) -> dict[str, Any]:
    try:
        return attempts.record_voice_candidate(
            attempt_id,
            position=position,
            transcript=request.transcript,
            elapsed_ms=request.elapsed_ms,
            idempotency_key=idempotency_key,
        )
    except (DomainValidationError, InvalidTransitionError) as exc:
        _raise_http(exc)


@router.post("/attempts/{attempt_id}/items/{position}/voice-candidates/{candidate_id}/confirm")
def confirm_voice_candidate(
    attempt_id: str,
    position: int,
    candidate_id: int,
    request: VoiceConfirmRequest,
    _: TokenPayload | None = Depends(require_auth),
    attempts: AttemptService = Depends(get_attempt_service),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=200
    ),
) -> dict[str, Any]:
    try:
        return attempts.confirm_voice_candidate(
            attempt_id,
            position=position,
            candidate_id=candidate_id,
            confidence=request.confidence,
            elapsed_ms=request.elapsed_ms,
            idempotency_key=idempotency_key,
        )
    except (DomainValidationError, InvalidTransitionError) as exc:
        _raise_http(exc)


@router.post("/attempts/{attempt_id}/items/{position}/voice-candidates/{candidate_id}/cancel")
def cancel_voice_candidate(
    attempt_id: str,
    position: int,
    candidate_id: int,
    _: TokenPayload | None = Depends(require_auth),
    attempts: AttemptService = Depends(get_attempt_service),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=200
    ),
) -> dict[str, Any]:
    try:
        return attempts.cancel_voice_candidate(
            attempt_id,
            position=position,
            candidate_id=candidate_id,
            idempotency_key=idempotency_key,
        )
    except (DomainValidationError, InvalidTransitionError) as exc:
        _raise_http(exc)


@router.post("/attempts/{attempt_id}/submit")
def submit_attempt(
    attempt_id: str,
    _: TokenPayload | None = Depends(require_auth),
    attempts: AttemptService = Depends(get_attempt_service),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=200
    ),
) -> dict[str, Any]:
    try:
        return attempts.submit_attempt(attempt_id, idempotency_key=idempotency_key)
    except (DomainValidationError, InvalidTransitionError) as exc:
        _raise_http(exc)


@router.get("/history")
def get_history(
    limit: int = Query(100, ge=1, le=500),
    _: TokenPayload | None = Depends(require_auth),
    attempts: AttemptService = Depends(get_attempt_service),
) -> dict[str, Any]:
    try:
        history = attempts.list_history(limit=limit)
    except DomainValidationError as exc:
        _raise_http(exc)
    return {"attempts": history, "total": len(history)}


@router.get("/review/queue")
def get_review_queue(
    _: TokenPayload | None = Depends(require_auth),
    attempts: AttemptService = Depends(get_attempt_service),
) -> dict[str, Any]:
    items = attempts.list_review_queue()
    return {"items": items, "total": len(items)}


@router.post("/review/attempts", status_code=status.HTTP_201_CREATED)
def start_review_attempt(
    request: ReviewAttemptRequest,
    _: TokenPayload | None = Depends(require_auth),
    attempts: AttemptService = Depends(get_attempt_service),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=200
    ),
) -> dict[str, Any]:
    try:
        return attempts.start_review_attempt(
            exam_id=request.exam_id,
            limit=request.limit,
            idempotency_key=idempotency_key,
        )
    except (DomainValidationError, InvalidTransitionError) as exc:
        _raise_http(exc)


@router.get("/analytics")
def get_analytics(
    _: TokenPayload | None = Depends(require_auth),
    attempts: AttemptService = Depends(get_attempt_service),
) -> dict[str, Any]:
    return attempts.analytics()


__all__ = ["get_attempt_service", "get_catalog_service", "router"]
