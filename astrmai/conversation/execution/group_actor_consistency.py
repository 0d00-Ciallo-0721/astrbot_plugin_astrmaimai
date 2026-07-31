from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


_ADDRESS_SUFFIXES = (
    "欧尼酱",
    "哥哥",
    "姐姐",
    "欧尼",
    "同学",
    "老师",
    "妈妈",
    "爸爸",
    "宝贝",
    "宝宝",
    "主人",
)


@dataclass(slots=True)
class ActorConsistencyResult:
    text: str
    action: str = "not_applicable"
    current_actor_id: str = ""
    current_actor_name: str = ""
    foreign_actor_names: list[str] = field(default_factory=list)
    reason: str = ""


class GroupActorConsistencyGuard:
    """Prevent a previous group participant from becoming the current addressee."""

    @staticmethod
    def _safe_call(obj: Any, name: str) -> str:
        func = getattr(obj, name, None)
        if not callable(func):
            return ""
        try:
            return str(func() or "").strip()
        except Exception:
            return ""

    @classmethod
    def _is_group_event(cls, event: Any) -> bool:
        if cls._safe_call(event, "get_group_id"):
            return True
        return "GroupMessage" in str(getattr(event, "unified_msg_origin", "") or "")

    @classmethod
    def _event_sender_name(cls, event: Any) -> str:
        return cls._safe_call(event, "get_sender_name")

    @classmethod
    def _current_actor(cls, event: Any) -> tuple[str, str]:
        actor_id = cls._safe_call(event, "get_sender_id")
        actor_name = cls._safe_call(event, "get_sender_name")
        if hasattr(event, "get_extra"):
            turn_context = event.get_extra("astrmai_turn_context", None)
            perception = getattr(turn_context, "perception", None)
            actor_id = str(getattr(perception, "sender_id", "") or "").strip() or actor_id
            actor_name = str(getattr(perception, "sender_name", "") or "").strip() or actor_name
            focus_context = event.get_extra("astrmai_focus_thread_context", None)
            turn_target = getattr(focus_context, "turn_target", None)
            actor_id = str(getattr(turn_target, "target_actor_id", "") or "").strip() or actor_id
            actor_name = str(getattr(turn_target, "target_actor_name", "") or "").strip() or actor_name
            actor_id = str(getattr(focus_context, "focus_sender_id", "") or "").strip() or actor_id
            actor_name = str(getattr(focus_context, "focus_sender_name", "") or "").strip() or actor_name
        return actor_id, actor_name

    @classmethod
    def _current_input_text(cls, event: Any) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        if not hasattr(event, "get_extra"):
            return text
        rich_text = str(event.get_extra("astrmai_rich_text", "") or "").strip()
        prompt_envelope = event.get_extra("astrmai_prompt_envelope", None)
        envelope_text = str(
            getattr(prompt_envelope, "raw_user_text", "")
            or getattr(prompt_envelope, "focus_message_text", "")
            or ""
        ).strip()
        return "\n".join(part for part in (text, rich_text, envelope_text) if part)

    @classmethod
    def _participant_events(cls, event: Any) -> list[Any]:
        events: list[Any] = []
        if not hasattr(event, "get_extra"):
            return events
        focus_context = event.get_extra("astrmai_focus_thread_context", None)
        if focus_context is not None:
            for attr in (
                "root_event",
                "focus_event",
                "core_events",
                "related_events",
                "ambient_events",
            ):
                value = getattr(focus_context, attr, None)
                candidates = value if isinstance(value, list) else [value]
                for candidate in candidates:
                    if candidate is not None and candidate not in events:
                        events.append(candidate)
        turn_context = event.get_extra("astrmai_turn_context", None)
        attention = getattr(turn_context, "attention", None)
        for candidate in list(getattr(attention, "window_events", []) or []):
            if candidate is not None and candidate not in events:
                events.append(candidate)
        return events

    @classmethod
    def _participant_names(cls, event: Any, current_name: str) -> list[str]:
        names: list[str] = []
        for candidate in cls._participant_events(event):
            name = cls._event_sender_name(candidate)
            if (
                not name
                or name == current_name
                or name.isdigit()
                or len(name) > 32
                or name in names
            ):
                continue
            names.append(name)

        if hasattr(event, "get_extra"):
            prompt_envelope = event.get_extra("astrmai_prompt_envelope", None)
            for attr in (
                "recent_transcript",
                "warm_zone_transcript",
                "direct_context_text",
                "related_context_text",
                "ambient_background_text",
            ):
                text = str(getattr(prompt_envelope, attr, "") or "")
                for match in re.finditer(r"(?m)^\s*([^:\n]{1,32})\s*:", text):
                    name = match.group(1).strip()
                    if (
                        not name
                        or name == current_name
                        or name.isdigit()
                        or name in names
                    ):
                        continue
                    names.append(name)
        return names

    @staticmethod
    def _address_tokens(name: str) -> list[str]:
        tokens: list[str] = []
        for suffix in _ADDRESS_SUFFIXES:
            tokens.append(f"{name}{suffix}")
            if len(name) == 1:
                tokens.append(f"{name}{name}{suffix}")
        return sorted(set(tokens), key=len, reverse=True)

    @classmethod
    def inspect_and_repair(cls, event: Any, reply_text: str) -> ActorConsistencyResult:
        text = str(reply_text or "")
        actor_id, actor_name = cls._current_actor(event)
        if not text or not cls._is_group_event(event):
            return ActorConsistencyResult(
                text=text,
                current_actor_id=actor_id,
                current_actor_name=actor_name,
            )

        current_input = cls._current_input_text(event)
        foreign_names = cls._participant_names(event, actor_name)
        repaired = text
        detected: list[str] = []
        explicit_reference = False
        for name in foreign_names:
            tokens = [token for token in cls._address_tokens(name) if token in repaired]
            if not tokens:
                continue
            if name in current_input:
                explicit_reference = True
                continue
            for token in tokens:
                repaired = repaired.replace(token, "你")
            detected.append(name)

        if detected:
            return ActorConsistencyResult(
                text=repaired,
                action="repaired",
                current_actor_id=actor_id,
                current_actor_name=actor_name,
                foreign_actor_names=detected,
                reason="foreign_participant_used_as_direct_addressee",
            )
        if explicit_reference:
            return ActorConsistencyResult(
                text=text,
                action="allowed_explicit_reference",
                current_actor_id=actor_id,
                current_actor_name=actor_name,
                reason="foreign_participant_explicitly_named_by_current_user",
            )
        return ActorConsistencyResult(
            text=text,
            action="passed",
            current_actor_id=actor_id,
            current_actor_name=actor_name,
        )


__all__ = ["ActorConsistencyResult", "GroupActorConsistencyGuard"]
