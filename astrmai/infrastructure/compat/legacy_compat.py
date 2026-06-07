"""Legacy compatibility bridge — **deprecated since v2.0, removal target: v3.0**.

This module serialises typed contract objects (FocusThreadContext, PromptEnvelope,
VisibleReplyArtifact) into flat ``event.set_extra("astrmai_*", …)`` dicts consumed
by the host plugin's legacy observer / debug infrastructure.

**Migration path**: each consumer of these extras should migrate to the typed
contract objects exported by ``astrmai.infrastructure.runtime.runtime_contracts``.
No new ``astrmai_*`` extra keys should be added here.

Current consumers (locked by architecture regression tests):
  - ``astrmai/conversation/attention/gate.py``
  - ``astrmai/conversation/execution/reply_artifact_builder.py``
  - ``astrmai/conversation/execution/reply_service.py``
  - ``astrmai/conversation/planning/planner_prompt_context.py``
  - ``astrmai/conversation/planning/prompt_refiner.py``
"""

from __future__ import annotations

import functools
import warnings
from typing import Any, Callable, Iterable, Optional

from ..runtime.runtime_contracts import (
    FocusThreadContext,
    FreshnessState,
    PromptEnvelope,
    ReplyFreshnessBudget,
    ReplyMode,
    VisibleReplyArtifact,
)


