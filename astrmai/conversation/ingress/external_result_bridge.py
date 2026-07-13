from __future__ import annotations

import logging
from importlib import import_module

try:
    from astrbot.api import logger
except Exception:
    logger = logging.getLogger(__name__)

from ...infrastructure.runtime.trace_runtime import debug_trace, preview_text
from ...learning.logging.bot_reply_recorder import record_bot_reply
from ...shared.helpers.plugin_helpers import build_external_reply_event, get_event_self_id

Comp = import_module("astrbot.api.message_components")


def extract_external_reply_text(result) -> str:
    if not result or not getattr(result, "chain", None):
        return ""

    reply_text = ""
    for comp in result.chain:
        if isinstance(comp, Comp.Plain):
            reply_text += comp.text
        elif isinstance(comp, Comp.Image):
            reply_text += "[图片]"
    return reply_text


async def bridge_external_plugin_result(runtime, event) -> None:
    if event.get_extra("astrmai_loop_source", "") == "external_result_bridge":
        return

    # ── 外部结果白名单检查 ──
    explicit_source = str(event.get_extra("astrmai_loop_source", "") or "").strip()
    loop_source = explicit_source or "astrbot_builtin"
    runtime_config = getattr(runtime, "config", None)
    allowed = getattr(getattr(runtime_config, "global_settings", None), "external_result_sources", ["astrbot_builtin"]) or ["astrbot_builtin"]
    if "*" not in allowed and loop_source not in allowed:
        logger.debug(f"[ExtBridge] skipped non-whitelisted source: {loop_source}")
        return
    if event.get_extra("astrmai_is_self_reply", False) and not explicit_source:
        return

    reply_text = extract_external_reply_text(event.get_result())
    if not reply_text:
        return

    host_bridge = getattr(runtime, "host_bridge", None)
    if host_bridge is not None:
        if host_bridge.is_ghost_sentinel(reply_text):
            return
        global_settings = getattr(runtime_config, "global_settings", None)
        interception_enabled = bool(getattr(global_settings, "enable_error_interception", True))
        if host_bridge.should_intercept_error(reply_text, enabled=interception_enabled):
            return

    chat_id = event.unified_msg_origin
    bot_id = get_event_self_id(event)
    bot_reply_event = build_external_reply_event(reply_text)
    bot_reply_event.update(
        {
            "unified_msg_origin": chat_id,
            "group_id": str(event.get_group_id() or "") if hasattr(event, "get_group_id") else "",
            "sender_id": bot_id,
            "sender_name": "",
            "self_id": bot_id,
        }
    )
    bot_reply_event["extra"] = {
        **dict(bot_reply_event.get("extra", {}) or {}),
        "astrmai_loop_source": "external_result_bridge",
        "is_external_bot_reply": True,
        "astrmai_external_result_source": loop_source,
        "astrmai_origin_sender_id": str(event.get_sender_id() or "") if hasattr(event, "get_sender_id") else "",
    }
    debug_trace(event, "ingress.external_result", preview=preview_text(reply_text, 100))

    if runtime.attention_gate and hasattr(runtime.attention_gate, "inject_external_event"):
        await runtime.attention_gate.inject_external_event(chat_id, bot_reply_event)
        logger.debug(f"[{chat_id}] external plugin result injected into attention window.")

    await record_bot_reply(runtime, chat_id, bot_id, reply_text, prefix="(内置插件执行结果): ")
