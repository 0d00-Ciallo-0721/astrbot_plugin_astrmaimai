from __future__ import annotations

import asyncio
import math
import time
import threading
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar


T = TypeVar("T")


@dataclass(slots=True)
class _BudgetLease:
    budget: "BackgroundTaskBudget"
    active: bool = True
    owner: asyncio.Future | None = None
    task_name: str = "unknown"
    scope_key: str = ""
    started_at: float = 0.0


_ACTIVE_BUDGET_LEASES: ContextVar[tuple[_BudgetLease, ...]] = ContextVar(
    "astrmai_active_background_budget_leases",
    default=(),
)


class BackgroundTaskQueueFull(RuntimeError):
    pass


class BackgroundTaskQueueTimeout(TimeoutError):
    pass


class BackgroundTaskExecutionTimeout(TimeoutError):
    pass


@dataclass(slots=True)
class BackgroundTaskBudget:
    limit: int = 2
    max_queue: int = 64
    wait_timeout_sec: float = 120.0
    execution_timeout_sec: float = 300.0
    _active: int = field(init=False, default=0, repr=False)
    _waiters: deque[asyncio.Future[None]] = field(init=False, default_factory=deque, repr=False)
    _rejected: int = field(init=False, default=0, repr=False)
    _timed_out: int = field(init=False, default=0, repr=False)
    _peak_queued: int = field(init=False, default=0, repr=False)
    _active_by_kind: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _rejected_by_kind: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _timed_out_by_kind: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _completed_by_kind: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _failed_by_kind: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _execution_timed_out_by_kind: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _succeeded_by_kind: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _cancelled_by_kind: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _timed_out_but_running: int = field(init=False, default=0, repr=False)
    _cancelled_but_running: int = field(init=False, default=0, repr=False)
    _late_completed_by_kind: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _queue_wait_samples_by_kind: dict[str, list[float]] = field(init=False, default_factory=dict, repr=False)
    _duration_samples_by_kind: dict[str, list[float]] = field(init=False, default_factory=dict, repr=False)
    _timeout_duration_samples_by_kind: dict[str, list[float]] = field(init=False, default_factory=dict, repr=False)
    _late_duration_samples_by_kind: dict[str, list[float]] = field(init=False, default_factory=dict, repr=False)
    _last_error_by_kind: dict[str, str] = field(init=False, default_factory=dict, repr=False)
    _resume_lock: threading.Lock = field(init=False, default_factory=threading.Lock, repr=False)
    _active_by_scope: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _rejected_by_scope: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _timed_out_by_scope: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _completed_by_scope: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _failed_by_scope: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _execution_timed_out_by_scope: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _succeeded_by_scope: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _cancelled_by_scope: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _scope_order: deque[str] = field(init=False, default_factory=deque, repr=False)
    _deferred_tasks: set[asyncio.Future] = field(init=False, default_factory=set, repr=False)
    _active_leases: dict[int, _BudgetLease] = field(init=False, default_factory=dict, repr=False)
    _drain_cancelled_waiters: int = field(init=False, default=0, repr=False)
    _accepting: bool = field(init=False, default=True, repr=False)
    _shutdown_rejected: int = field(init=False, default=0, repr=False)
    _shutdown_rejected_by_kind: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _owner_registry: Any = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self.limit = max(1, int(self.limit or 1))
        self.max_queue = max(0, int(self.max_queue or 0))
        self.wait_timeout_sec = max(0.1, float(self.wait_timeout_sec or 0.1))
        self.execution_timeout_sec = max(0.1, float(self.execution_timeout_sec or 0.1))

    def bind_owner_registry(self, owner_registry: Any) -> None:
        self._owner_registry = owner_registry

    def _register_deferred_owner(
        self,
        task: asyncio.Task,
        *,
        task_name: str,
        scope_id: str,
    ) -> None:
        registry = self._owner_registry
        register = getattr(registry, "register", None)
        if not callable(register):
            return
        try:
            register(
                task,
                task_family=f"{task_name}.physical",
                scope_id=str(scope_id or "GLOBAL"),
                run_id=f"budget-physical-{time.time_ns()}",
                owner="BackgroundTaskBudget",
                generation=getattr(registry, "generation", 0),
                cancel_status="cancelled",
            )
        except Exception:
            return

    def refresh_limit(
        self,
        limit: int,
        *,
        max_queue: int | None = None,
        wait_timeout_sec: float | None = None,
        execution_timeout_sec: float | None = None,
    ) -> None:
        self.limit = max(1, int(limit or 1))
        if max_queue is not None:
            self.max_queue = max(0, int(max_queue or 0))
        if wait_timeout_sec is not None:
            self.wait_timeout_sec = max(0.1, float(wait_timeout_sec or 0.1))
        if execution_timeout_sec is not None:
            self.execution_timeout_sec = max(0.1, float(execution_timeout_sec or 0.1))
        self._wake_waiters()

    async def run(
        self,
        awaitable_factory: Callable[[], Awaitable[T]],
        *,
        task_name: str = "unknown",
        scope_id: str = "",
        execution_timeout_sec: float | None = None,
        on_acquired: Callable[[], None] | None = None,
        defer_release_on_timeout: bool = False,
        wait_timeout_sec: float | None = None,
    ) -> T:
        task_name = str(task_name or "unknown").strip() or "unknown"
        active_leases = _ACTIVE_BUDGET_LEASES.get()
        if any(lease.budget is self and lease.active for lease in active_leases):
            # Nested work in one logical call chain reuses the outer physical
            # lease.  The outer execution deadline remains authoritative.
            return await awaitable_factory()
        if not self._accepting:
            self._shutdown_rejected += 1
            self._shutdown_rejected_by_kind[task_name] = self._shutdown_rejected_by_kind.get(task_name, 0) + 1
            raise BackgroundTaskQueueFull("background task budget is draining")
        scope_key = self._scope_key(task_name, scope_id)
        self._touch_scope(scope_key)
        queue_started_at = time.monotonic()
        try:
            await self._acquire(task_name, scope_key, wait_timeout_sec=wait_timeout_sec)
        except (BackgroundTaskQueueFull, BackgroundTaskQueueTimeout):
            if task_name != "unknown":
                samples = self._queue_wait_samples_by_kind.setdefault(task_name, [])
                samples.append(round(max(0.0, time.monotonic() - queue_started_at) * 1000.0, 1))
                if len(samples) > 256:
                    del samples[:-256]
            raise
        queue_wait_ms = max(0.0, time.monotonic() - queue_started_at) * 1000.0
        if task_name != "unknown":
            samples = self._queue_wait_samples_by_kind.setdefault(task_name, [])
            samples.append(round(queue_wait_ms, 1))
            if len(samples) > 256:
                del samples[:-256]
        started_at = time.monotonic()
        timeout_sec = self.execution_timeout_sec if execution_timeout_sec is None else max(0.1, float(execution_timeout_sec))
        timeout_scope = asyncio.timeout(timeout_sec)
        lease = _BudgetLease(
            self,
            owner=asyncio.current_task(),
            task_name=task_name,
            scope_key=scope_key,
            started_at=started_at,
        )
        self._active_leases[id(lease)] = lease
        lease_token = None
        work_task: asyncio.Future | None = None
        release_deferred = False
        result_succeeded = False
        try:
            if on_acquired is not None:
                on_acquired()
            lease_token = _ACTIVE_BUDGET_LEASES.set(active_leases + (lease,))
            if defer_release_on_timeout:
                async def _run_physical_work() -> T:
                    return await awaitable_factory()

                work_task = asyncio.create_task(
                    _run_physical_work(),
                    name=f"astrmai:budget:{task_name}:{scope_id or 'GLOBAL'}",
                )
            async with timeout_scope:
                result = (
                    await asyncio.shield(work_task)
                    if work_task is not None
                    else await awaitable_factory()
                )
                result_succeeded = True
                return result
        except asyncio.TimeoutError as exc:
            if timeout_scope.expired() and task_name != "unknown":
                self._execution_timed_out_by_kind[task_name] = self._execution_timed_out_by_kind.get(task_name, 0) + 1
                self._last_error_by_kind[task_name] = "execution_timeout"
                self._increment_scope(self._execution_timed_out_by_scope, scope_key)
            if timeout_scope.expired() and task_name != "unknown":
                samples = self._timeout_duration_samples_by_kind.setdefault(task_name, [])
                samples.append(round(max(0.0, time.monotonic() - started_at) * 1000.0, 1))
                if len(samples) > 256:
                    del samples[:-256]
            if timeout_scope.expired():
                if work_task is not None and not work_task.done() and defer_release_on_timeout:
                    self._defer_task(
                        work_task,
                        lease,
                        task_name,
                        scope_key,
                        scope_id,
                        started_at,
                        "timeout",
                    )
                    lease.owner = work_task
                    release_deferred = True
                elif work_task is not None and not work_task.done():
                    work_task.cancel()
                    await asyncio.gather(work_task, return_exceptions=True)
                raise BackgroundTaskExecutionTimeout(
                    f"background task execution timed out after {timeout_sec:.1f}s"
                ) from exc
            raise
        except asyncio.CancelledError:
            if work_task is not None and not work_task.done() and defer_release_on_timeout:
                self._defer_task(
                    work_task,
                    lease,
                    task_name,
                    scope_key,
                    scope_id,
                    started_at,
                    "cancelled",
                )
                lease.owner = work_task
                release_deferred = True
            elif work_task is not None and not work_task.done():
                work_task.cancel()
                await asyncio.gather(work_task, return_exceptions=True)
            if task_name != "unknown":
                self._cancelled_by_kind[task_name] = self._cancelled_by_kind.get(task_name, 0) + 1
                self._increment_scope(self._cancelled_by_scope, scope_key)
            raise
        except Exception as exc:
            if task_name != "unknown":
                self._failed_by_kind[task_name] = self._failed_by_kind.get(task_name, 0) + 1
                self._last_error_by_kind[task_name] = type(exc).__name__
                self._increment_scope(self._failed_by_scope, scope_key)
            raise
        finally:
            # A deferred work task still owns the physical slot and must keep
            # the lease reusable for nested calls until its done callback
            # releases that slot.
            if not release_deferred:
                lease.active = False
                self._active_leases.pop(id(lease), None)
            if lease_token is not None:
                _ACTIVE_BUDGET_LEASES.reset(lease_token)
            if task_name != "unknown":
                self._completed_by_kind[task_name] = self._completed_by_kind.get(task_name, 0) + 1
                self._increment_scope(self._completed_by_scope, scope_key)
                if result_succeeded:
                    self._succeeded_by_kind[task_name] = self._succeeded_by_kind.get(task_name, 0) + 1
                    self._increment_scope(self._succeeded_by_scope, scope_key)
                if not release_deferred:
                    samples = self._duration_samples_by_kind.setdefault(task_name, [])
                    samples.append(round(max(0.0, time.monotonic() - started_at) * 1000.0, 1))
                    if len(samples) > 256:
                        del samples[:-256]
            if not release_deferred:
                self._release(task_name, scope_key)

    def _defer_task(
        self,
        work_task: asyncio.Future,
        lease: _BudgetLease,
        task_name: str,
        scope_key: str,
        scope_id: str,
        started_at: float,
        reason: str,
    ) -> None:
        if reason == "timeout":
            self._timed_out_but_running += 1
        else:
            self._cancelled_but_running += 1
        if isinstance(work_task, asyncio.Task):
            self._register_deferred_owner(
                work_task,
                task_name=task_name,
                scope_id=scope_id,
            )
        self._deferred_tasks.add(work_task)
        work_task.add_done_callback(
            lambda completed: self._finish_deferred_task(
                completed,
                lease,
                task_name,
                scope_key,
                started_at,
                reason,
            )
        )

    def _finish_deferred_task(
        self,
        completed: asyncio.Future,
        lease: _BudgetLease,
        task_name: str,
        scope_key: str,
        started_at: float,
        reason: str,
    ) -> None:
        if reason == "timeout":
            self._timed_out_but_running = max(0, self._timed_out_but_running - 1)
        else:
            self._cancelled_but_running = max(0, self._cancelled_but_running - 1)
        if task_name != "unknown":
            duration_samples = self._duration_samples_by_kind.setdefault(task_name, [])
            duration_samples.append(round(max(0.0, time.monotonic() - started_at) * 1000.0, 1))
            if len(duration_samples) > 256:
                del duration_samples[:-256]
            samples = self._late_duration_samples_by_kind.setdefault(task_name, [])
            samples.append(round(max(0.0, time.monotonic() - started_at) * 1000.0, 1))
            if len(samples) > 256:
                del samples[:-256]
            self._late_completed_by_kind[task_name] = self._late_completed_by_kind.get(task_name, 0) + 1
        try:
            if not completed.cancelled():
                completed.exception()
        finally:
            lease.active = False
            self._active_leases.pop(id(lease), None)
            self._deferred_tasks.discard(completed)
            self._release(task_name, scope_key)

    def begin_drain(self) -> None:
        """Stop admitting new work before owners cancel their producers."""
        if self._accepting:
            self._drain_cancelled_waiters = 0
        self._accepting = False
        pending_waiters = [waiter for waiter in self._waiters if not waiter.done()]
        self._drain_cancelled_waiters += len(pending_waiters)
        for waiter in pending_waiters:
            if not waiter.done():
                waiter.cancel()

    def _owner_diagnostics(self) -> dict[str, object]:
        now = time.monotonic()
        active_by_kind = dict(self._active_by_kind)
        queued_by_kind: dict[str, int] = {}
        deferred_by_kind: dict[str, int] = {}
        owner_task_names: list[str] = []
        owner_ages: list[float] = []
        physical_owner_count = 0
        for waiter in self._waiters:
            if waiter.done():
                continue
            task_name = str(getattr(waiter, "_astrmai_task_name", "unknown") or "unknown")
            queued_by_kind[task_name] = queued_by_kind.get(task_name, 0) + 1
        deferred_tasks = self._deferred_tasks
        for lease in self._active_leases.values():
            if not lease.active:
                continue
            owner = lease.owner
            if owner is None or owner.done():
                continue
            physical_owner_count += 1
            if lease.started_at:
                owner_ages.append(max(0.0, now - lease.started_at))
            owner_name = getattr(owner, "get_name", lambda: "unknown")()
            owner_task_names.append(str(owner_name or lease.task_name or "unknown"))
            if owner in deferred_tasks:
                deferred_by_kind[lease.task_name] = deferred_by_kind.get(lease.task_name, 0) + 1
        return {
            "active_by_kind": active_by_kind,
            "queued_by_kind": queued_by_kind,
            "deferred_by_kind": deferred_by_kind,
            "physical_owner_count": physical_owner_count,
            "oldest_owner_age_ms": round(max(owner_ages, default=0.0) * 1000.0, 3),
            "owner_task_names": sorted(set(owner_task_names)),
        }

    async def drain(self, timeout_sec: float = 2.0) -> dict[str, object]:
        """Wait for all physical owners without pretending cancelled threads ended."""
        self.begin_drain()
        pending = [
            lease.owner
            for lease in self._active_leases.values()
            if lease.active and lease.owner is not None and not lease.owner.done()
        ]
        pending = list(dict.fromkeys(pending))
        queued = sum(1 for waiter in self._waiters if not waiter.done())
        if pending:
            await asyncio.wait(
                pending,
                timeout=max(0.0, float(timeout_sec or 0.0)),
            )
            await asyncio.sleep(0)
        remaining_leases = sum(1 for lease in self._active_leases.values() if lease.active)
        remaining_deferred = sum(1 for task in self._deferred_tasks if not task.done())
        report = {
            "observed": len(pending) + queued + self._drain_cancelled_waiters,
            "remaining": max(remaining_leases, remaining_deferred, queued, self._active),
        }
        if report["remaining"]:
            report.update(
                {
                    "active": int(self._active),
                    "queued_waiters": int(queued),
                    "deferred": int(remaining_deferred),
                    "physical": int(remaining_deferred),
                }
            )
            report.update(self._owner_diagnostics())
        return report

    async def wait_until_idle(self, timeout_sec: float | None = None) -> dict[str, int]:
        """Wait for every physical owner to finish after admission is fenced."""
        self.begin_drain()
        deadline = (
            None
            if timeout_sec is None
            else time.monotonic() + max(0.0, float(timeout_sec or 0.0))
        )
        while True:
            remaining = max(
                self._active,
                sum(1 for lease in self._active_leases.values() if lease.active),
                sum(1 for task in self._deferred_tasks if not task.done()),
                sum(1 for waiter in self._waiters if not waiter.done()),
            )
            if remaining == 0:
                return {"remaining": 0}
            if deadline is not None and time.monotonic() >= deadline:
                return {"remaining": remaining}
            await asyncio.sleep(0.05)

    def resume(self) -> bool:
        if (
            self._active > 0
            or any(lease.active for lease in self._active_leases.values())
            or any(not task.done() for task in self._deferred_tasks)
            or any(not waiter.done() for waiter in self._waiters)
        ):
            return False
        self._accepting = True
        return True

    def can_resume(self) -> bool:
        return not (
            self._active > 0
            or any(lease.active for lease in self._active_leases.values())
            or any(not task.done() for task in self._deferred_tasks)
            or any(not waiter.done() for waiter in self._waiters)
        )

    def resume_if_idle(self) -> bool:
        with self._resume_lock:
            if not self.can_resume():
                return False
            self._accepting = True
            return True

    async def _acquire(
        self,
        task_name: str,
        scope_key: str = "",
        *,
        wait_timeout_sec: float | None = None,
    ) -> None:
        if self._active < self.limit and not self._waiters:
            self._active += 1
            if task_name != "unknown":
                self._active_by_kind[task_name] = self._active_by_kind.get(task_name, 0) + 1
                self._increment_scope(self._active_by_scope, scope_key)
            return

        queued = sum(1 for item in self._waiters if not item.done())
        if queued >= self.max_queue:
            self._rejected += 1
            self._rejected_by_kind[task_name] = self._rejected_by_kind.get(task_name, 0) + 1
            self._increment_scope(self._rejected_by_scope, scope_key)
            raise BackgroundTaskQueueFull(
                f"background task queue is full: queued={queued} limit={self.max_queue}"
            )
        waiter = asyncio.get_running_loop().create_future()
        setattr(waiter, "_astrmai_task_name", task_name)
        setattr(waiter, "_astrmai_scope_key", scope_key)
        self._waiters.append(waiter)
        self._peak_queued = max(self._peak_queued, queued + 1)
        try:
            queue_timeout = self.wait_timeout_sec if wait_timeout_sec is None else max(0.1, float(wait_timeout_sec))
            await asyncio.wait_for(waiter, timeout=queue_timeout)
        except asyncio.TimeoutError as exc:
            self._timed_out += 1
            self._timed_out_by_kind[task_name] = self._timed_out_by_kind.get(task_name, 0) + 1
            self._increment_scope(self._timed_out_by_scope, scope_key)
            if waiter.done() and not waiter.cancelled():
                self._release(task_name, scope_key)
            else:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
            raise BackgroundTaskQueueTimeout(
                f"background task queue wait timed out after {queue_timeout:.1f}s"
            ) from exc
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():
                self._release(task_name, scope_key)
            else:
                waiter.cancel()
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
            raise

    def _release(self, task_name: str = "unknown", scope_key: str = "") -> None:
        self._active = max(0, self._active - 1)
        if task_name in self._active_by_kind:
            self._active_by_kind[task_name] = max(0, self._active_by_kind[task_name] - 1)
            if self._active_by_kind[task_name] == 0:
                self._active_by_kind.pop(task_name, None)
        if scope_key in self._active_by_scope:
            self._active_by_scope[scope_key] = max(0, self._active_by_scope[scope_key] - 1)
            if self._active_by_scope[scope_key] == 0:
                self._active_by_scope.pop(scope_key, None)
        self._wake_waiters()

    def _wake_waiters(self) -> None:
        while self._active < self.limit and self._waiters:
            waiter = self._waiters.popleft()
            if waiter.done():
                continue
            self._active += 1
            task_name = str(getattr(waiter, "_astrmai_task_name", "unknown") or "unknown")
            scope_key = str(getattr(waiter, "_astrmai_scope_key", "") or "")
            if task_name != "unknown":
                self._active_by_kind[task_name] = self._active_by_kind.get(task_name, 0) + 1
                self._increment_scope(self._active_by_scope, scope_key)
            waiter.set_result(None)

    def status(self) -> dict[str, object]:
        queued_by_kind: dict[str, int] = {}
        queued_by_scope: dict[str, int] = {}
        for waiter in self._waiters:
            if waiter.done():
                continue
            task_name = str(getattr(waiter, "_astrmai_task_name", "unknown") or "unknown")
            if task_name == "unknown":
                continue
            queued_by_kind[task_name] = queued_by_kind.get(task_name, 0) + 1
            scope_key = str(getattr(waiter, "_astrmai_scope_key", "") or "")
            self._increment_scope(queued_by_scope, scope_key)
        status: dict[str, object] = {
            "limit": int(self.limit),
            "max_queue": int(self.max_queue),
            "wait_timeout_sec": float(self.wait_timeout_sec),
            "active": int(self._active),
            "available_slots": max(0, int(self.limit - self._active)),
            "queued": sum(1 for waiter in self._waiters if not waiter.done()),
            "peak_queued": int(self._peak_queued),
            "rejected": int(self._rejected),
            "timed_out": int(self._timed_out),
            "shutdown_rejected": int(self._shutdown_rejected),
            "shutdown_rejected_by_kind": dict(self._shutdown_rejected_by_kind),
        }
        status.update(self._owner_diagnostics())
        deferred_count = sum(1 for task in self._deferred_tasks if not task.done())
        if deferred_count or not self._accepting:
            status["queued_waiters"] = sum(1 for waiter in self._waiters if not waiter.done())
            status["deferred"] = int(deferred_count)
            status["physical_tasks"] = int(deferred_count)
            status["draining"] = not bool(self._accepting)
        if deferred_count or not self._accepting:
            status["deferred_tasks"] = int(deferred_count)
            status["accepting"] = bool(self._accepting)
        if self._timed_out_but_running:
            status["timed_out_but_running"] = int(self._timed_out_but_running)
        if self._cancelled_but_running:
            status["cancelled_but_running"] = int(self._cancelled_but_running)
        if (
            self._active_by_kind
            or queued_by_kind
            or self._rejected_by_kind
            or self._timed_out_by_kind
            or self._completed_by_kind
            or self._succeeded_by_kind
            or self._cancelled_by_kind
            or self._failed_by_kind
            or self._execution_timed_out_by_kind
            or self._timeout_duration_samples_by_kind
            or self._late_duration_samples_by_kind
        ):
            duration_by_kind = {
                name: self._duration_summary(samples)
                for name, samples in self._duration_samples_by_kind.items()
            }
            status.update(
                {
                    "active_by_kind": dict(self._active_by_kind),
                    "queued_by_kind": queued_by_kind,
                    "rejected_by_kind": dict(self._rejected_by_kind),
                    "timed_out_by_kind": dict(self._timed_out_by_kind),
                    "completed_by_kind": dict(self._completed_by_kind),
                    "succeeded_by_kind": dict(self._succeeded_by_kind),
                    "cancelled_by_kind": dict(self._cancelled_by_kind),
                    "failed_by_kind": dict(self._failed_by_kind),
                    "execution_timed_out_by_kind": dict(self._execution_timed_out_by_kind),
                    "late_completed_by_kind": dict(self._late_completed_by_kind),
                    "queue_wait_ms_by_kind": {
                        name: self._duration_summary(samples)
                        for name, samples in self._queue_wait_samples_by_kind.items()
                    },
                    "duration_ms_by_kind": duration_by_kind,
                    "timeout_elapsed_ms_by_kind": {
                        name: self._duration_summary(samples)
                        for name, samples in self._timeout_duration_samples_by_kind.items()
                    },
                    "late_completion_elapsed_ms_by_kind": {
                        name: self._duration_summary(samples)
                        for name, samples in self._late_duration_samples_by_kind.items()
                    },
                    "last_error_by_kind": dict(self._last_error_by_kind),
                    "execution_timeout_sec": float(self.execution_timeout_sec),
                    "scope_stats": self._scope_stats(queued_by_scope),
                }
            )
        return status

    @staticmethod
    def _scope_key(task_name: str, scope_id: str) -> str:
        normalized_scope = str(scope_id or "").strip()
        if not normalized_scope or task_name == "unknown":
            return ""
        return f"{task_name}|{normalized_scope}"

    @staticmethod
    def _increment_scope(target: dict[str, int], scope_key: str) -> None:
        if scope_key:
            target[scope_key] = target.get(scope_key, 0) + 1

    def _scope_stats(self, queued_by_scope: dict[str, int]) -> dict[str, dict[str, int]]:
        keys = set().union(
            self._active_by_scope,
            queued_by_scope,
            self._rejected_by_scope,
            self._timed_out_by_scope,
            self._completed_by_scope,
            self._failed_by_scope,
            self._execution_timed_out_by_scope,
            self._succeeded_by_scope,
            self._cancelled_by_scope,
        )
        return {
            key: {
                "active": int(self._active_by_scope.get(key, 0)),
                "queued": int(queued_by_scope.get(key, 0)),
                "rejected": int(self._rejected_by_scope.get(key, 0)),
                "timed_out": int(self._timed_out_by_scope.get(key, 0)),
                "completed": int(self._completed_by_scope.get(key, 0)),
                "failed": int(self._failed_by_scope.get(key, 0)),
                "execution_timed_out": int(
                    self._execution_timed_out_by_scope.get(key, 0)
                ),
                "succeeded": int(self._succeeded_by_scope.get(key, 0)),
                "cancelled": int(self._cancelled_by_scope.get(key, 0)),
            }
            for key in sorted(keys)
        }

    def _touch_scope(self, scope_key: str) -> None:
        if not scope_key:
            return
        try:
            self._scope_order.remove(scope_key)
        except ValueError:
            pass
        self._scope_order.append(scope_key)
        attempts = len(self._scope_order)
        while len(self._scope_order) > 256 and attempts > 0:
            attempts -= 1
            candidate = self._scope_order.popleft()
            queued = any(
                not waiter.done()
                and str(getattr(waiter, "_astrmai_scope_key", "") or "") == candidate
                for waiter in self._waiters
            )
            if self._active_by_scope.get(candidate, 0) > 0 or queued:
                self._scope_order.append(candidate)
                continue
            for mapping in (
                self._rejected_by_scope,
                self._timed_out_by_scope,
                self._completed_by_scope,
                self._failed_by_scope,
                self._execution_timed_out_by_scope,
                self._succeeded_by_scope,
                self._cancelled_by_scope,
            ):
                mapping.pop(candidate, None)

    @staticmethod
    def _duration_summary(samples: list[float]) -> dict[str, float | int]:
        if not samples:
            return {"count": 0, "avg": 0.0, "p95": 0.0, "max": 0.0}
        ordered = sorted(float(value) for value in samples)
        p95_index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1))
        p95 = ordered[p95_index]
        return {
            "count": len(ordered),
            "avg": round(sum(ordered) / len(ordered), 1),
            "p95": round(p95, 1),
            "max": round(ordered[-1], 1),
        }


__all__ = [
    "BackgroundTaskBudget",
    "BackgroundTaskQueueFull",
    "BackgroundTaskQueueTimeout",
    "BackgroundTaskExecutionTimeout",
]
