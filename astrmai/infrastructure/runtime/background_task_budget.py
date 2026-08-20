from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar


T = TypeVar("T")


class BackgroundTaskQueueFull(RuntimeError):
    pass


class BackgroundTaskQueueTimeout(TimeoutError):
    pass


@dataclass(slots=True)
class BackgroundTaskBudget:
    limit: int = 2
    max_queue: int = 64
    wait_timeout_sec: float = 120.0
    _active: int = field(init=False, default=0, repr=False)
    _waiters: deque[asyncio.Future[None]] = field(init=False, default_factory=deque, repr=False)
    _rejected: int = field(init=False, default=0, repr=False)
    _timed_out: int = field(init=False, default=0, repr=False)
    _peak_queued: int = field(init=False, default=0, repr=False)

    def __post_init__(self) -> None:
        self.limit = max(1, int(self.limit or 1))
        self.max_queue = max(0, int(self.max_queue or 0))
        self.wait_timeout_sec = max(0.1, float(self.wait_timeout_sec or 0.1))

    def refresh_limit(
        self,
        limit: int,
        *,
        max_queue: int | None = None,
        wait_timeout_sec: float | None = None,
    ) -> None:
        self.limit = max(1, int(limit or 1))
        if max_queue is not None:
            self.max_queue = max(0, int(max_queue or 0))
        if wait_timeout_sec is not None:
            self.wait_timeout_sec = max(0.1, float(wait_timeout_sec or 0.1))
        self._wake_waiters()

    async def run(self, awaitable_factory: Callable[[], Awaitable[T]]) -> T:
        await self._acquire()
        try:
            return await awaitable_factory()
        finally:
            self._release()

    async def _acquire(self) -> None:
        if self._active < self.limit and not self._waiters:
            self._active += 1
            return

        queued = sum(1 for item in self._waiters if not item.done())
        if queued >= self.max_queue:
            self._rejected += 1
            raise BackgroundTaskQueueFull(
                f"background task queue is full: queued={queued} limit={self.max_queue}"
            )
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        self._peak_queued = max(self._peak_queued, queued + 1)
        try:
            await asyncio.wait_for(waiter, timeout=self.wait_timeout_sec)
        except asyncio.TimeoutError as exc:
            self._timed_out += 1
            if waiter.done() and not waiter.cancelled():
                self._release()
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
                self._release()
            else:
                waiter.cancel()
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
            raise

    def _release(self) -> None:
        self._active = max(0, self._active - 1)
        self._wake_waiters()

    def _wake_waiters(self) -> None:
        while self._active < self.limit and self._waiters:
            waiter = self._waiters.popleft()
            if waiter.done():
                continue
            self._active += 1
            waiter.set_result(None)

    def status(self) -> dict[str, int | float]:
        return {
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


__all__ = [
    "BackgroundTaskBudget",
    "BackgroundTaskQueueFull",
    "BackgroundTaskQueueTimeout",
]
