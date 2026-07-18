from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class StatusResponse(BaseModel):
    status: str = "ok"


class DashboardSnapshot(BaseModel):
    db_size_kb: float = 0.0
    webui_mem_mb: float = 0.0
    sys_cpu_percent: float = 0.0
    sys_mem_percent: float = 0.0
    total_users: int = 0
    pending_reviews: int = 0
    total_memory_events: int = 0
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class ReviewActionRequest(BaseModel):
    action: str
    replacement: str | None = None
    weight: float | None = None
    reason: str | None = None
    situation: str | None = None
    style: str | None = None
    shared_scope: str | None = None
    review_suggestion: str | None = None


class BatchReviewRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)
    action: str


class PagedResponse(BaseModel, Generic[T]):
    items: list[T] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


class ConfigSectionPayload(BaseModel):
    section: str
    value: dict[str, Any] = Field(default_factory=dict)


class ConfigSectionUpdate(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "BatchReviewRequest",
    "ConfigSectionPayload",
    "ConfigSectionUpdate",
    "DashboardSnapshot",
    "PagedResponse",
    "ReviewActionRequest",
    "StatusResponse",
]
