from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


ATTENTION_TOPIC_EVENT_KEY = "astrmai_attention_topic_identity"


@dataclass(frozen=True, slots=True)
class AttentionTopicIdentity:
    history_topic_epoch: int = 0
    attention_topic_key: str = ""
    source: str = "unknown"
    confidence: float = 0.0
    evidence: tuple[str, ...] = field(default_factory=tuple)
    anchor_text: str = ""
    root_event_id: str = ""

    @property
    def is_known(self) -> bool:
        return bool(self.history_topic_epoch > 0 and self.attention_topic_key)

    @property
    def exact_cacheable(self) -> bool:
        return bool(
            self.is_known
            and self.confidence >= 0.7
            and self.source not in {"text_fingerprint", "ambiguous_short_text"}
        )

    @property
    def ambient_cacheable(self) -> bool:
        return bool(self.is_known and self.confidence >= 0.5)

    def as_dict(self, *, include_anchor: bool = False) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence"] = list(self.evidence)
        payload["is_known"] = self.is_known
        payload["exact_cacheable"] = self.exact_cacheable
        payload["ambient_cacheable"] = self.ambient_cacheable
        if not include_anchor:
            payload.pop("anchor_text", None)
        return payload

    def bind(self, event: Any) -> None:
        if event is not None and hasattr(event, "set_extra"):
            event.set_extra(ATTENTION_TOPIC_EVENT_KEY, self.as_dict())

    @classmethod
    def from_value(cls, value: Any) -> "AttentionTopicIdentity":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls()
        try:
            epoch = max(0, int(value.get("history_topic_epoch", 0) or 0))
        except (TypeError, ValueError):
            epoch = 0
        try:
            confidence = max(0.0, min(1.0, float(value.get("confidence", 0.0) or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        return cls(
            history_topic_epoch=epoch,
            attention_topic_key=str(value.get("attention_topic_key", "") or "").strip(),
            source=str(value.get("source", "unknown") or "unknown").strip(),
            confidence=confidence,
            evidence=tuple(
                str(item).strip()
                for item in list(value.get("evidence", ()) or ())
                if str(item).strip()
            ),
            anchor_text=str(value.get("anchor_text", "") or "").strip(),
            root_event_id=str(value.get("root_event_id", "") or "").strip(),
        )

    @classmethod
    def from_event(cls, event: Any) -> "AttentionTopicIdentity":
        if event is None or not hasattr(event, "get_extra"):
            return cls()
        return cls.from_value(event.get_extra(ATTENTION_TOPIC_EVENT_KEY, None))


__all__ = ["ATTENTION_TOPIC_EVENT_KEY", "AttentionTopicIdentity"]
