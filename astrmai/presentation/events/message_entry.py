from __future__ import annotations

import time
from typing import TYPE_CHECKING

from astrbot.api import logger

from ...conversation.concurrency.controls import (
    record_conversation_concurrency_trace,
    resolve_conversation_concurrency_flags,
)
from ...conversation.contracts.turn_identity import TurnIdentity, build_p0_thread_id
from ...conversation.ingress.command_guard import check_framework_command
from ...conversation.ingress.dedupe import build_message_signature_text, check_message_dedup
from ...infrastructure.runtime.trace_runtime import debug_trace, preview_text
from ...infrastructure.runtime.turn_call_ledger import (
    bind_turn_telemetry_identity,
    observe_stage,
)
from ...presentation.dto.message_scope import IngressDecision, MessageScope
from ...shared.helpers.plugin_helpers import is_direct_call_event

if TYPE_CHECKING:
    from ...app.runtime_facade_protocol import RuntimeFacadeProtocol


def _runtime_fallback_text(facade: RuntimeFacadeProtocol) -> str:
    try:
        getter = getattr(facade, "get_runtime_config", None)
        config = getter() if callable(getter) else None
        reply = getattr(config, "reply", None) if config is not None else None
        return str(getattr(reply, "fallback_text", "") or "")
    except Exception:
        logger.debug("[AstrMai] failed to resolve runtime fallback text", exc_info=True)
        return ""


def _resolve_message_id(event) -> str:
    message_obj = getattr(event, "message_obj", None)
    candidates = [
        getattr(message_obj, "message_id", None),
        getattr(message_obj, "id", None),
        getattr(event, "message_id", None),
    ]
    for candidate in candidates:
        normalized = str(candidate or "").strip()
        if normalized:
            return normalized
    return ""


def _find_notice_payload(value, *, depth: int = 0, seen: set[int] | None = None) -> dict:
    if value is None or depth > 8:
        return {}
    seen = seen if seen is not None else set()
    object_id = id(value)
    if object_id in seen:
        return {}
    seen.add(object_id)
    if isinstance(value, dict):
        post_type = str(value.get("post_type") or "").strip().lower()
        notice_type = str(value.get("notice_type") or "").strip().lower()
        if post_type == "notice" or notice_type:
            return value
        children = value.values()
    elif isinstance(value, (list, tuple)):
        children = value
    else:
        raw = getattr(value, "raw_event", None)
        if raw is not None:
            found = _find_notice_payload(raw, depth=depth + 1, seen=seen)
            if found:
                return found
        message_obj = getattr(value, "message_obj", None)
        if message_obj is not None and message_obj is not value:
            found = _find_notice_payload(message_obj, depth=depth + 1, seen=seen)
            if found:
                return found
        data = getattr(value, "__dict__", None)
        children = data.values() if isinstance(data, dict) else ()
    for child in children:
        found = _find_notice_payload(child, depth=depth + 1, seen=seen)
        if found:
            return found
    return {}


def _classify_event_route(event) -> tuple[str, dict]:
    payload = _find_notice_payload(event)
    if not payload:
        return "message", {}
    notice_type = str(payload.get("notice_type") or "").strip().lower()
    sub_type = str(payload.get("sub_type") or "").strip().lower()
    if notice_type == "poke" or sub_type in {"poke", "戳一戳"}:
        return "poke_notice", payload
    return "notice_passthrough", payload


