from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from astrbot.api.event import AstrMessageEvent

from ..contracts.context_package import ContextBlock, ContextPackage, escape_untrusted_text
from ..contracts.conversation_event import ConversationEvent


@dataclass(frozen=True, slots=True)
class TopicPreviewProjection:
    text: str
    source: str
    message_kind: str
    safe: bool
    rejected_reason: str = ""
    vision_state: str = ""
    focus_superseded_by_text: bool = False


class MessageRenderer:
    """Single entry point for converting conversation messages into prompt text."""

    LEGACY_MESSAGE_RE = re.compile(r'^<message\s+speaker="([^"]+)">(.*)</message>$', re.IGNORECASE | re.DOTALL)
    VISION_DESCRIPTION_RE = re.compile(
        r"\[(表情包|图片)转述\s*[：:]\s*(.*?)(?:，传达情绪\s*[：:].*?)?\]",
        re.DOTALL,
    )
    INTERNAL_EVENT_RE = re.compile(
        r"\[事件=.*?\|\s*发言人=.*?\|\s*角色=.*?\|\s*类型=.*?\|\s*来源=",
        re.DOTALL,
    )
    MEDIA_PLACEHOLDERS = frozenset({"[图片]", "[表情包]", "图片", "表情包"})

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
    def _compact_topic_text(cls, text: str, max_chars: int) -> str:
        normalized = " ".join(str(text or "").strip().split())
        normalized = normalized.replace("[", "（").replace("]", "）")
        return normalized[: max(1, int(max_chars or 1))].strip()

    @classmethod
    def _meaningful_visible_text(cls, text: str) -> str:
        normalized = cls._normalize_text(text)
        if not normalized or normalized in cls.MEDIA_PLACEHOLDERS:
            return ""
        if re.fullmatch(r"obj_len_\d+", normalized, re.IGNORECASE):
            return ""
        if cls.VISION_DESCRIPTION_RE.fullmatch(normalized):
            return ""
        if cls.INTERNAL_EVENT_RE.search(normalized):
            return ""
        return normalized

    @classmethod
    def project_topic_preview(
        cls,
        candidate: ConversationEvent | AstrMessageEvent,
        *,
        max_chars: int = 120,
    ) -> TopicPreviewProjection:
        """Project an internal message into a compact, user-safe topic label."""
        canonical = cls._canonical_event(candidate)
        getter = getattr(candidate, "get_extra", None)
        visible_text = str(getattr(canonical, "visible_text", "") or "")
        rich_text = str(getattr(canonical, "rich_text", "") or "")
        message_kind = str(getattr(canonical, "message_kind", "") or "text")
        has_image = bool(
            getattr(canonical, "image_refs", ())
            or getattr(canonical, "direct_image_refs", ())
        )
        vision_state = ""
        records: list[dict] = []
        if callable(getter):
            visible_text = str(getattr(candidate, "message_str", "") or visible_text)
            rich_text = str(getter("astrmai_rich_text", rich_text or visible_text) or "")
            records = [
                dict(item)
                for item in list(getter("astrmai_vision_records", []) or [])
                if isinstance(item, dict)
            ]
            has_image = bool(
                has_image
                or getter("extracted_image_refs", getter("extracted_image_urls", []))
                or getter("direct_image_refs", getter("direct_vision_urls", []))
            )
            if bool(getter("astrmai_vision_barrier_failed", False)):
                vision_state = "failed"
            elif bool(getter("astrmai_vision_barrier_complete", False)):
                vision_state = "complete"
            elif has_image:
                vision_state = "pending_or_unknown"

        user_text = cls._meaningful_visible_text(visible_text)
        if user_text:
            return TopicPreviewProjection(
                text=cls._compact_topic_text(user_text, max_chars),
                source="user_text",
                message_kind=message_kind,
                safe=True,
                vision_state=vision_state,
                focus_superseded_by_text=has_image,
            )

        vision_type = ""
        vision_description = ""
        if records:
            first = records[0]
            vision_type = str(first.get("type", "") or "").strip().lower()
            vision_description = str(first.get("description", "") or "").strip()
        if not vision_description:
            match = cls.VISION_DESCRIPTION_RE.search(rich_text)
            if match:
                vision_type = "emoji" if match.group(1) == "表情包" else "image"
                vision_description = match.group(2).strip()
        if vision_description:
            label = "刚才那张表情包" if vision_type == "emoji" else "刚才发送的图片"
            remaining = max(1, int(max_chars or 1) - len(label) - 1)
            description = cls._compact_topic_text(vision_description, remaining)
            return TopicPreviewProjection(
                text=f"{label}：{description}" if description else label,
                source="vision_description",
                message_kind=message_kind,
                safe=True,
                vision_state=vision_state or "complete",
            )

        if has_image:
            return TopicPreviewProjection(
                text="",
                source="image_placeholder",
                message_kind=message_kind,
                safe=False,
                vision_state=vision_state or "pending_or_unknown",
                rejected_reason="unresolved_image_has_no_semantic_topic",
            )

        fallback = cls._meaningful_visible_text(rich_text)
        if fallback:
            return TopicPreviewProjection(
                text=cls._compact_topic_text(fallback, max_chars),
                source="rich_text",
                message_kind=message_kind,
                safe=True,
            )
        return TopicPreviewProjection(
            text="",
            source="none",
            message_kind=message_kind,
            safe=False,
            rejected_reason="no_safe_topic_text",
            vision_state=vision_state,
        )

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


__all__ = ["MessageRenderer", "TopicPreviewProjection"]
