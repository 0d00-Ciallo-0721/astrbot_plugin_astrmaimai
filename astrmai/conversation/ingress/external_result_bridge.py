from __future__ import annotations

import hashlib
import logging
import time
from importlib import import_module
from typing import Any

try:
    from astrbot.api import logger
except Exception:
    logger = logging.getLogger(__name__)

from ...infrastructure.runtime.trace_runtime import (
    debug_trace,
    ensure_external_result_id,
    ensure_trace_id,
    preview_text,
)
from .external_result_dispatcher import ExternalResultEnvelope, freeze_event_data

try:
    from ...shared.helpers.plugin_helpers import build_external_reply_event, get_event_self_id
except ImportError:
    # Keep the ingress module importable in reduced host/test environments.
    # AstrBot's full helper module is used whenever its optional platform
    # dependencies are available.
    def build_external_reply_event(reply_text: str) -> dict[str, Any]:
        return {"is_external_bot_reply": True, "content": reply_text, "timestamp": time.time()}

    def get_event_self_id(event) -> str:
        getter = getattr(event, "get_self_id", None)
        if callable(getter):
            try:
                value = getter()
            except Exception:
                value = ""
            if value is not None and str(value).strip():
                return str(value)
        return "unknown"

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


def _runtime_generation(runtime) -> int:
    return int(
        getattr(runtime, "runtime_generation", 0)
        or getattr(getattr(runtime, "status", None), "runtime_generation", 0)
        or 0
    )


def _event_message_id(event) -> str:
    for getter in ("get_message_id", "get_event_id"):
        method = getattr(event, getter, None)
        if callable(method):
            try:
                value = method()
            except Exception:
                value = ""
            if value is not None and str(value).strip():
                return str(value).strip()
    for owner in (event, getattr(event, "message_obj", None)):
        value = getattr(owner, "message_id", None)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _digest(*parts: Any) -> str:
    payload = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:16]


def snapshot_external_plugin_result(runtime, event) -> ExternalResultEnvelope | None:
    """Validate and copy a host result without retaining the mutable event."""
    external_result_id = ensure_external_result_id(event)
    bridge_started = time.monotonic()
    debug_trace(event, "external_result.bridge_enter", external_result_id=external_result_id)
    if event.get_extra("astrmai_loop_source", "") == "external_result_bridge":
        debug_trace(
            event,
            "external_result.bridge_skipped",
            external_result_id=external_result_id,
            reason="recursive_bridge_event",
        )
        return None

    # ── 外部结果白名单检查 ──
    explicit_source = str(event.get_extra("astrmai_loop_source", "") or "").strip()
    loop_source = explicit_source or "astrbot_builtin"
    runtime_config = getattr(runtime, "config", None)
    allowed = getattr(getattr(runtime_config, "global_settings", None), "external_result_sources", ["astrbot_builtin"]) or ["astrbot_builtin"]
    if "*" not in allowed and loop_source not in allowed:
        debug_trace(
            event,
            "external_result.bridge_skipped",
            external_result_id=external_result_id,
            reason="source_not_whitelisted",
            source=loop_source,
        )
        logger.debug(f"[ExtBridge] skipped non-whitelisted source: {loop_source}")
        return None
    if event.get_extra("astrmai_is_self_reply", False) and not explicit_source:
        debug_trace(
            event,
            "external_result.bridge_skipped",
            external_result_id=external_result_id,
            reason="self_reply",
        )
        return None

    reply_text = extract_external_reply_text(event.get_result())
    if not reply_text:
        debug_trace(
            event,
            "external_result.bridge_skipped",
            external_result_id=external_result_id,
            reason="empty_result",
        )
        return None

    host_bridge = getattr(runtime, "host_bridge", None)
    if host_bridge is not None:
        if host_bridge.is_ghost_sentinel(reply_text):
            debug_trace(
                event,
                "external_result.bridge_skipped",
                external_result_id=external_result_id,
                reason="ghost_sentinel",
            )
            return None
        global_settings = getattr(runtime_config, "global_settings", None)
        interception_enabled = bool(getattr(global_settings, "enable_error_interception", True))
        if host_bridge.should_intercept_error(reply_text, enabled=interception_enabled):
            debug_trace(
                event,
                "external_result.bridge_skipped",
                external_result_id=external_result_id,
                reason="intercepted_error",
            )
            return None

    chat_id = event.unified_msg_origin
    bot_id = get_event_self_id(event)
    trace_id = ensure_trace_id(event)
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
        "astrmai_event_provenance": "external_plugin",
        "astrmai_external_context_trusted": False,
        "astrmai_is_committed_astrmai_reply": False,
        "astrmai_origin_sender_id": str(event.get_sender_id() or "") if hasattr(event, "get_sender_id") else "",
        "astrmai_external_result_id": external_result_id,
        "astrmai_trace_id": trace_id,
    }
    chain = getattr(event.get_result(), "chain", None) or []
    has_image = any(isinstance(component, Comp.Image) for component in chain)
    result_chain_hash = _digest(
        loop_source,
        chat_id,
        bot_id,
        len(chain),
        *(component.__class__.__name__ for component in chain),
        reply_text,
    )
    envelope = ExternalResultEnvelope(
        external_result_id=external_result_id,
        trace_id=trace_id,
        source=loop_source,
        chat_id=str(chat_id or ""),
        group_id=str(bot_reply_event.get("group_id", "") or ""),
        sender_id=bot_id,
        self_id=bot_id,
        event_id=_event_message_id(event),
        result_chain_hash=result_chain_hash,
        text_preview_hash=_digest(reply_text),
        text_preview=preview_text(reply_text, 100),
        has_image=has_image,
        created_at=time.time(),
        runtime_generation=_runtime_generation(runtime),
        event_data=freeze_event_data(bot_reply_event),
    )
    debug_trace(
        event,
        "external_result.result_snapshot_ready",
        external_result_id=external_result_id,
        source=loop_source,
        event_id=_event_message_id(event),
        runtime_generation=_runtime_generation(runtime),
        has_image=has_image,
        result_chain_hash=result_chain_hash,
        text_preview_hash=_digest(reply_text),
        elapsed_ms=round((time.monotonic() - bridge_started) * 1000.0, 1),
    )
    debug_trace(
        event,
        "external_result.bridge_ready",
        external_result_id=external_result_id,
        source=loop_source,
        elapsed_ms=round((time.monotonic() - bridge_started) * 1000.0, 1),
        preview=preview_text(reply_text, 100),
    )
    # Preserve the original stage name for existing log consumers while adding
    # the external-result correlation id used by the probe.
    debug_trace(
        event,
        "ingress.external_result",
        external_result_id=external_result_id,
        source=loop_source,
        preview=preview_text(reply_text, 100),
    )

    return envelope


