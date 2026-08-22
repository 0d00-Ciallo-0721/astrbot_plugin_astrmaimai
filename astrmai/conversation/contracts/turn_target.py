from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping


class TargetKind(str, Enum):
    ACTOR = "actor"
    BOT = "bot"
    MESSAGE = "message"
    TOPIC = "topic"
    NONE = "none"


def _ordered_unique(values: Iterable[Any]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class TurnTarget:
    target_kind: TargetKind = TargetKind.NONE
    target_actor_id: str = ""
    target_actor_name: str = ""
    target_event_id: str = ""
    topic_epoch: int = 0
    attention_topic_key: str = ""
    source_event_ids: tuple[str, ...] = field(default_factory=tuple)
    evidence: str = ""
    confidence: float = 0.0
    resolved_by: str = "focus_resolver_v1"
    created_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_kind": self.target_kind.value,
            "target_actor_id": self.target_actor_id,
            "target_actor_name": self.target_actor_name,
            "target_event_id": self.target_event_id,
            "topic_epoch": int(self.topic_epoch or 0),
            "attention_topic_key": self.attention_topic_key,
            "source_event_ids": list(self.source_event_ids),
            "evidence": self.evidence,
            "confidence": float(self.confidence or 0.0),
            "resolved_by": self.resolved_by,
            "created_at": float(self.created_at or 0.0),
        }

    @classmethod
    def from_value(cls, value: Any) -> "TurnTarget":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls()
        try:
            kind = TargetKind(str(value.get("target_kind", TargetKind.NONE.value) or TargetKind.NONE.value))
        except ValueError:
            kind = TargetKind.NONE
        return cls(
            target_kind=kind,
            target_actor_id=str(value.get("target_actor_id", "") or ""),
            target_actor_name=str(value.get("target_actor_name", "") or ""),
            target_event_id=str(value.get("target_event_id", "") or ""),
            topic_epoch=max(0, int(value.get("topic_epoch", 0) or 0)),
            attention_topic_key=str(value.get("attention_topic_key", "") or "").strip(),
            source_event_ids=_ordered_unique(value.get("source_event_ids", ()) or ()),
            evidence=str(value.get("evidence", "") or ""),
            confidence=max(0.0, min(1.0, float(value.get("confidence", 0.0) or 0.0))),
            resolved_by=str(value.get("resolved_by", "focus_resolver_v1") or "focus_resolver_v1"),
            created_at=float(value.get("created_at", 0.0) or 0.0),
        )


@dataclass(frozen=True, slots=True)
class ActorSet:
    current_actor_id: str = ""
    explicit_target_actor_ids: tuple[str, ...] = field(default_factory=tuple)
    at_actor_ids: tuple[str, ...] = field(default_factory=tuple)
    quoted_actor_ids: tuple[str, ...] = field(default_factory=tuple)
    recent_topic_actor_ids: tuple[str, ...] = field(default_factory=tuple)
    bot_id: str = ""

    @property
    def allowed_actor_ids(self) -> tuple[str, ...]:
        return _ordered_unique(
            (
                self.current_actor_id,
                *self.explicit_target_actor_ids,
                *self.at_actor_ids,
                *self.quoted_actor_ids,
                *self.recent_topic_actor_ids,
                self.bot_id,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "current_actor_id": self.current_actor_id,
            "explicit_target_actor_ids": list(self.explicit_target_actor_ids),
            "at_actor_ids": list(self.at_actor_ids),
            "quoted_actor_ids": list(self.quoted_actor_ids),
            "recent_topic_actor_ids": list(self.recent_topic_actor_ids),
            "bot_id": self.bot_id,
            "allowed_actor_ids": list(self.allowed_actor_ids),
        }

    @classmethod
    def from_value(cls, value: Any) -> "ActorSet":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls()
        return cls(
            current_actor_id=str(value.get("current_actor_id", "") or ""),
            explicit_target_actor_ids=_ordered_unique(value.get("explicit_target_actor_ids", ()) or ()),
            at_actor_ids=_ordered_unique(value.get("at_actor_ids", ()) or ()),
            quoted_actor_ids=_ordered_unique(value.get("quoted_actor_ids", ()) or ()),
            recent_topic_actor_ids=_ordered_unique(value.get("recent_topic_actor_ids", ()) or ()),
            bot_id=str(value.get("bot_id", "") or ""),
        )


__all__ = ["ActorSet", "TargetKind", "TurnTarget"]
