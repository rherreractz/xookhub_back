"""Pydantic v2 schemas for the `users` module."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from src.users.models import GlobalRole


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    name: str | None
    global_role: GlobalRole
    created_at: datetime
    updated_at: datetime


class UserUpdate(BaseModel):
    """PATCH /users/me payload — every field optional (partial update)."""

    name: str | None = Field(default=None, max_length=120)


class UserSyncRequest(BaseModel):
    """Payload sent by the Supabase Auth post-signup webhook.

    `id` must equal the UUID Supabase generated for the new row in
    `auth.users` — this is what lets `verify_supabase_jwt`'s `sub` claim
    resolve to a local `User` row later on.
    """

    id: UUID
    email: EmailStr
    name: str | None = None


class APIKeyCreate(BaseModel):
    name: str | None = Field(default=None, max_length=100)


class APIKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str | None
    is_active: bool
    created_at: datetime


class APIKeyCreated(APIKeyRead):
    """Returned once, at creation time, only. The raw secret is never
    persisted (only its SHA-256 hash is) and can't be retrieved again —
    the client must store it immediately."""

    api_key: str