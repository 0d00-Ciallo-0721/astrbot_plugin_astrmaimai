from __future__ import annotations

from typing import Iterable


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
