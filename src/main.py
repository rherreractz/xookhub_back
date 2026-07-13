# Ruta: src/main.py
"""
XookHub API — application entrypoint.

Wires together the modular monolith: global exception handling, the
standardized response envelope, CORS, and (as each module comes online in
subsequent parts of this build) its router. This file intentionally stays
thin — business logic lives in each module's `service.py`.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import get_settings
from src.core.exceptions import register_exception_handlers
from src.core.responses import APIResponse
from src.core.security import SupabaseUser, verify_supabase_jwt

# Import every module's models so SQLAlchemy's mapper registry resolves all
# `relationship()` string references before the app starts serving requests.
from src.users import models as _users_models  # noqa: F401
from src.rooms import models as _rooms_models  # noqa: F401
from src.documents import models as _documents_models  # noqa: F401
from src.rag import models as _rag_models  # noqa: F401
from src.generation import models as _generation_models  # noqa: F401

from src.users.router import router as users_router
from src.rooms.router import router as rooms_router
from src.documents.router import router as documents_router
from src.rag.router import router as rag_router
from src.generation.router import router as generation_router

settings = get_settings()

app = FastAPI(
    title=settings.APP_NAME,
    description="Plataforma multi-tenant de estudio asistido por IA con motor RAG.",
    version=settings.APP_VERSION,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

register_exception_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(users_router)
app.include_router(rooms_router)
app.include_router(documents_router)
app.include_router(rag_router)
app.include_router(generation_router)


@app.get("/health", tags=["infra"])
async def health_check() -> APIResponse[dict]:
    """Liveness probe used by Docker/Nginx — intentionally bypasses auth."""
    return APIResponse.success({"status": "ok"})


@app.get("/api/v1/_whoami", tags=["infra"])
async def whoami(user: SupabaseUser = Depends(verify_supabase_jwt)) -> APIResponse[dict]:
    """Smoke-test endpoint proving the Supabase JWT dependency is wired up."""
    return APIResponse.success(
        {"id": str(user.id), "email": user.email, "role": user.role}
    )