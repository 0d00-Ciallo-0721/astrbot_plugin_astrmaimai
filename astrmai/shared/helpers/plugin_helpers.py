from __future__ import annotations

import asyncio
import time
from importlib import import_module
from typing import Any, List, Tuple

from astrbot.api import logger as _astrbot_logger

_comp_cache = None


def _get_comp():
    global _comp_cache
    if _comp_cache is None:
        _comp_cache = import_module("astrbot.api.message_components")
    return _comp_cache


def get_preferred_model(models: List[str], default: str = "Unconfigured") -> str:
    return (models or [default])[0]


def format_model_pool(models: List[str], default: str = "Unconfigured") -> str:
    if not models:
        return default
    return f"{models[0]} (+{len(models) - 1})"


def safe_create_task(
    coro,
    name: str = "",
    track_set: set = None,
    *,
    event: Any = None,
    task_name: str | None = None,
    metadata: dict[str, Any] | None = None,
):
    """ponytail: fire-and-forget with error logging, add structured task management when needed.
    
    Wraps asyncio.create_task() with automatic exception logging via add_done_callback.
    Use this for any background task where silent exception swallowing is unacceptable.
    """
    owns_loop = False
    if isinstance(coro, asyncio.Task):
        task = coro
        loop = None
    else:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            owns_loop = True
            task = loop.create_task(coro, name=name or None)
        else:
            task = asyncio.create_task(coro, name=name or None)
    if track_set is not None:
        track_set.add(task)

    def _log_task_result(t: asyncio.Task) -> None:
        if track_set is not None:
            track_set.discard(t)
        if t.cancelled():
            return
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        if exc:
            task_name = (name or t.get_name()) if hasattr(t, 'get_name') else name or "unknown"
            _astrbot_logger.error(f"[AstrMai] background task '{task_name}' crashed: {exc}", exc_info=exc)

    task.add_done_callback(_log_task_result)
    if event is not None:
        from ...infrastructure.runtime.turn_call_ledger import attach_background_task_trace

        attach_background_task_trace(
            task,
            event,
            task_name or name or (task.get_name() if hasattr(task, "get_name") else "background"),
            metadata=metadata,
        )
    if owns_loop:
        try:
            loop.run_until_complete(task)
        except Exception:
            pass
        finally:
            loop.close()
    return task


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
    return getattr(_get_comp(), name, None)


def is_direct_call_event(event: Any) -> bool:
    if not event.get_group_id():
        return True

    bot_id = str(event.get_self_id()) if hasattr(event, "get_self_id") else ""
    if not event.message_obj or not event.message_obj.message:
        return False

    at_component = _message_component_class("At")
    for component in event.message_obj.message:
        component_name = component.__class__.__name__.lstrip("_")
        is_at = (
            (isinstance(component, at_component) if at_component else False)
            or component_name == "At"
            or str(getattr(component, "type", "")).lower() == "at"
        )
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
        if owner is None:
            continue
        for attr_name in ("_background_tasks", "_session_tasks"):
            if hasattr(owner, attr_name):
                tasks.extend(list(getattr(owner, attr_name)))
    return tasks
