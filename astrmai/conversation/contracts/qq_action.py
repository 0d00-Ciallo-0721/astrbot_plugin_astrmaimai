from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


@dataclass(slots=True)
class PendingQQAction:
    action_type: str
    target_id: str = ""
    target_name: str = ""
    group_id: str = ""
    message_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    requested_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["action"] = self.action_type
        return data

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PendingQQAction | None":
        action_type = str(value.get("action_type") or value.get("action") or "").strip()
        if not action_type:
            return None
        payload = value.get("payload")
        try:
            requested_at = float(value.get("requested_at") or 0.0)
        except (TypeError, ValueError):
            requested_at = 0.0
        return cls(
            action_type=action_type,
            target_id=str(value.get("target_id") or "").strip(),
            target_name=str(value.get("target_name") or "").strip(),
            group_id=str(value.get("group_id") or "").strip(),
            message_id=str(value.get("message_id") or "").strip(),
            payload=dict(payload) if isinstance(payload, Mapping) else {},
            requested_at=requested_at,
        )

    def idempotency_key(self, turn_key: str) -> str:
        canonical = json.dumps(
            {
                "turn": str(turn_key or ""),
                "action": self.action_type,
                "target": self.target_id,
                "group": self.group_id,
                "message": self.message_id,
                "payload": self.payload,
            },
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["PendingQQAction"]
