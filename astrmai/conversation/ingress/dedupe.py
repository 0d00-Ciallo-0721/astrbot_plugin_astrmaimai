from __future__ import annotations

import hashlib
from time import monotonic

from astrbot.api import logger

from ...presentation.dto.message_scope import IngressDecision

# ponytail: module-level dict, asyncio single-threaded — no lock needed
_debounce_cache: dict[str, float] = {}


def build_message_signature_text(event) -> str:
    msg_str = event.message_str.strip() if event.message_str else ""
    if msg_str:
        return msg_str
    return f"obj_len_{len(str(getattr(event.message_obj, 'message', '')))}"


def check_message_dedup(event, ttl_seconds: float = 1.5) -> IngressDecision:
    msg_str = build_message_signature_text(event)
    sender_id = str(event.get_sender_id())
    chat_id = str(event.unified_msg_origin)
    message_obj = getattr(event, "message_obj", None)
    message_id = str(
        getattr(message_obj, "message_id", "")
        or getattr(event, "message_id", "")
        or ""
    ).strip()
    fingerprint = f"{chat_id}_{sender_id}_id:{message_id}" if message_id else f"{chat_id}_{sender_id}_text:{msg_str}"
    now = monotonic()

    # ponytail: asyncio single-threaded, no lock needed
    keys_to_delete = [key for key, value in _debounce_cache.items() if now - value > ttl_seconds]
    for key in keys_to_delete:
        _debounce_cache.pop(key, None)

    if fingerprint in _debounce_cache:
        logger.warning(f"[AstrMai-Sensor] 极速防抖生效，已拦截 AstrBot 框架双发/分身消息: sha256={hashlib.sha256(msg_str.encode()).hexdigest()[:8]}")
        return IngressDecision.stop("duplicate_message")

    _debounce_cache[fingerprint] = now
    return IngressDecision.allow()
