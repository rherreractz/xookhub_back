"""SQLAlchemy models for the `rooms` module.

`StudyRoom` is the tenant boundary in XookHub: documents, conversations,
flashcards and exams all hang off a room, and access is governed by a
user's `RoomMember.room_role` within that room.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base, CreatedAtMixin

if TYPE_CHECKING:
    from src.documents.models import Document
    from src.generation.models import Flashcard
    from src.users.models import User


class RoomRole(str, enum.Enum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"
    VIEWER = "VIEWER"


class StudyRoom(Base, CreatedAtMixin):
    __tablename__ = "study_rooms"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_public: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    # Self-service join code (e.g. "A7X9P2") — nullable because a room has
    # none until its OWNER explicitly generates one. Regenerating overwrites
    # this single column, invalidating any previously shared code (same
    # behavior as "reset invite link" elsewhere) rather than keeping a
    # history of still-valid codes.
    join_code: Mapped[str | None] = mapped_column(
        String(6), unique=True, index=True, nullable=True
    )

    members: Mapped[list["RoomMember"]] = relationship(
        back_populates="room", cascade="all, delete-orphan"
    )
    documents: Mapped[list["Document"]] = relationship(
        back_populates="room", cascade="all, delete-orphan"
    )
    # Deliberately NO `cascade="delete-orphan"` here: `Document.flashcards`
    # is already the delete-orphan owner (a Flashcard's more specific
    # parent). SQLAlchemy disallows the same class being the delete-orphan
    # target of two different relationships at once. Deleting a room still
    # correctly removes its flashcards — via `ON DELETE CASCADE` cascading
    # room -> documents -> flashcards at the database level — this
    # relationship is just a convenient read-only collection.
    flashcards: Mapped[list["Flashcard"]] = relationship(back_populates="room")
    # Delete-orphan owner is fine here — nothing else references
    # GroupMessage (unlike Flashcard, which Document also owns).
    group_messages: Mapped[list["GroupMessage"]] = relationship(
        back_populates="room",
        cascade="all, delete-orphan",
        order_by="GroupMessage.created_at",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<StudyRoom id={self.id} name={self.name!r}>"


class RoomMember(Base):
    __tablename__ = "room_members"
    __table_args__ = (
        UniqueConstraint("user_id", "room_id", name="uq_room_members_user_room"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    room_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("study_rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    room_role: Mapped[RoomRole] = mapped_column(
        Enum(RoomRole, name="room_role_enum", native_enum=True),
        default=RoomRole.MEMBER,
        server_default=RoomRole.MEMBER.value,
        nullable=False,
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    room: Mapped["StudyRoom"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="room_memberships")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RoomMember room_id={self.room_id} user_id={self.user_id} role={self.room_role}>"


class GroupMessage(Base, CreatedAtMixin):
    """Human-to-human chat for a room's "Comunidad" tab — distinct from
    `rag.models.Message` (the AI conversation history). Persisted here for
    history/pagination; live delivery to connected clients happens via a
    Supabase Realtime Broadcast the service layer fires after commit (see
    `rooms/service.py` — not `postgres_changes`, since this table lives in
    our own Postgres, not Supabase's)."""

    __tablename__ = "group_messages"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    room_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("study_rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)

    room: Mapped["StudyRoom"] = relationship(back_populates="group_messages")
    user: Mapped["User"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<GroupMessage id={self.id} room_id={self.room_id}>"