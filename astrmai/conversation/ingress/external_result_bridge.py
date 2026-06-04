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
    if event.get_extra("astrmai_is_self_reply", False):
        return

    if event.get_extra("astrmai_loop_source", "") == "external_result_bridge":
        return

    reply_text = extract_external_reply_text(event.get_result())
    if not reply_text:
        return

    chat_id = event.unified_msg_origin
    bot_id = get_event_self_id(event)
    bot_reply_event = build_external_reply_event(reply_text)
    bot_reply_event["extra"] = {
        **dict(bot_reply_event.get("extra", {}) or {}),
        "astrmai_loop_source": "external_result_bridge",
        "is_external_bot_reply": True,
    }
    debug_trace(event, "ingress.external_result", preview=preview_text(reply_text, 100))

    if runtime.attention_gate and hasattr(runtime.attention_gate, "inject_external_event"):
        await runtime.attention_gate.inject_external_event(chat_id, bot_reply_event)
        logger.debug(f"[{chat_id}] external plugin result injected into attention window.")

    await record_bot_reply(runtime, chat_id, bot_id, reply_text, prefix="(内置插件执行结果): ")
