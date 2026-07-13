"""Business logic for the `rooms` module.

`StudyRoom` is XookHub's tenant boundary. `require_role` is the single
choke point that enforces it: every mutating operation on a room, its
membership, or (in later modules) its documents/conversations should route
through a membership check here rather than re-implementing authorization
ad hoc in each router.
"""

from __future__ import annotations

import logging
import secrets
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.core.exceptions import AuthorizationException, ConflictException, NotFoundException
from src.rooms.models import GroupMessage, RoomMember, RoomRole, StudyRoom

logger = logging.getLogger("xookhub.rooms")
settings = get_settings()
from src.rooms.schemas import RoomCreate, RoomMemberInvite, RoomUpdate

# Excludes visually ambiguous characters (0/O, 1/I/L) so a code read aloud
# or copied by hand is less likely to be mistyped.
_INVITE_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_INVITE_CODE_LENGTH = 6
_INVITE_CODE_MAX_ATTEMPTS = 10

# Ordinal ranking used to compare roles ("is ADMIN at least as strong as
# MEMBER?") without hardcoding the comparison in every call site.
_ROLE_RANK: dict[RoomRole, int] = {
    RoomRole.VIEWER: 0,
    RoomRole.MEMBER: 1,
    RoomRole.ADMIN: 2,
    RoomRole.OWNER: 3,
}


