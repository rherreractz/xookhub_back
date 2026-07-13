# Ruta: src/documents/router.py
"""Router for `/api/v1/rooms/{room_id}/documents` and `/api/v1/documents/*`.

Two different URL namespaces share this one router file because both are
conceptually the `documents` module — matching the endpoint list in the
spec rather than forcing an artificial prefix split.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.responses import APIResponse
from src.core.security import SupabaseUser, verify_supabase_jwt
from src.database import get_db
from src.documents.schemas import (
    DocumentRead,
    DocumentStatusRead,
    DocumentUploadAccepted,
)
from src.documents.service import DocumentService
from src.worker.tasks import process_document_task

router = APIRouter(tags=["documents"])


@router.post(
    "/api/v1/rooms/{room_id}/documents",
    response_model=APIResponse[DocumentUploadAccepted],
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    room_id: UUID,
    file: UploadFile = File(...),
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[DocumentUploadAccepted]:
    """Accepts the multipart upload, stores it in MinIO with status=PENDING,
    commits, and only THEN enqueues the Celery ingestion task — see the
    commit-ordering note in `DocumentService.upload`."""
    document = await DocumentService(db).upload(room_id, user.id, file)
    await db.commit()
    process_document_task.delay(str(document.id))

    return APIResponse.success(
        DocumentUploadAccepted(id=document.id, status=document.status)
    )


@router.get(
    "/api/v1/rooms/{room_id}/documents",
    response_model=APIResponse[list[DocumentRead]],
)
async def list_room_documents(
    room_id: UUID,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[list[DocumentRead]]:
    documents = await DocumentService(db).list_for_room(room_id, user.id)
    return APIResponse.success([DocumentRead.model_validate(d) for d in documents])


@router.get(
    "/api/v1/documents/{document_id}/status",
    response_model=APIResponse[DocumentStatusRead],
)
async def get_document_status(
    document_id: UUID,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[DocumentStatusRead]:
    document = await DocumentService(db).get_status(document_id, user.id)
    return APIResponse.success(DocumentStatusRead.model_validate(document))


@router.delete(
    "/api/v1/documents/{document_id}",
    response_model=APIResponse[None],
)
async def delete_document(
    document_id: UUID,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[None]:
    await DocumentService(db).delete(document_id, user.id)
    return APIResponse.success(None)


@router.post(
    "/api/v1/documents/{document_id}/chunk",
    response_model=APIResponse[DocumentStatusRead],
    status_code=status.HTTP_202_ACCEPTED,
)
async def force_rechunk(
    document_id: UUID,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[DocumentStatusRead]:
    """Manual re-chunk trigger — resets status to PENDING and re-enqueues."""
    document = await DocumentService(db).request_rechunk(document_id, user.id)
    await db.commit()
    process_document_task.delay(str(document.id))

    return APIResponse.success(DocumentStatusRead.model_validate(document))