async def bridge_external_plugin_result(runtime, event_or_envelope) -> str:
    """Inject either a validated envelope or a legacy direct event."""
    is_envelope = isinstance(event_or_envelope, ExternalResultEnvelope)
    if is_envelope:
        envelope = event_or_envelope
        event = envelope.as_event_data()
        external_result_id = envelope.external_result_id
        bridge_started = time.monotonic()
        debug_trace(event, "external_result.bridge_start", external_result_id=external_result_id)
        bot_reply_event = envelope.as_event_data()
        chat_id = envelope.chat_id
    else:
        envelope = snapshot_external_plugin_result(runtime, event_or_envelope)
        if envelope is None:
            return "skipped"
        event = event_or_envelope
        external_result_id = envelope.external_result_id
        bridge_started = time.monotonic()
        bot_reply_event = envelope.as_event_data()
        chat_id = envelope.chat_id

    if runtime.attention_gate and hasattr(runtime.attention_gate, "inject_external_event"):
        debug_trace(
            event,
            "external_result.inject_start",
            external_result_id=external_result_id,
            chat_id=chat_id,
        )
        inject_started = time.monotonic()
        try:
            await runtime.attention_gate.inject_external_event(chat_id, bot_reply_event)
        except Exception as exc:
            debug_trace(
                event,
                "external_result.inject_failed",
                external_result_id=external_result_id,
                elapsed_ms=round((time.monotonic() - inject_started) * 1000.0, 1),
                error_type=type(exc).__name__,
            )
            debug_trace(
                event,
                "external_result.bridge_end",
                external_result_id=external_result_id,
                status="failed",
                elapsed_ms=round((time.monotonic() - bridge_started) * 1000.0, 1),
            )
            raise
        debug_trace(
            event,
            "external_result.inject_done",
            external_result_id=external_result_id,
            elapsed_ms=round((time.monotonic() - inject_started) * 1000.0, 1),
            bridge_elapsed_ms=round((time.monotonic() - bridge_started) * 1000.0, 1),
        )
        logger.debug(f"[{chat_id}] external plugin result injected into attention window.")
        debug_trace(
            event,
            "external_result.bridge_end",
            external_result_id=external_result_id,
            status="injected",
            elapsed_ms=round((time.monotonic() - bridge_started) * 1000.0, 1),
        )
        return "injected"
    else:
        debug_trace(
            event,
            "external_result.inject_skipped",
            external_result_id=external_result_id,
            reason="attention_gate_unavailable",
        )
        debug_trace(
            event,
            "external_result.bridge_end",
            external_result_id=external_result_id,
            status="failed",
            elapsed_ms=round((time.monotonic() - bridge_started) * 1000.0, 1),
        )
        return "failed"


__all__ = [
    "ExternalResultEnvelope",
    "bridge_external_plugin_result",
    "extract_external_reply_text",
    "snapshot_external_plugin_result",
]
