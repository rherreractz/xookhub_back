# Ruta: src/documents/schemas.py
"""Pydantic v2 schemas for the `documents` module.

`file_path` (the internal MinIO object key) is deliberately never exposed
on any response schema — clients that need the actual file get it through
a dedicated presigned-URL endpoint in a later part, not by reading this
field directly.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from src.documents.models import DocumentStatus


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    room_id: UUID
    uploaded_by: UUID | None
    title: str
    mime_type: str | None
    size_bytes: int | None
    status: DocumentStatus
    created_at: datetime


class DocumentStatusRead(BaseModel):
    """Minimal payload for polling ingestion progress."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: DocumentStatus


class DocumentUploadAccepted(BaseModel):
    """Body returned alongside the `202` from `POST /rooms/{id}/documents`."""

    id: UUID
    status: DocumentStatus
    message: str = "Documento recibido, procesamiento en curso."