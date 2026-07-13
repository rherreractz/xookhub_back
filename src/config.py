"""
Centralized application settings for XookHub.

Every module should import `get_settings()` instead of calling `os.getenv`
directly, so configuration is validated once, at process startup, and typed
end-to-end. This replaces the inline `os.getenv` calls used as a stopgap in
Part 1 (`src/database.py`, `src/core/security.py`, `src/main.py`), which are
now refactored to consume this module instead.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- App ---
    APP_NAME: str = "XookHub API"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: str = "development"
    CORS_ALLOWED_ORIGINS: str = "*"

    # --- Database ---
    DB_URL: str = "postgresql://xookhub:xookhub@localhost:5432/xookhub"
    DB_ECHO: bool = False

    # --- Redis / Celery / RabbitMQ ---
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "amqp://guest:guest@localhost:5672//"

    # --- MinIO (document storage) ---
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ROOT_USER: str = "minioadmin"
    MINIO_ROOT_PASSWORD: str = "minioadmin"
    MINIO_BUCKET: str = "xookhub-documents"
    MINIO_SECURE: bool = False

    # --- Supabase Auth ---
    # Base project URL (e.g. https://xxxxx.supabase.co) — used to derive the
    # JWKS discovery endpoint for asymmetric (ES256/RS256) token verification.
    SUPABASE_URL: str = ""
    # Legacy symmetric secret. Optional now: only needed while HS256-signed
    # tokens issued before a project's rotation to asymmetric keys are still
    # valid — Supabase keeps accepting both simultaneously during that window.
    SUPABASE_JWT_SECRET: str = ""
    SUPABASE_JWT_AUDIENCE: str = "authenticated"
    # Matches Supabase's own ~10-minute edge cache for the JWKS endpoint —
    # no point caching it locally for longer than the source does.
    SUPABASE_JWKS_CACHE_SECONDS: int = 600
    # Elevated-privilege key, server-side ONLY — never sent to the browser.
    # Used exclusively to call Supabase's Realtime Broadcast REST API
    # (POST /realtime/v1/api/broadcast) after committing a GroupMessage,
    # since our Postgres isn't Supabase's own DB (postgres_changes can't
    # see it) — see the note on RoomService.post_group_message.
    SUPABASE_SERVICE_ROLE_KEY: str = ""

    # --- Gemini (rag / generation modules) ---
    GEMINI_API_KEY: str = ""
    GEMINI_CHAT_MODEL: str = "gemini-3.1-pro-preview"
    GEMINI_EMBEDDING_MODEL: str = "gemini-embedding-001"
    # Pinned to the pgvector column width (vector(1536)); Gemini embeddings
    # support output_dimensionality so we request exactly this.
    GEMINI_EMBEDDING_DIM: int = 1536

    @property
    def cors_origins(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.CORS_ALLOWED_ORIGINS.split(",")
            if origin.strip()
        ]

    @property
    def supabase_jwks_url(self) -> str:
        """The JWKS discovery endpoint for this Supabase project.

        Per Supabase's docs, this is served directly from the Auth server
        and does not exist as a callable RPC — it's a static, publicly
        readable JSON document.
        """
        return f"{self.SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json"

    @property
    def supabase_realtime_broadcast_url(self) -> str:
        return f"{self.SUPABASE_URL.rstrip('/')}/realtime/v1/api/broadcast"

    @property
    def async_db_url(self) -> str:
        """`DB_URL` normalized to the asyncpg driver for SQLAlchemy's async engine.

        `.env` / docker-compose deliberately expose a driver-less DSN
        (`postgresql://...`) since that same value is reused by Alembic and
        by tools that expect a sync-style URL; only the async engine needs
        the explicit driver, so we append it here instead.
        """
        return self.DB_URL.replace("postgresql://", "postgresql+asyncpg://", 1)


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached Settings instance — env is parsed once."""
    return Settings()