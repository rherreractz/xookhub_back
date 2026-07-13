# Ruta: src/generation/schemas.py
"""Pydantic v2 schemas for the `generation` module.

Note the deliberate split between `ExamQuestionRead` (no answer key —
served while an attempt is live) and `ExamQuestionResult` (includes the
correct index + explanation — served only after submit). Leaking
`correct_index` before submission would make the exam pointless.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.generation.models import AttemptStatus, ExamGenerationStatus, QuestionType

# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #
class SummaryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    content: str
    created_at: datetime


# --------------------------------------------------------------------------- #
# Flashcards
# --------------------------------------------------------------------------- #
class FlashcardBase(BaseModel):
    """Fields common to every flashcard representation.

    Field names stay `front`/`back` (not `question`/`answer`) deliberately —
    renaming would require an Alembic column rename and break every
    endpoint already wired to these names.
    """

    front: str
    back: str
    source_reference: str | None = Field(
        default=None,
        description=(
            "Cita o fragmento exacto del documento del que se extrajo esta "
            "tarjeta, para que el frontend pueda mostrarlo como referencia "
            "bibliográfica. Nulo en tarjetas creadas antes de este campo."
        ),
    )
    deck_name: str | None = Field(
        default=None,
        description="Agrupación semántica para la vista de Mazos — por "
        "defecto, el título del documento fuente.",
    )


class FlashcardGenerateRequest(BaseModel):
    count: int = Field(default=10, ge=1, le=50)


class FlashcardCreate(FlashcardBase):
    """Payload for inserting a flashcard directly (bypassing AI generation).

    Deliberately does NOT accept `room_id` from the caller: the service
    layer always derives it from `document.room_id` — trusting a
    client-supplied `room_id` here could let someone associate a flashcard
    with a room its own document doesn't belong to, corrupting the
    denormalized invariant described in `generation/models.py`.
    """

    document_id: UUID


class FlashcardRead(FlashcardBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    room_id: UUID
    created_at: datetime


class FlashcardDueRead(FlashcardRead):
    """A due card plus the scheduling fields the client needs to show it."""

    next_review: datetime
    interval_days: int


class RoomDeckRead(BaseModel):
    """One tile in the "Mazos" grid — a deck name plus how many cards it
    has. Not tied to a DB row (no separate Deck table, see the note on
    Flashcard.deck_name) — this is just the grouped-count query result."""

    deck_name: str
    card_count: int


class FlashcardReviewRequest(BaseModel):
    grade: int = Field(ge=0, le=5, description="Calidad de recuerdo SM-2 (0–5).")


class FlashcardReviewResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    flashcard_id: UUID
    ease: float
    interval_days: int
    repetitions: int
    next_review: datetime
    last_reviewed_at: datetime | None


# --------------------------------------------------------------------------- #
# Strict schema for Gemini's structured flashcard output
# --------------------------------------------------------------------------- #
class FlashcardAIGeneration(BaseModel):
    """One AI-generated flashcard, validated against Gemini's raw JSON output.

    Unlike `FlashcardBase.source_reference` (optional, for backward
    compatibility with pre-existing rows), `source_reference` here is
    REQUIRED — this schema's whole purpose is to force every newly
    generated card to cite its source. A response missing it fails
    validation and is rejected rather than silently persisted without a
    citation.
    """

    front: str = Field(min_length=1)
    back: str = Field(min_length=1)
    source_reference: str = Field(
        min_length=1,
        description=(
            "Fragmento o cita exacta del material de origen que respalda "
            "esta tarjeta — obligatorio, nunca inferido."
        ),
    )


class FlashcardAIGenerationBatch(BaseModel):
    """The full structured-output envelope Gemini must return for a batch."""

    flashcards: list[FlashcardAIGeneration]


# --------------------------------------------------------------------------- #
# Exams
# --------------------------------------------------------------------------- #
class ExamGenerateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    num_questions: int = Field(default=5, ge=1, le=30)


class ExamQuestionRead(BaseModel):
    """Question as shown DURING an attempt — no answer key."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    position: int
    question_type: QuestionType
    prompt: str
    options: list[str]


class ExamRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    room_id: UUID
    document_id: UUID | None
    title: str | None
    status: ExamGenerationStatus
    config: dict[str, Any] | None
    created_at: datetime


class ExamDetail(ExamRead):
    questions: list[ExamQuestionRead] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Attempts
# --------------------------------------------------------------------------- #
class AttemptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    exam_id: UUID
    user_id: UUID
    status: AttemptStatus
    score: float | None
    submitted_at: datetime | None
    created_at: datetime


class AttemptAnswerRequest(BaseModel):
    question_id: UUID
    selected_index: int = Field(ge=0)


class ExamQuestionResult(BaseModel):
    """Per-question breakdown returned AFTER submit — reveals the key."""

    question_id: UUID
    prompt: str
    options: list[str]
    selected_index: int | None
    correct_index: int
    is_correct: bool
    explanation: str | None


class AttemptResult(BaseModel):
    attempt_id: UUID
    status: AttemptStatus
    score: float
    total_questions: int
    correct_count: int
    submitted_at: datetime
    breakdown: list[ExamQuestionResult]