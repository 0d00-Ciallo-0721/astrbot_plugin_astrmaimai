from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

try:
    from astrbot.api import logger
except Exception:
    logger = logging.getLogger(__name__)


def new_trace_id() -> str:
    return uuid4().hex[:12]


def ensure_external_result_id(event: Any) -> str:
    """Return a stable, privacy-safe id for one external result hook call."""
    key = "astrmai_external_result_id"
    value = ""
    if hasattr(event, "get_extra"):
        try:
            value = str(event.get_extra(key, "") or "").strip()
        except Exception:
            value = ""
    elif isinstance(event, dict):
        value = str(event.get(key, "") or "").strip()
    if value:
        return value
    value = f"ext-{new_trace_id()}"
    if hasattr(event, "set_extra"):
        try:
            event.set_extra(key, value)
        except Exception:
            pass
    elif isinstance(event, dict):
        event[key] = value
    return value


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


def ensure_trace_id(event: Any) -> str:
    trace_id = ""
    if hasattr(event, "get_extra"):
        trace_id = str(event.get_extra("astrmai_trace_id", "") or "")
    elif isinstance(event, dict):
        trace_id = str(event.get("astrmai_trace_id", "") or "")
    if not trace_id:
        trace_id = new_trace_id()
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_trace_id", trace_id)
        elif isinstance(event, dict):
            event["astrmai_trace_id"] = trace_id
    return trace_id


def append_trace_stage(event: Any, stage: str, **fields: Any) -> str:
    trace_id = ensure_trace_id(event)
    trace_log = []
    if hasattr(event, "get_extra"):
        trace_log = list(event.get_extra("astrmai_trace_log", []) or [])
    elif isinstance(event, dict):
        trace_log = list(event.get("astrmai_trace_log", []) or [])
    record = {"trace_id": trace_id, "stage": stage}
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, str):
            record[key] = preview_text(value, 160)
        else:
            record[key] = value
    trace_log.append(record)
    if hasattr(event, "set_extra"):
        event.set_extra("astrmai_trace_log", trace_log)
    elif isinstance(event, dict):
        event["astrmai_trace_log"] = trace_log
    return trace_id


def debug_trace(event: Any, stage: str, **fields: Any) -> str:
    trace_id = append_trace_stage(event, stage, **fields)
    preview_fields = ", ".join(
        f"{key}={value!r}" for key, value in fields.items() if value not in (None, "", [], {})
    )
    if preview_fields:
        logger.debug(f"[AstrMai-Trace] {trace_id} {stage} | {preview_fields}")
    else:
        logger.debug(f"[AstrMai-Trace] {trace_id} {stage}")
    return trace_id


__all__ = [
    "FocusSnapshot",
    "GatewaySnapshot",
    "PromptSnapshot",
    "ReplySnapshot",
    "append_trace_stage",
    "debug_trace",
    "ensure_external_result_id",
    "ensure_trace_id",
    "new_trace_id",
    "preview_text",
]
