from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


def new_trace_id() -> str:
    return uuid4().hex[:12]


def preview_text(text: str, limit: int = 120) -> str:
    if not isinstance(text, str):
        text = str(text or "")
    text = text.replace("\r\n", "\n").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


@dataclass
class FocusSnapshot:
    trace_id: str
    focus_reason: str
    root_reason: str
    focus_preview: str


@dataclass
class PromptSnapshot:
    trace_id: str
    recent_preview: str
    focus_preview: str
    ambient_preview: str


@dataclass
class GatewaySnapshot:
    trace_id: str
    lane_key: str
    model_id: str
    ok: bool


@dataclass
class ReplySnapshot:
    trace_id: str
    blocked: bool
    visible_preview: str
