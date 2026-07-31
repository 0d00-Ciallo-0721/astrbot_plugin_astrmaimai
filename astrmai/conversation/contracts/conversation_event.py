from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable


CONVERSATION_EVENT_SCHEMA_VERSION = 1
_ALLOWED_PROVENANCE = {
    "original",
    "bot_echo",
    "external_plugin",
    "proactive",
    "replay",
    "synthetic",
}


def _ordered_unique(values: Iterable[Any], *, limit: int = 32) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result[-max(1, int(limit or 1)) :])


def _component_type(component: Any) -> str:
    return str(getattr(component, "type", component.__class__.__name__) or "").lstrip("_").lower()


def _event_components(event: Any) -> list[Any]:
    return list(getattr(getattr(event, "message_obj", None), "message", None) or [])


def _platform_message_id(event: Any) -> str:
    message_obj = getattr(event, "message_obj", None)
    return str(
        getattr(message_obj, "message_id", "")
        or getattr(event, "message_id", "")
        or ""
    ).strip()


def _chat_kind(chat_id: str, group_id: str) -> str:
    if group_id or ":GroupMessage:" in chat_id:
        return "group"
    if ":FriendMessage:" in chat_id:
        return "private"
    return "unknown"


def _stable_fallback_event_id(
    *,
    chat_id: str,
    actor_id: str,
    timestamp: float,
    message_kind: str,
    visible_text: str,
    reply_target_actor_id: str,
    interaction_kind: str,
    image_refs: tuple[str, ...],
) -> str:
    payload = {
        "chat_id": chat_id,
        "actor_id": actor_id,
        "timestamp_ms": int(float(timestamp or 0.0) * 1000),
        "message_kind": message_kind,
        "visible_text": visible_text,
        "reply_target_actor_id": reply_target_actor_id,
        "interaction_kind": interaction_kind,
        "image_refs": list(image_refs),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "fallback_" + hashlib.sha256(encoded).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    event_id: str
    chat_id: str
    chat_kind: str
    timestamp: float
    actor_id: str
    actor_name: str
    visible_text: str
    rich_text: str
    message_kind: str
    role: str
    schema_version: int = CONVERSATION_EVENT_SCHEMA_VERSION
    event_id_source: str = "fallback"
    platform_message_id: str = ""
    group_id: str = ""
    actor_role: str = "member"
    is_bot: bool = False
    reply_target_event_id: str = ""
    reply_target_actor_id: str = ""
    reply_target_actor_name: str = ""
    quote_event_id: str = ""
    at_actor_ids: tuple[str, ...] = field(default_factory=tuple)
    topic_epoch: int = 0
    causal_parent_event_id: str = ""
    source_event_ids: tuple[str, ...] = field(default_factory=tuple)
    provenance: str = "original"
    image_refs: tuple[str, ...] = field(default_factory=tuple)
    attachment_refs: tuple[str, ...] = field(default_factory=tuple)
    interaction_kind: str = ""
    is_at_bot: bool = False
    is_reply_to_bot: bool = False
    is_direct_wakeup: bool = False
    has_direct_vision: bool = False
    is_image_only: bool = False
    recalled: bool = False
    outcome: str = ""

    @classmethod
    def from_astr_event(
        cls,
        event: Any,
        *,
        self_id: str = "",
        rich_text: str = "",
        image_refs: Iterable[Any] = (),
        direct_image_refs: Iterable[Any] = (),
        reply_target_actor_id: str = "",
        reply_target_actor_name: str = "",
        is_at_bot: bool = False,
        is_reply_to_bot: bool = False,
        is_direct_wakeup: bool = False,
        topic_epoch: int = 0,
        provenance: str = "original",
    ) -> "ConversationEvent":
        chat_id = str(getattr(event, "unified_msg_origin", "") or "").strip()
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
        actor_id = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
        actor_name = str(getattr(event, "get_sender_name", lambda: "")() or "").strip()
        visible_text = str(getattr(event, "message_str", "") or "").strip()
        resolved_rich_text = str(rich_text or visible_text or "").strip()
        timestamp = float(
            getattr(event, "get_extra", lambda _key, default=None: default)(
                "astrmai_timestamp",
                getattr(event, "timestamp", 0.0),
            )
            or 0.0
        )
        components = _event_components(event)
        at_actor_ids = _ordered_unique(
            getattr(component, "qq", "") or getattr(component, "target", "")
            for component in components
            if _component_type(component) == "at"
        )
        reply_component = next(
            (component for component in components if _component_type(component) == "reply"),
            None,
        )
        reply_event_id = str(
            getattr(reply_component, "id", "")
            or getattr(reply_component, "message_id", "")
            or ""
        ).strip()
        if reply_component is not None:
            reply_target_actor_id = str(
                reply_target_actor_id
                or getattr(reply_component, "sender_id", "")
                or ""
            ).strip()
            reply_target_actor_name = str(
                reply_target_actor_name
                or getattr(reply_component, "sender_nickname", "")
                or getattr(reply_component, "sender_name", "")
                or ""
            ).strip()

        all_image_refs = _ordered_unique([*image_refs, *direct_image_refs])
        direct_refs = _ordered_unique(direct_image_refs)
        interaction_kind = str(
            getattr(event, "get_extra", lambda _key, default=None: default)(
                "astrmai_interaction_kind",
                "",
            )
            or ""
        ).strip()
        has_media = bool(all_image_refs)
        if interaction_kind:
            message_kind = "interaction"
        elif has_media and visible_text:
            message_kind = "mixed"
        elif has_media:
            message_kind = "image"
        else:
            message_kind = "text"
        is_bot = bool(actor_id and actor_id == str(self_id or ""))
        platform_message_id = _platform_message_id(event)
        event_id_source = "platform_message_id" if platform_message_id else "fallback_hash"
        event_id = platform_message_id or _stable_fallback_event_id(
            chat_id=chat_id,
            actor_id=actor_id,
            timestamp=timestamp,
            message_kind=message_kind,
            visible_text=visible_text,
            reply_target_actor_id=reply_target_actor_id,
            interaction_kind=interaction_kind,
            image_refs=all_image_refs,
        )
        normalized_provenance = str(provenance or "original").strip().lower()
        if normalized_provenance not in _ALLOWED_PROVENANCE:
            normalized_provenance = "original"
        source_event_ids = _ordered_unique(
            getattr(event, "get_extra", lambda _key, default=None: default)(
                "astrmai_source_event_ids",
                (),
            )
            or (event_id,)
        )
        causal_parent_event_id = str(
            getattr(event, "get_extra", lambda _key, default=None: default)(
                "astrmai_causal_parent_event_id",
                reply_event_id,
            )
            or reply_event_id
            or ""
        ).strip()
        return cls(
            event_id=event_id,
            event_id_source=event_id_source,
            platform_message_id=platform_message_id,
            chat_id=chat_id,
            chat_kind=_chat_kind(chat_id, group_id),
            group_id=group_id,
            timestamp=timestamp,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role="bot" if is_bot else "member",
            visible_text=visible_text,
            rich_text=resolved_rich_text,
            message_kind=message_kind,
            role="assistant" if is_bot else ("interaction" if interaction_kind else "user"),
            is_bot=is_bot,
            reply_target_event_id=reply_event_id,
            reply_target_actor_id=reply_target_actor_id,
            reply_target_actor_name=reply_target_actor_name,
            quote_event_id=reply_event_id,
            at_actor_ids=at_actor_ids,
            topic_epoch=max(0, int(topic_epoch or 0)),
            causal_parent_event_id=causal_parent_event_id,
            source_event_ids=source_event_ids,
            provenance=normalized_provenance,
            image_refs=all_image_refs,
            interaction_kind=interaction_kind,
            is_at_bot=bool(is_at_bot),
            is_reply_to_bot=bool(is_reply_to_bot),
            is_direct_wakeup=bool(is_direct_wakeup),
            has_direct_vision=bool(direct_refs),
            is_image_only=bool(has_media and not visible_text),
            recalled=bool(
                getattr(event, "get_extra", lambda _key, default=None: default)(
                    "astrmai_recall_tombstoned",
                    False,
                )
            ),
            outcome=str(
                getattr(event, "get_extra", lambda _key, default=None: default)(
                    "astrmai_event_outcome",
                    "",
                )
                or ""
            ).strip(),
        )

    def to_dialogue_segment_kwargs(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "speaker_id": self.actor_id,
            "speaker_name": self.actor_name,
            "content": self.rich_text or self.visible_text,
            "role": self.role,
            "message_kind": self.message_kind,
            "is_bot": self.is_bot,
            "reply_target_sender_id": self.reply_target_actor_id,
            "reply_target_sender_name": self.reply_target_actor_name,
            "is_at_bot": self.is_at_bot,
            "is_reply_to_bot": self.is_reply_to_bot,
            "has_direct_vision": self.has_direct_vision,
            "is_image_only": self.is_image_only,
            "is_recalled": self.recalled,
            "timestamp": self.timestamp,
            "topic_epoch": self.topic_epoch,
            "causal_parent_event_id": self.causal_parent_event_id,
            "provenance": self.provenance,
            "outcome": self.outcome,
            "source_event_ids": list(self.source_event_ids),
        }


__all__ = [
    "CONVERSATION_EVENT_SCHEMA_VERSION",
    "ConversationEvent",
]
