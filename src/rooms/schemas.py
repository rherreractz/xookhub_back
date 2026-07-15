"""Pydantic v2 schemas for the `rooms` module."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.rooms.models import RoomRole


class RoomCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    is_public: bool = False


class RoomUpdate(BaseModel):
    """PATCH /rooms/{id} payload — every field optional (partial update)."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    is_public: bool | None = None


class RoomRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    is_public: bool
    created_at: datetime
    # The CALLER's own role in this room — not a column on StudyRoom
    # itself, so `model_validate(room)` can't fill it from the ORM object
    # alone; only endpoints that already looked up the caller's
    # membership populate it explicitly (currently just GET
    # /rooms/{room_id}). None elsewhere (e.g. GET /rooms' list) rather
    # than an extra join every caller doesn't need.
    my_role: RoomRole | None = None


class RoomMemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    room_id: UUID
    user_id: UUID
    room_role: RoomRole
    joined_at: datetime


class RoomMemberDetail(RoomMemberRead):
    """RoomMemberRead + datos denormalizados del usuario, para que el
    frontend pueda listar integrantes de una sala (nombre/correo) sin un
    round-trip extra por persona."""

    name: str | None = None
    email: str


class RoomMemberInvite(BaseModel):
    user_id: UUID
    room_role: RoomRole = RoomRole.MEMBER


class RoomMemberRoleUpdate(BaseModel):
    room_role: RoomRole


class RoomJoinRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class RoomInviteCodeRead(BaseModel):
    """Deliberately its own schema, NOT a field on `RoomRead`: a join code
    is a semi-secret credential (anyone holding it can add themselves as a
    MEMBER), so it must only ever be returned from the one endpoint that
    generates it — never leaked to every member via GET /rooms or
    GET /rooms/{id}."""

    join_code: str


class GroupMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class GroupMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    room_id: UUID
    user_id: UUID
    content: str
    created_at: datetime
    # Denormalized at read time (service layer joins User) so the frontend
    # can render a sender name without a second round-trip per message.
    author_name: str | None = None