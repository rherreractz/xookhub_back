# Ruta: src/worker/tasks.py
"""
Celery task definitions for XookHub.

Architecture note (persistent event loop): `AsyncSessionLocal`/`engine`
(src.database) are module-level singletons, imported ONCE per worker
*process* and reused across every task that process executes for its
entire lifetime (Celery recycles OS processes — via `--max-tasks-per-child`
or pool restarts — not the Python module state in between individual
tasks). The engine's underlying asyncpg connection pool caches live
connections bound to whichever event loop was running when they were first
checked out.

Wrapping each task in its own `asyncio.run(...)` call — the previous
design — creates a BRAND NEW event loop per task and destroys it the
moment the coroutine returns. Any connection the pool cached under loop #1
becomes invalid the instant loop #1 closes. The next task's `asyncio.run()`
spins up loop #2, SQLAlchemy tries to reuse that now-orphaned pooled
connection, and asyncio raises exactly what was reported:

    RuntimeError: Event loop is closed
    RuntimeError: Task <...> got Future <...> attached to a different loop

Fix: run ONE event loop per worker process, for that process's entire
lifetime, in a dedicated background thread. Every task submits its
coroutine to that SAME loop via `asyncio.run_coroutine_threadsafe(...)`
instead of spinning up a new one, so the connection pool only ever sees a
single, stable loop and its pooled connections stay valid across every
task execution.

Prefork caveat: Celery's default worker pool is prefork (multiple OS
processes forked from the parent). Threads do NOT survive `fork()` — only
the forking thread continues to exist in a child process. Starting the
loop's background thread at MODULE IMPORT time (before any fork) would
leave forked children with no thread actually running the loop, and
`run_coroutine_threadsafe` would hang forever waiting on it. The loop is
therefore created lazily / via the `worker_process_init` signal — both of
which fire only after Celery has already forked (or, for non-prefork
pools, at normal process start) — guarded by a lock so concurrent task
threads can't race to create two loops.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import tempfile
import threading
from pathlib import Path
from uuid import UUID

from celery.signals import worker_process_init, worker_process_shutdown
from minio import Minio
from minio.error import S3Error
from sqlalchemy import delete

from src.config import get_settings
from src.database import AsyncSessionLocal, engine
from src.documents.models import Document, DocumentChunk, DocumentStatus
from src.documents.parser import UnsupportedMimeTypeError, chunk_text, parse_document
from src.generation.service import GenerationService
from src.rag.llm_adapter import get_llm_adapter
from src.worker.celery_app import celery_app

logger = logging.getLogger("xookhub.worker")
settings = get_settings()

# How long a single task's coroutine may run before the sync Celery thread
# gives up waiting on it. Generous on purpose — document ingestion can be
# slow (large files, embedding API latency) — but bounded, so a genuinely
# stuck coroutine can't wedge a worker slot (and this process's ONE shared
# loop) forever.
TASK_TIMEOUT_SECONDS = 600


# --------------------------------------------------------------------------- #
# Persistent per-process event loop
# --------------------------------------------------------------------------- #
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_lock = threading.Lock()


def _run_loop_forever(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _get_worker_loop() -> asyncio.AbstractEventLoop:
    """Return this process's persistent event loop, creating it (and its
    dedicated background thread) on first use. Idempotent and thread-safe:
    concurrent callers racing to initialize it will still only ever get
    one loop (double-checked locking).
    """
    global _loop, _loop_thread
    if _loop is not None:
        return _loop
    with _loop_lock:
        if _loop is None:
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=_run_loop_forever,
                args=(loop,),
                name="xookhub-worker-loop",
                daemon=True,
            )
            thread.start()
            _loop = loop
            _loop_thread = thread
            logger.info(
                "Started persistent asyncio event loop for worker process (pid=%s)",
                os.getpid(),
            )
    return _loop


@worker_process_init.connect
def _init_worker_loop(**_kwargs: object) -> None:
    """Eagerly start the persistent loop right after Celery forks this
    worker child process (fires once per child, after fork — see the
    prefork caveat in the module docstring). Safe to call redundantly:
    `_get_worker_loop()` is a no-op if already initialized, so this is just
    an optimization to avoid the first task paying loop-startup latency.
    """
    _get_worker_loop()


@worker_process_shutdown.connect
def _shutdown_worker_loop(**_kwargs: object) -> None:
    """Best-effort graceful cleanup when this worker process exits —
    including routine recycling via `--max-tasks-per-child`, not just
    final shutdown. Disposes the AsyncEngine's connection pool ON its own
    loop (required — asyncpg connections can only be closed from the loop
    that owns them), then stops the loop and joins its thread.

    The loop's thread is a daemon, so skipping this wouldn't hang process
    exit either way — but letting Postgres see connections close cleanly
    is kinder than the OS yanking the sockets shut.
    """
    global _loop, _loop_thread
    if _loop is None:
        return
    try:
        future = asyncio.run_coroutine_threadsafe(engine.dispose(), _loop)
        future.result(timeout=10)
    except Exception:  # noqa: BLE001 - shutdown path, never let this raise
        logger.exception("Error disposing the async engine during worker shutdown")
    finally:
        _loop.call_soon_threadsafe(_loop.stop)
        if _loop_thread is not None:
            _loop_thread.join(timeout=5)
        _loop = None
        _loop_thread = None


def _run_coroutine(coro: "asyncio.coroutines.Coroutine") -> None:
    """Submit `coro` to this process's persistent loop from Celery's (sync)
    task thread, block until it completes, and re-raise whatever exception
    it raised — mirroring how `asyncio.run(coro)` used to behave, just
    without tearing down the loop afterward.
    """
    loop = _get_worker_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        future.result(timeout=TASK_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        # Ask the loop to cancel the still-running coroutine at its next
        # await point. Without this, a timed-out task that Celery then
        # retries could race a "zombie" execution of the same document
        # still running in the background on the shared loop.
        future.cancel()
        raise


def _minio_client() -> Minio:
    return Minio(
        settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ROOT_USER,
        secret_key=settings.MINIO_ROOT_PASSWORD,
        secure=settings.MINIO_SECURE,
    )


def _download_to_tmp(document: Document) -> str:
    """Blocking MinIO download — intentionally NOT async (the official
    MinIO client has no asyncio variant). Always called via
    `asyncio.to_thread(...)` from `_process_document`, not directly: now
    that the event loop is long-lived and shared across every task this
    process ever runs (rather than a fresh throwaway loop per task), a
    blocking call made directly on the loop would stall it for whatever
    else might be scheduled on it — harmless under the default prefork
    pool (one task per process at a time) but a real hazard under
    `--pool=threads`/gevent/eventlet, where multiple tasks in the same
    process could have coroutines on this loop concurrently. Offloading to
    a thread makes this safe regardless of worker pool type.
    """
    client = _minio_client()
    suffix = Path(document.title).suffix or ""
    _, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        client.fget_object(settings.MINIO_BUCKET, document.file_path, tmp_path)
    except S3Error as exc:
        raise RuntimeError(
            f"No se pudo descargar {document.file_path!r} de MinIO."
        ) from exc
    return tmp_path


async def _embed_texts(texts: list[str]) -> list[list[float] | None]:
    """Embed a batch of chunk texts via the LLM adapter.

    On any embedding failure we log and fall back to `None` embeddings
    (the column is nullable) so a transient provider outage degrades to
    "indexed but not yet searchable" instead of failing the whole
    document — a later reconciliation job can backfill missing vectors.
    """
    if not texts:
        return []
    try:
        adapter = get_llm_adapter()
        vectors = await adapter.embed_batch(texts)
        return list(vectors)
    except Exception:  # noqa: BLE001 - degrade gracefully, see docstring
        logger.exception("Embedding batch failed; storing chunks without vectors")
        return [None] * len(texts)


async def _set_status(document_id: str, status: DocumentStatus) -> None:
    """Open a short-lived session, flip `Document.status`, commit, close.

    Used for the two transitions that must be visible independently of
    whether the main processing transaction ever completes: PENDING ->
    PROCESSING at the start, and the terminal FAILED/QUARANTINED state on
    error. Each call is its own isolated commit — see `_process_document`
    for why that separation matters.
    """
    async with AsyncSessionLocal() as db:
        document = await db.get(Document, document_id)
        if document is None:
            logger.warning("document %s no longer exists", document_id)
            return
        document.status = status
        await db.commit()


async def _process_document(document_id: str) -> None:
    """The actual ingestion pipeline: download -> parse -> chunk -> embed
    -> persist. Everything here is `async def` using `AsyncSession`
    (asyncpg) — the same stack FastAPI itself uses.

    Split into two DB transactions on purpose:
      1. PENDING -> PROCESSING, committed immediately (via `_set_status`),
         so `GET /documents/{id}/status` reflects progress right away
         instead of only after the whole job finishes.
      2. The actual parse/chunk/persist work, committed atomically as a
         single unit together with the final INDEXED status — a
         partially-written chunk set is never visible.

    Raises whatever exception caused a genuine (potentially transient)
    failure, after recording FAILED, so the sync Celery wrapper can hand
    it to `self.retry`. `UnsupportedMimeTypeError` is handled entirely
    here (terminal, not retryable) and does NOT propagate.
    """
    await _set_status(document_id, DocumentStatus.PROCESSING)

    try:
        async with AsyncSessionLocal() as db:
            document = await db.get(Document, document_id)
            if document is None:
                return

            local_path = await asyncio.to_thread(_download_to_tmp, document)
            try:
                parsed = parse_document(local_path, document.mime_type or "text/plain")
            finally:
                Path(local_path).unlink(missing_ok=True)

            # Covers the manual re-chunk path: drop any previous chunks
            # before writing the new set.
            await db.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id == document.id)
            )

            # Collect all (page, text) pairs first, embed in ONE batch
            # call, then persist — far fewer provider round-trips than
            # embedding chunk-by-chunk.
            pending: list[tuple[int | None, str]] = []
            for page in parsed.pages:
                for piece in chunk_text(page.content):
                    pending.append((page.page_number, piece))

            embeddings = await _embed_texts([text for _, text in pending])

            if not pending:
                # Real possibility (a scanned PDF with no OCR text layer,
                # an empty file), not just a parser bug — but it was
                # previously indistinguishable from a genuinely-indexed
                # document with content, which is exactly how a dummy
                # parser stub went unnoticed for this long. Surfacing it
                # loudly here means the NEXT silent-extraction case (this
                # parser or a future format's) gets caught in the logs
                # instead of only showing up as "the chat has no context."
                logger.warning(
                    "document %s produced 0 chunks after parsing (mime_type=%s) "
                    "— it will be marked INDEXED with no retrievable content.",
                    document.id,
                    document.mime_type,
                )

            for chunk_index, ((page_number, piece), vector) in enumerate(
                zip(pending, embeddings)
            ):
                db.add(
                    DocumentChunk(
                        document_id=document.id,
                        chunk_index=chunk_index,
                        content=piece,
                        page_number=page_number,
                        embedding=vector,
                    )
                )

            document.status = DocumentStatus.INDEXED
            await db.commit()

    except UnsupportedMimeTypeError:
        logger.error("Unsupported mime type for document %s", document_id)
        await _set_status(document_id, DocumentStatus.QUARANTINED)
        # Not re-raised: an unsupported mime type is a permanent condition,
        # retrying it would just fail identically every time.

    except Exception:
        logger.exception("process_document_task failed for document %s", document_id)
        await _set_status(document_id, DocumentStatus.FAILED)
        raise  # re-raised so the sync Celery wrapper can trigger self.retry


@celery_app.task(
    name="src.worker.tasks.process_document_task",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def process_document_task(self, document_id: str) -> None:
    """Sync Celery entrypoint — submits the async pipeline to this
    process's persistent event loop instead of spinning up (and tearing
    down) a new one per call. See the module docstring for why a fresh
    loop-per-task broke asyncpg's pooled connections.
    """
    try:
        _run_coroutine(_process_document(document_id))
    except Exception as exc:
        # `_process_document` already recorded FAILED status before
        # re-raising; this just hands the exception to Celery's retry
        # machinery (respecting max_retries/default_retry_delay above).
        raise self.retry(exc=exc)


async def _generate_room_exam(exam_id: str) -> None:
    """Runs on this process's persistent loop — one DB session, scoped to
    this single generation job, exactly like `_process_document`."""
    async with AsyncSessionLocal() as db:
        await GenerationService(db).generate_room_exam_questions(UUID(exam_id))


@celery_app.task(
    name="src.worker.tasks.generate_exam_task",
    bind=True,
    max_retries=1,
    default_retry_delay=15,
)
def generate_exam_task(self, exam_id: str) -> None:
    """Sync Celery entrypoint for room-wide exam generation.

    Only 1 retry (vs. 3 for document ingestion): `generate_room_exam_
    questions` already catches its own failures and flips the exam to
    FAILED with a clean, dedicated status the frontend is actively polling
    for — a Celery-level retry mostly just delays the user seeing that
    terminal state, since the vast majority of failures here are "the LLM
    call itself errored", which a single retry can help with, but three
    is more redundant than useful.
    """
    try:
        _run_coroutine(_generate_room_exam(exam_id))
    except Exception as exc:
        raise self.retry(exc=exc)