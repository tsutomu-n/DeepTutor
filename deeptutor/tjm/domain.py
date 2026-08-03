from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Mapping
from urllib.parse import urlsplit


class TJMError(RuntimeError):
    """Base error for deterministic TJM domain operations."""


class DomainValidationError(TJMError):
    pass


class DuplicateRecordError(TJMError):
    pass


class InvalidTransitionError(TJMError):
    pass


class ImmutableVersionError(TJMError):
    pass


_EXAM_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")


def normalize_exam_id(value: Any) -> str:
    if not isinstance(value, str):
        raise DomainValidationError("exam id must be a string")
    exam_id = value.strip()
    if not exam_id:
        raise DomainValidationError("exam id is required")
    if _EXAM_ID_PATTERN.fullmatch(exam_id) is None:
        raise DomainValidationError("exam id must be one URL-safe ASCII path segment")
    return exam_id


@dataclass(frozen=True, slots=True)
class Choice:
    key: str
    text: str


@dataclass(frozen=True, slots=True)
class ExamSpec:
    id: str
    title: str
    duration_seconds: int
    question_count: int
    blueprint: Mapping[str, int] = field(default_factory=dict)
    description: str = ""
    official_passing_score: int | None = None
    official_passing_score_source: OfficialPassingScoreSource | Mapping[str, Any] | None = None

    def normalized(self) -> "ExamSpec":
        exam_id = normalize_exam_id(self.id)
        title = self.title.strip()
        if not title:
            raise DomainValidationError("exam title is required")
        if self.duration_seconds <= 0:
            raise DomainValidationError("duration_seconds must be positive")
        if self.question_count <= 0:
            raise DomainValidationError("question_count must be positive")
        if isinstance(self.official_passing_score, bool) or (
            self.official_passing_score is not None
            and not isinstance(self.official_passing_score, int)
        ):
            raise DomainValidationError("official_passing_score must be an integer")
        if self.official_passing_score is not None and not (
            0 <= self.official_passing_score <= self.question_count
        ):
            raise DomainValidationError(
                "official_passing_score must be between zero and question_count"
            )
        if (self.official_passing_score is None) != (self.official_passing_score_source is None):
            raise DomainValidationError(
                "official_passing_score and official_passing_score_source must be set together"
            )
        official_source = (
            OfficialPassingScoreSource.from_value(self.official_passing_score_source).normalized()
            if self.official_passing_score_source is not None
            else None
        )
        blueprint: dict[str, int] = {}
        for raw_area, raw_count in self.blueprint.items():
            area = str(raw_area).strip()
            if not area or isinstance(raw_count, bool) or not isinstance(raw_count, int):
                raise DomainValidationError("blueprint requires non-empty areas and integer counts")
            if raw_count <= 0:
                raise DomainValidationError("blueprint counts must be positive")
            if area in blueprint:
                raise DomainValidationError(f"duplicate blueprint area: {area}")
            blueprint[area] = raw_count
        if blueprint and sum(blueprint.values()) != self.question_count:
            raise DomainValidationError("blueprint counts must equal question_count")

        return ExamSpec(
            id=exam_id,
            title=title,
            description=self.description.strip(),
            duration_seconds=self.duration_seconds,
            question_count=self.question_count,
            blueprint=blueprint,
            official_passing_score=self.official_passing_score,
            official_passing_score_source=official_source,
        )


@dataclass(frozen=True, slots=True)
class OfficialPassingScoreSource:
    """Human-verifiable provenance for an official passing standard."""

    title: str
    publisher: str
    url: str | None = None
    published_at: str | None = None

    def normalized(self) -> "OfficialPassingScoreSource":
        title = _required_text(self.title, field_name="source.title")
        publisher = _required_text(self.publisher, field_name="source.publisher")
        url = _optional_text(self.url, field_name="source.url")
        if url is not None:
            if any(character.isspace() or ord(character) < 32 for character in url):
                raise DomainValidationError("source.url must be an absolute http(s) URL")
            try:
                parsed = urlsplit(url)
                _ = parsed.port
            except ValueError as exc:
                raise DomainValidationError("source.url must be an absolute http(s) URL") from exc
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.netloc
                or parsed.hostname is None
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise DomainValidationError("source.url must be an absolute http(s) URL")
        published_at = _optional_text(self.published_at, field_name="source.published_at")
        return OfficialPassingScoreSource(
            title=title,
            publisher=publisher,
            url=url,
            published_at=published_at,
        )

    def as_dict(self) -> dict[str, str]:
        normalized = self.normalized()
        result = {
            "title": normalized.title,
            "publisher": normalized.publisher,
        }
        if normalized.url is not None:
            result["url"] = normalized.url
        if normalized.published_at is not None:
            result["published_at"] = normalized.published_at
        return result

    @classmethod
    def from_value(
        cls, value: "OfficialPassingScoreSource | Mapping[str, Any]"
    ) -> "OfficialPassingScoreSource":
        if isinstance(value, cls):
            return value.normalized()
        if not isinstance(value, Mapping):
            raise DomainValidationError("official passing score source must be an object")
        allowed = {"title", "publisher", "url", "published_at"}
        unknown = set(value) - allowed
        if unknown:
            raise DomainValidationError(
                "unsupported official passing score source field: "
                + ", ".join(sorted(str(field) for field in unknown))
            )
        if "title" not in value or "publisher" not in value:
            raise DomainValidationError("source.title and source.publisher are required")
        return cls(
            title=value["title"],
            publisher=value["publisher"],
            url=value.get("url"),
            published_at=value.get("published_at"),
        ).normalized()


