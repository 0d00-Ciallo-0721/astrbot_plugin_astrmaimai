from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from .focus_context import FreshnessState, ReplyMode
from .context_package import ContextPackage, escape_untrusted_text


@dataclass
class PromptEnvelope:
    # ── Prompt injection defense helpers ────────────────────────────
    _ESCAPE_TAGS: ClassVar[dict[str, str]] = {
        "<user_input>": "[escaped:user_input]",
        "</user_input>": "[escaped:/user_input]",
        "<retrieved_memory>": "[escaped:retrieved_memory]",
        "</retrieved_memory>": "[escaped:/retrieved_memory]",
    }

    @classmethod
    def _escape_injection_tags(cls, text: str) -> str:
        """Replace literal closing/opening tags that could break the wrapper."""
        for tag, escaped in cls._ESCAPE_TAGS.items():
            text = text.replace(tag, escaped)
        return text

    @classmethod
    def sanitize_inline_text(cls, text: str) -> str:
        """Escape prompt boundary tags without adding a block wrapper."""
        return escape_untrusted_text(cls._escape_injection_tags(str(text or "")))

    @staticmethod
    def sanitize_derived_context(text: str, *, source: str = "derived") -> str:
        if not text or not str(text).strip():
            return str(text or "")
        safe_source = str(source or "derived").replace('"', "").replace("\n", " ").strip()
        safe = PromptEnvelope.sanitize_inline_text(str(text))
        return (
            f'<untrusted_context type="derived" source="{safe_source}" trusted="false">\n'
            f"{safe}\n"
            "</untrusted_context>"
        )

    @staticmethod
    def sanitize_user_input(text: str) -> str:
        """Wrap user-supplied text in boundary tags to prevent prompt injection.
        Tags inside the user text are escaped to prevent early closure."""
        if not text or not str(text).strip():
            return str(text or "")
        safe = PromptEnvelope.sanitize_inline_text(str(text))
        return f"<user_input>\n{safe}\n</user_input>"

    @staticmethod
    def sanitize_memory_content(text: str) -> str:
        """Wrap retrieved-memory content to prevent persistent prompt injection.
        Tags inside the memory content are escaped to prevent early closure."""
        if not text or not str(text).strip():
            return str(text or "")
        safe = PromptEnvelope.sanitize_inline_text(str(text))
        return f"<retrieved_memory>\n{safe}\n</retrieved_memory>"

    raw_user_text: str = ""
    recent_transcript: str = ""
    recent_transcript_source: str = ""
    recent_transcript_reason: str = ""
    warm_zone_transcript: str = ""
    warm_zone_transcript_source: str = ""
    warm_zone_summary: str = ""
    warm_zone_quotes: str = ""
    warm_topics_preview: str = ""
    warm_zone_has_latest_assistant: bool = False
    warm_zone_quote_event_ids: list[str] = field(default_factory=list)
    last_assistant_reply: str = ""
    current_speaker_block: str = ""
    referenced_entity_block: str = ""
    focus_message_text: str = ""
    focus_message_identity: str = ""
    direct_context_text: str = ""
    related_context_text: str = ""
    ambient_background_text: str = ""
    focus_reason: str = ""
    focus_thread_reason: str = ""
    near_context_priority: bool = False
    reply_mode: ReplyMode = ReplyMode.CASUAL_FOLLOWUP
    social_state: str = ""
    freshness_state: FreshnessState = FreshnessState.FRESH
    thread_signature: str = ""
    state_block: str = ""
    memory_block: str = ""
    background_memory_block: str = ""
    background_memory_sections: dict[str, str] = field(default_factory=dict)
    background_memory_budget_chars: int = 0
    background_memory_trimmed_sections: list[str] = field(default_factory=list)
    background_memory_rendered_chars: int = 0
    background_memory_skipped_reason: str = ""
    cognitive_drive_block: str = ""
    soft_background_block: str = ""
    soft_background_sections: dict[str, str] = field(default_factory=dict)
    soft_background_budget_chars: int = 0
    soft_background_trimmed_sections: list[str] = field(default_factory=list)
    soft_background_rendered_chars: int = 0
    soft_background_skipped_reason: str = ""
    learning_context_block: str = ""
    learning_context_sections: dict[str, str] = field(default_factory=dict)
    learning_context_budget_chars: int = 0
    learning_context_trimmed_sections: list[str] = field(default_factory=list)
    learning_context_rendered_chars: int = 0
    learning_context_skipped_reason: str = ""
    flex_context_budget_chars: int = 0
    flex_context_trimmed_sections: list[str] = field(default_factory=list)
    flex_context_protected_sections: list[str] = field(default_factory=list)
    warm_context_rendered_chars: int = 0
    recent_context_rendered_chars: int = 0
    memory_context_rendered_chars: int = 0
    situational_context_block: str = ""
    planner_runtime_instruction_block: str = ""
    guidance_lines: list[str] = field(default_factory=list)
    context_package: ContextPackage | None = None
    persona_schema_version: int = 0
    persona_core_fields_present: list[str] = field(default_factory=list)
    persona_core_fields_missing: list[str] = field(default_factory=list)
    persona_shards_selected: list[str] = field(default_factory=list)


__all__ = [
    "FreshnessState",
    "PromptEnvelope",
    "ReplyMode",
]
