# Ruta: src/generation/models.py
"""SQLAlchemy models for the `generation` module.

Covers AI-generated study artifacts and the spaced-repetition / exam
machinery around them: `Summary`, `Flashcard`, `FlashcardReview` (SM-2
scheduling state), `Exam`, `ExamQuestion`, `ExamAttempt`, `AttemptAnswer`.

`Flashcard` and `Exam` were part of the original DDL; the rest are added
here to support the flows in Part 5, following the same conventions
(UUID PKs, `created_at` via mixin, JSONB for flexible payloads).

`Flashcard.room_id` + `.source_reference` were added later for citation
traceability (every flashcard must be able to point back to the exact
document/fragment it was extracted from). `room_id` is a DENORMALIZED
column — `document_id` already implies a room via `Document.room_id` — kept
here anyway per explicit product requirement, for room-scoped queries
without a join. This duplication has a real consistency risk: nothing at
the database level enforces `Flashcard.room_id == Document.room_id` for its
own `document_id`. The service layer (`generation/service.py`) is the ONLY
place a `Flashcard` gets constructed, and it always derives `room_id` from
`document.room_id` rather than trusting a caller-supplied value — never
bypass that by setting `room_id` any other way.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base, CreatedAtMixin

if TYPE_CHECKING:
    from src.documents.models import Document
    from src.rooms.models import StudyRoom
    from src.users.models import User


class Summary(Base, CreatedAtMixin):
    __tablename__ = "summaries"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Free-form generation metadata (model used, key points, etc.).
    summary_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Summary id={self.id} document_id={self.document_id}>"


class Flashcard(Base, CreatedAtMixin):
    __tablename__ = "flashcards"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized from document.room_id — see the module docstring for why,
    # and the invariant the service layer must uphold.
    room_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("study_rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    front: Mapped[str] = mapped_column(Text, nullable=False)
    back: Mapped[str] = mapped_column(Text, nullable=False)
    # The exact fragment/citation this card was extracted from. Nullable
    # for backward compatibility with cards created before this column
    # existed — new AI-generated cards always populate it (enforced by the
    # FlashcardAIGeneration schema, which requires it).
    source_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Semantic grouping for the "Mazos" (decks) grid in the frontend —
    # deliberately a plain string, not a separate Deck table: it's a label,
    # not an entity with its own lifecycle. Defaults to the source
    # document's title when AI-generated (see generate_flashcards()), so
    # decks appear automatically without asking the user to name anything.
    deck_name: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)

    document: Mapped["Document"] = relationship(back_populates="flashcards")
    # NOT delete-orphan here — `document` is the single owning parent for
    # deletion purposes (a Flashcard's lifecycle is tied to its document,
    # which is the more specific of the two parents). Two delete-orphan
    # owners on the same class is a SQLAlchemy configuration conflict; see
    # `StudyRoom.flashcards` for the other half of this.
    room: Mapped["StudyRoom"] = relationship(back_populates="flashcards")
    reviews: Mapped[list["FlashcardReview"]] = relationship(
        back_populates="flashcard", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Flashcard id={self.id} document_id={self.document_id}>"


class FlashcardReview(Base):
    """Per-user SM-2 scheduling state for a flashcard.

    One row per (flashcard, user) — the `UNIQUE` matches the DDL. Holds the
    three SM-2 variables (`ease`, `interval_days`, `repetitions`) plus the
    computed `next_review` date the `GET /flashcards/due` query filters on.
    """

    __tablename__ = "flashcard_reviews"
    __table_args__ = (
        UniqueConstraint("flashcard_id", "user_id", name="uq_flashcard_reviews_card_user"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    flashcard_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("flashcards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ease: Mapped[float] = mapped_column(
        Float, default=2.5, server_default="2.5", nullable=False
    )
    interval_days: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    repetitions: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    next_review: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    flashcard: Mapped["Flashcard"] = relationship(back_populates="reviews")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<FlashcardReview card={self.flashcard_id} user={self.user_id} "
            f"ease={self.ease} interval={self.interval_days}>"
        )


class ExamGenerationStatus(str, enum.Enum):
    """Celery generation lifecycle for a room-wide exam — mirrors
    `DocumentStatus`'s PENDING/(INDEXED|FAILED) pattern. Single-document
    exams (the original, synchronous flow) skip straight to READY, since
    nothing async is happening for them."""

    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"


class Exam(Base, CreatedAtMixin):
    __tablename__ = "exams"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    document_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # NEW: room-wide exams (aggregating context across every document in a
    # room, generated via Celery — see generate_exam_task) aren't tied to
    # one document, so document_id became optional above and room_id is
    # now the field every exam has. Existing single-document exams keep
    # both set, with room_id backfilled from document.room_id (see the
    # Alembic migration note).
    room_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("study_rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Celery-backed generation status — mirrors DocumentStatus's pattern
    # (PENDING while the task runs, READY on success, FAILED otherwise) so
    # the frontend can poll GET /exams/{id} instead of blocking on the
    # generation request.
    status: Mapped[ExamGenerationStatus] = mapped_column(
        Enum(ExamGenerationStatus, name="exam_generation_status_enum", native_enum=True),
        default=ExamGenerationStatus.READY,
        server_default=ExamGenerationStatus.READY.value,
        nullable=False,
    )

    questions: Mapped[list["ExamQuestion"]] = relationship(
        back_populates="exam",
        cascade="all, delete-orphan",
        order_by="ExamQuestion.position",
    )
    attempts: Mapped[list["ExamAttempt"]] = relationship(
        back_populates="exam", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Exam id={self.id} document_id={self.document_id}>"


class QuestionType(str, enum.Enum):
    MULTIPLE_CHOICE = "MULTIPLE_CHOICE"
    TRUE_FALSE = "TRUE_FALSE"


class ExamQuestion(Base, CreatedAtMixin):
    __tablename__ = "exam_questions"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    exam_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("exams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    question_type: Mapped[QuestionType] = mapped_column(
        Enum(QuestionType, name="question_type_enum", native_enum=True),
        default=QuestionType.MULTIPLE_CHOICE,
        server_default=QuestionType.MULTIPLE_CHOICE.value,
        nullable=False,
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # List of option strings, e.g. ["A", "B", "C", "D"].
    options: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # Index into `options` of the correct choice. Never serialized to the
    # client until an attempt is submitted — see schemas.
    correct_index: Mapped[int] = mapped_column(Integer, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)

    exam: Mapped["Exam"] = relationship(back_populates="questions")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ExamQuestion id={self.id} exam_id={self.exam_id} pos={self.position}>"


class AttemptStatus(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    SUBMITTED = "SUBMITTED"


class ExamAttempt(Base, CreatedAtMixin):
    __tablename__ = "exam_attempts"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    exam_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("exams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[AttemptStatus] = mapped_column(
        Enum(AttemptStatus, name="attempt_status_enum", native_enum=True),
        default=AttemptStatus.IN_PROGRESS,
        server_default=AttemptStatus.IN_PROGRESS.value,
        nullable=False,
    )
    # Percentage 0.0–100.0, populated only on submit.
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    exam: Mapped["Exam"] = relationship(back_populates="attempts")
    answers: Mapped[list["AttemptAnswer"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ExamAttempt id={self.id} exam_id={self.exam_id} status={self.status}>"


class AttemptAnswer(Base, CreatedAtMixin):
    __tablename__ = "attempt_answers"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id", "question_id", name="uq_attempt_answers_attempt_question"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    attempt_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("exam_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("exam_questions.id", ondelete="CASCADE"),
        nullable=False,
    )
    selected_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # Graded lazily at submit time; NULL while the attempt is in progress.
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    attempt: Mapped["ExamAttempt"] = relationship(back_populates="answers")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AttemptAnswer attempt={self.attempt_id} question={self.question_id}>"