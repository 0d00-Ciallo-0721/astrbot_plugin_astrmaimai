from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar


T = TypeVar("T")


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
    _duration_samples_by_kind: dict[str, list[float]] = field(init=False, default_factory=dict, repr=False)
    _last_error_by_kind: dict[str, str] = field(init=False, default_factory=dict, repr=False)
    _active_by_scope: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _rejected_by_scope: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _timed_out_by_scope: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _completed_by_scope: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _failed_by_scope: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _execution_timed_out_by_scope: dict[str, int] = field(init=False, default_factory=dict, repr=False)
    _scope_order: deque[str] = field(init=False, default_factory=deque, repr=False)

    def __post_init__(self) -> None:
        self.limit = max(1, int(self.limit or 1))
        self.max_queue = max(0, int(self.max_queue or 0))
        self.wait_timeout_sec = max(0.1, float(self.wait_timeout_sec or 0.1))
        self.execution_timeout_sec = max(0.1, float(self.execution_timeout_sec or 0.1))

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
    ) -> T:
        task_name = str(task_name or "unknown").strip() or "unknown"
        scope_key = self._scope_key(task_name, scope_id)
        self._touch_scope(scope_key)
        await self._acquire(task_name, scope_key)
        if on_acquired is not None:
            try:
                on_acquired()
            except BaseException:
                self._release(task_name, scope_key)
                raise
        started_at = time.monotonic()
        timeout_sec = self.execution_timeout_sec if execution_timeout_sec is None else max(0.1, float(execution_timeout_sec))
        timeout_scope = asyncio.timeout(timeout_sec)
        try:
            async with timeout_scope:
                return await awaitable_factory()
        except asyncio.TimeoutError as exc:
            if timeout_scope.expired() and task_name != "unknown":
                self._execution_timed_out_by_kind[task_name] = self._execution_timed_out_by_kind.get(task_name, 0) + 1
                self._last_error_by_kind[task_name] = "execution_timeout"
                self._increment_scope(self._execution_timed_out_by_scope, scope_key)
            if timeout_scope.expired():
                raise BackgroundTaskExecutionTimeout(
                    f"background task execution timed out after {timeout_sec:.1f}s"
                ) from exc
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if task_name != "unknown":
                self._failed_by_kind[task_name] = self._failed_by_kind.get(task_name, 0) + 1
                self._last_error_by_kind[task_name] = type(exc).__name__
                self._increment_scope(self._failed_by_scope, scope_key)
            raise
        finally:
            self._release(task_name, scope_key)
            if task_name != "unknown":
                self._completed_by_kind[task_name] = self._completed_by_kind.get(task_name, 0) + 1
                self._increment_scope(self._completed_by_scope, scope_key)
                samples = self._duration_samples_by_kind.setdefault(task_name, [])
                samples.append(round(max(0.0, time.monotonic() - started_at) * 1000.0, 1))
                if len(samples) > 256:
                    del samples[:-256]

    async def _acquire(self, task_name: str, scope_key: str = "") -> None:
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
            await asyncio.wait_for(waiter, timeout=self.wait_timeout_sec)
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
                f"background task queue wait timed out after {self.wait_timeout_sec:.1f}s"
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
        }
        if (
            self._active_by_kind
            or queued_by_kind
            or self._rejected_by_kind
            or self._timed_out_by_kind
            or self._completed_by_kind
            or self._failed_by_kind
            or self._execution_timed_out_by_kind
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
                    "failed_by_kind": dict(self._failed_by_kind),
                    "execution_timed_out_by_kind": dict(self._execution_timed_out_by_kind),
                    "duration_ms_by_kind": duration_by_kind,
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
            ):
                mapping.pop(candidate, None)

    @staticmethod
    def _duration_summary(samples: list[float]) -> dict[str, float | int]:
        if not samples:
            return {"count": 0, "avg": 0.0, "p95": 0.0, "max": 0.0}
        ordered = sorted(float(value) for value in samples)
        p95 = ordered[min(len(ordered) - 1, int((len(ordered) - 1) * 0.95))]
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
