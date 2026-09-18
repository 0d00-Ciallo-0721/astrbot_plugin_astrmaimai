from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PersistenceFailure:
    candidate_id: str
    content_hash: str
    failure_stage: str
    failure_kind: str
    retryable: bool
    detail: str = ""

    def to_report(self) -> dict[str, Any]:
        return {
            "candidate_id": str(self.candidate_id or ""),
            "content_hash": str(self.content_hash or ""),
            "failure_stage": str(self.failure_stage or "persist"),
            "failure_kind": str(self.failure_kind or "persist_error"),
            "retryable": bool(self.retryable),
            "detail": str(self.detail or "")[:300],
        }


@dataclass(frozen=True, slots=True)
class JargonSaveReport:
    attempted: int
    saved: int
    deduplicated: int
    failed: int
    memory_ids: tuple[str, ...] = ()
    failures: tuple[PersistenceFailure, ...] = ()
    failure_stage: str | None = None
    failure_kind: str | None = None

    def __post_init__(self) -> None:
        for name in ("attempted", "saved", "deduplicated", "failed"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        ids = tuple(str(item or "").strip() for item in (self.memory_ids or ()))
        if any(not item for item in ids):
            raise ValueError("successful memory_ids must be non-empty")
        failures = tuple(self.failures or ())
        if any(not isinstance(item, PersistenceFailure) for item in failures):
            raise TypeError("failures must contain PersistenceFailure values")
        object.__setattr__(self, "memory_ids", ids)
        object.__setattr__(self, "failures", failures)

    @property
    def conservation_valid(self) -> bool:
        return (
            self.attempted == self.saved + self.deduplicated + self.failed
            and len(self.memory_ids) == self.saved + self.deduplicated
            and len(self.failures) == self.failed
        )

    @property
    def complete(self) -> bool:
        return self.conservation_valid and self.failed == 0 and not self.failure_kind

    @property
    def retryable(self) -> bool:
        return bool(self.failures) and all(item.retryable for item in self.failures)

    def to_report(self) -> dict[str, Any]:
        report = {
            "attempted": self.attempted,
            "saved": self.saved,
            "deduplicated": self.deduplicated,
            "failed": self.failed,
            "memory_ids": list(self.memory_ids),
            "failures": [item.to_report() for item in self.failures],
            "complete": self.complete,
            "conservation_valid": self.conservation_valid,
            "retryable": self.retryable,
        }
        if not self.conservation_valid:
            report.update({
                "failure_stage": self.failure_stage or "persist",
                "failure_kind": self.failure_kind or "persist_error",
            })
        elif self.failure_stage:
            report["failure_stage"] = self.failure_stage
        if self.failure_kind:
            report["failure_kind"] = self.failure_kind
        return report


__all__ = ["PersistenceFailure", "JargonSaveReport"]
