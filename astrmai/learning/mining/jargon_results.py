from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class JargonEnrichmentResult:
    status: str
    items: list[dict[str, Any]] = field(default_factory=list)
    input_count: int = 0
    returned_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    missing_indexes: list[int] = field(default_factory=list)
    invalid_indexes: list[Any] = field(default_factory=list)
    retryable: bool = False
    reason: str = ""
    error_type: str = ""
    attempts: int = 1

    @property
    def terminal(self) -> bool:
        # A partial provider response is not complete coverage.  Persisting
        # the returned subset must not authorize a source-cursor advance.
        return self.status in {"completed", "all_rejected"}

    def to_report(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "terminal": self.terminal,
            "retryable": self.retryable,
            "reason": self.reason,
            "error_type": self.error_type,
            "input_count": self.input_count,
            "returned_count": self.returned_count,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "missing_indexes": list(self.missing_indexes),
            "invalid_indexes": list(self.invalid_indexes),
            "attempts": self.attempts,
        }


__all__ = ["JargonEnrichmentResult"]
