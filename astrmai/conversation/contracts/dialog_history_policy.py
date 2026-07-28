from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


HistoryMode = Literal["none", "current_topic", "explicit_recall"]
POLICY_EVENT_KEY = "astrmai_dialog_history_policy"


@dataclass(frozen=True, slots=True)
class DialogHistoryPolicy:
    history_mode: HistoryMode = "none"
    group_id: str = ""
    thread_key: str = ""
    topic_epoch: int = 0
    current_sender_id: str = ""
    approved_event_ids: tuple[str, ...] = field(default_factory=tuple)
    allow_provider_session: bool = False
    rotation_reason: str = ""
    topic_age_seconds: float = 0.0
    continuity_evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def uses_lane_history(self) -> bool:
        return self.history_mode == "current_topic"

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["approved_event_ids"] = list(self.approved_event_ids)
        payload["continuity_evidence"] = list(self.continuity_evidence)
        payload["uses_lane_history"] = self.uses_lane_history
        return payload

    @classmethod
    def from_value(cls, value: Any) -> "DialogHistoryPolicy":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return cls()
        mode = str(value.get("history_mode", "none") or "none").strip()
        if mode not in {"none", "current_topic", "explicit_recall"}:
            mode = "none"
        try:
            topic_epoch = max(0, int(value.get("topic_epoch", 0) or 0))
        except (TypeError, ValueError):
            topic_epoch = 0
        try:
            topic_age_seconds = max(0.0, float(value.get("topic_age_seconds", 0.0) or 0.0))
        except (TypeError, ValueError):
            topic_age_seconds = 0.0
        return cls(
            history_mode=mode,
            group_id=str(value.get("group_id", "") or "").strip(),
            thread_key=str(value.get("thread_key", "") or "").strip(),
            topic_epoch=topic_epoch,
            current_sender_id=str(value.get("current_sender_id", "") or "").strip(),
            approved_event_ids=tuple(
                str(item).strip()
                for item in list(value.get("approved_event_ids", []) or [])
                if str(item).strip()
            ),
            allow_provider_session=bool(value.get("allow_provider_session", False)),
            rotation_reason=str(value.get("rotation_reason", "") or "").strip(),
            topic_age_seconds=topic_age_seconds,
            continuity_evidence=tuple(
                str(item).strip()
                for item in list(value.get("continuity_evidence", []) or [])
                if str(item).strip()
            ),
        )

    @classmethod
    def from_event(cls, event: Any) -> "DialogHistoryPolicy":
        if event is None or not hasattr(event, "get_extra"):
            return cls()
        try:
            return cls.from_value(event.get_extra(POLICY_EVENT_KEY, None))
        except Exception:
            return cls()

    def bind(self, event: Any) -> None:
        if event is not None and hasattr(event, "set_extra"):
            event.set_extra(POLICY_EVENT_KEY, self.as_dict())


__all__ = [
    "DialogHistoryPolicy",
    "HistoryMode",
    "POLICY_EVENT_KEY",
]
