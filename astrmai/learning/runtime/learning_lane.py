from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, Literal, TypeVar

from ...infrastructure.runtime.background_task_budget import (
    BackgroundTaskBudget,
    BackgroundTaskExecutionTimeout,
    BackgroundTaskQueueFull,
    BackgroundTaskQueueTimeout,
)


T = TypeVar("T")

LEARNING_TASK_NAMES = frozenset(
    {
        "learning.expression_enrichment",
        "learning.jargon_enrichment",
        "learning.retrieval_regeneration",
    }
)


@dataclass(frozen=True, slots=True)
class LearningLaneConfig:
    limit: int = 1
    max_queue: int = 8
    admission_timeout_sec: float = 10.0
    execution_timeout_sec: float = 45.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "limit", min(2, max(1, int(self.limit or 1))))
        object.__setattr__(self, "max_queue", max(0, int(self.max_queue or 0)))
        object.__setattr__(
            self,
            "admission_timeout_sec",
            max(0.1, float(self.admission_timeout_sec or 0.1)),
        )
        object.__setattr__(
            self,
            "execution_timeout_sec",
            max(0.1, float(self.execution_timeout_sec or 0.1)),
        )


@dataclass(frozen=True, slots=True)
class LearningAdmission:
    acquired: bool
    task_name: str
    scope_id: str
    queue_wait_ms: float
    status: str


@dataclass(frozen=True, slots=True)
class LearningWorkRequest:
    run_id: str
    candidate_id: str | None
    scope_id: str
    task_name: str
    workload_family: str
    wait_timeout_sec: float
    execution_timeout_sec: float
    config_revision: int

    def __post_init__(self) -> None:
        task_name = str(self.task_name or "").strip()
        if task_name not in LEARNING_TASK_NAMES:
            raise ValueError(f"unsupported learning task: {task_name or '<empty>'}")
        scope_id = str(self.scope_id or "").strip()
        if not scope_id:
            raise ValueError("learning scope_id must not be empty")
        object.__setattr__(self, "task_name", task_name)
        object.__setattr__(self, "scope_id", scope_id)
        object.__setattr__(self, "wait_timeout_sec", max(0.1, float(self.wait_timeout_sec or 0.1)))
        object.__setattr__(
            self,
            "execution_timeout_sec",
            max(0.1, float(self.execution_timeout_sec or 0.1)),
        )


@dataclass(frozen=True, slots=True)
class LearningWorkResult(Generic[T]):
    value: T | None
    acquired: bool
    status: Literal[
        "completed",
        "queue_full",
        "queue_timeout",
        "execution_timeout",
        "shutdown_rejected",
        "cancelled",
        "failed",
    ]
    stage: str
    failure_kind: str | None
    queue_wait_ms: float
    execution_ms: float
    error_type: str = ""
    error: BaseException | None = None


class LearningLaneBudget:
    """Single-owner logical quota for learning enrichment work."""

    def __init__(self, config: LearningLaneConfig | None = None) -> None:
        self.config = config or LearningLaneConfig()
        self._budget = BackgroundTaskBudget(
            limit=self.config.limit,
            max_queue=self.config.max_queue,
            wait_timeout_sec=self.config.admission_timeout_sec,
            execution_timeout_sec=self.config.execution_timeout_sec,
        )

    async def run_learning_work(
        self,
        request: LearningWorkRequest,
        awaitable_factory: Callable[[], Awaitable[T]],
    ) -> LearningWorkResult[T]:
        submitted_at = time.monotonic()
        acquired_at: float | None = None

        def _on_acquired() -> None:
            nonlocal acquired_at
            acquired_at = time.monotonic()

        try:
            value = await self._budget.run(
                awaitable_factory,
                task_name=request.task_name,
                scope_id=f"learning:{request.scope_id}",
                wait_timeout_sec=request.wait_timeout_sec,
                execution_timeout_sec=request.execution_timeout_sec,
                on_acquired=_on_acquired,
            )
        except BackgroundTaskQueueFull as exc:
            shutdown_rejected = "draining" in str(exc).lower()
            status = "shutdown_rejected" if shutdown_rejected else "queue_full"
            return LearningWorkResult(
                value=None,
                acquired=False,
                status=status,
                stage="queue_admission",
                failure_kind=status,
                queue_wait_ms=(time.monotonic() - submitted_at) * 1000.0,
                execution_ms=0.0,
                error_type=type(exc).__name__,
                error=exc,
            )
        except BackgroundTaskQueueTimeout as exc:
            return LearningWorkResult(
                value=None,
                acquired=False,
                status="queue_timeout",
                stage="queue_admission",
                failure_kind="queue_timeout",
                queue_wait_ms=(time.monotonic() - submitted_at) * 1000.0,
                execution_ms=0.0,
                error_type=type(exc).__name__,
                error=exc,
            )
        except BackgroundTaskExecutionTimeout as exc:
            acquired_at = acquired_at or submitted_at
            return LearningWorkResult(
                value=None,
                acquired=True,
                status="execution_timeout",
                stage="provider_request",
                failure_kind="execution_timeout",
                queue_wait_ms=(acquired_at - submitted_at) * 1000.0,
                execution_ms=(time.monotonic() - acquired_at) * 1000.0,
                error_type=type(exc).__name__,
                error=exc,
            )
        except asyncio.CancelledError:
            # Cancellation is owned by the pipeline/lifecycle settlement layer;
            # never turn it into an ordinary return value that callers can ignore.
            raise
        except Exception as exc:
            acquired = acquired_at is not None
            now = time.monotonic()
            return LearningWorkResult(
                value=None,
                acquired=acquired,
                status="failed",
                stage="provider_request" if acquired else "queue_admission",
                failure_kind="unknown",
                queue_wait_ms=((acquired_at or now) - submitted_at) * 1000.0,
                execution_ms=(now - acquired_at) * 1000.0 if acquired_at is not None else 0.0,
                error_type=type(exc).__name__,
                error=exc,
            )

        acquired_at = acquired_at or submitted_at
        return LearningWorkResult(
            value=value,
            acquired=True,
            status="completed",
            stage="provider_request",
            failure_kind=None,
            queue_wait_ms=(acquired_at - submitted_at) * 1000.0,
            execution_ms=(time.monotonic() - acquired_at) * 1000.0,
        )

    def refresh(self, config: LearningLaneConfig) -> None:
        self.config = config
        self._budget.refresh_limit(
            config.limit,
            max_queue=config.max_queue,
            wait_timeout_sec=config.admission_timeout_sec,
            execution_timeout_sec=config.execution_timeout_sec,
        )

    def begin_drain(self) -> None:
        self._budget.begin_drain()

    async def wait_until_idle(self, timeout_sec: float | None = None) -> dict[str, int]:
        return await self._budget.wait_until_idle(timeout_sec=timeout_sec)

    def status(self) -> dict[str, object]:
        return self._budget.status()


__all__ = [
    "LEARNING_TASK_NAMES",
    "LearningAdmission",
    "LearningLaneBudget",
    "LearningLaneConfig",
    "LearningWorkRequest",
    "LearningWorkResult",
]
