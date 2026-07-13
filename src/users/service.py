"""Business logic for the `users` module.

Kept framework-agnostic on purpose: this class only knows about SQLAlchemy
and domain exceptions, never about FastAPI, so it's trivially unit-testable
and reusable from Celery tasks if ever needed.
"""

from __future__ import annotations

import hashlib
import secrets
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ConflictException, NotFoundException
from src.users.models import APIKey, GlobalRole, User
from src.users.schemas import APIKeyCreate, UserSyncRequest, UserUpdate

API_KEY_PREFIX = "xk_"


class UserService:
    """Encapsulates persistence and business rules for `users`."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_by_id(self, user_id: UUID) -> User:
        user = await self._db.get(User, user_id)
        if user is None:
            raise NotFoundException(f"Usuario {user_id} no encontrado.")
        return user

    async def update_profile(self, user_id: UUID, payload: UserUpdate) -> User:
        user = await self.get_by_id(user_id)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(user, field, value)
        await self._db.flush()
        await self._db.refresh(user)
        return user

    async def sync_from_supabase(self, payload: UserSyncRequest) -> User:
        """Idempotent upsert invoked by the Supabase post-signup webhook.

        Safe to call more than once for the same `id` (e.g. webhook
        retries): updates email/name on an existing row instead of
        duplicating it.
        """
        existing = await self._db.get(User, payload.id)
        if existing is not None:
            existing.email = payload.email
            if payload.name is not None:
                existing.name = payload.name
            await self._db.flush()
            await self._db.refresh(existing)
            return existing

        user = User(
            id=payload.id,
            email=payload.email,
            name=payload.name,
            global_role=GlobalRole.USER,
        )
        self._db.add(user)
        await self._db.flush()
        await self._db.refresh(user)
        return user

    async def create_api_key(
        self, user_id: UUID, payload: APIKeyCreate
    ) -> tuple[APIKey, str]:
        """Generate a new API key. Returns `(record, raw_key)`.

        Only `key_hash` (SHA-256 of the raw key) is ever persisted — the raw
        value is returned exactly once here and must be shown to the caller
        immediately, since it can't be recovered afterwards.
        """
        raw_key = f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

        api_key = APIKey(user_id=user_id, key_hash=key_hash, name=payload.name)
        self._db.add(api_key)
        try:
            await self._db.flush()
        except IntegrityError as exc:  # pragma: no cover - astronomically unlikely
            raise ConflictException(
                "No se pudo generar una API key única, intenta de nuevo."
            ) from exc
        await self._db.refresh(api_key)
        return api_key, raw_key

    async def list_api_keys(self, user_id: UUID) -> list[APIKey]:
        result = await self._db.execute(
            select(APIKey)
            .where(APIKey.user_id == user_id)
            .order_by(APIKey.created_at.desc())
        )
        return list(result.scalars().all())