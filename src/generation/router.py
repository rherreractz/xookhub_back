# Ruta: src/generation/router.py
"""Router for the `generation` module.

Spans several URL namespaces (documents, flashcards, exams, attempts) that
all belong conceptually to generation, matching the endpoint list in the
spec. Every endpoint injects `Depends(verify_supabase_jwt)`.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.responses import APIResponse
from src.core.security import SupabaseUser, verify_supabase_jwt
from src.database import get_db
from src.generation.schemas import (
    AttemptAnswerRequest,
    AttemptRead,
    AttemptResult,
    ExamDetail,
    ExamGenerateRequest,
    ExamRead,
    FlashcardDueRead,
    FlashcardGenerateRequest,
    FlashcardRead,
    FlashcardReviewRequest,
    FlashcardReviewResult,
    RoomDeckRead,
    SummaryRead,
)
from src.generation.service import GenerationService
from src.worker.tasks import generate_exam_task

router = APIRouter(tags=["generation"])


# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #
@router.post(
    "/api/v1/documents/{document_id}/summaries",
    response_model=APIResponse[SummaryRead],
    status_code=status.HTTP_201_CREATED,
)
async def generate_summary(
    document_id: UUID,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[SummaryRead]:
    summary = await GenerationService(db).generate_summary(document_id, user.id)
    return APIResponse.success(SummaryRead.model_validate(summary))


# --------------------------------------------------------------------------- #
# Flashcards
# --------------------------------------------------------------------------- #
@router.post(
    "/api/v1/documents/{document_id}/flashcards/generate",
    response_model=APIResponse[list[FlashcardRead]],
    status_code=status.HTTP_201_CREATED,
)
async def generate_flashcards(
    document_id: UUID,
    payload: FlashcardGenerateRequest,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[list[FlashcardRead]]:
    cards = await GenerationService(db).generate_flashcards(
        document_id, user.id, payload.count
    )
    return APIResponse.success([FlashcardRead.model_validate(c) for c in cards])


@router.get(
    "/api/v1/flashcards/due",
    response_model=APIResponse[list[FlashcardDueRead]],
)
async def get_due_flashcards(
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[list[FlashcardDueRead]]:
    due = await GenerationService(db).list_due_flashcards(user.id)
    payload = [
        FlashcardDueRead(
            id=card.id,
            document_id=card.document_id,
            room_id=card.room_id,
            front=card.front,
            back=card.back,
            source_reference=card.source_reference,
            created_at=card.created_at,
            next_review=review.next_review,
            interval_days=review.interval_days,
        )
        for card, review in due
    ]
    return APIResponse.success(payload)


@router.post(
    "/api/v1/flashcards/{flashcard_id}/review",
    response_model=APIResponse[FlashcardReviewResult],
)
async def review_flashcard(
    flashcard_id: UUID,
    payload: FlashcardReviewRequest,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[FlashcardReviewResult]:
    review = await GenerationService(db).review_flashcard(
        flashcard_id, user.id, payload.grade
    )
    return APIResponse.success(FlashcardReviewResult.model_validate(review))


# --------------------------------------------------------------------------- #
# Exams
# --------------------------------------------------------------------------- #
# Exams — room-wide (Celery-backed)
# --------------------------------------------------------------------------- #
@router.post(
    "/api/v1/rooms/{room_id}/exams/generate",
    response_model=APIResponse[ExamRead],
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_room_exam(
    room_id: UUID,
    payload: ExamGenerateRequest,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[ExamRead]:
    """Creates the exam row (status=PENDING) and hands off to Celery —
    202, not 201: the exam isn't actually ready yet. The frontend polls
    `GET /exams/{id}` until `status` flips to READY or FAILED, the same
    pattern already used for document ingestion."""
    exam = await GenerationService(db).create_room_exam_placeholder(
        room_id, user.id, payload.title, payload.num_questions
    )
    # Commit BEFORE dispatching — the worker needs to see this row when it
    # picks up the task (same reasoning as the document-upload endpoint).
    await db.commit()
    generate_exam_task.delay(str(exam.id))
    return APIResponse.success(ExamRead.model_validate(exam))


@router.get(
    "/api/v1/rooms/{room_id}/exams",
    response_model=APIResponse[list[ExamRead]],
)
async def list_room_exams(
    room_id: UUID,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[list[ExamRead]]:
    """Every exam generated in a room (any status), newest first — the
    history view, analogous to how flashcard decks are listed."""
    exams = await GenerationService(db).list_room_exams(room_id, user.id)
    return APIResponse.success([ExamRead.model_validate(e) for e in exams])


@router.get(
    "/api/v1/exams/{exam_id}",
    response_model=APIResponse[ExamDetail],
)
async def get_exam(
    exam_id: UUID,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[ExamDetail]:
    """Fetches the exam + its questions (no answer key — see
    ExamQuestionRead). Used both for polling generation status and for
    actually taking the exam once status=READY."""
    service = GenerationService(db)
    exam = await service.get_exam(exam_id, user.id)
    await db.refresh(exam, attribute_names=["questions"])
    return APIResponse.success(ExamDetail.model_validate(exam))


# --------------------------------------------------------------------------- #
# Flashcards — decks
# --------------------------------------------------------------------------- #
@router.get(
    "/api/v1/rooms/{room_id}/flashcards",
    response_model=APIResponse[list[FlashcardRead]],
)
async def list_flashcards(
    room_id: UUID,
    deck_name: str | None = None,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[list[FlashcardRead]]:
    """Every flashcard in a room (optionally filtered to one deck) — how the
    client sees a deck's actual cards, including ones never reviewed yet
    (which `/flashcards/due` deliberately excludes)."""
    cards = await GenerationService(db).list_flashcards(
        room_id, user.id, deck_name=deck_name
    )
    return APIResponse.success([FlashcardRead.model_validate(c) for c in cards])


@router.get(
    "/api/v1/rooms/{room_id}/flashcards/decks",
    response_model=APIResponse[list[RoomDeckRead]],
)
async def list_room_decks(
    room_id: UUID,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[list[RoomDeckRead]]:
    decks = await GenerationService(db).list_decks(room_id, user.id)
    payload = [RoomDeckRead(deck_name=name, card_count=count) for name, count in decks]
    return APIResponse.success(payload)


# --------------------------------------------------------------------------- #
# Exams — per-document (original, synchronous flow)
# --------------------------------------------------------------------------- #
@router.post(
    "/api/v1/documents/{document_id}/exams/generate",
    response_model=APIResponse[ExamRead],
    status_code=status.HTTP_201_CREATED,
)
async def generate_exam(
    document_id: UUID,
    payload: ExamGenerateRequest,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[ExamRead]:
    exam = await GenerationService(db).generate_exam(
        document_id, user.id, payload.title, payload.num_questions
    )
    return APIResponse.success(ExamRead.model_validate(exam))


# --------------------------------------------------------------------------- #
# Attempts
# --------------------------------------------------------------------------- #
@router.post(
    "/api/v1/exams/{exam_id}/attempts",
    response_model=APIResponse[AttemptRead],
    status_code=status.HTTP_201_CREATED,
)
async def start_attempt(
    exam_id: UUID,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[AttemptRead]:
    attempt = await GenerationService(db).start_attempt(exam_id, user.id)
    return APIResponse.success(AttemptRead.model_validate(attempt))


@router.post(
    "/api/v1/attempts/{attempt_id}/answers",
    response_model=APIResponse[None],
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_answer(
    attempt_id: UUID,
    payload: AttemptAnswerRequest,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[None]:
    await GenerationService(db).submit_answer(
        attempt_id, user.id, payload.question_id, payload.selected_index
    )
    return APIResponse.success(None)


@router.post(
    "/api/v1/attempts/{attempt_id}/submit",
    response_model=APIResponse[AttemptResult],
)
async def submit_attempt(
    attempt_id: UUID,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[AttemptResult]:
    result = await GenerationService(db).submit_attempt(attempt_id, user.id)
    return APIResponse.success(AttemptResult(**result))