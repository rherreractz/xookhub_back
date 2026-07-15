# Ruta: src/generation/service.py
"""
Business logic for the `generation` module.

Three concerns live here, all room-authorized through `RoomService`:
  1. AI generation (summaries / flashcards / exams) via the shared
     `LLMAdapter` from the `rag` module, parsing strict-JSON model output.
  2. The SM-2 flashcard review flow (delegating the math to `sm2.py`).
  3. The exam attempt lifecycle: start → answer → submit+grade.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import (
    AppException,
    ConflictException,
    NotFoundException,
    ValidationException,
)
from src.documents.models import Document, DocumentChunk
from src.generation.models import (
    AttemptAnswer,
    AttemptStatus,
    Exam,
    ExamAttempt,
    ExamGenerationStatus,
    ExamQuestion,
    Flashcard,
    FlashcardReview,
    QuestionType,
    Summary,
)
from src.generation.prompts import (
    EXAM_SYSTEM_PROMPT,
    FLASHCARDS_SYSTEM_PROMPT,
    SUMMARY_SYSTEM_PROMPT,
    build_material_turn,
)
from src.generation.schemas import FlashcardAIGenerationBatch
from src.generation.sm2 import SM2State, compute_sm2
from src.rag.llm_adapter import ChatMessage, LLMAdapter, get_llm_adapter
from src.rooms.models import RoomRole
from src.rooms.service import RoomService

# Cap how much document text we feed the model per generation call. A real
# deployment would map-reduce over all chunks; for now we take the leading
# slice, which is deterministic and bounded.
_MAX_MATERIAL_CHARS = 12_000


class GenerationError(AppException):
    code = "GENERATION_ERROR"
    status_code = 502


logger = logging.getLogger("xookhub.generation")


class GenerationService:
    def __init__(self, db: AsyncSession, adapter: LLMAdapter | None = None) -> None:
        self._db = db
        self._room_service = RoomService(db)
        self._adapter = adapter or get_llm_adapter()

    # --- Shared helpers --------------------------------------------------
    async def _get_document_authorized(
        self, document_id: UUID, user_id: UUID, minimum: RoomRole
    ) -> Document:
        document = await self._db.get(Document, document_id)
        if document is None:
            raise NotFoundException(f"Documento {document_id} no encontrado.")
        await self._room_service.require_role(document.room_id, user_id, minimum)
        return document

    async def _load_material(self, document_id: UUID) -> str:
        """Concatenate the document's chunk text, bounded by _MAX_MATERIAL_CHARS."""
        result = await self._db.execute(
            select(DocumentChunk.content)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index)
        )
        pieces = list(result.scalars().all())
        if not pieces:
            raise ValidationException(
                "El documento aún no tiene contenido indexado para generar material."
            )
        material = "\n\n".join(pieces)
        return material[:_MAX_MATERIAL_CHARS]

    async def _generate_json(self, system_prompt: str, material: str) -> dict:
        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=build_material_turn(material)),
        ]
        raw = await self._adapter.complete_json(messages)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GenerationError(
                "El modelo devolvió una respuesta no válida (JSON malformado)."
            ) from exc

    # --- Summaries -------------------------------------------------------
    async def generate_summary(self, document_id: UUID, user_id: UUID) -> Summary:
        document = await self._get_document_authorized(
            document_id, user_id, RoomRole.MEMBER
        )
        material = await self._load_material(document_id)
        data = await self._generate_json(SUMMARY_SYSTEM_PROMPT, material)

        content = (data.get("summary") or "").strip()
        if not content:
            raise GenerationError("El modelo no devolvió un resumen.")

        summary = Summary(
            document_id=document.id,
            created_by=user_id,
            content=content,
            summary_metadata={"key_points": data.get("key_points", [])},
        )
        self._db.add(summary)
        await self._db.flush()
        await self._db.refresh(summary)
        return summary

    # --- Flashcards ------------------------------------------------------
    async def generate_flashcards(
        self, document_id: UUID, user_id: UUID, count: int
    ) -> list[Flashcard]:
        document = await self._get_document_authorized(
            document_id, user_id, RoomRole.MEMBER
        )
        material = await self._load_material(document_id)
        prompt = FLASHCARDS_SYSTEM_PROMPT.format(count=count)
        data = await self._generate_json(prompt, material)

        try:
            batch = FlashcardAIGenerationBatch.model_validate(data)
        except ValidationError as exc:
            raise GenerationError(
                "El modelo devolvió flashcards con una forma inválida "
                "(falta front/back/source_reference en alguna tarjeta)."
            ) from exc

        if not batch.flashcards:
            raise GenerationError("El modelo no devolvió flashcards.")

        cards: list[Flashcard] = []
        for item in batch.flashcards:
            card = Flashcard(
                document_id=document.id,
                # ALWAYS derived from the document, never trusted from the
                # model's output or any caller input — see the invariant
                # note on Flashcard.room_id in generation/models.py.
                room_id=document.room_id,
                created_by=user_id,
                front=item.front,
                back=item.back,
                source_reference=item.source_reference,
                # Defaults every AI-generated batch to a deck named after
                # its source document — decks appear in the "Mazos" grid
                # automatically, without asking the user to name anything.
                deck_name=document.title,
            )
            self._db.add(card)
            cards.append(card)

        await self._db.flush()
        for card in cards:
            await self._db.refresh(card)
        return cards

    async def list_decks(self, room_id: UUID, user_id: UUID) -> list[tuple[str, int]]:
        """Distinct deck names in a room + how many cards each has — the
        "Mazos" grid shows these; drilling into one filters the existing
        SM-2 study queue (`list_due_flashcards`) client-side by deck_name,
        the same way it's already filtered client-side by room_id."""
        await self._room_service.require_role(room_id, user_id, RoomRole.VIEWER)

        result = await self._db.execute(
            select(Flashcard.deck_name, func.count(Flashcard.id))
            .where(Flashcard.room_id == room_id)
            .group_by(Flashcard.deck_name)
            .order_by(Flashcard.deck_name)
        )
        return [(name or "Sin nombre", count) for name, count in result.all()]

    async def list_flashcards(
        self, room_id: UUID, user_id: UUID, *, deck_name: str | None = None
    ) -> list[Flashcard]:
        """All flashcards in a room, optionally filtered to one deck.

        Unlike `list_due_flashcards`, this doesn't require a prior
        `FlashcardReview` — it's how the client sees a freshly generated
        deck's actual cards for the first time, before any of them have
        ever been reviewed.
        """
        await self._room_service.require_role(room_id, user_id, RoomRole.VIEWER)

        stmt = select(Flashcard).where(Flashcard.room_id == room_id)
        if deck_name is not None:
            stmt = stmt.where(Flashcard.deck_name == deck_name)
        stmt = stmt.order_by(Flashcard.created_at)

        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def list_due_flashcards(
        self, user_id: UUID, *, now: datetime | None = None
    ) -> list[tuple[Flashcard, FlashcardReview]]:
        """Cards whose `next_review` has arrived for this user.

        Only returns cards the user has already reviewed at least once (i.e.
        that have a `FlashcardReview` row). Brand-new, never-reviewed cards
        are surfaced through the flashcard listing, not the "due" queue.
        """
        now = now or datetime.now(timezone.utc)
        result = await self._db.execute(
            select(Flashcard, FlashcardReview)
            .join(FlashcardReview, FlashcardReview.flashcard_id == Flashcard.id)
            .where(
                FlashcardReview.user_id == user_id,
                FlashcardReview.next_review <= now,
            )
            .order_by(FlashcardReview.next_review)
        )
        return [(row[0], row[1]) for row in result.all()]

    async def review_flashcard(
        self, flashcard_id: UUID, user_id: UUID, grade: int
    ) -> FlashcardReview:
        """Apply an SM-2 review, upserting the per-user review row.

        The card's document lives in a room; the user must be at least a
        VIEWER of that room to review it.
        """
        card = await self._db.get(Flashcard, flashcard_id)
        if card is None:
            raise NotFoundException(f"Flashcard {flashcard_id} no encontrada.")
        document = await self._db.get(Document, card.document_id)
        if document is not None:
            await self._room_service.require_role(
                document.room_id, user_id, RoomRole.VIEWER
            )

        result = await self._db.execute(
            select(FlashcardReview).where(
                FlashcardReview.flashcard_id == flashcard_id,
                FlashcardReview.user_id == user_id,
            )
        )
        review = result.scalar_one_or_none()

        now = datetime.now(timezone.utc)
        if review is None:
            # First-ever review: seed with SM-2 defaults, then apply grade.
            state = SM2State(ease=2.5, interval_days=0, repetitions=0)
            outcome = compute_sm2(state, grade, now=now)
            review = FlashcardReview(
                flashcard_id=flashcard_id,
                user_id=user_id,
                ease=outcome.ease,
                interval_days=outcome.interval_days,
                repetitions=outcome.repetitions,
                next_review=outcome.next_review,
                last_reviewed_at=now,
            )
            self._db.add(review)
        else:
            state = SM2State(
                ease=review.ease,
                interval_days=review.interval_days,
                repetitions=review.repetitions,
            )
            outcome = compute_sm2(state, grade, now=now)
            review.ease = outcome.ease
            review.interval_days = outcome.interval_days
            review.repetitions = outcome.repetitions
            review.next_review = outcome.next_review
            review.last_reviewed_at = now

        await self._db.flush()
        await self._db.refresh(review)
        return review

    # --- Exams -----------------------------------------------------------
    async def _persist_exam_questions(self, exam_id: UUID, raw_questions: list[dict]) -> int:
        """Validate + persist Gemini's raw question list against `exam_id`.

        Shared by both the original per-document flow (generate_exam,
        synchronous) and the new room-wide flow (generate_room_exam_
        questions, Celery) — the validation rules for "is this a gradeable
        question" don't depend on where the material came from.
        """
        position = 0
        for rq in raw_questions:
            prompt_text = (rq.get("prompt") or "").strip()
            options = rq.get("options") or []
            correct_index = rq.get("correct_index")
            # Defensive validation: skip anything the model got structurally
            # wrong rather than persisting an ungradeable question.
            if (
                not prompt_text
                or not isinstance(options, list)
                or len(options) < 2
                or not isinstance(correct_index, int)
                or not 0 <= correct_index < len(options)
            ):
                continue
            self._db.add(
                ExamQuestion(
                    exam_id=exam_id,
                    position=position,
                    question_type=QuestionType.MULTIPLE_CHOICE,
                    prompt=prompt_text,
                    options=[str(o) for o in options],
                    correct_index=correct_index,
                    explanation=(rq.get("explanation") or None),
                )
            )
            position += 1
        return position

    async def generate_exam(
        self, document_id: UUID, user_id: UUID, title: str | None, num_questions: int
    ) -> Exam:
        document = await self._get_document_authorized(
            document_id, user_id, RoomRole.MEMBER
        )
        material = await self._load_material(document_id)
        prompt = EXAM_SYSTEM_PROMPT.format(num_questions=num_questions)
        data = await self._generate_json(prompt, material)

        raw_questions = data.get("questions") or []
        if not raw_questions:
            raise GenerationError("El modelo no devolvió preguntas.")

        exam = Exam(
            document_id=document.id,
            room_id=document.room_id,
            created_by=user_id,
            title=title or f"Examen: {document.title}",
            status=ExamGenerationStatus.READY,  # synchronous flow — done immediately
            config={"requested_questions": num_questions},
        )
        self._db.add(exam)
        await self._db.flush()

        created = await self._persist_exam_questions(exam.id, raw_questions)
        if created == 0:
            raise GenerationError("Ninguna pregunta generada era válida.")

        await self._db.flush()
        await self._db.refresh(exam)
        return exam

    # --- Room-wide exams (Celery-backed) ---------------------------------
    _MAX_ROOM_MATERIAL_CHARS = 60_000  # ~ a few dozen chunks; keeps the
    # Gemini prompt bounded even for a room with many large documents.

    async def _load_room_material(self, room_id: UUID) -> str:
        """Consolidated chunk text across EVERY document in a room —
        the room-wide counterpart to `_load_material` (single document).
        """
        result = await self._db.execute(
            select(DocumentChunk.content)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(Document.room_id == room_id)
            .order_by(Document.created_at, DocumentChunk.chunk_index)
        )
        pieces = list(result.scalars().all())
        if not pieces:
            raise ValidationException(
                "Esta sala aún no tiene documentos con contenido indexado."
            )
        material = "\n\n".join(pieces)
        return material[: self._MAX_ROOM_MATERIAL_CHARS]

    async def create_room_exam_placeholder(
        self, room_id: UUID, user_id: UUID, title: str | None, num_questions: int
    ) -> Exam:
        """Creates the Exam row immediately (status=PENDING) so the router
        has an id to return/dispatch with — the actual Gemini call and
        question persistence happen in `generate_room_exam_questions`,
        invoked from Celery (`generate_exam_task`). Mirrors the document
        ingestion pattern: create the row, commit, THEN dispatch the task.
        """
        await self._room_service.require_role(room_id, user_id, RoomRole.MEMBER)

        exam = Exam(
            room_id=room_id,
            document_id=None,
            created_by=user_id,
            title=title or "Examen de la sala",
            status=ExamGenerationStatus.PENDING,
            config={"requested_questions": num_questions},
        )
        self._db.add(exam)
        await self._db.flush()
        await self._db.refresh(exam)
        return exam

    async def generate_room_exam_questions(self, exam_id: UUID) -> None:
        """Runs inside the Celery task — fills in questions for a PENDING
        room-wide exam and flips its status to READY or FAILED.

        Every exception is caught here (not re-raised) deliberately: a
        Celery retry would just re-run the whole Gemini call again, but
        the more useful behavior for a generation failure the frontend is
        actively polling for is a terminal FAILED status the UI can show
        immediately, not a silent multi-minute retry loop.
        """
        exam = await self._db.get(Exam, exam_id)
        if exam is None:
            logger.warning("generate_room_exam_questions: exam %s not found", exam_id)
            return

        try:
            material = await self._load_room_material(exam.room_id)
            num_questions = (exam.config or {}).get("requested_questions", 10)
            prompt = EXAM_SYSTEM_PROMPT.format(num_questions=num_questions)
            data = await self._generate_json(prompt, material)

            raw_questions = data.get("questions") or []
            if not raw_questions:
                raise GenerationError("El modelo no devolvió preguntas.")

            created = await self._persist_exam_questions(exam.id, raw_questions)
            if created == 0:
                raise GenerationError("Ninguna pregunta generada era válida.")

            exam.status = ExamGenerationStatus.READY
            await self._db.commit()
        except Exception:
            logger.exception("Room exam generation failed for exam %s", exam_id)
            await self._db.rollback()
            # Re-fetch: the failed transaction above was rolled back, so
            # `exam` needs a fresh, clean session state before this update.
            exam = await self._db.get(Exam, exam_id)
            if exam is not None:
                exam.status = ExamGenerationStatus.FAILED
                await self._db.commit()

    async def list_room_exams(self, room_id: UUID, user_id: UUID) -> list[Exam]:
        """Every exam ever generated in a room (any status), newest first —
        the room-wide counterpart to `list_flashcards`, so the client can
        show a history of exams the same way it shows flashcard decks."""
        await self._room_service.require_role(room_id, user_id, RoomRole.VIEWER)

        result = await self._db.execute(
            select(Exam)
            .where(Exam.room_id == room_id)
            .order_by(Exam.created_at.desc())
        )
        return list(result.scalars().all())

    async def _get_exam_authorized(
        self, exam_id: UUID, user_id: UUID, minimum: RoomRole
    ) -> Exam:
        exam = await self._db.get(Exam, exam_id)
        if exam is None:
            raise NotFoundException(f"Examen {exam_id} no encontrado.")
        # exam.room_id is unconditionally set now (both per-document and
        # room-wide exams have it) — checking through document_id was a
        # real gap: document_id is nullable for room-wide exams, so that
        # branch used to SKIP authorization entirely whenever it was None.
        await self._room_service.require_role(exam.room_id, user_id, minimum)
        return exam

    async def get_exam(self, exam_id: UUID, user_id: UUID) -> Exam:
        return await self._get_exam_authorized(exam_id, user_id, RoomRole.VIEWER)

    # --- Attempt lifecycle ----------------------------------------------
    async def start_attempt(self, exam_id: UUID, user_id: UUID) -> ExamAttempt:
        await self._get_exam_authorized(exam_id, user_id, RoomRole.VIEWER)
        attempt = ExamAttempt(
            exam_id=exam_id, user_id=user_id, status=AttemptStatus.IN_PROGRESS
        )
        self._db.add(attempt)
        await self._db.flush()
        await self._db.refresh(attempt)
        return attempt

    async def _get_own_attempt(self, attempt_id: UUID, user_id: UUID) -> ExamAttempt:
        attempt = await self._db.get(ExamAttempt, attempt_id)
        if attempt is None:
            raise NotFoundException(f"Intento {attempt_id} no encontrado.")
        # An attempt is private to the user who started it — not shared at
        # the room level like the exam itself.
        if attempt.user_id != user_id:
            raise NotFoundException(f"Intento {attempt_id} no encontrado.")
        return attempt

    async def submit_answer(
        self, attempt_id: UUID, user_id: UUID, question_id: UUID, selected_index: int
    ) -> AttemptAnswer:
        attempt = await self._get_own_attempt(attempt_id, user_id)
        if attempt.status is AttemptStatus.SUBMITTED:
            raise ConflictException("Este intento ya fue enviado; no admite más respuestas.")

        question = await self._db.get(ExamQuestion, question_id)
        if question is None or question.exam_id != attempt.exam_id:
            raise NotFoundException("La pregunta no pertenece a este examen.")
        if not 0 <= selected_index < len(question.options):
            raise ValidationException("La opción seleccionada está fuera de rango.")

        # Upsert: re-answering a question overwrites the previous choice.
        result = await self._db.execute(
            select(AttemptAnswer).where(
                AttemptAnswer.attempt_id == attempt_id,
                AttemptAnswer.question_id == question_id,
            )
        )
        answer = result.scalar_one_or_none()
        if answer is None:
            answer = AttemptAnswer(
                attempt_id=attempt_id,
                question_id=question_id,
                selected_index=selected_index,
            )
            self._db.add(answer)
        else:
            answer.selected_index = selected_index

        await self._db.flush()
        await self._db.refresh(answer)
        return answer

    async def submit_attempt(self, attempt_id: UUID, user_id: UUID) -> dict:
        """Grade every question, persist the score, and return a breakdown.

        Grading happens here (not per-answer) so the correct index never
        needs to be exposed until the whole attempt is finalized.
        """
        attempt = await self._get_own_attempt(attempt_id, user_id)
        if attempt.status is AttemptStatus.SUBMITTED:
            raise ConflictException("Este intento ya fue enviado.")

        questions = list(
            (
                await self._db.execute(
                    select(ExamQuestion)
                    .where(ExamQuestion.exam_id == attempt.exam_id)
                    .order_by(ExamQuestion.position)
                )
            ).scalars().all()
        )
        if not questions:
            raise ValidationException("El examen no tiene preguntas para calificar.")

        answers = list(
            (
                await self._db.execute(
                    select(AttemptAnswer).where(AttemptAnswer.attempt_id == attempt_id)
                )
            ).scalars().all()
        )
        answers_by_q = {a.question_id: a for a in answers}

        correct_count = 0
        breakdown: list[dict] = []
        for question in questions:
            answer = answers_by_q.get(question.id)
            selected = answer.selected_index if answer is not None else None
            is_correct = selected == question.correct_index
            if answer is not None:
                answer.is_correct = is_correct
            if is_correct:
                correct_count += 1
            breakdown.append(
                {
                    "question_id": question.id,
                    "prompt": question.prompt,
                    "options": question.options,
                    "selected_index": selected,
                    "correct_index": question.correct_index,
                    "is_correct": is_correct,
                    "explanation": question.explanation,
                }
            )

        total = len(questions)
        score = round(correct_count / total * 100, 2)
        now = datetime.now(timezone.utc)

        attempt.status = AttemptStatus.SUBMITTED
        attempt.score = score
        attempt.submitted_at = now
        await self._db.flush()

        return {
            "attempt_id": attempt.id,
            "status": attempt.status,
            "score": score,
            "total_questions": total,
            "correct_count": correct_count,
            "submitted_at": now,
            "breakdown": breakdown,
        }