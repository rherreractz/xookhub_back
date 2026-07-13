# Ruta: src/core/security.py
"""
Supabase JWT verification dependency.

The frontend authenticates directly against Supabase Auth and attaches the
issued JWT as a Bearer token on every request to this API. This module
verifies that token locally using Supabase's public JWKS (no network round
-trip to the Auth server itself), and exposes the authenticated user's
identity to route handlers via dependency injection.

Supabase projects can sign JWTs asymmetrically (ES256 on a P-256 curve, or
RS256) using a per-project JSON Web Key Set exposed at:

    https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json

`jwt.PyJWKClient` (bundled with PyJWT) handles fetching that JWKS, matching
the token's `kid` header to the right public key, and caching the result —
so this module never hand-parses JWK material.

Legacy fallback: a project mid-rotation to asymmetric keys still accepts
JWTs signed with its old shared HS256 secret for a transition window
(Supabase's own migration guidance). This module honors that by branching
on the token's `alg` header: ES256/RS256 verify against the JWKS public
key, HS256 verifies against `SUPABASE_JWT_SECRET` if one is configured.

Lazy user sync (added after a real FK-violation incident): `POST
/users/sync` is designed as a webhook Supabase itself should call right
after sign-up — but that webhook is easy to forget to wire up in a
Supabase project's dashboard, and every FK to `users.id` (room_members,
documents.uploaded_by, flashcards.created_by, ...) breaks the moment the
corresponding row is missing, with an unhelpful 500 (which browsers often
misreport as a CORS error, since Starlette's own error response for an
unhandled exception is built outside CORSMiddleware's wrapping). This
module closes that gap at the source: `verify_supabase_jwt` ensures a
`users` row exists for the caller before returning, regardless of whether
the webhook is configured. The webhook remains the "fast path" — this is
the safety net for whichever request happens to be this user's first.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.core.exceptions import AuthenticationException
from src.database import get_db
from src.users.models import User

settings = get_settings()

# Algorithms Supabase issues asymmetric JWTs with — verified against the
# project's JWKS public key. Anything else falls back to the legacy path
# (HS256) or is rejected outright.
ASYMMETRIC_ALGORITHMS = ("ES256", "RS256")

# `auto_error=False` so a missing/malformed header falls through to our own
# `AuthenticationException` below, keeping every auth failure in the app's
# standard `{data, meta, error}` envelope instead of FastAPI's default
# `{"detail": "..."}` 403 shape.
bearer_scheme = HTTPBearer(
    scheme_name="SupabaseJWT",
    description=(
        "Pega el JWT emitido por Supabase Auth para el usuario autenticado "
        "(sin el prefijo 'Bearer', Swagger lo añade automáticamente)."
    ),
    auto_error=False,
)


@dataclass(frozen=True, slots=True)
class SupabaseUser:
    """Minimal, verified identity extracted from a Supabase JWT."""

    id: UUID
    email: str | None
    role: str | None


# Module-level singleton: constructed once, reused across requests, so
# PyJWKClient's internal cache (`cache_keys=True`) actually persists instead
# of re-fetching the JWKS on every single login. This mirrors the pattern
# already used for `bearer_scheme`.
_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        if not settings.SUPABASE_URL:
            raise AuthenticationException(
                "El servidor no tiene configurado SUPABASE_URL (requerido "
                "para verificar tokens firmados asimétricamente vía JWKS).",
                code="SERVER_MISCONFIGURED",
                status_code=500,
            )
        _jwks_client = PyJWKClient(
            settings.supabase_jwks_url,
            cache_keys=True,
            lifespan=settings.SUPABASE_JWKS_CACHE_SECONDS,
        )
    return _jwks_client


async def _decode(token: str) -> dict:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise AuthenticationException("Token inválido (encabezado ilegible).") from exc

    alg = header.get("alg")

    if alg in ASYMMETRIC_ALGORITHMS:
        try:
            # The only genuinely blocking step (HTTP fetch on a JWKS cache
            # miss) — offloaded to a thread so it never blocks the event
            # loop now that this whole function runs inside `async def
            # verify_supabase_jwt` (needed below for DB access).
            signing_key = await asyncio.to_thread(
                _get_jwks_client().get_signing_key_from_jwt, token
            )
        except PyJWKClientError as exc:
            raise AuthenticationException(
                "No se pudo obtener la clave pública de Supabase (JWKS) "
                "para verificar la firma del token."
            ) from exc
        key = signing_key.key
        algorithms = [alg]
    elif alg == "HS256":
        if not settings.SUPABASE_JWT_SECRET:
            raise AuthenticationException(
                "El token está firmado con HS256 (secreto legado), pero no "
                "hay SUPABASE_JWT_SECRET configurado en el servidor."
            )
        key = settings.SUPABASE_JWT_SECRET
        algorithms = ["HS256"]
    else:
        raise AuthenticationException(f"Algoritmo de firma no soportado: {alg!r}.")

    try:
        # Local cryptographic verification only, once `key` is resolved —
        # no I/O, safe to call directly without a thread.
        return jwt.decode(
            token,
            key,
            algorithms=algorithms,
            audience=settings.SUPABASE_JWT_AUDIENCE,
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationException("El token ha expirado.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationException("Token inválido.") from exc


async def _ensure_user_row(db: AsyncSession, user: SupabaseUser) -> None:
    """Idempotently guarantee `users.id = user.id` exists.

    SELECT-first rather than a blind upsert on every request: the common
    case (already synced — via the webhook or a prior call here) is then a
    single indexed read with no write or lock at all. `ON CONFLICT DO
    NOTHING` on the INSERT covers the rare race between two concurrent
    first-requests for a brand-new user.
    """
    already_exists = await db.scalar(select(User.id).where(User.id == user.id))
    if already_exists is not None:
        return

    # `email` is NOT NULL with a UNIQUE index — Supabase JWTs always carry
    # it in practice, but fall back to a synthetic placeholder rather than
    # letting a malformed/edge-case token crash this safety net itself.
    email = user.email or f"{user.id}@unknown.supabase.local"

    stmt = (
        pg_insert(User)
        .values(id=user.id, email=email)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await db.execute(stmt)
    await db.commit()


async def verify_supabase_jwt(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> SupabaseUser:
    """FastAPI dependency: validate the Supabase JWT, ensure the caller has
    a corresponding `users` row, and return their identity.

    Usage:
        @router.get("/me")
        async def me(user: SupabaseUser = Depends(verify_supabase_jwt)):
            ...
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationException("No se proporcionó un token de autenticación.")

    token = credentials.credentials
    payload = await _decode(token)

    subject = payload.get("sub")
    if not subject:
        raise AuthenticationException("El token no contiene un 'sub' válido.")

    try:
        user_id = UUID(subject)
    except ValueError as exc:
        raise AuthenticationException(
            "El 'sub' del token no es un UUID válido."
        ) from exc

    user = SupabaseUser(
        id=user_id,
        email=payload.get("email"),
        role=payload.get("role"),
    )
    await _ensure_user_row(db, user)
    return user