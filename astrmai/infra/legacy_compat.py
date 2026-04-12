from __future__ import annotations

from typing import Any, Iterable, Optional

from .runtime_contracts import (
    FocusThreadContext,
    FreshnessState,
    PromptEnvelope,
    ReplyFreshnessBudget,
    ReplyMode,
    VisibleReplyArtifact,
)


def emit_legacy_focus_thread_extras(
    event: Any,
    focus_context: FocusThreadContext,
    *,
    window_events: Optional[Iterable[Any]] = None,
) -> None:
    if not event or not focus_context:
        return
    event.set_extra("astrmai_focus_event", focus_context.focus_event)
    event.set_extra("astrmai_focus_reason", focus_context.focus_reason)
    event.set_extra("astrmai_focus_message_text", focus_context.focus_message_text)
    event.set_extra("astrmai_focus_sender_id", focus_context.focus_sender_id)
    event.set_extra("astrmai_focus_sender_name", focus_context.focus_sender_name)
    event.set_extra("astrmai_reply_mode", focus_context.reply_mode.value)
    event.set_extra("astrmai_social_state", focus_context.social_state)
    event.set_extra("astrmai_thread_signature", focus_context.thread_signature)
    event.set_extra("astrmai_background_events", list(focus_context.ambient_events or []))
    event.set_extra("astrmai_focus_thread_root_event", focus_context.root_event)
    event.set_extra("astrmai_focus_thread_root_reason", focus_context.root_reason)
    event.set_extra("astrmai_focus_thread_core_events", list(focus_context.core_events or []))
    event.set_extra("astrmai_focus_thread_related_events", list(focus_context.related_events or []))
    event.set_extra("astrmai_focus_thread_ambient_events", list(focus_context.ambient_events or []))
    event.set_extra("astrmai_focus_thread_reason", focus_context.focus_reason)
    event.set_extra("astrmai_focus_thread_context", focus_context)
    event.set_extra("astrmai_anchor_event", focus_context.focus_event)
    if window_events is not None:
        event.set_extra("astrmai_window_events", list(window_events))


def read_legacy_focus_thread_context(event: Any, *, default_event: Any = None) -> FocusThreadContext:
    focus_event = event.get_extra("astrmai_focus_event", default_event or event)
    return FocusThreadContext(
        focus_event=focus_event,
        root_event=event.get_extra("astrmai_focus_thread_root_event", None),
        core_events=list(event.get_extra("astrmai_focus_thread_core_events", []) or []),
        related_events=list(event.get_extra("astrmai_focus_thread_related_events", []) or []),
        ambient_events=list(
            event.get_extra("astrmai_focus_thread_ambient_events", [])
            or event.get_extra("astrmai_background_events", [])
            or []
        ),
        focus_reason=str(event.get_extra("astrmai_focus_reason", "") or ""),
        root_reason=str(event.get_extra("astrmai_focus_thread_root_reason", "") or ""),
        focus_message_text=str(event.get_extra("astrmai_focus_message_text", "") or ""),
        focus_sender_id=str(event.get_extra("astrmai_focus_sender_id", "") or ""),
        focus_sender_name=str(event.get_extra("astrmai_focus_sender_name", "") or ""),
        reply_mode=ReplyMode(str(event.get_extra("astrmai_reply_mode", ReplyMode.CASUAL_FOLLOWUP.value) or ReplyMode.CASUAL_FOLLOWUP.value)),
        social_state=str(event.get_extra("astrmai_social_state", "") or ""),
        thread_signature=str(event.get_extra("astrmai_thread_signature", "") or ""),
        freshness_budget=ReplyFreshnessBudget(),
    )


def emit_legacy_prompt_envelope_extras(
    event: Any,
    prompt_envelope: PromptEnvelope,
    *,
    use_lane_history: bool = True,
) -> None:
    if not event or not prompt_envelope:
        return
    event.set_extra("astrmai_prompt_envelope", prompt_envelope)
    event.set_extra("astrmai_raw_user_text", prompt_envelope.raw_user_text)
    event.set_extra("astrmai_background_window_text", prompt_envelope.ambient_background_text)
    event.set_extra("astrmai_focus_thread_text", prompt_envelope.focus_thread_text)
    event.set_extra("astrmai_ambient_background_text", prompt_envelope.ambient_background_text)
    event.set_extra("astrmai_recent_transcript", prompt_envelope.recent_transcript)
    event.set_extra("astrmai_near_context_priority", bool(prompt_envelope.near_context_priority))
    event.set_extra("astrmai_focus_thread_reason", prompt_envelope.focus_thread_reason)
    event.set_extra("astrmai_use_lane_history", bool(use_lane_history))
    event.set_extra("astrmai_reply_mode", prompt_envelope.reply_mode.value)
    event.set_extra("astrmai_social_state", prompt_envelope.social_state)
    event.set_extra("astrmai_freshness_state", prompt_envelope.freshness_state.value)
    event.set_extra("astrmai_thread_signature", prompt_envelope.thread_signature)


def read_legacy_prompt_envelope(event: Any, *, prompt: str = "") -> PromptEnvelope:
    return PromptEnvelope(
        raw_user_text=str(event.get_extra("astrmai_raw_user_text", prompt) or prompt).strip(),
        recent_transcript=str(event.get_extra("astrmai_recent_transcript", "") or "").strip(),
        last_assistant_reply="",
        focus_thread_text=str(event.get_extra("astrmai_focus_thread_text", "") or "").strip(),
        ambient_background_text=str(
            event.get_extra("astrmai_ambient_background_text", "")
            or event.get_extra("astrmai_background_window_text", "")
            or ""
        ).strip(),
        focus_reason=str(event.get_extra("astrmai_focus_reason", "") or "").strip(),
        focus_thread_reason=str(
            event.get_extra("astrmai_focus_thread_reason", "")
            or event.get_extra("astrmai_focus_thread_root_reason", "")
            or event.get_extra("astrmai_focus_reason", "")
            or ""
        ).strip(),
        near_context_priority=bool(event.get_extra("astrmai_near_context_priority", False)),
        reply_mode=ReplyMode(str(event.get_extra("astrmai_reply_mode", ReplyMode.CASUAL_FOLLOWUP.value) or ReplyMode.CASUAL_FOLLOWUP.value)),
        social_state=str(event.get_extra("astrmai_social_state", "") or "").strip(),
        freshness_state=FreshnessState(
            str(event.get_extra("astrmai_freshness_state", FreshnessState.FRESH.value) or FreshnessState.FRESH.value)
        ),
        thread_signature=str(event.get_extra("astrmai_thread_signature", "") or "").strip(),
    )


def emit_legacy_reply_runtime_extras(
    event: Any,
    artifact: Optional[VisibleReplyArtifact] = None,
    *,
    reply_sent: Optional[bool] = None,
    wait_targets: Optional[Iterable[str]] = None,
    wait_target_name: Optional[str] = None,
    is_self_reply: Optional[bool] = None,
) -> None:
    if not event:
        return
    if is_self_reply is not None:
        event.set_extra("astrmai_is_self_reply", bool(is_self_reply))
    if reply_sent is not None:
        event.set_extra("astrmai_reply_sent", bool(reply_sent))
    if artifact and artifact.persistable_text:
        event.set_extra("astrmai_last_reply_text", artifact.persistable_text)
    if wait_targets is not None:
        normalized = [str(target) for target in wait_targets if str(target)]
        event.set_extra("astrmai_wait_targets", normalized)
    if wait_target_name is not None:
        event.set_extra("astrmai_wait_target_name", str(wait_target_name or ""))
