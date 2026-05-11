from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class WaitStateSnapshot:
    chat_id: str
    target_user_id: str = ""
    target_name: str = ""
    reason: str = ""
    thread_signature: str = ""
    reply_mode: str = ""
    remaining_messages: int = 0
    remaining_seconds: float = 0.0

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "WaitStateSnapshot | None":
        if not data:
            return None
        return cls(
            chat_id=str(data.get("chat_id", "") or ""),
            target_user_id=str(data.get("target_user_id", "") or ""),
            target_name=str(data.get("target_name", "") or ""),
            reason=str(data.get("reason", "") or ""),
            thread_signature=str(data.get("thread_signature", "") or ""),
            reply_mode=str(data.get("reply_mode", "") or ""),
            remaining_messages=int(data.get("remaining_messages", 0) or 0),
            remaining_seconds=float(data.get("remaining_seconds", 0.0) or 0.0),
        )
