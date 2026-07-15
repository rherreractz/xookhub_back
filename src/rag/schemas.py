# Ruta: src/rag/schemas.py
"""Pydantic v2 schemas for the `rag` module."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreate(BaseModel):
    document_id: UUID | None = None
    title: str | None = Field(default=None, max_length=255)


class ConversationUpdate(BaseModel):
    """PATCH /conversations/{id} payload — renombra la conversación."""

    title: str = Field(min_length=1, max_length=255)


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    room_id: UUID
    user_id: UUID
    document_id: UUID | None
    title: str | None
    created_at: datetime


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    role: str
    content: str
    citations: list[dict[str, Any]] | None
    tokens_used: int | None
    created_at: datetime


class ConversationDetail(ConversationRead):
    messages: list[MessageRead] = Field(default_factory=list)


class MessageCreate(BaseModel):
    """Body of `POST /rooms/{id}/conversations/{conv_id}/messages`.

    The response to this endpoint is an SSE stream, not JSON — see the
    router — so there is no paired response schema here.
    """

    content: str = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Stateless room chat (POST /rooms/{id}/chat)
# --------------------------------------------------------------------------- #
class RoomChatRequest(BaseModel):
    query: str = Field(min_length=1, description="La pregunta del usuario.")


class RoomChatResponse(BaseModel):
    response: str