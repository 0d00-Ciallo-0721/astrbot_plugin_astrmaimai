from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(slots=True)
class UserMessageRecordedEvent:
    chat_id: str
    sender_id: str
    sender_name: str
    content: str

    def to_payload(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class BotReplyRecordedEvent:
    chat_id: str
    bot_id: str
    content: str

    def to_payload(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class MiningCompletedEvent:
    group_id: str
    pattern_count: int
    jargon_count: int

    def to_payload(self) -> dict:
        return asdict(self)