async def _bind_turn_identity(facade: RuntimeFacadeProtocol, event, scope: MessageScope) -> None:
    prepare_turn = getattr(facade, "prepare_conversation_turn", None)
    if callable(prepare_turn):
        try:
            await prepare_turn(event, scope)
            bind_turn_telemetry_identity(event)
            return
        except Exception:
            logger.debug("[AstrMai] facade prepare_conversation_turn degraded", exc_info=True)
    try:
        getter = getattr(facade, "get_runtime_coordinator", None)
        runtime_coordinator = getter() if callable(getter) else None
    except Exception:
        logger.debug("[AstrMai] failed to resolve runtime coordinator for turn identity", exc_info=True)
        runtime_coordinator = None
    if runtime_coordinator is None or not hasattr(runtime_coordinator, "advance_generation"):
        return
    mode = "private" if scope.is_private_chat else "group"
    thread_id = build_p0_thread_id(mode, scope.chat_id)
    try:
        generation = await runtime_coordinator.advance_generation(scope.chat_id, thread_id)
    except Exception:
        logger.debug("[AstrMai] failed to advance turn generation", exc_info=True)
        return
    created_at = time.time()
    turn = TurnIdentity(
        mode=mode,
        chat_id=scope.chat_id,
        thread_id=thread_id,
        generation=generation,
        sender_id=scope.sender_id,
        input_message_ids=tuple(item for item in (_resolve_message_id(event),) if item),
        created_at=created_at,
    )
    event.set_extra("astrmai_turn_identity", turn)
    event.set_extra("astrmai_turn_mode", mode)
    event.set_extra("astrmai_turn_thread_id", thread_id)
    event.set_extra("astrmai_turn_generation", generation)
    event.set_extra("astrmai_turn_created_at", created_at)
    bind_turn_telemetry_identity(event)
    debug_trace(event, "ingress.turn_bound", chat_id=scope.chat_id, thread_id=thread_id, generation=generation, mode=mode)


