# Ruta: src/rag/service.py
"""
RAG orchestration for the `rag` module.

Retrieval uses pgvector's cosine-distance operator (`<=>`) through
SQLAlchemy, and EVERY retrieval query is filtered by `room_id` so a
conversation can only ever surface chunks from documents in its own room —
this join-level filter is the multi-tenant isolation guarantee for the RAG
path, mirroring the role checks that guard the rest of the room's data.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import NotFoundException, ValidationException
from src.documents.models import Document, DocumentChunk
from src.rag.llm_adapter import ChatMessage, LLMAdapter, get_llm_adapter
from src.rag.models import Conversation, Message
from src.rag.prompts import (
    QUICK_ANSWER_INSTRUCTION,
    RAG_SYSTEM_PROMPT,
    RetrievedChunk,
    build_quick_answer_user_turn,
    build_user_turn,
)
from src.rooms.models import RoomRole
from src.rooms.service import RoomService

DEFAULT_TOP_K = 5


class RAGService:
    """Encapsulates conversation persistence and the retrieve→generate flow."""

    def __init__(self, db: AsyncSession, adapter: LLMAdapter | None = None) -> None:
        self._db = db
        self._room_service = RoomService(db)
        self._adapter = adapter or get_llm_adapter()

    # --- Conversation lifecycle -----------------------------------------
    async def create_conversation(
        self,
        room_id: UUID,
        user_id: UUID,
        *,
        document_id: UUID | None = None,
        title: str | None = None,
    ) -> Conversation:
        await self._room_service.require_role(room_id, user_id, RoomRole.VIEWER)

        conversation = Conversation(
            room_id=room_id,
            user_id=user_id,
            document_id=document_id,
            title=title,
        )
        self._db.add(conversation)
        await self._db.flush()
        await self._db.refresh(conversation)
        return conversation

    async def get_conversation(
        self, conversation_id: UUID, user_id: UUID
    ) -> Conversation:
        conversation = await self._db.get(Conversation, conversation_id)
        if conversation is None:
            raise NotFoundException(f"Conversación {conversation_id} no encontrada.")
        # Authorization is inherited from the room, consistent with the rest
        # of the app — membership check doubles as an existence guard.
        await self._room_service.require_role(
            conversation.room_id, user_id, RoomRole.VIEWER
        )
        return conversation

    async def list_messages(
        self, conversation_id: UUID, user_id: UUID
    ) -> list[Message]:
        await self.get_conversation(conversation_id, user_id)
        result = await self._db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at)
        )
        return list(result.scalars().all())

    # --- Retrieval -------------------------------------------------------
    async def retrieve(
        self, room_id: UUID, query_embedding: list[float], *, top_k: int = DEFAULT_TOP_K
    ) -> list[RetrievedChunk]:
        """Return the `top_k` chunks nearest to `query_embedding` by cosine
        distance, restricted to documents belonging to `room_id`.

        The `<=>` operator is pgvector's cosine distance; `.cosine_distance`
        is SQLAlchemy's binding for it. The join to `documents` plus the
        `Document.room_id == room_id` predicate is what enforces tenant
        isolation — without it, a query could retrieve chunks from any room.
        """
        distance = DocumentChunk.embedding.cosine_distance(query_embedding)
        stmt = (
            select(
                DocumentChunk.id,
                DocumentChunk.document_id,
                DocumentChunk.content,
                DocumentChunk.page_number,
                distance.label("distance"),
            )
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                Document.room_id == room_id,
                DocumentChunk.embedding.isnot(None),
            )
            .order_by(distance)
            .limit(top_k)
        )
        result = await self._db.execute(stmt)

        chunks: list[RetrievedChunk] = []
        for source_index, row in enumerate(result.all(), start=1):
            chunks.append(
                RetrievedChunk(
                    source_index=source_index,
                    chunk_id=str(row.id),
                    document_id=str(row.document_id),
                    content=row.content,
                    page_number=row.page_number,
                    distance=float(row.distance),
                )
            )
        return chunks

    # --- Generation ------------------------------------------------------
    async def stream_answer(
        self, conversation_id: UUID, user_id: UUID, question: str
    ) -> AsyncIterator[str]:
        """Full RAG turn as an async generator of answer deltas.

        Persists the user message immediately, retrieves grounded context,
        streams the model's reply while accumulating it, then persists the
        assistant message (with citations) once the stream completes.

        NOTE: this generator manages its own commit at the end. The SSE
        endpoint that consumes it must NOT wrap it in the request-scoped
        `get_db` transaction (which would commit/close too early); it uses
        a dedicated session — see `rag/router.py`.
        """
        conversation = await self.get_conversation(conversation_id, user_id)

        # 1. Persist the user's message.
        self._db.add(
            Message(conversation_id=conversation.id, role="user", content=question)
        )
        await self._db.flush()

        # 2. Retrieve grounded context (tenant-isolated by room_id).
        query_embedding = await self._adapter.embed(question)
        chunks = await self.retrieve(conversation.room_id, query_embedding)

        # 3. Assemble the prompt and stream the answer.
        prompt_messages = [
            ChatMessage(role="system", content=RAG_SYSTEM_PROMPT),
            ChatMessage(role="user", content=build_user_turn(question, chunks)),
        ]

        collected: list[str] = []
        async for delta in self._adapter.stream_chat(prompt_messages):
            collected.append(delta)
            yield delta

        # 4. Persist the assistant message with its citations.
        citations = [
            {
                "source_index": c.source_index,
                "chunk_id": c.chunk_id,
                "document_id": c.document_id,
                "page_number": c.page_number,
                "distance": c.distance,
            }
            for c in chunks
        ]
        self._db.add(
            Message(
                conversation_id=conversation.id,
                role="assistant",
                content="".join(collected),
                citations=citations or None,
            )
        )
        await self._db.commit()

    async def quick_answer(self, room_id: UUID, user_id: UUID, query: str) -> str:
        """Stateless retrieve-then-generate turn for `POST /rooms/{id}/chat`.

        Unlike `stream_answer` (the SSE conversation endpoint), this does
        NOT persist a Conversation/Message — it's a one-shot Q&A against a
        room's indexed documents for callers that just want a plain answer
        back, not a running chat history. Reuses `retrieve()` so the
        pgvector cosine-distance query and its `room_id` tenant-isolation
        filter are never duplicated.
        """
        await self._room_service.require_role(room_id, user_id, RoomRole.VIEWER)

        if not query or not query.strip():
            raise ValidationException("La consulta ('query') no puede estar vacía.")

        query_embedding = await self._adapter.embed(query)
        chunks = await self.retrieve(room_id, query_embedding)

        messages = [
            ChatMessage(role="system", content=QUICK_ANSWER_INSTRUCTION),
            ChatMessage(role="user", content=build_quick_answer_user_turn(query, chunks)),
        ]

        # No SSE here — this endpoint returns one plain JSON response, so we
        # fully drain the (still token-by-token) stream and join it rather
        # than adding a separate non-streaming adapter method just for this.
        parts: list[str] = []
        async for delta in self._adapter.stream_chat(messages):
            parts.append(delta)
        return "".join(parts).strip()