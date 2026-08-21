from __future__ import annotations

from typing import Any

from ...conversation.contracts.dialog_history_policy import DialogHistoryPolicy
from .lane_manager import LaneKey


def is_group_chat_event(event: Any, chat_id: str) -> bool:
    try:
        if str(event.get_group_id() or "").strip():
            return True
    except Exception:
        pass
    return "GroupMessage" in str(chat_id or "")


def resolve_dialog_lane_identity(event: Any, chat_id: str) -> tuple[LaneKey, str]:
    normalized_chat_id = str(chat_id or "")
    if not is_group_chat_event(event, normalized_chat_id):
        return (
            LaneKey(subsystem="sys2", task_family="dialog", scope_id=normalized_chat_id),
            normalized_chat_id,
        )

    history_policy = DialogHistoryPolicy.from_event(event)
    topic_epoch = max(1, int(history_policy.topic_epoch or 1))
    return (
        LaneKey(
            subsystem="sys2",
            task_family="dialog",
            scope_id=f"{normalized_chat_id}#topic:{topic_epoch}",
        ),
        f"{normalized_chat_id}@@topic:{topic_epoch}",
    )


__all__ = ["is_group_chat_event", "resolve_dialog_lane_identity"]