def deprecated(since: str = "", removal: str = "", replacement: str = ""):
    """Mark a function as deprecated with a runtime warning.

    Usage::

        @deprecated(since="v2.0", removal="v3.0", replacement="Use contracts.X directly")
        def old_func(...):
            ...
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            msg = f"{func.__qualname__} is deprecated"
            if since:
                msg += f" since {since}"
            if removal:
                msg += f" (removal target: {removal})"
            if replacement:
                msg += f" — {replacement}"
            warnings.warn(msg, DeprecationWarning, stacklevel=2)
            return func(*args, **kwargs)
        return wrapper
    return decorator


@deprecated(since="v2.0", removal="v3.0", replacement="consume typed FocusThreadContext directly")
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


def _read_freshness_budget(event: Any) -> ReplyFreshnessBudget:
    """Extract freshness_budget from stored focus context or return default."""
    stored_focus = event.get_extra("astrmai_focus_thread_context", None)
    if not stored_focus:
        return ReplyFreshnessBudget()
    # Guard: event system JSON serialization may degrade dataclass → dict
    if isinstance(stored_focus, dict):
        fb_dict = stored_focus.get("freshness_budget", {})
        if isinstance(fb_dict, dict):
            return ReplyFreshnessBudget(
                state=FreshnessState(str(fb_dict.get("state", FreshnessState.FRESH.value))),
                created_at=float(fb_dict.get("created_at", 0.0)),
                max_age_seconds=float(fb_dict.get("max_age_seconds", 0.0)),
                salvage_window_seconds=float(fb_dict.get("salvage_window_seconds", 0.0)),
                latest_activity_ts=float(fb_dict.get("latest_activity_ts", 0.0)),
                stale_reason=str(fb_dict.get("stale_reason", "")),
            )
        return ReplyFreshnessBudget()
    if hasattr(stored_focus, "freshness_budget"):
        return stored_focus.freshness_budget
    return ReplyFreshnessBudget()


@deprecated(since="v2.0", removal="v3.0", replacement="construct FocusThreadContext from contracts directly")
def read_legacy_focus_thread_context(event: Any, *, default_event: Any = None) -> FocusThreadContext:
    focus_event = event.get_extra("astrmai_focus_event", default_event or event)
    return FocusThreadContext(
        focus_event=focus_event,
        root_event=event.get_extra("astrmai_focus_thread_root_event", None),
        core_events=list(event.get_extra("astrmai_focus_thread_core_events", []) or []),
        related_events=list(event.get_extra("astrmai_focus_thread_related_events", []) or []),
        ambient_events=list(
            event.get_extra("astrmai_focus_thread_ambient_events", []) or []
        ),
        focus_reason=str(event.get_extra("astrmai_focus_reason", "") or ""),
        root_reason=str(event.get_extra("astrmai_focus_thread_root_reason", "") or ""),
        focus_message_text=str(event.get_extra("astrmai_focus_message_text", "") or ""),
        focus_sender_id=str(event.get_extra("astrmai_focus_sender_id", "") or ""),
        focus_sender_name=str(event.get_extra("astrmai_focus_sender_name", "") or ""),
        reply_mode=ReplyMode(str(event.get_extra("astrmai_reply_mode", ReplyMode.CASUAL_FOLLOWUP.value) or ReplyMode.CASUAL_FOLLOWUP.value)),
        social_state=str(event.get_extra("astrmai_social_state", "") or ""),
        thread_signature=str(event.get_extra("astrmai_thread_signature", "") or ""),
        freshness_budget=_read_freshness_budget(event),
    )


@deprecated(since="v2.0", removal="v3.0", replacement="consume typed PromptEnvelope directly")
def emit_legacy_prompt_envelope_extras(
    event: Any,
    prompt_envelope: PromptEnvelope,
    *,
    use_lane_history: bool = True,
) -> None:
    if not event or not prompt_envelope:
        return
    focus_sections = []
    if prompt_envelope.focus_message_text:
        focus_sections.append(prompt_envelope.focus_message_text)
    if prompt_envelope.direct_context_text:
        focus_sections.append(f"前因:\n{prompt_envelope.direct_context_text}")
    if prompt_envelope.related_context_text:
        focus_sections.append(f"补充:\n{prompt_envelope.related_context_text}")
    legacy_focus_thread_text = "\n\n".join(section for section in focus_sections if section).strip()
    event.set_extra("astrmai_prompt_envelope", prompt_envelope)
    event.set_extra("astrmai_raw_user_text", prompt_envelope.raw_user_text)
    event.set_extra("astrmai_focus_message_text", prompt_envelope.focus_message_text)
    event.set_extra("astrmai_direct_context_text", prompt_envelope.direct_context_text)
    event.set_extra("astrmai_related_context_text", prompt_envelope.related_context_text)
    event.set_extra("astrmai_background_window_text", prompt_envelope.ambient_background_text)
    event.set_extra("astrmai_focus_thread_text", legacy_focus_thread_text)
    event.set_extra("astrmai_ambient_background_text", prompt_envelope.ambient_background_text)
    event.set_extra("astrmai_recent_transcript", prompt_envelope.recent_transcript)
    event.set_extra("astrmai_recent_transcript_reason", getattr(prompt_envelope, "recent_transcript_reason", ""))
    event.set_extra("astrmai_warm_zone_summary", getattr(prompt_envelope, "warm_zone_summary", ""))
    event.set_extra("astrmai_warm_zone_quotes", getattr(prompt_envelope, "warm_zone_quotes", ""))
    event.set_extra("astrmai_near_context_priority", bool(prompt_envelope.near_context_priority))
    event.set_extra("astrmai_focus_thread_reason", prompt_envelope.focus_thread_reason)
    event.set_extra("astrmai_use_lane_history", bool(use_lane_history))
    event.set_extra("astrmai_reply_mode", prompt_envelope.reply_mode.value)
    event.set_extra("astrmai_social_state", prompt_envelope.social_state)
    event.set_extra("astrmai_freshness_state", prompt_envelope.freshness_state.value)
    event.set_extra("astrmai_thread_signature", prompt_envelope.thread_signature)


@deprecated(since="v2.0", removal="v3.0", replacement="construct PromptEnvelope from contracts directly")
def read_legacy_prompt_envelope(event: Any, *, prompt: str = "") -> PromptEnvelope:
    raw_user_text = str(event.get_extra("astrmai_raw_user_text", prompt) or prompt).strip()
    focus_message_text = str(
        event.get_extra("astrmai_focus_message_text", "")
        or raw_user_text
        or event.get_extra("astrmai_focus_thread_text", "")
        or prompt
    ).strip()
    direct_context_text = str(event.get_extra("astrmai_direct_context_text", "") or "").strip()
    related_context_text = str(event.get_extra("astrmai_related_context_text", "") or "").strip()
    return PromptEnvelope(
        raw_user_text=raw_user_text,
        recent_transcript=str(event.get_extra("astrmai_recent_transcript", "") or "").strip(),
        recent_transcript_reason=str(event.get_extra("astrmai_recent_transcript_reason", "") or "").strip(),
        warm_zone_summary=str(event.get_extra("astrmai_warm_zone_summary", "") or "").strip(),
        warm_zone_quotes=str(event.get_extra("astrmai_warm_zone_quotes", "") or "").strip(),
        last_assistant_reply="",
        focus_message_text=focus_message_text,
        direct_context_text=direct_context_text,
        related_context_text=related_context_text,
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


@deprecated(since="v2.0", removal="v3.0", replacement="consume typed VisibleReplyArtifact directly")
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
