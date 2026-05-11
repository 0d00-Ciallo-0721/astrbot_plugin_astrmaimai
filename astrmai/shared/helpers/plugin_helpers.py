from __future__ import annotations

import time
from importlib import import_module
from typing import Any, List, Tuple

Comp = import_module("astrbot.api.message_components")


def get_preferred_model(models: List[str], default: str = "Unconfigured") -> str:
    return (models or [default])[0]


def format_model_pool(models: List[str], default: str = "Unconfigured") -> str:
    if not models:
        return default
    return f"{models[0]} (+{len(models) - 1})"


def extract_result_text(result: Any) -> str:
    if not result:
        return ""

    plain_text_getter = getattr(result, "get_plain_text", None)
    if callable(plain_text_getter):
        plain_text = plain_text_getter()
        if plain_text:
            return str(plain_text)

    reply_text = ""
    chain = getattr(result, "chain", None)
    if isinstance(chain, str):
        return chain
    if hasattr(chain, "__iter__"):
        for comp in chain:
            if hasattr(comp, "text"):
                reply_text += str(comp.text)
            elif isinstance(comp, str):
                reply_text += comp
    return reply_text


def resolve_event_scope(event: Any) -> Tuple[str, str, str]:
    umo = str(event.unified_msg_origin)
    parts = umo.split(":")
    platform_type = parts[1] if len(parts) >= 3 else ("GroupMessage" if event.get_group_id() else "FriendMessage")
    entity_id = parts[2] if len(parts) >= 3 else str(event.get_group_id() or event.get_sender_id())
    return umo, platform_type, entity_id


def get_event_self_id(event: Any) -> str:
    if hasattr(event, "message_obj") and hasattr(event.message_obj, "self_id"):
        return str(event.message_obj.self_id)
    if hasattr(event, "bot") and hasattr(event.bot, "self_id"):
        return str(event.bot.self_id)
    return "unknown"


def _message_component_class(name: str):
    current = import_module("astrbot.api.message_components")
    return getattr(current, name, None) or getattr(Comp, name, None)


def is_direct_call_event(event: Any) -> bool:
    if not event.get_group_id():
        return True

    bot_id = str(event.get_self_id()) if hasattr(event, "get_self_id") else ""
    if not event.message_obj or not event.message_obj.message:
        return False

    at_component = _message_component_class("At")
    for component in event.message_obj.message:
        is_at = isinstance(component, at_component) if at_component else component.__class__.__name__ == "At"
        if is_at and str(getattr(component, "qq", "")) == bot_id:
            return True
    return False


def build_external_reply_event(reply_text: str) -> dict[str, Any]:
    return {
        "is_external_bot_reply": True,
        "content": reply_text,
        "timestamp": time.time(),
    }


async def cleanup_stale_focus_pools(attention_gate: Any, ttl_seconds: float = 86400.0, now: float | None = None) -> int:
    if not attention_gate or not hasattr(attention_gate, "focus_pools") or not hasattr(attention_gate, "_pool_lock"):
        return 0

    current_time = now if now is not None else time.time()
    stale_count = 0
    async with attention_gate._pool_lock:
        for chat_id, ctx in list(attention_gate.focus_pools.items()):
            if current_time - ctx.last_active_time <= ttl_seconds:
                continue
            async with ctx.lock:
                if current_time - ctx.last_active_time > ttl_seconds:
                    attention_gate.focus_pools.pop(chat_id, None)
                    stale_count += 1
    return stale_count


def collect_background_tasks(*owners: Any) -> list[Any]:
    tasks: list[Any] = []
    for owner in owners:
        if owner is None or not hasattr(owner, "_background_tasks"):
            continue
        tasks.extend(list(getattr(owner, "_background_tasks")))
    return tasks
