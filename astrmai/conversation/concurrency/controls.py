from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

try:
    from astrbot.api import logger
except Exception:
    logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConversationConcurrencyFlags:
    generation_enabled: bool = True
    send_claim_enabled: bool = True
    group_thread_wait_enabled: bool = False
    non_conversational_guard_enabled: bool = True
    debug_trace_enabled: bool = False


_SUMMARY_FIELDS = frozenset(
    {
        "chat_id",
        "mode",
        "thread_id",
        "generation",
        "blocked_reason",
        "claim_status",
        "send_key_hash",
        "wait_scope",
        "wait_resume_reason",
        "resolver_source",
    }
)


def resolve_conversation_concurrency_flags(config: Any) -> ConversationConcurrencyFlags:
    conversation = getattr(config, "conversation", None)
    return ConversationConcurrencyFlags(
        generation_enabled=bool(getattr(conversation, "conversation_generation_enabled", True)),
        send_claim_enabled=bool(getattr(conversation, "reply_send_claim_enabled", True)),
        group_thread_wait_enabled=bool(getattr(conversation, "group_thread_wait_enabled", False)),
        non_conversational_guard_enabled=bool(
            getattr(conversation, "non_conversational_guard_enabled", True)
        ),
        debug_trace_enabled=bool(
            getattr(conversation, "conversation_concurrency_debug_trace_enabled", False)
        ),
    )


def record_conversation_concurrency_trace(
    event: Any,
    action: str,
    *,
    debug_enabled: bool = False,
    debug_factory: Callable[[], Mapping[str, Any]] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {"action": str(action or "unknown")}
    for key in _SUMMARY_FIELDS:
        value = fields.get(key)
        if value not in (None, "", [], {}):
            record[key] = value

    if debug_enabled and debug_factory is not None:
        try:
            debug_payload = dict(debug_factory() or {})
        except Exception:
            logger.debug("[ConversationConcurrency] debug trace payload degraded", exc_info=True)
        else:
            if debug_payload:
                record["debug"] = debug_payload

    existing: list[dict[str, Any]] = []
    if hasattr(event, "get_extra"):
        try:
            existing = list(event.get_extra("astrmai_conversation_concurrency_trace", []) or [])
        except Exception:
            existing = []
    existing = (existing + [record])[-64:]
    if hasattr(event, "set_extra"):
        event.set_extra("astrmai_conversation_concurrency_trace", existing)

    summary = ", ".join(f"{key}={value!r}" for key, value in record.items() if key != "debug")
    logger.debug(f"[ConversationConcurrency] {summary}")
    return record


__all__ = [
    "ConversationConcurrencyFlags",
    "record_conversation_concurrency_trace",
    "resolve_conversation_concurrency_flags",
]
