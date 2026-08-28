from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


ACTION_TYPE_ALIASES: dict[str, str] = {
    "message_emoji_like": "message_emoji_reaction",
    "message_emoji_like_action": "message_emoji_reaction",
    "message_reaction": "message_emoji_reaction",
    "message_reaction_action": "message_emoji_reaction",
}


def canonical_action_type(value: str) -> str:
    action = str(value or "").strip()
    return ACTION_TYPE_ALIASES.get(action, action)


@dataclass(slots=True)
class PendingQQAction:
    action_type: str
    action_instance_id: str = field(default_factory=lambda: f"qqai_{uuid.uuid4().hex}")
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
        action_type = canonical_action_type(value.get("action_type") or value.get("action") or "")
        if not action_type:
            return None
        payload = value.get("payload")
        try:
            requested_at = float(value.get("requested_at") or 0.0)
        except (TypeError, ValueError):
            requested_at = 0.0
        return cls(
            action_type=action_type,
            action_instance_id=str(value.get("action_instance_id") or value.get("instance_id") or "").strip()
            or f"qqai_{uuid.uuid4().hex}",
            target_id=str(value.get("target_id") or "").strip(),
            target_name=str(value.get("target_name") or "").strip(),
            group_id=str(value.get("group_id") or "").strip(),
            message_id=str(value.get("message_id") or "").strip(),
            payload=dict(payload) if isinstance(payload, Mapping) else {},
            requested_at=requested_at,
        )

    def idempotency_key(self, turn_key: str) -> str:
        # Idempotency is scoped to one action instance.  Action content and
        # turn identity are intentionally not part of the key so two
        # deliberate identical actions in one turn remain independently
        # dispatchable while transport retries reuse this instance key.
        instance_id = str(self.action_instance_id or "").strip()
        if instance_id:
            return instance_id
        canonical = json.dumps(
            {"turn": str(turn_key or ""), "action": canonical_action_type(self.action_type)},
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["ACTION_TYPE_ALIASES", "PendingQQAction", "canonical_action_type"]
