from __future__ import annotations

from ..contracts.prompt_envelope import PromptEnvelope
from ..contracts.focus_context import FreshnessState


def build_prompt_envelope(
    planner,
    focus_context,
    focus_message_text: str,
    direct_context_text: str,
    related_context_text: str,
    background_window_text: str,
    recent_transcript: str,
    last_assistant_reply: str,
    near_context_priority: bool,
):
    return PromptEnvelope(
        raw_user_text=focus_message_text,
        recent_transcript=recent_transcript,
        last_assistant_reply=last_assistant_reply,
        focus_message_text=focus_message_text,
        direct_context_text=direct_context_text,
        related_context_text=related_context_text,
        ambient_background_text=background_window_text,
        focus_reason=focus_context.focus_reason,
        focus_thread_reason=focus_context.root_reason or focus_context.focus_reason,
        near_context_priority=near_context_priority,
        reply_mode=focus_context.reply_mode,
        social_state=focus_context.social_state,
        freshness_state=focus_context.freshness_budget.state or FreshnessState.FRESH,
        thread_signature=focus_context.thread_signature,
        guidance_lines=planner._build_guidance_lines(focus_context.reply_mode),
    )


__all__ = ["build_prompt_envelope"]
