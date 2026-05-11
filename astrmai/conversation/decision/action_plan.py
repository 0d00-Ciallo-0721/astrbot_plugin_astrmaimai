from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class BrainActionPlan:
    action: str = "IGNORE"
    thought: str = "..."
    relevance: int = 0
    necessity: float = 0.0
    confidence: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    def should_act(self) -> bool:
        return self.action in {"REPLY", "WAIT", "SUMMARIZE_REPLY", "TOOL_CALL"}


__all__ = ["BrainActionPlan"]
