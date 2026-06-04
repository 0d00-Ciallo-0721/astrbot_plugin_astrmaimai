from __future__ import annotations

import hashlib
import sys
import threading
import time

from astrbot.api import logger

from ...presentation.dto.message_scope import IngressDecision


def build_message_signature_text(event) -> str:
    msg_str = event.message_str.strip() if event.message_str else ""
    if msg_str:
        return msg_str
    return f"obj_len_{len(str(getattr(event.message_obj, 'message', '')))}"


def check_message_dedup(event, ttl_seconds: float = 1.5) -> IngressDecision:
    if not hasattr(sys, "_astrmai_debounce_lock"):
        sys._astrmai_debounce_lock = threading.Lock()
        sys._astrmai_global_debounce_cache = {}

    msg_str = build_message_signature_text(event)
    sender_id = str(event.get_sender_id())
    chat_id = str(event.unified_msg_origin)
    fingerprint = f"{chat_id}_{sender_id}_{msg_str}"
    now = time.time()

    with sys._astrmai_debounce_lock:
        keys_to_delete = [key for key, value in sys._astrmai_global_debounce_cache.items() if now - value > ttl_seconds]
        for key in keys_to_delete:
            sys._astrmai_global_debounce_cache.pop(key, None)

        if fingerprint in sys._astrmai_global_debounce_cache:
            logger.warning(f"[AstrMai-Sensor] 极速防抖生效，已拦截 AstrBot 框架双发/分身消息: sha256={hashlib.sha256(msg_str.encode()).hexdigest()[:8]}")
            return IngressDecision.stop("duplicate_message")

        sys._astrmai_global_debounce_cache[fingerprint] = now

    return IngressDecision.allow()
