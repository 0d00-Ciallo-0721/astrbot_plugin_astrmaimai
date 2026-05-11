from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger

from ...conversation.ingress.command_guard import check_framework_command
from ...conversation.ingress.dedupe import build_message_signature_text, check_message_dedup
from ...conversation.ingress.permission_guard import check_message_scope_access
from ...conversation.ingress.poke_handler import handle_poke_if_needed
from ...infrastructure.runtime.trace_runtime import debug_trace, preview_text
from ...presentation.dto.message_scope import MessageScope
from ...shared.helpers.plugin_helpers import is_direct_call_event

if TYPE_CHECKING:
    from ...app.runtime_context import PluginRuntimeContext


async def handle_global_message(runtime: PluginRuntimeContext, facade, event):
    scope = MessageScope.from_event(event)
    msg = event.message_str.strip() if event.message_str else ""
    msg_str = build_message_signature_text(event)
    debug_trace(event, "ingress.enter", chat_id=scope.chat_id, sender_id=scope.sender_id, preview=preview_text(msg_str, 80))

    if check_message_dedup(event).should_stop:
        debug_trace(event, "ingress.stop", reason="duplicate_message")
        return

    if (await handle_poke_if_needed(runtime, event)).should_stop:
        debug_trace(event, "ingress.stop", reason="poke_event")
        return

    if check_framework_command(facade, msg).should_stop:
        debug_trace(event, "ingress.stop", reason="framework_command")
        return

    if check_message_scope_access(runtime, scope).should_stop:
        debug_trace(event, "ingress.stop", reason="permission_guard")
        return

    if scope.sender_id == scope.self_id:
        debug_trace(event, "ingress.stop", reason="self_message")
        return

    group_wait_result = "NONE"
    if event.get_group_id() and runtime.group_reply_wait_manager:
        group_wait_result = runtime.group_reply_wait_manager.handle_incoming_message(event)

    if getattr(runtime.config.global_settings, "debug_mode", False):
        sender_name = event.get_sender_name()
        logger.info(f"[AstrMai-Sensor] 收到消息 | 发送者: {sender_name} | 内容: {msg_str[:20]}...")

    user_id = event.get_sender_id()
    if user_id and runtime.lifecycle.manager:
        runtime.lifecycle.manager.track_task(facade.update_user_stats(user_id))

    if runtime.reflect_tracker:
        review_feedback = await runtime.reflect_tracker.try_consume_feedback(event)
        if review_feedback:
            yield event.plain_result(review_feedback)
            return

    await runtime.evolution.record_user_message(event)
    status = await runtime.attention_gate.process_event(event)
    is_direct_call = is_direct_call_event(event)
    debug_trace(event, "ingress.after_attention", status=status, direct_call=is_direct_call)

    if (
        event.get_group_id()
        and runtime.group_reply_wait_manager
        and group_wait_result != "RESUME"
        and status in {"ENGAGED", "BUFFERED"}
    ):
        runtime.group_reply_wait_manager.cancel_wait(
            event.unified_msg_origin,
            reason=f"interrupted_by_{status.lower()}",
        )

    if status == "ENGAGED" or is_direct_call:
        ghost_message = runtime.host_bridge.suppress_default_llm(event)
        yield event.plain_result(ghost_message)
