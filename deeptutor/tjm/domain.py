from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Mapping


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
    pass_score: int | None = None
    blueprint: Mapping[str, int] = field(default_factory=dict)
    description: str = ""

    def normalized(self) -> "ExamSpec":
        exam_id = self.id.strip()
        title = self.title.strip()
        if not exam_id:
            raise DomainValidationError("exam id is required")
        if not title:
            raise DomainValidationError("exam title is required")
        if self.duration_seconds <= 0:
            raise DomainValidationError("duration_seconds must be positive")
        if self.question_count <= 0:
            raise DomainValidationError("question_count must be positive")
        if self.pass_score is not None and not 0 <= self.pass_score <= self.question_count:
            raise DomainValidationError("pass_score must be between zero and question_count")

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
            pass_score=self.pass_score,
            blueprint=blueprint,
        )


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


__all__ = [
    "Choice",
    "DomainValidationError",
    "DuplicateRecordError",
    "ExamSpec",
    "GradeResult",
    "ImmutableVersionError",
    "InvalidTransitionError",
    "QuestionVersionDraft",
    "TJMError",
    "grade_responses",
]
