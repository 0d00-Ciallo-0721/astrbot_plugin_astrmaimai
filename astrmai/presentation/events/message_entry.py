from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger

from ...conversation.ingress.command_guard import check_framework_command
from ...conversation.ingress.dedupe import build_message_signature_text, check_message_dedup
from ...infrastructure.runtime.trace_runtime import debug_trace, preview_text
from ...presentation.dto.message_scope import MessageScope
from ...shared.helpers.plugin_helpers import is_direct_call_event

if TYPE_CHECKING:
    from ...app.runtime_facade_protocol import RuntimeFacadeProtocol


async def handle_global_message(facade: RuntimeFacadeProtocol, event):
    scope = MessageScope.from_event(event)
    msg = event.message_str.strip() if event.message_str else ""
    msg_str = build_message_signature_text(event)
    debug_trace(event, "ingress.enter", chat_id=scope.chat_id, sender_id=scope.sender_id, preview=preview_text(msg_str, 80))

    if check_message_dedup(event).should_stop:
        debug_trace(event, "ingress.stop", reason="duplicate_message")
        return

    if (await facade.handle_poke(event)).should_stop:
        debug_trace(event, "ingress.stop", reason="poke_event")
        return

    if check_framework_command(facade, msg).should_stop:
        debug_trace(event, "ingress.stop", reason="framework_command")
        return

    if facade.check_message_scope_access(scope).should_stop:
        debug_trace(event, "ingress.stop", reason="permission_guard")
        return

    if scope.sender_id == scope.self_id:
        debug_trace(event, "ingress.stop", reason="self_message")
        return

    group_wait_result = await facade.handle_group_reply_wait(event, scope)

    if facade.is_debug_mode():
        sender_name = event.get_sender_name()
        logger.info(f"[AstrMai-Sensor] 收到消息 | 发送者: {sender_name} | 内容: {msg_str[:20]}...")

    facade.track_incoming_user_activity(event.get_sender_id())

    review_feedback = await facade.try_consume_reflect_feedback(event)
    if review_feedback:
        yield event.plain_result(review_feedback)
        return

    status = await facade.record_and_dispatch_attention(event, scope)
    is_direct_call = is_direct_call_event(event)
    debug_trace(event, "ingress.after_attention", status=status, direct_call=is_direct_call)

    facade.cancel_group_wait_if_interrupted(event, group_wait_result, status)

    ghost_message = facade.suppress_default_llm_if_engaged(event, status, is_direct_call)
    if ghost_message is not None:
        yield event.plain_result(ghost_message)
