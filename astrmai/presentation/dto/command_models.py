from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class WorkCommandRequest:
    raw_message: str
    task_query: str

    @classmethod
    def from_message(cls, message: str) -> "WorkCommandRequest":
        raw = str(message or "")
        task_query = raw.replace("/work", "", 1).strip() if raw.startswith("/work") else raw.strip()
        return cls(raw_message=raw, task_query=task_query)

    @property
    def is_empty(self) -> bool:
        return not self.task_query


@dataclass(slots=True)
class ReviewDecisionRequest:
    pattern_id: int
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
