"""Router for `/api/v1/users/*`."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.responses import APIResponse
from src.core.security import SupabaseUser, verify_supabase_jwt
from src.database import get_db
from src.users.schemas import (
    APIKeyCreate,
    APIKeyCreated,
    APIKeyRead,
    UserRead,
    UserSyncRequest,
    UserUpdate,
)
from src.users.service import UserService

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.get("/me", response_model=APIResponse[UserRead])
async def get_me(
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[UserRead]:
    profile = await UserService(db).get_by_id(user.id)
    return APIResponse.success(UserRead.model_validate(profile))


@router.patch("/me", response_model=APIResponse[UserRead])
async def update_me(
    payload: UserUpdate,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[UserRead]:
    profile = await UserService(db).update_profile(user.id, payload)
    return APIResponse.success(UserRead.model_validate(profile))


@router.post(
    "/sync",
    response_model=APIResponse[UserRead],
    status_code=status.HTTP_201_CREATED,
)
async def sync_user(
    payload: UserSyncRequest,
    db: AsyncSession = Depends(get_db),
) -> APIResponse[UserRead]:
    """Webhook target: Supabase Auth calls this right after a sign-up.

    Deliberately does NOT depend on `verify_supabase_jwt` — there is no
    end-user session yet at signup time, Supabase itself is the caller.
    IMPORTANT: before this reaches production it must be locked down at the
    infrastructure layer (Nginx IP allow-list for Supabase's webhook range,
    or a shared-secret header validated in `core/middleware.py`), otherwise
    anyone can forge arbitrary user rows. Tracked for the middleware part
    of this build.
    """
    profile = await UserService(db).sync_from_supabase(payload)
    return APIResponse.success(UserRead.model_validate(profile))


@router.post(
    "/api-keys",
    response_model=APIResponse[APIKeyCreated],
    status_code=status.HTTP_201_CREATED,
)
async def create_api_key(
    payload: APIKeyCreate,
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[APIKeyCreated]:
    api_key, raw_key = await UserService(db).create_api_key(user.id, payload)
    body = APIKeyCreated(
        id=api_key.id,
        name=api_key.name,
        is_active=api_key.is_active,
        created_at=api_key.created_at,
        api_key=raw_key,
    )
    return APIResponse.success(body)


@router.get("/api-keys", response_model=APIResponse[list[APIKeyRead]])
async def list_api_keys(
    user: SupabaseUser = Depends(verify_supabase_jwt),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[list[APIKeyRead]]:
    keys = await UserService(db).list_api_keys(user.id)
    return APIResponse.success([APIKeyRead.model_validate(k) for k in keys])