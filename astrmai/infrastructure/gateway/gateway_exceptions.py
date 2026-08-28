from __future__ import annotations

import asyncio
from typing import Iterable


class GatewayQueueTimeout(asyncio.TimeoutError):
    """A bounded wait for a Gateway concurrency slot expired."""

    def __init__(self, stage: str, timeout_sec: float) -> None:
        self.stage = str(stage or "gateway.semaphore_wait")
        self.timeout_sec = max(0.0, float(timeout_sec or 0.0))
        super().__init__(f"{self.stage} exceeded {self.timeout_sec:.3f}s")


class GatewayShutdownRejected(asyncio.CancelledError):
    """Provider work rejected because the runtime generation is no longer live."""

    def __init__(self, reason: str = "shutdown_rejected") -> None:
        self.reason = str(reason or "shutdown_rejected")
        super().__init__(self.reason)


class LLMCascadeFailureException(Exception):
    """Raised when every model candidate in the cascade fails."""

    def __init__(
        self,
        error_message: str,
        *,
        pool_name: str = "",
        last_failure_kind: str = "unknown",
        attempted_models: Iterable[str] | None = None,
        model_id: str = "",
        raw_completion: str = "",
        failure_reason: str = "",
    ) -> None:
        super().__init__(error_message)
        self.error_message = str(error_message or "")
        self.pool_name = str(pool_name or "")
        self.last_failure_kind = str(last_failure_kind or "unknown")
        self.attempted_models = list(attempted_models or [])
        self.model_id = str(model_id or "")
        self.raw_completion = str(raw_completion or "")
        self.failure_reason = str(failure_reason or "")
