from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ExpressionEnrichmentResult:
    status: str
    items: list[dict[str, Any]] = field(default_factory=list)
    input_count: int = 0
    returned_count: int = 0
    rejected_count: int = 0
    missing_candidate_ids: list[str] = field(default_factory=list)
    retryable: bool = False
    reason: str = ""
    attempts: int = 0
    fallback_count: int = 0

    @property
    def terminal(self) -> bool:
        return self.status in {"completed", "all_rejected", "completed_fallback"}

    def to_report(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "terminal": self.terminal,
            "retryable": self.retryable,
            "reason": self.reason,
            "input_count": self.input_count,
            "returned_count": self.returned_count,
            "rejected_count": self.rejected_count,
            "missing_candidate_ids": list(self.missing_candidate_ids),
            "attempts": self.attempts,
            "fallback_count": self.fallback_count,
        }


@dataclass(slots=True)
class PatternSaveReport:
    attempted: int = 0
    saved: int = 0
    deduplicated: int = 0
    failed: int = 0
    memory_ids: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.failed == 0 and self.saved + self.deduplicated == self.attempted

    def to_report(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "saved": self.saved,
            "deduplicated": self.deduplicated,
            "failed": self.failed,
            "memory_ids": list(self.memory_ids),
            "failures": list(self.failures),
            "complete": self.complete,
        }


__all__ = ["ExpressionEnrichmentResult", "PatternSaveReport"]
