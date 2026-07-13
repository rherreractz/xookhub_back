"""SQLAlchemy models for the `users` module.

`User.id` is NOT server-generated: it is expected to equal the UUID of the
corresponding row in Supabase's `auth.users` table, written by the
`POST /api/v1/users/sync` webhook the first time a person signs up.
"""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base, CreatedAtMixin


class GlobalRole(str, enum.Enum):
    SUPERADMIN = "SUPERADMIN"
    USER = "USER"


class User(Base, CreatedAtMixin):
    __tablename__ = "users"

    # Provided by Supabase Auth — never generated locally.
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)

    email: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    global_role: Mapped[GlobalRole] = mapped_column(
        Enum(GlobalRole, name="global_role_enum", native_enum=True),
        default=GlobalRole.USER,
        server_default=GlobalRole.USER.value,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    api_keys: Mapped[list["APIKey"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    room_memberships: Mapped[list["RoomMember"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} email={self.email!r}>"


class APIKey(Base, CreatedAtMixin):
    __tablename__ = "api_keys"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key_hash: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="api_keys")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<APIKey id={self.id} user_id={self.user_id} active={self.is_active}>"