class RoomService:
    """Encapsulates persistence and authorization rules for study rooms."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _get_membership(self, room_id: UUID, user_id: UUID) -> RoomMember | None:
        result = await self._db.execute(
            select(RoomMember).where(
                RoomMember.room_id == room_id, RoomMember.user_id == user_id
            )
        )
        return result.scalar_one_or_none()

    async def require_role(
        self, room_id: UUID, user_id: UUID, minimum: RoomRole
    ) -> RoomMember:
        """Ensure `user_id` belongs to `room_id` with at least `minimum` role.

        Deliberately raises `NotFoundException` (not `AuthorizationException`)
        when the caller isn't a member at all, so the existence of private
        rooms isn't leaked to non-members via a 403 vs. 404 timing/status
        oracle.
        """
        membership = await self._get_membership(room_id, user_id)
        if membership is None:
            raise NotFoundException(f"Sala {room_id} no encontrada.")
        if _ROLE_RANK[membership.room_role] < _ROLE_RANK[minimum]:
            raise AuthorizationException(
                f"Se requiere el rol '{minimum.value}' o superior en esta sala."
            )
        return membership

    async def list_for_user(self, user_id: UUID) -> list[StudyRoom]:
        result = await self._db.execute(
            select(StudyRoom)
            .join(RoomMember, RoomMember.room_id == StudyRoom.id)
            .where(RoomMember.user_id == user_id)
            .order_by(StudyRoom.created_at.desc())
        )
        return list(result.scalars().all())

    async def create(self, owner_id: UUID, payload: RoomCreate) -> StudyRoom:
        room = StudyRoom(
            name=payload.name,
            description=payload.description,
            is_public=payload.is_public,
        )
        self._db.add(room)
        await self._db.flush()

        self._db.add(
            RoomMember(room_id=room.id, user_id=owner_id, room_role=RoomRole.OWNER)
        )
        await self._db.flush()
        await self._db.refresh(room)
        return room

    async def get_for_user(
        self, room_id: UUID, user_id: UUID
    ) -> tuple[StudyRoom, RoomMember]:
        """Returns both the room and the CALLER's own membership row —
        the latter is what lets `get_room` expose `my_role` in `RoomRead`
        (e.g. so the frontend can show/hide owner-only actions like
        generating an invite code, without guessing or calling an
        endpoint just to find out it's forbidden)."""
        membership = await self.require_role(room_id, user_id, RoomRole.VIEWER)
        room = await self._db.get(StudyRoom, room_id)
        if room is None:
            raise NotFoundException(f"Sala {room_id} no encontrada.")
        return room, membership

    async def update(self, room_id: UUID, user_id: UUID, payload: RoomUpdate) -> StudyRoom:
        await self.require_role(room_id, user_id, RoomRole.ADMIN)
        room = await self._db.get(StudyRoom, room_id)
        if room is None:
            raise NotFoundException(f"Sala {room_id} no encontrada.")
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(room, field, value)
        await self._db.flush()
        await self._db.refresh(room)
        return room

    async def delete(self, room_id: UUID, user_id: UUID) -> None:
        await self.require_role(room_id, user_id, RoomRole.OWNER)
        room = await self._db.get(StudyRoom, room_id)
        if room is None:
            raise NotFoundException(f"Sala {room_id} no encontrada.")
        await self._db.delete(room)
        await self._db.flush()

    async def invite_member(
        self, room_id: UUID, requester_id: UUID, payload: RoomMemberInvite
    ) -> RoomMember:
        await self.require_role(room_id, requester_id, RoomRole.ADMIN)

        if await self._get_membership(room_id, payload.user_id) is not None:
            raise ConflictException("El usuario ya es miembro de esta sala.")

        membership = RoomMember(
            room_id=room_id, user_id=payload.user_id, room_role=payload.room_role
        )
        self._db.add(membership)
        await self._db.flush()
        await self._db.refresh(membership)
        return membership

    async def change_member_role(
        self,
        room_id: UUID,
        requester_id: UUID,
        target_user_id: UUID,
        new_role: RoomRole,
    ) -> RoomMember:
        # Only OWNERs may promote/demote — an ADMIN could otherwise grant
        # itself OWNER, escalating its own privileges.
        await self.require_role(room_id, requester_id, RoomRole.OWNER)

        target = await self._get_membership(room_id, target_user_id)
        if target is None:
            raise NotFoundException("Ese usuario no es miembro de esta sala.")

        target.room_role = new_role
        await self._db.flush()
        await self._db.refresh(target)
        return target

    async def generate_invite_code(self, room_id: UUID, requester_id: UUID) -> str:
        """(Re)generate this room's join code. OWNER-only.

        Overwrites any previous code — a room has exactly one valid code at
        a time, so regenerating implicitly invalidates whatever was shared
        before (mirrors "reset invite link" elsewhere, e.g. Discord).
        """
        await self.require_role(room_id, requester_id, RoomRole.OWNER)
        room = await self._db.get(StudyRoom, room_id)
        if room is None:
            raise NotFoundException(f"Sala {room_id} no encontrada.")

        # Retry on the (astronomically unlikely, ~1-in-a-billion-per-pair)
        # chance of colliding with another room's existing code — the
        # UNIQUE constraint is the actual guarantee; this loop just turns a
        # collision into a quiet retry instead of a 500.
        for _ in range(_INVITE_CODE_MAX_ATTEMPTS):
            code = "".join(
                secrets.choice(_INVITE_CODE_ALPHABET) for _ in range(_INVITE_CODE_LENGTH)
            )
            room.join_code = code
            try:
                await self._db.flush()
            except IntegrityError:
                await self._db.rollback()
                continue
            return code

        raise ConflictException(
            "No se pudo generar un código de invitación único; intenta de nuevo."
        )

    async def join_by_code(self, code: str, user_id: UUID) -> StudyRoom:
        """Self-service join via a room's current invite code."""
        normalized = code.strip().upper()

        result = await self._db.execute(
            select(StudyRoom).where(StudyRoom.join_code == normalized)
        )
        room = result.scalar_one_or_none()
        if room is None:
            # Generic message on purpose: doesn't distinguish "never
            # existed" from "was regenerated/expired" — nothing for a
            # guesser to learn either way.
            raise NotFoundException("Código de invitación inválido.")

        if await self._get_membership(room.id, user_id) is not None:
            raise ConflictException("Ya eres miembro de esta sala.")

        self._db.add(
            RoomMember(room_id=room.id, user_id=user_id, room_role=RoomRole.MEMBER)
        )
        await self._db.flush()
        await self._db.refresh(room)
        return room

    async def broadcast_group_message(self, room_id: UUID, message: "GroupMessageRead") -> None:
        """Best-effort push to Supabase Realtime Broadcast — never raises.

        Called by the router AFTER `db.commit()`, mirroring the existing
        commit-then-external-side-effect pattern used for Celery dispatch
        (`documents/router.py`'s upload endpoint) — the message must be
        durably committed before anything outside this transaction (a
        realtime-notified client, a background worker) is told it exists.

        If Supabase's Realtime service is slow, misconfigured, or down,
        connected clients simply miss the instant push and see the message
        on their next reload/history fetch instead of losing it — failing
        the whole request over a notification channel would be a much
        worse trade-off than a logged warning here.
        """
        if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY:
            logger.warning(
                "SUPABASE_SERVICE_ROLE_KEY not configured — skipping realtime "
                "broadcast for room %s (message was still saved).",
                room_id,
            )
            return

        # Public channel keyed by the room's UUID: true per-user RLS
        # authorization isn't available here (see the module docstring on
        # GroupMessage) since room_members lives in OUR Postgres, not
        # Supabase's — Supabase's RLS can't see it. The UUID itself is
        # never exposed publicly, so this is "obscure, not access-
        # controlled" — acceptable for a v1, not a substitute for real
        # authorization if this ever needs a stronger guarantee.
        topic = f"room:{room_id}:community"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    settings.supabase_realtime_broadcast_url,
                    headers={"apikey": settings.SUPABASE_SERVICE_ROLE_KEY},
                    json={
                        "messages": [
                            {
                                "topic": topic,
                                "event": "new-message",
                                "payload": message.model_dump(mode="json"),
                                "private": False,
                            }
                        ]
                    },
                )
            if response.status_code >= 400:
                logger.warning(
                    "Realtime broadcast for room %s returned %s: %s",
                    room_id,
                    response.status_code,
                    response.text[:500],
                )
        except httpx.HTTPError:
            logger.exception("Realtime broadcast request failed for room %s", room_id)

    async def post_group_message(
        self, room_id: UUID, user_id: UUID, content: str
    ) -> GroupMessage:
        # VIEWERs can read a room but not participate — same bar as
        # posting a document. Caller (router) commits and broadcasts
        # afterward — this method only validates and stages the insert.
        await self.require_role(room_id, user_id, RoomRole.MEMBER)

        message = GroupMessage(room_id=room_id, user_id=user_id, content=content)
        self._db.add(message)
        await self._db.flush()
        await self._db.refresh(message, attribute_names=["created_at", "user"])
        return message

    async def list_group_messages(
        self, room_id: UUID, user_id: UUID, limit: int = 50
    ) -> list[GroupMessage]:
        await self.require_role(room_id, user_id, RoomRole.VIEWER)

        result = await self._db.execute(
            select(GroupMessage)
            .where(GroupMessage.room_id == room_id)
            .order_by(GroupMessage.created_at.desc())
            .limit(limit)
        )
        # Oldest-first for rendering — the DB query itself is newest-first
        # (so LIMIT keeps the most recent N, not the oldest N).
        return list(reversed(result.scalars().all()))