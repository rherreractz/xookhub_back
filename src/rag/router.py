# Ruta: src/rag/router.py
"""Router for conversations and the RAG chat stream.

The streaming endpoint returns a FastAPI `StreamingResponse` emitting
Server-Sent Events. Unlike every other endpoint in the app, it does NOT
use the request-scoped `get_db` dependency for its DB work: an SSE response
outlives the handler function (the body streams after `return`), so it
opens and owns its own `AsyncSession` for the full lifetime of the stream
and closes it when the generator finishes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.responses import APIResponse
from src.core.security import SupabaseUser, verify_supabase_jwt
from src.database import AsyncSessionLocal, get_db
from src.rag.schemas import (
    ConversationCreate,
    ConversationDetail,
    ConversationRead,
    ConversationUpdate,
    MessageCreate,
    MessageRead,
)
from src.rag.service import RAGService

router = APIRouter(tags=["rag"])


@router.post(
    "/api/v1/rooms/{room_id}/conversations",
    response_model=APIResponse[ConversationRead],
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    room_id: UUID,
    payload: ConversationCreate,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[ConversationRead]:
    conversation = await RAGService(db).create_conversation(
        room_id, user.id, document_id=payload.document_id, title=payload.title
    )
    return APIResponse.success(ConversationRead.model_validate(conversation))


@router.get(
    "/api/v1/rooms/{room_id}/conversations",
    response_model=APIResponse[list[ConversationRead]],
)
async def list_conversations(
    room_id: UUID,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[list[ConversationRead]]:
    """Historial de conversaciones DEL USUARIO ACTUAL en esta sala — no el
    de otros miembros."""
    conversations = await RAGService(db).list_conversations(room_id, user.id)
    return APIResponse.success(
        [ConversationRead.model_validate(c) for c in conversations]
    )


@router.get(
    "/api/v1/conversations/{conversation_id}",
    response_model=APIResponse[ConversationDetail],
)
async def get_conversation(
    conversation_id: UUID,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[ConversationDetail]:
    service = RAGService(db)
    conversation = await service.get_conversation(conversation_id, user.id)
    messages = await service.list_messages(conversation_id, user.id)

    # Built from ConversationRead's dump (not ConversationDetail.model_validate
    # on the ORM object directly): ConversationDetail declares a `messages`
    # field, so validating straight off `conversation` makes pydantic read
    # `conversation.messages` — a lazy relationship — outside any awaited
    # SQLAlchemy call, which raises MissingGreenlet.
    detail = ConversationDetail(
        **ConversationRead.model_validate(conversation).model_dump(),
        messages=[MessageRead.model_validate(m) for m in messages],
    )
    return APIResponse.success(detail)


@router.patch(
    "/api/v1/conversations/{conversation_id}",
    response_model=APIResponse[ConversationRead],
)
async def rename_conversation(
    conversation_id: UUID,
    payload: ConversationUpdate,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[ConversationRead]:
    conversation = await RAGService(db).rename_conversation(
        conversation_id, user.id, payload.title
    )
    return APIResponse.success(ConversationRead.model_validate(conversation))


@router.delete(
    "/api/v1/conversations/{conversation_id}",
    response_model=APIResponse[None],
)
async def delete_conversation(
    conversation_id: UUID,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[None]:
    await RAGService(db).delete_conversation(conversation_id, user.id)
    return APIResponse.success(None)


def _sse(event: str, data: dict) -> str:
    """Format a single Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/api/v1/conversations/{conversation_id}/messages")
async def stream_message(
    conversation_id: UUID,
    payload: MessageCreate,
    user: SupabaseUser = Depends(verify_supabase_jwt),
) -> StreamingResponse:
    
    async def event_generator() -> AsyncIterator[str]:
        async with AsyncSessionLocal() as db:
            service = RAGService(db)
            try:
                async for delta in service.stream_answer(
                    conversation_id, user.id, payload.content
                ):
                    yield _sse("token", {"delta": delta})
                yield _sse("done", {"conversation_id": str(conversation_id)})
            except Exception as exc:  # surface a clean terminal SSE error
                await db.rollback()
                yield _sse("error", {"message": str(exc)})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable Nginx proxy buffering for SSE
        },
    )