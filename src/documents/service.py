# Ruta: src/documents/service.py
"""
Business logic for the `documents` module: MinIO upload/storage and
persistence. Task dispatch to Celery is deliberately kept OUT of this
class and lives in `documents/router.py` instead — see the note on
`upload()` below for why.
"""

from __future__ import annotations

import io
from pathlib import PurePosixPath
from uuid import UUID

from fastapi import UploadFile
from minio import Minio
from minio.error import S3Error
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.core.exceptions import AppException, AuthorizationException, NotFoundException
from src.documents.models import Document, DocumentStatus
from src.rooms.models import RoomRole
from src.rooms.service import RoomService

settings = get_settings()

# Mime types accepted at ingestion time as-is (trusted from the browser) —
# expand as real parsers land in `documents/parser.py`.
_ALLOWED_MIME_TYPES = {
    "text/plain",
    "text/markdown",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# Source code extensions accepted regardless of the browser-reported
# Content-Type. Browsers guess MIME types for code files inconsistently
# (a .py might arrive as text/plain, application/octet-stream, or
# text/x-python depending on OS/browser) — extension is the only reliable
# signal here, so validation and canonicalization both key off it instead.
_CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".sql", ".sh",
    ".json", ".yaml", ".yml", ".html", ".css", ".scss", ".xml",
}

# Explicitly rejected regardless of what Content-Type claims — compiled
# and executable formats have no business in a text-extraction RAG
# pipeline. This isn't malware *detection*; it's a smaller, honest
# guarantee: we never accept a format capable of being an executable, and
# we never `eval`/`exec`/run anything we do accept — uploaded "code" is
# only ever read as inert text for chunking/embedding, never executed. A
# renamed binary is still caught separately below via the UTF-8 check.
_BLOCKED_EXTENSIONS = {
    ".exe", ".dll", ".so", ".dylib", ".bat", ".cmd", ".msi", ".scr",
    ".com", ".jar", ".apk", ".app", ".deb", ".rpm", ".bin", ".ps1",
}

# Canonical mime_type stored/parsed-by for ANY accepted code extension,
# regardless of what the browser's Content-Type header said — this is
# what makes `get_parser_for()` in parser.py route reliably to CodeParser
# instead of depending on inconsistent browser mime-sniffing.
_CODE_CANONICAL_MIME_TYPE = "text/x-code"

_MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


class DocumentStorageError(AppException):
    code = "DOCUMENT_STORAGE_ERROR"
    status_code = 502


class UnsupportedFileTypeError(AppException):
    code = "UNSUPPORTED_FILE_TYPE"
    status_code = 415


class FileTooLargeError(AppException):
    code = "FILE_TOO_LARGE"
    status_code = 413


def _resolve_upload(file: UploadFile) -> tuple[str, str]:
    """Decide the (extension, mime_type-to-store) pair for an upload.

    Raises `UnsupportedFileTypeError` for anything on the blocklist or not
    recognized by either the mime allowlist or the code-extension
    allowlist. Returns the CANONICAL mime type to persist — `text/x-code`
    for any accepted code extension, or the browser's own Content-Type
    otherwise.
    """
    extension = PurePosixPath(file.filename or "").suffix.lower()

    if extension in _BLOCKED_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"Tipo de archivo no permitido por seguridad: {extension!r}."
        )

    if extension in _CODE_EXTENSIONS:
        return extension, _CODE_CANONICAL_MIME_TYPE

    if file.content_type in _ALLOWED_MIME_TYPES:
        return extension, file.content_type

    raise UnsupportedFileTypeError(
        f"Tipo de archivo no soportado: content_type={file.content_type!r}, "
        f"extensión={extension!r}."
    )


def _build_minio_client() -> Minio:
    return Minio(
        settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ROOT_USER,
        secret_key=settings.MINIO_ROOT_PASSWORD,
        secure=settings.MINIO_SECURE,
    )


