"""Cross-instance lifecycle coordination for hot reloads.

The AstrBot host may construct a replacement plugin before the previous
instance has finished terminating.  This module keeps the hand-off state at
process scope so instance-local lifecycle locks cannot race each other.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from astrbot.api import logger


@dataclass(frozen=True)
class RuntimeRegistration:
    generation: int
    previous_termination: Any = None


class RuntimeInstanceCoordinator:
    """Serialize same-process facade replacement and resource ownership."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active_facade: Any = None
        self._active_generation = 0
        self._resource_owner: tuple[Any, int] | None = None
        self._termination_by_facade: dict[int, Any] = {}
        self._termination_meta: dict[int, dict[str, Any]] = {}
        self._termination_tail: Any = None

    @property
    def active_facade(self) -> Any:
        with self._lock:
            return self._active_facade

    def register_facade(
        self,
        facade: Any,
        terminate_factory: Callable[[], Awaitable[Any] | Any] | None = None,
    ) -> RuntimeRegistration:
        """Register a facade and schedule the previous one exactly once."""
        with self._lock:
            previous = self._active_facade
            if previous is facade:
                return RuntimeRegistration(self._active_generation, self._termination_tail)

            self._active_generation += 1
            generation = self._active_generation
            predecessor = self._termination_tail
            self._active_facade = facade

            if previous is not None and previous is not facade:
                key = id(previous)
                termination = self._termination_by_facade.get(key)
                if termination is None:
                    # The caller supplies a factory for the new facade; obtain
                    # the old facade's terminate method before switching.
                    old_terminate = getattr(previous, "terminate", None)
                    old_begin_shutdown = getattr(previous, "begin_shutdown", None)
                    if callable(old_begin_shutdown):
                        try:
                            old_begin_shutdown()
                        except Exception as exc:
                            logger.warning("[AstrMai] previous facade begin_shutdown degraded: %s", exc)

                    async def _terminate_previous() -> Any:
                        if callable(old_terminate):
                            result = old_terminate()
                            return await result if inspect.isawaitable(result) else result
                        return None

                    termination = self._schedule(_terminate_previous, predecessor)
                    self._termination_by_facade[key] = termination
                    self._termination_meta[key] = {
                        "generation": generation - 1,
                        "started_at": time.time(),
                        "completed_at": 0.0,
                        "error": "",
                    }
                    self._attach_completion(key, termination)
                    self._termination_tail = termination
                else:
                    self._termination_tail = termination
            return RuntimeRegistration(generation, self._termination_tail)

    def _schedule(self, factory: Callable[[], Awaitable[Any] | Any], predecessor: Any) -> Any:
        async def _run() -> Any:
            if predecessor is not None:
                await self._await_handle(predecessor)
            result = factory()
            return await result if inspect.isawaitable(result) else result

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            return loop.create_task(_run(), name="astrmai:runtime:previous-termination")

        future: concurrent.futures.Future[Any] = concurrent.futures.Future()

        def _worker() -> None:
            try:
                # A predecessor created in a different thread is represented
                # by a concurrent Future.  asyncio Tasks cannot safely cross
                # event loops; in that case the owner remains unresolved.
                if predecessor is not None:
                    if isinstance(predecessor, concurrent.futures.Future):
                        predecessor.result()
                    else:
                        raise RuntimeError("previous termination belongs to another event loop")
                result = factory()
                if inspect.isawaitable(result):
                    result = asyncio.run(result)
                future.set_result(result)
            except BaseException as exc:
                future.set_exception(exc)

        threading.Thread(
            target=_worker,
            name="astrmai-old-facade-shutdown",
            daemon=True,
        ).start()
        return future

    async def _await_handle(self, handle: Any) -> Any:
        if isinstance(handle, concurrent.futures.Future):
            return await asyncio.wrap_future(handle)
        if isinstance(handle, asyncio.Future):
            current = asyncio.get_running_loop()
            if handle.get_loop() is current:
                return await handle
            raise RuntimeError("previous termination belongs to another event loop")
        if inspect.isawaitable(handle):
            return await handle
        return handle

    def _attach_completion(self, key: int, handle: Any) -> None:
        def _done(done: Any) -> None:
            error = ""
            try:
                if isinstance(done, concurrent.futures.Future):
                    done.result()
                else:
                    done.result()
            except BaseException as exc:
                error = f"{type(exc).__name__}: {exc}"
                logger.warning("[AstrMai] previous facade termination degraded: %s", exc)
            with self._lock:
                meta = self._termination_meta.get(key)
                if meta is not None:
                    meta["completed_at"] = time.time()
                    meta["error"] = error
                self._termination_by_facade.pop(key, None)
                if len(self._termination_meta) > 32:
                    completed = [
                        (item_key, item)
                        for item_key, item in self._termination_meta.items()
                        if item.get("completed_at", 0.0)
                    ]
                    excess = len(self._termination_meta) - 32
                    for item_key, _item in sorted(
                        completed,
                        key=lambda pair: pair[1].get("completed_at", 0.0),
                    )[:excess]:
                        self._termination_meta.pop(item_key, None)

        if isinstance(handle, asyncio.Future):
            handle.add_done_callback(_done)
        elif isinstance(handle, concurrent.futures.Future):
            handle.add_done_callback(_done)

    async def wait_for_previous_termination(
        self,
        handle: Any,
        *,
        timeout_sec: float = 10.0,
    ) -> tuple[bool, str]:
        if handle is None:
            return True, ""
        started = time.monotonic()
        try:
            await asyncio.wait_for(asyncio.shield(self._await_handle(handle)), timeout=max(0.0, timeout_sec))
            return True, ""
        except asyncio.TimeoutError:
            return False, f"previous facade termination timed out after {time.monotonic() - started:.3f}s"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return False, f"previous facade termination failed: {type(exc).__name__}: {exc}"

    def claim_resource_owner(self, facade: Any, generation: int) -> bool:
        with self._lock:
            if facade is not self._active_facade or generation != self._active_generation:
                return False
            self._resource_owner = (facade, generation)
            return True

    def can_use_shared_resources(self, facade: Any, generation: int) -> bool:
        with self._lock:
            return self._resource_owner == (facade, generation)

    def describe(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active_generation": self._active_generation,
                "active_facade_identity": id(self._active_facade) if self._active_facade is not None else None,
                "resource_owner_generation": self._resource_owner[1] if self._resource_owner else None,
                "termination_pending": sum(
                    1 for handle in self._termination_by_facade.values() if not handle.done()
                ),
                "terminations": [dict(meta) for meta in self._termination_meta.values()],
            }


RUNTIME_INSTANCE_COORDINATOR = RuntimeInstanceCoordinator()


__all__ = ["RuntimeRegistration", "RuntimeInstanceCoordinator", "RUNTIME_INSTANCE_COORDINATOR"]
