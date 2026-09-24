from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(slots=True)
class TaskOwnerRecord:
    """Runtime ownership metadata for one asyncio task.

    The registry is deliberately limited to ownership and lifecycle
    observability.  Admission, leases, retries, and budgets remain owned by
    their existing services.
    """

    task_id: str
    task_family: str
    scope_id: str
    run_id: str
    owner: str
    generation: int
    cancel_status: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    error_type: str = ""
    error: str = ""
    task: asyncio.Task[Any] | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_family": self.task_family,
            "scope_id": self.scope_id,
            "run_id": self.run_id,
            "owner": self.owner,
            "generation": self.generation,
            "cancel_status": self.cancel_status,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error_type": self.error_type,
            "error": self.error,
        }


class BackgroundTaskOwnerRegistry:
    """Shared in-process registry for background task ownership.

    This class does not replace ``BackgroundTaskBudget`` or
    ``BackgroundTaskLedger``.  It makes task ownership discoverable by
    lifecycle shutdown and runtime diagnostics, including tasks created by
    components that do not have a durable ledger lease.
    """

    TERMINAL_STATUSES = frozenset(
        {
            "succeeded",
            "failed",
            "cancelled",
            "rejected",
            "retry_wait",
            "shutdown",
            "stale",
            "exhausted",
            "superseded",
        }
    )

    def __init__(
        self,
        *,
        generation: int = 0,
        max_terminal_records: int = 256,
        terminal_ttl_sec: float | None = 3600.0,
        clock: Any = time.time,
    ) -> None:
        self.generation = int(generation or 0)
        self.max_terminal_records = max(0, int(max_terminal_records))
        self.terminal_ttl_sec = (
            None
            if terminal_ttl_sec is None
            else max(0.0, float(terminal_ttl_sec))
        )
        self._clock = clock if callable(clock) else time.time
        self._records: dict[str, TaskOwnerRecord] = {}

    def _now(self) -> float:
        try:
            return float(self._clock())
        except (TypeError, ValueError):
            return time.time()

    def _prune_terminal_history(self) -> None:
        terminal = [
            record
            for record in self._records.values()
            if record.status in self.TERMINAL_STATUSES
        ]
        if self.terminal_ttl_sec is not None:
            cutoff = self._now() - self.terminal_ttl_sec
            for record in terminal:
                finished_at = record.finished_at or record.created_at
                if finished_at <= cutoff:
                    self._records.pop(record.task_id, None)
            terminal = [
                record
                for record in terminal
                if record.task_id in self._records
            ]
        if len(terminal) <= self.max_terminal_records:
            return
        terminal.sort(key=lambda record: (record.finished_at or record.created_at, record.task_id))
        for record in terminal[: len(terminal) - self.max_terminal_records]:
            self._records.pop(record.task_id, None)

    def set_generation(self, generation: int) -> None:
        """Update the generation used for subsequently registered tasks."""
        try:
            self.generation = int(generation or 0)
        except (TypeError, ValueError):
            self.generation = 0

    @staticmethod
    def _normalize(value: Any, fallback: str) -> str:
        text = str(value or "").strip()
        return text or fallback

    @property
    def _background_tasks(self) -> set[asyncio.Task[Any]]:
        """Compatibility view consumed by the existing shutdown collector."""
        return {
            record.task
            for record in self._records.values()
            if record.task is not None and not record.task.done()
        }

    @property
    def tasks(self) -> set[asyncio.Task[Any]]:
        return self._background_tasks

    def track(
        self,
        awaitable: Any,
        *,
        task_family: str,
        scope_id: str,
        run_id: str = "",
        owner: str = "",
        generation: int | None = None,
        cancel_status: str = "cancelled",
        name: str = "",
    ) -> asyncio.Task[Any]:
        """Create and register a task in one operation."""
        normalized_family = self._normalize(task_family, "unknown")
        normalized_scope = self._normalize(scope_id, "GLOBAL")
        normalized_run = str(run_id or "").strip()
        if normalized_run:
            for record in self._records.values():
                if (
                    record.task_family == normalized_family
                    and record.scope_id == normalized_scope
                    and record.run_id == normalized_run
                    and record.task is not None
                    and not record.task.done()
                ):
                    # A commit/run id is the idempotency key for post-send and
                    # other durable background consumers.  Reuse the original
                    # task instead of creating a duplicate side effect.
                    close = getattr(awaitable, "close", None)
                    if callable(close):
                        close()
                    return record.task
        task = asyncio.create_task(awaitable, name=name or None)
        try:
            self.register(
                task,
                task_family=normalized_family,
                scope_id=normalized_scope,
                run_id=normalized_run,
                owner=owner,
                generation=generation,
                cancel_status=cancel_status,
            )
        except BaseException:
            # Registration failure must not leave a task that can execute the
            # side effect outside lifecycle accounting.
            task.cancel()
            task.add_done_callback(self._consume_untracked_task)
            raise
        return task

    @staticmethod
    def _consume_untracked_task(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except BaseException:
            return

    def register(
        self,
        task: asyncio.Task[Any],
        *,
        task_family: str,
        scope_id: str,
        run_id: str = "",
        owner: str = "",
        generation: int | None = None,
        cancel_status: str = "cancelled",
        status: str = "queued",
    ) -> str:
        if not isinstance(task, asyncio.Task):
            raise TypeError("owner registry requires an asyncio.Task")
        task_id = f"owner_{uuid.uuid4().hex[:20]}"
        record = TaskOwnerRecord(
            task_id=task_id,
            task_family=self._normalize(task_family, "unknown"),
            scope_id=self._normalize(scope_id, "GLOBAL"),
            run_id=self._normalize(run_id, task_id),
            owner=self._normalize(owner, task.get_name()),
            generation=self.generation if generation is None else int(generation),
            cancel_status=self._normalize(cancel_status, "cancelled"),
            status=self._normalize(status, "queued"),
            created_at=self._now(),
            task=task,
        )
        self._records[task_id] = record
        task.add_done_callback(lambda completed, key=task_id: self._settle(key, completed))
        # A task may be cancelled before its coroutine gets its first turn;
        # this callback still marks it terminal because it is attached to the
        # Task object itself rather than inside the coroutine wrapper.
        try:
            asyncio.get_running_loop().call_soon(self.mark_started, task_id)
        except RuntimeError:
            self.mark_started(task_id)
        return task_id

    def register_rejected(
        self,
        *,
        task_family: str,
        scope_id: str,
        run_id: str = "",
        owner: str = "",
        generation: int | None = None,
        status: str = "rejected",
        error: str = "",
    ) -> str:
        task_id = f"owner_{uuid.uuid4().hex[:20]}"
        now = self._now()
        record = TaskOwnerRecord(
            task_id=task_id,
            task_family=self._normalize(task_family, "unknown"),
            scope_id=self._normalize(scope_id, "GLOBAL"),
            run_id=self._normalize(run_id, task_id),
            owner=self._normalize(owner, "unknown"),
            generation=self.generation if generation is None else int(generation),
            cancel_status="cancelled",
            status=self._normalize(status, "rejected"),
            created_at=now,
            finished_at=now,
            error=error,
        )
        self._records[task_id] = record
        self._prune_terminal_history()
        return task_id

    def mark_started(self, task_id: str) -> bool:
        record = self._records.get(str(task_id or ""))
        if record is None or record.status in self.TERMINAL_STATUSES:
            return False
        if not record.started_at:
            record.started_at = time.time()
        if record.status == "queued":
            record.status = "running"
        return True

    def mark_status(
        self,
        task_id: str,
        status: str,
        *,
        error: str = "",
        error_type: str = "",
    ) -> bool:
        record = self._records.get(str(task_id or ""))
        if record is None:
            return False
        normalized = self._normalize(status, "unknown")
        record.status = normalized
        if error:
            record.error = str(error)[:500]
        if error_type:
            record.error_type = str(error_type)[:120]
        if normalized in self.TERMINAL_STATUSES and not record.finished_at:
            record.finished_at = self._now()
            self._prune_terminal_history()
        return True

    def _settle(self, task_id: str, task: asyncio.Task[Any]) -> None:
        record = self._records.get(task_id)
        if record is None:
            return
        if task.cancelled():
            record.status = record.cancel_status if record.cancel_status in self.TERMINAL_STATUSES else "cancelled"
            record.error_type = "CancelledError"
        else:
            try:
                error = task.exception()
            except asyncio.CancelledError:
                error = asyncio.CancelledError()
            if error is not None:
                record.status = "failed"
                record.error_type = type(error).__name__
                record.error = str(error)[:500]
            elif record.status not in self.TERMINAL_STATUSES:
                record.status = "succeeded"
        if not record.started_at:
            record.started_at = record.created_at
        record.finished_at = record.finished_at or self._now()
        record.task = task
        self._prune_terminal_history()

    def forget(self, task_id: str) -> bool:
        return self._records.pop(str(task_id or ""), None) is not None

    def records(self, *, include_terminal: bool = True) -> list[TaskOwnerRecord]:
        self._prune_terminal_history()
        values = list(self._records.values())
        if include_terminal:
            return values
        return [record for record in values if record.status not in self.TERMINAL_STATUSES]

    def describe(self, *, include_terminal: bool = True) -> dict[str, Any]:
        records = self.records(include_terminal=include_terminal)
        by_status: dict[str, int] = {}
        by_family: dict[str, int] = {}
        for record in records:
            by_status[record.status] = by_status.get(record.status, 0) + 1
            by_family[record.task_family] = by_family.get(record.task_family, 0) + 1
        return {
            "total": len(records),
            "active": sum(1 for record in records if record.status not in self.TERMINAL_STATUSES),
            "terminal_count": sum(1 for record in records if record.status in self.TERMINAL_STATUSES),
            "retention": {
                "max_terminal_records": self.max_terminal_records,
                "terminal_ttl_sec": self.terminal_ttl_sec,
            },
            "by_status": by_status,
            "by_task_family": by_family,
            "tasks": [record.as_dict() for record in records],
        }

    async def cancel_all(self, *, timeout_sec: float = 2.0) -> int:
        tasks = [task for task in self._background_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=max(0.0, float(timeout_sec)))
        return sum(1 for task in tasks if not task.done())

    def __iter__(self) -> Iterable[asyncio.Task[Any]]:
        return iter(self._background_tasks)


__all__ = ["BackgroundTaskOwnerRegistry", "TaskOwnerRecord"]