def _required_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field_name} is required")
    return value.strip()


def _optional_text(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field_name} must be a non-empty string when provided")
    return value.strip()


@dataclass(frozen=True, slots=True)
class QuestionVersionDraft:
    exam_id: str
    stable_id: str
    stem: str
    choices: tuple[Choice, ...]
    correct_option_key: str
    area: str
    explanation: str = ""
    hints: tuple[str, ...] = ()
    source: Mapping[str, Any] = field(default_factory=dict)

    def normalized(self) -> "QuestionVersionDraft":
        exam_id = self.exam_id.strip()
        stable_id = self.stable_id.strip()
        stem = self.stem.strip()
        area = self.area.strip()
        correct = self.correct_option_key.strip()
        if not exam_id or not stable_id:
            raise DomainValidationError("exam_id and stable_id are required")
        if not stem:
            raise DomainValidationError("question stem is required")
        if not area:
            raise DomainValidationError("question area is required")
        if len(self.choices) < 2:
            raise DomainValidationError("at least two choices are required")

        choices: list[Choice] = []
        keys: set[str] = set()
        for raw in self.choices:
            key = raw.key.strip()
            text = raw.text.strip()
            if not key or not text:
                raise DomainValidationError("choice key and text are required")
            if key in keys:
                raise DomainValidationError(f"duplicate choice key: {key}")
            keys.add(key)
            choices.append(Choice(key=key, text=text))
        if correct not in keys:
            raise DomainValidationError("correct_option_key must reference an existing choice")

        hints = tuple(str(hint).strip() for hint in self.hints)
        if any(not hint for hint in hints):
            raise DomainValidationError("hints cannot contain empty values")
        try:
            json.dumps(self.source, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise DomainValidationError("source must be JSON serializable") from exc

        return QuestionVersionDraft(
            exam_id=exam_id,
            stable_id=stable_id,
            stem=stem,
            choices=tuple(choices),
            correct_option_key=correct,
            area=area,
            explanation=self.explanation.strip(),
            hints=hints,
            source=dict(self.source),
        )


@dataclass(frozen=True, slots=True)
class GradeResult:
    total: int
    answered: int
    correct: int
    items: dict[str, bool]


def grade_responses(
    *, answer_key: Mapping[str, str], responses: Mapping[str, str | None]
) -> GradeResult:
    """Grade confirmed responses without consulting an LLM or external provider."""
    unknown = set(responses) - set(answer_key)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise DomainValidationError(f"unknown question version in responses: {names}")

    items: dict[str, bool] = {}
    answered = 0
    for version_id, correct_key in answer_key.items():
        selected = responses.get(version_id)
        if selected is not None:
            selected = selected.strip()
            if selected:
                answered += 1
        items[version_id] = bool(selected) and selected == correct_key
    return GradeResult(
        total=len(answer_key),
        answered=answered,
        correct=sum(items.values()),
        items=items,
    )


def _score_integer(value: Any, *, field_name: str, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DomainValidationError(f"{field_name} must be a non-negative integer")
    return value


def normalize_attempt_snapshot(
    exam_snapshot: Mapping[str, Any],
    *,
    mode: str,
    allow_legacy: bool = True,
) -> dict[str, Any]:
    """Validate scoring fields once for runtime evaluation and SQLite guards."""
    if mode not in {"practice", "exam", "review"}:
        raise DomainValidationError(f"unsupported attempt mode: {mode}")
    if not isinstance(exam_snapshot, Mapping):
        raise DomainValidationError("exam_snapshot must be an object")

    snapshot_schema_version = exam_snapshot.get("snapshot_schema_version")
    legacy_snapshot = snapshot_schema_version is None
    if legacy_snapshot:
        if not allow_legacy:
            raise DomainValidationError("new attempts require snapshot schema version 2")
        raw_exam_id = exam_snapshot.get("id")
        if raw_exam_id is not None and (
            not isinstance(raw_exam_id, str) or not raw_exam_id.strip()
        ):
            raise DomainValidationError("snapshot exam id must be a non-empty string")
        snapshot_exam_id = raw_exam_id.strip() if isinstance(raw_exam_id, str) else None
        raw_duration = exam_snapshot.get("duration_seconds")
        duration_seconds = (
            _score_integer(raw_duration, field_name="duration_seconds")
            if raw_duration is not None
            else None
        )
        if duration_seconds == 0:
            raise DomainValidationError("duration_seconds must be positive")
        maximum = _score_integer(exam_snapshot.get("question_count"), field_name="question_count")
        official_threshold = None
        official_source = None
        target_threshold = _score_integer(
            exam_snapshot.get("pass_score"),
            field_name="legacy pass_score",
            optional=True,
        )
        target_origin = "legacy_pass_score" if target_threshold is not None else None
    else:
        if (
            isinstance(snapshot_schema_version, bool)
            or not isinstance(snapshot_schema_version, int)
            or snapshot_schema_version != 2
        ):
            raise DomainValidationError("unsupported attempt snapshot schema version")
        allowed_fields = {
            "snapshot_schema_version",
            "id",
            "title",
            "description",
            "duration_seconds",
            "question_count",
            "blueprint",
            "revision",
            "maximum_score",
            "official_passing_score",
            "official_passing_score_source",
            "practice_target_score",
            "practice_target_origin",
            "scoring_policy",
        }
        if set(exam_snapshot) != allowed_fields:
            raise DomainValidationError(
                "snapshot schema version 2 fields do not match the contract"
            )
        raw_exam_id = exam_snapshot.get("id")
        if not isinstance(raw_exam_id, str) or not raw_exam_id.strip():
            raise DomainValidationError("snapshot exam id must be a non-empty string")
        snapshot_exam_id = normalize_exam_id(raw_exam_id)
        if (
            not isinstance(exam_snapshot.get("title"), str)
            or not str(exam_snapshot["title"]).strip()
        ):
            raise DomainValidationError("snapshot title must be a non-empty string")
        if not isinstance(exam_snapshot.get("description"), str):
            raise DomainValidationError("snapshot description must be a string")
        duration_seconds = _score_integer(
            exam_snapshot.get("duration_seconds"), field_name="duration_seconds"
        )
        if duration_seconds is None or duration_seconds <= 0:
            raise DomainValidationError("duration_seconds must be positive")
        scoring_policy = exam_snapshot.get("scoring_policy")
        if (
            not isinstance(scoring_policy, Mapping)
            or set(scoring_policy) != {"type", "version", "points_per_item"}
            or scoring_policy.get("type") != "unit_correct"
            or isinstance(scoring_policy.get("version"), bool)
            or not isinstance(scoring_policy.get("version"), int)
            or scoring_policy.get("version") != 1
            or isinstance(scoring_policy.get("points_per_item"), bool)
            or not isinstance(scoring_policy.get("points_per_item"), int)
            or scoring_policy.get("points_per_item") != 1
        ):
            raise DomainValidationError("unsupported attempt scoring policy")
        maximum = _score_integer(exam_snapshot.get("maximum_score"), field_name="maximum_score")
        question_count = _score_integer(
            exam_snapshot.get("question_count"), field_name="question_count"
        )
        if question_count is None or question_count <= 0:
            raise DomainValidationError("question_count must be positive")
        if maximum is None or maximum > question_count:
            raise DomainValidationError("maximum_score cannot exceed question_count")
        revision = _score_integer(exam_snapshot.get("revision"), field_name="revision")
        if revision is None or revision <= 0:
            raise DomainValidationError("revision must be positive")
        raw_blueprint = exam_snapshot.get("blueprint")
        if not isinstance(raw_blueprint, Mapping):
            raise DomainValidationError("snapshot blueprint must be an object")
        blueprint: dict[str, int] = {}
        for raw_area, raw_count in raw_blueprint.items():
            if not isinstance(raw_area, str) or not raw_area.strip():
                raise DomainValidationError("snapshot blueprint area must be non-empty")
            count = _score_integer(raw_count, field_name="snapshot blueprint count")
            if count is None or count <= 0 or raw_area.strip() in blueprint:
                raise DomainValidationError("snapshot blueprint counts must be positive")
            blueprint[raw_area.strip()] = count
        if blueprint and sum(blueprint.values()) != question_count:
            raise DomainValidationError("snapshot blueprint counts must equal question_count")
        official_threshold = _score_integer(
            exam_snapshot.get("official_passing_score"),
            field_name="official_passing_score",
            optional=True,
        )
        raw_official_source = exam_snapshot.get("official_passing_score_source")
        if (official_threshold is None) != (raw_official_source is None):
            raise DomainValidationError("official passing score and source must be set together")
        official_source = (
            OfficialPassingScoreSource.from_value(raw_official_source).as_dict()
            if raw_official_source is not None
            else None
        )
        if official_threshold is not None and official_threshold > question_count:
            raise DomainValidationError("official_passing_score cannot exceed question_count")
        target_threshold = _score_integer(
            exam_snapshot.get("practice_target_score"),
            field_name="practice_target_score",
            optional=True,
        )
        target_origin = exam_snapshot.get("practice_target_origin")
        if target_origin not in {None, "user", "legacy_pass_score"}:
            raise DomainValidationError("unsupported practice target origin")
        if target_threshold is not None and target_origin is None:
            raise DomainValidationError("configured practice target requires an origin")
        if target_threshold is None and target_origin == "legacy_pass_score":
            raise DomainValidationError("legacy practice target origin requires a score")
        if target_threshold is not None and target_threshold > question_count:
            raise DomainValidationError("practice_target_score cannot exceed question_count")
    if maximum is None or maximum <= 0:
        raise DomainValidationError("maximum_score must be positive")
    if mode == "exam" and official_threshold is not None and official_threshold > maximum:
        raise DomainValidationError("official_passing_score cannot exceed maximum_score")
    if mode in {"exam", "practice"} and target_threshold is not None and target_threshold > maximum:
        raise DomainValidationError("practice_target_score cannot exceed maximum_score")
    return {
        "legacy": legacy_snapshot,
        "exam_id": snapshot_exam_id,
        "duration_seconds": duration_seconds,
        "maximum_score": maximum,
        "official_passing_score": official_threshold,
        "official_passing_score_source": official_source,
        "practice_target_score": target_threshold,
        "practice_target_origin": target_origin,
    }


def evaluate_attempt_result(
    *,
    mode: str,
    status: str,
    correct_count: int | None,
    total_count: int | None,
    content_invalidated_count: int,
    exam_snapshot: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Evaluate immutable attempt facts without consulting AI or mutable catalog data."""
    if mode not in {"practice", "exam", "review"}:
        raise DomainValidationError(f"unsupported attempt mode: {mode}")
    if status not in {"in_progress", "submitted", "expired"}:
        raise DomainValidationError(f"unsupported attempt status: {status}")
    if status == "in_progress":
        return None
    correct = _score_integer(correct_count, field_name="correct_count", optional=True)
    total = _score_integer(total_count, field_name="total_count", optional=True)
    invalidated = _score_integer(content_invalidated_count, field_name="content_invalidated_count")
    if correct is not None and total is not None and correct > total:
        raise DomainValidationError("correct_count cannot exceed total_count")
    if correct is None or total is None:
        raise DomainValidationError("finalized attempt score is required")

    snapshot = normalize_attempt_snapshot(exam_snapshot, mode=mode)
    legacy_snapshot = bool(snapshot["legacy"])
    maximum = int(snapshot["maximum_score"])
    official_threshold = snapshot["official_passing_score"]
    official_source = snapshot["official_passing_score_source"]
    target_threshold = snapshot["practice_target_score"]

    validity = "content_invalidated" if invalidated else "eligible"
    common_reason: str | None = None
    if invalidated:
        common_reason = "content_invalidated"
    elif total != maximum:
        common_reason = "incomplete_score_scope"

    if common_reason is not None:
        official_status = "not_evaluated"
        official_reason = common_reason
        target_status = "not_evaluated"
        target_reason = common_reason
    else:
        if mode != "exam":
            official_status = "not_evaluated"
            official_reason = "mode_not_eligible"
        elif legacy_snapshot:
            official_status = "not_evaluated"
            official_reason = "legacy_score_ambiguous"
        elif official_threshold is None:
            official_status = "not_evaluated"
            official_reason = "official_score_unavailable"
        else:
            official_status = "passed" if correct >= official_threshold else "failed"
            official_reason = None

        if mode == "review":
            target_status = "not_evaluated"
            target_reason = "mode_not_eligible"
        elif target_threshold is None:
            target_status = "not_evaluated"
            target_reason = "practice_target_unset"
        else:
            target_status = "achieved" if correct >= target_threshold else "not_achieved"
            target_reason = None

    return {
        "score": correct,
        "maximum_score": maximum,
        "validity": validity,
        "official": {
            "status": official_status,
            "threshold": official_threshold,
            "source": official_source,
            "not_evaluated_reason": official_reason,
        },
        "practice_target": {
            "status": target_status,
            "threshold": target_threshold,
            "not_evaluated_reason": target_reason,
        },
    }


__all__ = [
    "Choice",
    "DomainValidationError",
    "DuplicateRecordError",
    "ExamSpec",
    "GradeResult",
    "ImmutableVersionError",
    "InvalidTransitionError",
    "OfficialPassingScoreSource",
    "QuestionVersionDraft",
    "TJMError",
    "evaluate_attempt_result",
    "grade_responses",
    "normalize_attempt_snapshot",
    "normalize_exam_id",
]
