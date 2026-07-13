# Ruta: src/worker/celery_app.py
"""
Celery application instance for XookHub's asynchronous pipeline.

Handles document ingestion (parsing, chunking, embedding) today and, in
later parts, flashcard/exam generation — anything too slow to run inline
in the FastAPI request/response cycle.

Model-import note (fixes `InvalidRequestError: ... failed to locate a name
'StudyRoom'`): several models declare relationships via STRING references
to sibling classes — e.g. `Document.room` is `relationship("StudyRoom")` in
`src/documents/models.py`. SQLAlchemy only resolves that string lazily, the
first time a query actually touches the `Document` mapper, and it can only
resolve it if the `StudyRoom` class has been REGISTERED — i.e. its module
has been imported — somewhere in that same process.

`src/main.py` (the FastAPI process) already imports every model module
explicitly for exactly this reason. The Celery worker is a SEPARATE
process that never imports `src.main`, so without the same explicit
imports here, the worker only ever loads `src.documents.models` (via
`src.worker.tasks`) and never `src.rooms.models`, `src.users.models`,
`src.rag.models`, or `src.generation.models` — so `StudyRoom` (and every
other cross-module relationship target) is simply missing from the
registry when the first task runs. Celery treats that as a retryable
exception (see `process_document_task`'s `except Exception: raise
self.retry(...)`), so the failure doesn't surface as a crash — it silently
retries every 30s, `max_retries` times, then gives up.

The fix: import every model module here, at the actual Celery entrypoint
(`-A src.worker.celery_app`), and call `configure_mappers()` immediately
afterward so a misconfigured relationship fails LOUDLY at worker startup —
right here, before `celery@... ready.` is ever logged — instead of lazily,
90 seconds and 3 failed retries into the worker's first real task.
"""

from __future__ import annotations

from celery import Celery
from sqlalchemy.orm import configure_mappers

from src.config import get_settings

# Import every module's models so SQLAlchemy's mapper registry is fully
# populated in THIS process before any task can run. Order doesn't matter
# (SQLAlchemy resolves string relationship() targets lazily, at
# configure_mappers()/first-use time, not at each individual import) — what
# matters is that all five modules have been imported at least once.
import src.users.models  # noqa: F401,E402
import src.rooms.models  # noqa: F401,E402
import src.documents.models  # noqa: F401,E402
import src.rag.models  # noqa: F401,E402
import src.generation.models  # noqa: F401,E402

settings = get_settings()

# Fail fast: resolve every relationship() string reference right now. If
# something is still missing or misconfigured, this raises immediately at
# worker startup with a clear traceback, instead of surfacing as a
# mysterious retry loop on the first task.
configure_mappers()

celery_app = Celery(
    "xookhub",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.REDIS_URL,  # task state/results, reusing the existing Redis
    include=["src.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,  # re-deliver to another worker if this one dies mid-task
    worker_prefetch_multiplier=1,  # don't hoard long-running document jobs
    task_default_queue="xookhub.default",
    task_routes={
        "src.worker.tasks.process_document_task": {"queue": "xookhub.documents"},
    },
)