class DocumentService:
    """Encapsulates persistence + MinIO storage for `documents`.

    Every read/write goes through `RoomService.require_role` first — a
    document's authorization boundary is entirely inherited from its
    parent room, there is no independent ACL on the document itself.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._room_service = RoomService(db)
        self._minio = _build_minio_client()

    # --- Storage helpers -------------------------------------------------
    def _ensure_bucket(self) -> None:
        try:
            if not self._minio.bucket_exists(settings.MINIO_BUCKET):
                self._minio.make_bucket(settings.MINIO_BUCKET)
        except S3Error as exc:
            raise DocumentStorageError(
                "No se pudo verificar/crear el bucket de almacenamiento."
            ) from exc

    @staticmethod
    def _object_key(room_id: UUID, document_id: UUID, filename: str) -> str:
        safe_name = filename.replace("/", "_")
        return f"rooms/{room_id}/{document_id}/{safe_name}"

    def _put_object(self, object_key: str, data: bytes, content_type: str) -> None:
        self._ensure_bucket()
        try:
            self._minio.put_object(
                settings.MINIO_BUCKET,
                object_key,
                data=io.BytesIO(data),
                length=len(data),
                content_type=content_type,
            )
        except S3Error as exc:
            raise DocumentStorageError("Falló la subida del archivo a MinIO.") from exc

    # --- Use cases ---------------------------------------------------------
    async def upload(self, room_id: UUID, user_id: UUID, file: UploadFile) -> Document:
        """Validate, persist metadata (status=PENDING) and store the file.

        NOTE on transactions: this method only `flush()`es — it does NOT
        commit. `documents/router.py` commits explicitly right after
        calling this, *before* enqueueing the Celery task. That ordering
        matters: if we handed the task to Celery before the row was
        durably committed, the worker (a separate process/connection)
        could query for `document_id` and find nothing yet.
        """
        # Any active member (MEMBER+) may contribute documents to a room;
        # VIEWERs are read-only.
        await self._room_service.require_role(room_id, user_id, RoomRole.MEMBER)

        _extension, resolved_mime_type = _resolve_upload(file)

        raw = await file.read()
        if len(raw) > _MAX_UPLOAD_BYTES:
            raise FileTooLargeError(
                f"El archivo excede el límite de {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
            )

        # Lightweight, honest defense (not a substitute for real malware
        # scanning): source code and plain text are expected to actually
        # BE text. A file with a code/text extension whose bytes don't
        # decode as UTF-8 is most likely a renamed binary trying to slip
        # past the extension allowlist above — reject it here rather than
        # storing it and only failing confusingly later at parse time.
        if resolved_mime_type in (_CODE_CANONICAL_MIME_TYPE, "text/plain", "text/markdown"):
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise UnsupportedFileTypeError(
                    "El archivo tiene una extensión de texto/código, pero su "
                    "contenido no es UTF-8 válido (posible binario renombrado)."
                ) from exc

        document = Document(
            room_id=room_id,
            uploaded_by=user_id,
            title=file.filename or "documento-sin-titulo",
            file_path="",  # backfilled below once document.id is known
            mime_type=resolved_mime_type,
            size_bytes=len(raw),
            status=DocumentStatus.PENDING,
        )
        self._db.add(document)
        await self._db.flush()  # assigns document.id without committing

        object_key = self._object_key(room_id, document.id, document.title)
        self._put_object(object_key, raw, file.content_type or "application/octet-stream")

        document.file_path = object_key
        await self._db.flush()
        await self._db.refresh(document)
        return document

    async def list_for_room(self, room_id: UUID, user_id: UUID) -> list[Document]:
        await self._room_service.require_role(room_id, user_id, RoomRole.VIEWER)
        result = await self._db.execute(
            select(Document)
            .where(Document.room_id == room_id)
            .order_by(Document.created_at.desc())
        )
        return list(result.scalars().all())

    async def _get_or_404(self, document_id: UUID) -> Document:
        document = await self._db.get(Document, document_id)
        if document is None:
            raise NotFoundException(f"Documento {document_id} no encontrado.")
        return document

    async def get_status(self, document_id: UUID, user_id: UUID) -> Document:
        document = await self._get_or_404(document_id)
        await self._room_service.require_role(document.room_id, user_id, RoomRole.VIEWER)
        return document

    async def delete(self, document_id: UUID, user_id: UUID) -> None:
        document = await self._get_or_404(document_id)

        # A MEMBER may delete their own upload; ADMIN/OWNER may delete
        # anyone's.
        membership = await self._room_service.require_role(
            document.room_id, user_id, RoomRole.MEMBER
        )
        is_uploader = document.uploaded_by == user_id
        is_room_admin = membership.room_role in (RoomRole.ADMIN, RoomRole.OWNER)
        if not (is_uploader or is_room_admin):
            raise AuthorizationException(
                "Solo el autor del documento o un ADMIN/OWNER de la sala pueden eliminarlo."
            )

        try:
            self._minio.remove_object(settings.MINIO_BUCKET, document.file_path)
        except S3Error:
            # Best-effort cleanup: don't let a storage hiccup block the DB
            # delete. A reconciliation job can sweep orphaned objects later.
            pass

        await self._db.delete(document)
        await self._db.flush()

    async def request_rechunk(self, document_id: UUID, user_id: UUID) -> Document:
        """Reset a document to PENDING so the router can re-enqueue it.

        Same commit caveat as `upload()` — the router commits before
        calling `.delay()`.
        """
        document = await self._get_or_404(document_id)
        await self._room_service.require_role(document.room_id, user_id, RoomRole.MEMBER)

        document.status = DocumentStatus.PENDING
        await self._db.flush()
        await self._db.refresh(document)
        return document