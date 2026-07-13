"""Router for `/api/v1/rooms/*`."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.responses import APIResponse
from src.core.security import SupabaseUser, verify_supabase_jwt
from src.database import get_db
from src.rag.schemas import RoomChatRequest, RoomChatResponse
from src.rag.service import RAGService
from src.rooms.schemas import (
    GroupMessageCreate,
    GroupMessageRead,
    RoomCreate,
    RoomInviteCodeRead,
    RoomJoinRequest,
    RoomMemberInvite,
    RoomMemberRead,
    RoomMemberRoleUpdate,
    RoomRead,
    RoomUpdate,
)
from src.rooms.service import RoomService

router = APIRouter(prefix="/api/v1/rooms", tags=["rooms"])


@router.get("", response_model=APIResponse[list[RoomRead]])
async def list_rooms(
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[list[RoomRead]]:
    rooms = await RoomService(db).list_for_user(user.id)
    return APIResponse.success([RoomRead.model_validate(r) for r in rooms])


@router.post("", response_model=APIResponse[RoomRead], status_code=status.HTTP_201_CREATED)
async def create_room(
    payload: RoomCreate,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[RoomRead]:
    room = await RoomService(db).create(user.id, payload)
    return APIResponse.success(RoomRead.model_validate(room))


# Registered here — a literal path ("/join"), before any "/{room_id}"
# routes below — so it can never be shadowed by the room_id path param.
@router.post("/join", response_model=APIResponse[RoomRead])
async def join_room(
    payload: RoomJoinRequest,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[RoomRead]:
    room = await RoomService(db).join_by_code(payload.code, user.id)
    return APIResponse.success(RoomRead.model_validate(room))


@router.get("/{room_id}", response_model=APIResponse[RoomRead])
async def get_room(
    room_id: UUID,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[RoomRead]:
    room, membership = await RoomService(db).get_for_user(room_id, user.id)
    payload = RoomRead.model_validate(room).model_copy(
        update={"my_role": membership.room_role}
    )
    return APIResponse.success(payload)


@router.patch("/{room_id}", response_model=APIResponse[RoomRead])
async def update_room(
    room_id: UUID,
    payload: RoomUpdate,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[RoomRead]:
    room = await RoomService(db).update(room_id, user.id, payload)
    return APIResponse.success(RoomRead.model_validate(room))


@router.delete("/{room_id}", response_model=APIResponse[None])
async def delete_room(
    room_id: UUID,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[None]:
    await RoomService(db).delete(room_id, user.id)
    return APIResponse.success(None)


@router.post(
    "/{room_id}/members",
    response_model=APIResponse[RoomMemberRead],
    status_code=status.HTTP_201_CREATED,
)
async def invite_member(
    room_id: UUID,
    payload: RoomMemberInvite,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[RoomMemberRead]:
    membership = await RoomService(db).invite_member(room_id, user.id, payload)
    return APIResponse.success(RoomMemberRead.model_validate(membership))


@router.post(
    "/{room_id}/invite-code",
    response_model=APIResponse[RoomInviteCodeRead],
)
async def generate_invite_code(
    room_id: UUID,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[RoomInviteCodeRead]:
    code = await RoomService(db).generate_invite_code(room_id, user.id)
    return APIResponse.success(RoomInviteCodeRead(join_code=code))


@router.patch(
    "/{room_id}/members/{member_user_id}",
    response_model=APIResponse[RoomMemberRead],
)
async def change_member_role(
    room_id: UUID,
    member_user_id: UUID,
    payload: RoomMemberRoleUpdate,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[RoomMemberRead]:
    membership = await RoomService(db).change_member_role(
        room_id, user.id, member_user_id, payload.room_role
    )
    return APIResponse.success(RoomMemberRead.model_validate(membership))


@router.post(
    "/{room_id}/chat",
    response_model=APIResponse[RoomChatResponse],
)
async def chat_with_room(
    room_id: UUID,
    payload: RoomChatRequest,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[RoomChatResponse]:
    """Stateless RAG Q&A over this room's indexed documents.

    Retrieves the 5 nearest chunks (pgvector cosine distance, tenant-
    isolated to this room) and asks Gemini to answer strictly from that
    context. For a persistent, streaming (SSE) chat history instead, see
    `POST /api/v1/rooms/{room_id}/conversations` + `POST
    /api/v1/conversations/{conversation_id}/messages` in `src/rag/router.py`.
    """
    answer = await RAGService(db).quick_answer(room_id, user.id, payload.query)
    return APIResponse.success(RoomChatResponse(response=answer))


@router.post(
    "/{room_id}/group-chat",
    response_model=APIResponse[GroupMessageRead],
    status_code=status.HTTP_201_CREATED,
)
async def post_group_message(
    room_id: UUID,
    payload: GroupMessageCreate,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[GroupMessageRead]:
    message = await RoomService(db).post_group_message(room_id, user.id, payload.content)
    # Commit BEFORE broadcasting — see the note on broadcast_group_message
    # for why (mirrors the commit-before-.delay() pattern used for
    # document ingestion).
    await db.commit()

    read = GroupMessageRead.model_validate(message).model_copy(
        update={"author_name": message.user.name if message.user else None}
    )
    await RoomService(db).broadcast_group_message(room_id, read)
    return APIResponse.success(read)


@router.get(
    "/{room_id}/group-chat",
    response_model=APIResponse[list[GroupMessageRead]],
)
async def list_group_messages(
    room_id: UUID,
    limit: int = 50,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[list[GroupMessageRead]]:
    messages = await RoomService(db).list_group_messages(room_id, user.id, limit)
    payload = [
        GroupMessageRead.model_validate(m).model_copy(
            update={"author_name": m.user.name if m.user else None}
        )
        for m in messages
    ]
    return APIResponse.success(payload)