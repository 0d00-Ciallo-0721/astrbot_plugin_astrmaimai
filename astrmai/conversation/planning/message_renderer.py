from __future__ import annotations

import re
from typing import Iterable

from astrbot.api.event import AstrMessageEvent

from ..contracts.context_package import ContextBlock, ContextPackage, escape_untrusted_text
from ..contracts.conversation_event import ConversationEvent


class MessageRenderer:
    """Single entry point for converting conversation messages into prompt text."""

    LEGACY_MESSAGE_RE = re.compile(r'^<message\s+speaker="([^"]+)">(.*)</message>$', re.IGNORECASE | re.DOTALL)

    @staticmethod
    def _normalize_text(text: str) -> str:
        return str(text or "").strip()

    @staticmethod
    def _safe_label(value: str, fallback: str = "") -> str:
        normalized = str(value or fallback or "").replace("\r", " ").replace("\n", " ").strip()
        return normalized.replace("|", "¦").replace("[", "［").replace("]", "］")

    @classmethod
    def render_conversation_event(cls, event: ConversationEvent) -> str:
        actor_name = cls._safe_label(event.actor_name, "群友")
        actor_id = cls._safe_label(event.actor_id, "unknown")
        fields = [
            f"事件={cls._safe_label(event.event_id)}",
            f"发言人={actor_name}（ID:{actor_id}）",
            f"角色={'机器人' if event.is_bot else '成员'}",
            f"类型={cls._safe_label(event.message_kind, 'text')}",
            f"来源={cls._safe_label(event.provenance, 'original')}",
        ]
        if event.reply_target_event_id:
            fields.append(f"回复事件={cls._safe_label(event.reply_target_event_id)}")
        if event.reply_target_actor_id:
            fields.append(f"回复对象={cls._safe_label(event.reply_target_actor_id)}")
        if event.at_actor_ids:
            fields.append(f"@={','.join(cls._safe_label(item) for item in event.at_actor_ids)}")
        if event.topic_epoch:
            fields.append(f"话题={event.topic_epoch}")
        if event.image_refs:
            fields.append(f"媒体=图片:{len(event.image_refs)}")
        if event.attachment_refs:
            fields.append(f"附件={len(event.attachment_refs)}")
        if event.interaction_kind:
            fields.append(f"互动={cls._safe_label(event.interaction_kind)}")
        content = cls._normalize_text(event.rich_text or event.visible_text)
        if not content and event.image_refs:
            content = "[图片]"
        return f"[{' | '.join(fields)}]\n内容：{escape_untrusted_text(content)}".strip()

    @classmethod
    def render_event(cls, event: AstrMessageEvent, *, include_identity: bool = False) -> str:
        canonical = event.get_extra("astrmai_conversation_event", None)
        if isinstance(canonical, ConversationEvent):
            return cls.render_conversation_event(canonical)
        speaker = cls._normalize_text(event.get_sender_name()) or "群友"
        if include_identity:
            try:
                sender_id = cls._normalize_text(event.get_sender_id())
            except Exception:
                sender_id = ""
            if sender_id:
                speaker = f"{speaker}（QQ: {sender_id}）"
        text = cls._normalize_text(event.get_extra("astrmai_rich_text", event.message_str))
        interaction = cls._normalize_text(event.get_extra("astrmai_interaction_kind", ""))
        if interaction:
            interaction_text = text or interaction
            return f"[互动：{speaker} {interaction_text}]".strip()
        if not text:
            return ""
        return f"{speaker}: {text}"

    @staticmethod
    def _canonical_event(candidate) -> ConversationEvent | None:
        if isinstance(candidate, ConversationEvent):
            return candidate
        getter = getattr(candidate, "get_extra", None)
        if callable(getter):
            value = getter("astrmai_conversation_event", None)
            if isinstance(value, ConversationEvent):
                return value
        return None

    @classmethod
    def build_context_package(
        cls,
        *,
        shared_events: Iterable[ConversationEvent | AstrMessageEvent] = (),
        owned_events: Iterable[ConversationEvent | AstrMessageEvent] = (),
        derived_blocks: Iterable[ContextBlock] = (),
        turn_instruction: str = "",
        shared_char_budget: int = 8000,
    ) -> ContextPackage:
        shared_candidates = tuple(shared_events)
        owned_candidates = tuple(owned_events)
        unique_events: dict[str, ConversationEvent] = {}
        duplicate_ids: list[str] = []
        for candidate in (*shared_candidates, *owned_candidates):
            canonical = cls._canonical_event(candidate)
            if canonical is None:
                continue
            if canonical.event_id in unique_events:
                duplicate_ids.append(canonical.event_id)
                continue
            unique_events[canonical.event_id] = canonical

        owned_ids = tuple(
            dict.fromkeys(
                canonical.event_id
                for canonical in (cls._canonical_event(item) for item in owned_candidates)
                if canonical is not None
            )
        )
        blocks: list[ContextBlock] = []
        if unique_events:
            timeline = "\n".join(
                cls.render_conversation_event(item)
                for item in unique_events.values()
            )
            provenance = (
                "external_plugin"
                if any(item.provenance == "external_plugin" for item in unique_events.values())
                else "conversation"
            )
            blocks.append(
                ContextBlock.create(
                    block_type="shared_visible_timeline",
                    source="canonical_conversation_events",
                    provenance=provenance,
                    trusted=False,
                    source_event_ids=unique_events,
                    content=timeline,
                    char_budget=shared_char_budget,
                )
            )
        if owned_ids:
            blocks.append(
                ContextBlock.create(
                    block_type="owned_turn_batch",
                    source="turn_target_resolver",
                    provenance="runtime",
                    trusted=True,
                    source_event_ids=owned_ids,
                    content=f"Owned event references: {', '.join(owned_ids)}",
                )
            )
        blocks.extend(block for block in derived_blocks if isinstance(block, ContextBlock))
        if turn_instruction:
            blocks.append(
                ContextBlock.create(
                    block_type="turn_instruction",
                    source="turn_target_resolver",
                    provenance="runtime",
                    trusted=True,
                    source_event_ids=owned_ids,
                    content=turn_instruction,
                    char_budget=1200,
                )
            )
        return ContextPackage(
            blocks=tuple(blocks),
            shared_event_ids=tuple(unique_events),
            owned_event_ids=owned_ids,
            deduplicated_event_ids=tuple(dict.fromkeys(duplicate_ids)),
        )

    @classmethod
    def render_bot_turn(cls, content: str, bot_name: str = "Bot") -> str:
        normalized = cls._normalize_text(content)
        if not normalized:
            return ""
        speaker = cls._normalize_text(bot_name) or "Bot"
        return f"{speaker}: {normalized}"

    @classmethod
    def render_user_turn(cls, content: str, sender_name: str = "") -> str:
        normalized = cls._normalize_text(content)
        if not normalized:
            return ""
        speaker = cls._normalize_text(sender_name) or "用户"
        return f"{speaker}: {normalized}"

    @classmethod
    def render_social_event(cls, content: str) -> str:
        normalized = cls._normalize_text(content)
        if not normalized:
            return ""
        legacy_match = cls.LEGACY_MESSAGE_RE.match(normalized)
        if legacy_match:
            speaker = cls._normalize_text(legacy_match.group(1)) or "用户"
            legacy_content = cls._normalize_text(legacy_match.group(2))
            return cls.render_user_turn(legacy_content, speaker)
        if normalized.startswith("["):
            return normalized[:180]
        return f"[{normalized[:180]}]"


__all__ = ["MessageRenderer"]
