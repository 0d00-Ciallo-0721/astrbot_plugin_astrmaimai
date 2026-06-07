from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class WorkCommandRequest:
    raw_message: str
    task_query: str

    @classmethod
    def from_message(cls, message: str) -> "WorkCommandRequest":
        raw = str(message or "")
        if raw == "/work" or raw.startswith("/work "):
            task_query = raw.replace("/work", "", 1).strip()
        else:
            task_query = raw.strip()
        return cls(raw_message=raw, task_query=task_query)

    @property
    def has_query(self) -> bool:
        return bool(self.task_query)


@dataclass(slots=True)
class ReviewDecisionRequest:
    pattern_id: str
    decision: str
    reviewer_id: str
    replacement_expression: str = ""
    style: str = ""
    reason: str = ""
    weight_delta: float = 0.0


@dataclass(slots=True)
class AdminCommandRequest:
    action: str
    payload: dict[str, object]


@dataclass(slots=True)
class HelpCommandView:
    title: str
    body: str


__all__ = [
    "AdminCommandRequest",
    "HelpCommandView",
    "ReviewDecisionRequest",
    "WorkCommandRequest",
]
