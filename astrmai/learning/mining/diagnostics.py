from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any


_STAGES = {
    "discover", "normalize", "route", "extract", "dedup", "enrichment_admission",
    "provider", "parse", "persist", "cursor_commit", "review", "admission",
    "index", "retrieval", "prompt_visibility",
}
_STATUSES = {
    "started", "completed", "partial", "retry_wait", "failed", "cancelled",
    "skipped", "quarantined", "blocked",
}
_FAILURE_KINDS = {
    "", "insufficient_context", "provider_timeout", "provider_error",
    "invalid_schema", "persist_empty_id", "persist_locked", "persist_error",
    "validation_error", "dependency_unavailable", "cursor_commit_conflict",
    "cursor_commit_error", "cancelled", "shutdown", "unknown_error",
    "unknown_status", "cursor_regression", "cursor_out_of_scope",
}
_SECRET = re.compile(r"(?i)(?:bearer\s+|token|secret|api[_-]?key|password|cookie)[^\s,;]*")


def _safe_detail(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key)[:80]: _safe_detail(item) for key, item in list(value.items())[:30]}
    if isinstance(value, (list, tuple)):
        return [_safe_detail(item) for item in list(value)[:20]]
    text = str(value or "")
    text = _SECRET.sub("[REDACTED]", text)
    text = re.sub(r"https?://[^\s?]+(?:\?[^\s]*)?", "[URL]", text)
    return text[:300]


@dataclass(frozen=True, slots=True)
class LearningStageDiagnostic:
    stage: str
    status: str
    attempt: int = 1
    started_at: float = 0.0
    finished_at: float = 0.0
    elapsed_ms: float = 0.0
    input_count: int = 0
    output_count: int = 0
    candidate_count: int = 0
    persisted_count: int = 0
    failure_stage: str = ""
    failure_kind: str = ""
    retryable: bool = False
    cursor_before: int = 0
    cursor_after: int = 0
    batch_id_hash: str = ""
    provider_id: str = ""
    model_id: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.stage not in _STAGES:
            raise ValueError(f"unsupported learning stage: {self.stage!r}")
        if self.status not in _STATUSES:
            raise ValueError(f"unsupported learning stage status: {self.status!r}")
        if self.attempt < 0 or any(int(getattr(self, key)) < 0 for key in ("input_count", "output_count", "candidate_count", "persisted_count")):
            raise ValueError("diagnostic counts must be non-negative")

    def to_report(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "attempt": int(self.attempt),
            "started_at": float(self.started_at),
            "finished_at": float(self.finished_at),
            "elapsed_ms": float(self.elapsed_ms),
            "input_count": int(self.input_count),
            "output_count": int(self.output_count),
            "candidate_count": int(self.candidate_count),
            "persisted_count": int(self.persisted_count),
            "failure_stage": str(self.failure_stage or ""),
            "failure_kind": str(self.failure_kind or "") if self.failure_kind in _FAILURE_KINDS else "unknown_error",
            "retryable": bool(self.retryable),
            "cursor_before": int(self.cursor_before),
            "cursor_after": int(self.cursor_after),
            "batch_id_hash": self.batch_id_hash or "",
            "provider_id": str(self.provider_id or ""),
            "model_id": str(self.model_id or ""),
            "diagnostics": _safe_detail(self.diagnostics),
        }

    @staticmethod
    def hash_batch(batch_id: str) -> str:
        return hashlib.sha256(str(batch_id or "").encode("utf-8")).hexdigest()[:24]


def build_learning_diagnostics(
    stages: list[LearningStageDiagnostic] | tuple[LearningStageDiagnostic, ...] = (),
    *,
    input_scan_complete: bool = False,
    enrichment_complete: bool = False,
    persistence_complete: bool = False,
    cursor_commit_complete: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": "learning-stage-diagnostics-v1",
        "stages": [stage.to_report() for stage in stages],
        "terminal_proof": {
            "input_scan_complete": bool(input_scan_complete),
            "enrichment_complete": bool(enrichment_complete),
            "persistence_complete": bool(persistence_complete),
            "cursor_commit_complete": bool(cursor_commit_complete),
        },
    }


__all__ = ["LearningStageDiagnostic", "build_learning_diagnostics"]
