from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    mode: Literal["group", "private"]
    chat_id: str
    thread_id: str
    generation: int
    sender_id: str = ""
    input_message_ids: tuple[str, ...] = ()
    created_at: float = 0.0


def build_p0_thread_id(mode: str, chat_id: str) -> str:
    normalized_mode = str(mode or "").strip().lower()
    normalized_chat_id = str(chat_id or "").strip()
    if normalized_mode == "private":
        return f"private:{normalized_chat_id}"
    return normalized_chat_id


def build_turn_send_key(turn: TurnIdentity, response_kind: str = "final") -> str:
    kind = str(response_kind or "final").strip() or "final"
    return f"{turn.mode}:{turn.chat_id}:{turn.thread_id}:{int(turn.generation)}:{kind}"


__all__ = ["TurnIdentity", "build_p0_thread_id", "build_turn_send_key"]
