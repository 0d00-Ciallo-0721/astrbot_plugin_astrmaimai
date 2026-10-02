from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar


ANCHOR_MAX_PARTICIPANTS = 8
ANCHOR_MAX_SUBJECT_CHARS = 180
ANCHOR_MAX_OPEN_LOOP_CHARS = 220
ANCHOR_MAX_EVENT_IDS = 12
ANCHOR_MAX_PROMPT_CHARS = 900
ANCHOR_MAX_ID_CHARS = 96
ANCHOR_MAX_SOURCE_CHARS = 48
ANCHOR_MAX_SERIALIZED_CHARS = 4096
ANCHOR_SOURCE_VALUES = frozenset(
    {
        "user_message",
        "assistant_reply",
        "quote",
        "explicit_question",
        "reply_target",
        "topic_transition",
    }
)


@dataclass(frozen=True, slots=True)
class TopicAttentionAnchor:
    """Immutable, bounded view of the current topic focus."""

    topic_epoch: int = 0
    participants: tuple[str, ...] = ()
    subject_preview: str = ""
    open_loop: str = ""
    recent_event_ids: tuple[str, ...] = ()
    updated_at: float = 0.0
    confidence: float = 0.0
    source: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "topic_epoch": int(self.topic_epoch or 0),
            "participants": [str(item or "").strip()[:ANCHOR_MAX_ID_CHARS] for item in self.participants[:ANCHOR_MAX_PARTICIPANTS]],
            "subject_preview": str(self.subject_preview or "").strip()[:ANCHOR_MAX_SUBJECT_CHARS],
            "open_loop": str(self.open_loop or "").strip()[:ANCHOR_MAX_OPEN_LOOP_CHARS],
            "recent_event_ids": [str(item or "").strip()[:ANCHOR_MAX_ID_CHARS] for item in self.recent_event_ids[:ANCHOR_MAX_EVENT_IDS]],
            "updated_at": float(self.updated_at or 0.0),
            "confidence": float(self.confidence or 0.0),
            "source": [str(item or "").strip()[:ANCHOR_MAX_SOURCE_CHARS] for item in self.source[:8]],
        }
        if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) > ANCHOR_MAX_SERIALIZED_CHARS:
            payload["subject_preview"] = payload["subject_preview"][:90]
            payload["open_loop"] = payload["open_loop"][:110]
        return payload

    @classmethod
    def from_value(cls, value: Any) -> "TopicAttentionAnchor":
        if isinstance(value, cls):
            value = value.as_dict()
        if not isinstance(value, Mapping):
            return cls()

        def _values(name: str, limit: int, max_chars: int) -> tuple[str, ...]:
            raw = value.get(name, ())
            if not isinstance(raw, (list, tuple)):
                return ()
            result: list[str] = []
            for item in raw:
                if len(result) >= limit:
                    break
                if not isinstance(item, str):
                    continue
                normalized = item.strip()[:max_chars]
                if normalized and normalized not in result:
                    result.append(normalized)
            return tuple(result)

        try:
            confidence = max(0.0, min(1.0, float(value.get("confidence", 0.0) or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        try:
            updated_at = max(0.0, float(value.get("updated_at", 0.0) or 0.0))
        except (TypeError, ValueError):
            updated_at = 0.0
        try:
            topic_epoch = max(0, int(value.get("topic_epoch", 0) or 0))
        except (TypeError, ValueError):
            topic_epoch = 0
        subject_preview = value.get("subject_preview", "")
        open_loop = value.get("open_loop", "")
        return cls(
            topic_epoch=topic_epoch,
            participants=_values("participants", ANCHOR_MAX_PARTICIPANTS, ANCHOR_MAX_ID_CHARS),
            subject_preview=subject_preview.strip()[:ANCHOR_MAX_SUBJECT_CHARS] if isinstance(subject_preview, str) else "",
            open_loop=open_loop.strip()[:ANCHOR_MAX_OPEN_LOOP_CHARS] if isinstance(open_loop, str) else "",
            recent_event_ids=_values("recent_event_ids", ANCHOR_MAX_EVENT_IDS, ANCHOR_MAX_ID_CHARS),
            updated_at=updated_at,
            confidence=confidence,
            source=tuple(
                item
                for item in _values("source", 8, ANCHOR_MAX_SOURCE_CHARS)
                if item in ANCHOR_SOURCE_VALUES
            ),
        )
