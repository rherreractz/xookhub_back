"""
Unified API response contract for XookHub.

Every successful endpoint response is wrapped in `APIResponse`, guaranteeing
the exact envelope required by the frontend contract:

    {
      "data": ...,
      "meta": {...} | null,
      "error": null
    }
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class Meta(BaseModel):
    """Optional pagination / auxiliary metadata attached to a response."""

    model_config = ConfigDict(extra="allow")

    page: int | None = None
    per_page: int | None = None
    total: int | None = None


class ErrorDetail(BaseModel):
    """Standardized error payload returned on failure."""

    code: str
    message: str
    details: list[Any] | dict[str, Any] | None = None


class APIResponse(BaseModel, Generic[T]):
    """Generic success/error envelope wrapping every API response."""

    data: T | None = None
    meta: Meta | None = None
    error: ErrorDetail | None = None

    @classmethod
    def success(cls, data: T, meta: Meta | dict[str, Any] | None = None) -> "APIResponse[T]":
        if isinstance(meta, dict):
            meta = Meta(**meta)
        return cls(data=data, meta=meta, error=None)

    @classmethod
    def fail(cls, error: ErrorDetail) -> "APIResponse[None]":
        return cls(data=None, meta=None, error=error)