async def handle_global_message(facade: RuntimeFacadeProtocol, event):
    event_route, notice_payload = _classify_event_route(event)
    if event_route == "notice_passthrough":
        notice_type = str(notice_payload.get("notice_type") or "unknown").strip().lower()
        sub_type = str(notice_payload.get("sub_type") or "").strip().lower()
        try:
            event.set_extra("astrmai_event_route", event_route)
            event.set_extra("astrmai_notice_type", notice_type)
            event.set_extra("astrmai_non_conversational", True)
            debug_trace(
                event,
                "ingress.notice_passthrough",
                notice_type=notice_type,
                sub_type=sub_type,
            )
        except Exception:
            logger.debug("[AstrMai] notice passthrough trace degraded", exc_info=True)
        return

    scope = MessageScope.from_event(event)
    msg = event.message_str.strip() if event.message_str else ""
    try:
        event.set_extra("is_private_chat", bool(scope.is_private_chat))
        event.set_extra("astrmai_turn_mode", "private" if scope.is_private_chat else "group")
    except Exception:
        logger.debug("[AstrMai] failed to attach message scope markers", exc_info=True)

    try:
        command_decision = check_framework_command(facade, msg, event=event)
    except Exception:
        logger.exception("[AstrMai] check_framework_command failed")
        command_decision = None
    if command_decision is not None and command_decision.should_passthrough:
        passthrough_metadata = {
            "reason": command_decision.reason,
            "command_name": command_decision.command_name,
            "owner_module": command_decision.owner_module,
            "handler_name": command_decision.handler_name,
            "detection_source": command_decision.detection_source,
        }
        try:
            event.set_extra("astrmai_command_passthrough", passthrough_metadata)
            debug_trace(event, "ingress.command_passthrough", **passthrough_metadata)
        except Exception:
            logger.debug("[AstrMai] command passthrough trace degraded", exc_info=True)
        return

    msg_str = build_message_signature_text(event)
    try:
        flag_getter = getattr(facade, "get_conversation_concurrency_flags", None)
        concurrency_flags = flag_getter() if callable(flag_getter) else resolve_conversation_concurrency_flags(None)
    except Exception:
        logger.debug("[AstrMai] failed to resolve conversation concurrency flags", exc_info=True)
        concurrency_flags = resolve_conversation_concurrency_flags(None)

    if check_message_dedup(event).should_stop:
        debug_trace(event, "ingress.stop", reason="duplicate_message")
        event.stop_event()
        return

    if scope.sender_id == scope.self_id:
        debug_trace(event, "ingress.stop", reason="self_message")
        if hasattr(event, "stop_event"):
            event.stop_event()
        return

    try:
        poke_decision = await facade.handle_poke(event)
    except Exception:
        logger.exception("[AstrMai] handle_poke failed, assuming not a poke")
        poke_decision = IngressDecision.allow()
    if poke_decision.should_stop:
        debug_trace(event, "ingress.stop", reason="poke_event")
        event.stop_event()
        return

    try:
        if facade.check_message_scope_access(scope).should_stop:
            debug_trace(event, "ingress.stop", reason="permission_guard")
            return
    except Exception:
        logger.exception("[AstrMai] check_message_scope_access failed — denying by default")
        event.stop_event()
        return

    readiness_check = getattr(facade, "is_runtime_ready", None)
    if callable(readiness_check) and not readiness_check():
        try:
            direct_call = scope.is_private_chat or is_direct_call_event(event)
        except Exception:
            direct_call = scope.is_private_chat
        if direct_call:
            message_getter = getattr(facade, "get_runtime_startup_message", None)
            message = message_getter() if callable(message_getter) else "人格正在初始化，请稍后再试。"
            yield event.plain_result(message)
        debug_trace(event, "ingress.stop", reason="runtime_not_ready")
        event.stop_event()
        return

    if (
        concurrency_flags.non_conversational_guard_enabled
        and bool(event.get_extra("astrmai_non_conversational", False))
    ):
        record_conversation_concurrency_trace(
            event,
            "non_conversational_blocked",
            chat_id=scope.chat_id,
            mode="private" if scope.is_private_chat else "group",
            debug_enabled=concurrency_flags.debug_trace_enabled,
        )
        try:
            coordinator_getter = getattr(facade, "get_runtime_coordinator", None)
            coordinator = coordinator_getter() if callable(coordinator_getter) else None
            if coordinator is not None and hasattr(coordinator, "record_concurrency_event"):
                await coordinator.record_concurrency_event("non_conversational_blocked")
        except Exception:
            logger.debug("[AstrMai] non-conversational metric degraded", exc_info=True)
        debug_trace(event, "ingress.stop", reason="non_conversational")
        event.stop_event()
        return

    debug_trace(event, "ingress.enter", chat_id=scope.chat_id, sender_id=scope.sender_id, preview=preview_text(msg_str, 80))

    await _bind_turn_identity(facade, event, scope)

    try:
        group_wait_result = await facade.handle_group_reply_wait(event, scope)
    except Exception:
        logger.exception("[AstrMai] handle_group_reply_wait failed")
        yield event.plain_result("\u26a0\ufe0f \u5904\u7406\u6d88\u606f\u65f6\u9047\u5230\u95ee\u9898\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\u3002")
        event.stop_event()
        return

    if facade.is_debug_mode():
        sender_name = event.get_sender_name()
        logger.info(f"[AstrMai-Sensor] 收到消息 | 发送者: {sender_name} | 内容: {msg_str[:20]}...")

    if not scope.is_anonymous_sender:
        try:
            facade.track_incoming_user_activity(event.get_sender_id())
        except Exception:
            logger.warning("[AstrMai] track_incoming_user_activity failed")
    else:
        debug_trace(event, "ingress.anonymous_sender", chat_id=scope.chat_id)

    try:
        review_feedback = await facade.try_consume_reflect_feedback(event)
    except Exception:
        logger.exception("[AstrMai] try_consume_reflect_feedback failed")
        review_feedback = None
    if review_feedback:
        yield event.plain_result(review_feedback)
        event.stop_event()
        return

    try:
        is_direct_call = is_direct_call_event(event)
    except Exception:
        logger.exception("[AstrMai] is_direct_call_event failed")
        is_direct_call = False
    try:
        with observe_stage(event, "attention.dispatch"):
            status = await facade.record_and_dispatch_attention(event, scope)
    except Exception:
        logger.exception("[AstrMai] record_and_dispatch_attention failed")
        status = "error"
        is_direct_call = False

    if status == "error":
        # ponytail: surface the failure to the user instead of silently dropping the message (R10)
        fallback_text = _runtime_fallback_text(facade)
        yield event.plain_result(fallback_text or "处理出错，请稍后重试")
        event.stop_event()
        return
    debug_trace(event, "ingress.after_attention", status=status, direct_call=is_direct_call)

    try:
        facade.cancel_group_wait_if_interrupted(event, group_wait_result, status)
    except Exception:
        logger.exception("[AstrMai] cancel_group_wait_if_interrupted failed")

    try:
        ghost_message = facade.suppress_default_llm_if_engaged(event, status, is_direct_call)
    except Exception:
        logger.exception("[AstrMai] suppress_default_llm_if_engaged failed")
        ghost_message = None
    if ghost_message is not None:
        yield event.plain_result(ghost_message)
