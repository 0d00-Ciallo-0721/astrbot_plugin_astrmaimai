from __future__ import annotations

import asyncio
import collections
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

import logging

try:
    from astrbot.api import logger
except Exception:
    logger = logging.getLogger(__name__)

from ...infrastructure.runtime.trace_runtime import debug_trace


@dataclass(frozen=True, slots=True)
class ExternalResultEnvelope:
    external_result_id: str
    trace_id: str
    source: str
    chat_id: str
    group_id: str
    sender_id: str
    self_id: str
    event_id: str
    result_chain_hash: str
    text_preview_hash: str
    text_preview: str
    has_image: bool
    created_at: float
    runtime_generation: int
    event_data: Mapping[str, Any]

    def as_event_data(self) -> dict[str, Any]:
        data = dict(self.event_data)
        data["extra"] = dict(data.get("extra", {}) or {})
        return data


class ExternalResultDispatcher:
    """Bounded, lifecycle-aware dispatcher for external plugin results."""

    DEFAULT_QUEUE_MAX = 64
    DEFAULT_PER_CHAT_MAX = 8
    DEFAULT_TTL_SECONDS = 300.0
    DEFAULT_PROCESS_TIMEOUT_SECONDS = 30.0
    COMPLETED_ID_CACHE_MAX = 512

    def __init__(
        self,
        runtime: Any,
        *,
        queue_max: int = DEFAULT_QUEUE_MAX,
        per_chat_max: int = DEFAULT_PER_CHAT_MAX,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        process_timeout_seconds: float = DEFAULT_PROCESS_TIMEOUT_SECONDS,
    ) -> None:
        self.runtime = runtime
        self.queue_max = max(1, int(queue_max or self.DEFAULT_QUEUE_MAX))
        self.per_chat_max = max(1, int(per_chat_max or self.DEFAULT_PER_CHAT_MAX))
        self.ttl_seconds = max(1.0, float(ttl_seconds or self.DEFAULT_TTL_SECONDS))
        self.process_timeout_seconds = max(
            0.1,
            float(process_timeout_seconds or self.DEFAULT_PROCESS_TIMEOUT_SECONDS),
        )
        self._queue: collections.deque[ExternalResultEnvelope] = collections.deque()
        self._queued_by_chat: collections.Counter[str] = collections.Counter()
        self._seen_ids: collections.OrderedDict[str, float] = collections.OrderedDict()
        self._terminal_counts: collections.Counter[str] = collections.Counter()
        self._worker_task: asyncio.Task[Any] | None = None
        self._accepting = True
        self._last_error = ""
        self._last_progress_at = 0.0

    @property
    def accepting(self) -> bool:
        return self._accepting

    def resume(self) -> None:
        """Re-open admission when the same runtime instance is reinitialized."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = None
        self._accepting = True

    def request_shutdown(self) -> None:
        self._accepting = False
        while self._queue:
            envelope = self._queue.popleft()
            self._queued_by_chat[envelope.chat_id] -= 1
            self._record_terminal(envelope, "shutdown")
        self._queued_by_chat += collections.Counter()

    def _record_terminal(self, envelope: ExternalResultEnvelope, status: str) -> None:
        self._terminal_counts[str(status or "failed")] += 1
        self._last_progress_at = time.time()
        debug_trace(
            dict(envelope.event_data),
            "external_result.dispatch_terminal",
            external_result_id=envelope.external_result_id,
            status=status,
            chat_id=envelope.chat_id,
        )

    def _prune_seen(self, now: float) -> None:
        cutoff = now - self.ttl_seconds
        while self._seen_ids:
            key, created = next(iter(self._seen_ids.items()))
            if created >= cutoff and len(self._seen_ids) <= self.COMPLETED_ID_CACHE_MAX:
                break
            self._seen_ids.pop(key, None)

    def enqueue(self, envelope: ExternalResultEnvelope) -> str:
        now = time.time()
        self._prune_seen(now)
        result_id = str(envelope.external_result_id or "").strip()
        if not self._accepting:
            self._record_terminal(envelope, "shutdown")
            return "shutdown"
        if not result_id:
            self._record_terminal(envelope, "failed")
            return "failed"
        if result_id in self._seen_ids:
            self._record_terminal(envelope, "duplicate")
            return "duplicate"
        if len(self._queue) >= self.queue_max:
            self._record_terminal(envelope, "queue_full")
            return "queue_full"
        if self._queued_by_chat[envelope.chat_id] >= self.per_chat_max:
            self._record_terminal(envelope, "queue_full")
            return "queue_full"

        self._seen_ids[result_id] = now
        self._queue.append(envelope)
        self._queued_by_chat[envelope.chat_id] += 1
        debug_trace(
            dict(envelope.event_data),
            "external_result.task_scheduled",
            external_result_id=result_id,
            chat_id=envelope.chat_id,
            queue_depth=len(self._queue),
        )
        self._ensure_worker()
        return "queued"

    def _ensure_worker(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        manager = getattr(getattr(self.runtime, "lifecycle", None), "manager", None)
        if manager is not None and callable(getattr(manager, "track_task", None)):
            self._worker_task = manager.track_task(self._run())
        else:
            self._worker_task = asyncio.create_task(self._run(), name="astrmai:external-result-dispatch")
            task_set = getattr(self.runtime, "background_tasks", None)
            if isinstance(task_set, set):
                task_set.add(self._worker_task)
                self._worker_task.add_done_callback(task_set.discard)

    async def _run(self) -> None:
        from .external_result_bridge import bridge_external_plugin_result

        while self._queue:
            envelope = self._queue.popleft()
            self._queued_by_chat[envelope.chat_id] -= 1
            self._queued_by_chat += collections.Counter()
            age = max(0.0, time.time() - float(envelope.created_at or time.time()))
            if age > self.ttl_seconds:
                self._record_terminal(envelope, "timeout")
                continue
            if not self._is_current_generation(envelope):
                self._record_terminal(envelope, "stale")
                continue
            try:
                debug_trace(
                    dict(envelope.event_data),
                    "external_result.dispatch_start",
                    external_result_id=envelope.external_result_id,
                    queue_age_ms=round(age * 1000.0, 1),
                )
                result = await asyncio.wait_for(
                    bridge_external_plugin_result(self.runtime, envelope),
                    timeout=self.process_timeout_seconds,
                )
            except asyncio.CancelledError:
                self._record_terminal(envelope, "shutdown" if not self._accepting else "failed")
                raise
            except asyncio.TimeoutError:
                self._last_error = "process_timeout"
                self._record_terminal(envelope, "timeout")
                logger.warning(
                    "[AstrMai] external result dispatch timed out id=%s chat=%s",
                    envelope.external_result_id,
                    envelope.chat_id,
                )
                continue
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._record_terminal(envelope, "failed")
                logger.warning(
                    "[AstrMai] external result dispatch failed id=%s type=%s",
                    envelope.external_result_id,
                    type(exc).__name__,
                )
                continue
            status = "injected" if result in (None, "injected") else str(result)
            self._record_terminal(envelope, status)

    def _is_current_generation(self, envelope: ExternalResultEnvelope) -> bool:
        expected = int(envelope.runtime_generation or 0)
        current = int(
            getattr(self.runtime, "runtime_generation", 0)
            or getattr(getattr(self.runtime, "status", None), "runtime_generation", 0)
            or 0
        )
        if expected and expected != current:
            return False
        manager = getattr(getattr(self.runtime, "lifecycle", None), "manager", None)
        if bool(getattr(manager, "_shutdown_requested", False)) or bool(getattr(manager, "_terminated", False)):
            return False
        return True

    async def shutdown(self) -> None:
        self.request_shutdown()
        task = self._worker_task
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        self._worker_task = None

    def describe_status(self) -> dict[str, Any]:
        return {
            "accepting": self._accepting,
            "queue_depth": len(self._queue),
            "queue_max": self.queue_max,
            "per_chat_max": self.per_chat_max,
            "queued_by_chat": dict(self._queued_by_chat),
            "terminal_counts": dict(self._terminal_counts),
            "worker_running": bool(self._worker_task is not None and not self._worker_task.done()),
            "last_error": self._last_error,
            "last_progress_at": self._last_progress_at,
        }


def freeze_event_data(data: Mapping[str, Any]) -> Mapping[str, Any]:
    extra = MappingProxyType(dict(data.get("extra", {}) or {}))
    frozen = dict(data)
    frozen["extra"] = extra
    return MappingProxyType(frozen)


__all__ = ["ExternalResultDispatcher", "ExternalResultEnvelope", "freeze_event_data"]
