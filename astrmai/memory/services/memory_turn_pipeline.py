from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from typing import Any

from astrbot.api import logger

from ...infrastructure.runtime.event_bus import EventBus
from ...infrastructure.runtime.background_task_budget import (
    BackgroundTaskBudget,
    BackgroundTaskQueueFull,
    BackgroundTaskQueueTimeout,
    BackgroundTaskExecutionTimeout,
)
from ...infrastructure.runtime.turn_call_ledger import detach_turn_telemetry
from ..contracts.memory_query import CommittedMemoryTurn, InstantGateResult


class MemoryTurnPipeline:
    TURN_IDLE_SWEEP_SECONDS = 60.0
    TURN_FORCE_SUMMARIZE_AFTER_SECONDS = 30 * 60.0

    def __init__(
        self,
        *,
        context: Any,
        gateway: Any,
        engine: Any,
        session_summarizer: Any,
        instant_gate: Any,
        event_bus: EventBus | None = None,
        config: Any = None,
        observer: Any = None,
        checkpoint_store: Any = None,
        turn_ledger: Any = None,
        task_ledger: Any = None,
        background_task_budget: Any = None,
        owner_registry: Any = None,
    ) -> None:
        self.context = context
        self.gateway = gateway
        self.engine = engine
        self.session_summarizer = session_summarizer
        self.instant_gate = instant_gate
        self.event_bus = event_bus or EventBus()
        self.config = config if config is not None else getattr(gateway, "config", None)
        self.observer = observer
        self.checkpoint_store = checkpoint_store
        self.turn_ledger = turn_ledger
        self.task_ledger = task_ledger
        self.background_task_budget = background_task_budget or BackgroundTaskBudget()
        self.owner_registry = owner_registry
        self._session_history_buffer: dict[str, dict[str, Any]] = {}
        self._inflight_maintenance: dict[str, list[Any]] = {}
        self._inflight_maintenance_turn_ids: dict[str, list[str]] = {}
        self._memory_locks: dict[str, asyncio.Lock] = {}
        self._worker_tasks: dict[str, asyncio.Task[Any]] = {}
        self._worker_queues: dict[str, asyncio.Queue[CommittedMemoryTurn]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._instant_llm_last_check: dict[str, float] = {}
        self._running = False
        self._accepting = True
        self._shutdown_rejected_count = 0
        self._started_after_shutdown = 0
        self._shutdown_generation = 0
        self._sweep_task: asyncio.Task[Any] | None = None
        self._maintenance_limit = self._maintenance_concurrency()
        self._maintenance_semaphore = asyncio.Semaphore(self._maintenance_limit)
        self._active_maintenance = 0

    def _register_owner_task(
        self,
        task: asyncio.Task[Any],
        *,
        task_family: str,
        scope_id: str = "GLOBAL",
        run_id: str = "",
    ) -> None:
        registry = getattr(self, "owner_registry", None)
        register = getattr(registry, "register", None)
        if not callable(register):
            return
        try:
            register(
                task,
                task_family=task_family,
                scope_id=scope_id or "GLOBAL",
                run_id=run_id,
                owner="MemoryTurnPipeline",
                generation=getattr(registry, "generation", 0),
                cancel_status="cancelled",
            )
        except Exception as exc:
            logger.debug("[MemoryTurnPipeline] owner registry registration degraded: %s", exc)

    def refresh_config(self, config: Any) -> None:
        self.config = config
        configured = self._maintenance_concurrency()
        if configured != self._maintenance_limit and self._active_maintenance == 0:
            self._maintenance_limit = configured
            self._maintenance_semaphore = asyncio.Semaphore(configured)
        for component in (self.session_summarizer, self.instant_gate):
            refresh = getattr(component, "refresh_config", None)
            if callable(refresh):
                refresh(config)
            elif component is not None:
                component.config = config

    def _maintenance_concurrency(self) -> int:
        value = getattr(getattr(self.config, "memory", None), "maintenance_concurrency", 1)
        try:
            return max(1, min(4, int(value or 1)))
        except (TypeError, ValueError):
            return 1

    async def start(self) -> None:
        if self._running:
            return
        await self._restore_checkpoints()
        self._accepting = True
        self._running = True
        self.event_bus.subscribe(self.event_bus.TOPIC_MEMORY_TURN_COMMITTED, self.on_turn_committed)
        self._sweep_task = asyncio.create_task(self._sweep_loop())
        self._background_tasks.add(self._sweep_task)
        self._register_owner_task(
            self._sweep_task,
            task_family="memory.maintenance.sweep",
            scope_id="GLOBAL",
            run_id=f"memory-sweep-{uuid.uuid4().hex[:12]}",
        )
        self._sweep_task.add_done_callback(self._handle_task_result)
        await self._observe_global("memory_pipeline", "pipeline_started", summary="Memory pipeline started")

    async def stop(self) -> None:
        self.begin_shutdown()
        self.event_bus.unsubscribe(self.event_bus.TOPIC_MEMORY_TURN_COMMITTED, self.on_turn_committed)
        self._running = False
        await self._persist_all_checkpoints()
        tasks = [task for task in [self._sweep_task, *self._worker_tasks.values()] if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            grace = self._timing_value("shutdown_cancel_grace_sec", 1.0)
            done, pending = await asyncio.wait(tasks, timeout=grace)
            for task in done:
                self._consume_stopped_task(task)
            for task in pending:
                task.add_done_callback(self._consume_stopped_task)
            for chat_id, worker in list(self._worker_tasks.items()):
                if worker in pending:
                    worker.add_done_callback(
                        lambda completed, key=chat_id, expected=worker: self._remove_stopped_worker(
                            key, expected, completed
                        )
                    )
                elif worker in done:
                    self._remove_stopped_worker(chat_id, worker, worker)
        if self._sweep_task is not None and self._sweep_task.done():
            self._sweep_task = None
        await self._observe_global("memory_pipeline", "pipeline_stopped", level="warning", summary="Memory pipeline stopped")

    def _remove_stopped_worker(
        self,
        chat_id: str,
        expected: asyncio.Task[Any],
        completed: asyncio.Task[Any],
    ) -> None:
        self._consume_stopped_task(completed)
        if self._worker_tasks.get(chat_id) is expected:
            self._worker_tasks.pop(chat_id, None)
            self._worker_queues.pop(chat_id, None)

    def begin_shutdown(self) -> None:
        """Fence new maintenance work while retaining durable buffers/checkpoints."""
        if not self._accepting:
            return
        self._accepting = False
        self._running = False
        self._shutdown_generation += 1

    def is_accepting_work(self) -> bool:
        return bool(self._accepting)

    def _timing_value(self, name: str, default: float) -> float:
        timing = getattr(self.config, "timing", None)
        try:
            return max(0.0, float(getattr(timing, name, default) or default))
        except (TypeError, ValueError):
            return default

    def _long_term_memory_cooldown(self) -> float:
        value = getattr(getattr(self.config, "memory", None), "long_term_memory_cooldown_sec", 7200)
        try:
            return max(60.0, float(value or 7200))
        except (TypeError, ValueError):
            return 7200.0

    @staticmethod
    def _consume_stopped_task(task: asyncio.Task[Any]) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    async def flush_pending_sessions(self) -> dict[str, dict[str, Any]]:
        chat_ids = [
            str(chat_id)
            for chat_id, session_data in list(self._session_history_buffer.items())
            if list((session_data or {}).get("buffer", []) or [])
        ]
        results: dict[str, dict[str, Any]] = {}
        for chat_id in chat_ids:
            try:
                results[chat_id] = await self.run_maintenance_for_session(chat_id, force=True)
            except Exception as exc:
                logger.warning(f"[MemoryTurnPipeline] shutdown flush degraded for {chat_id}: {exc}")
                results[chat_id] = {"performed": False, "reason": "flush_failed", "error": str(exc)}
        return results

    def _checkpoint_session(self, chat_id: str) -> dict[str, Any] | None:
        session_data = dict(self._session_history_buffer.get(chat_id) or {})
        in_flight = list(self._inflight_maintenance.get(chat_id) or [])
        in_flight_turn_ids = list(
            self._inflight_maintenance_turn_ids.get(chat_id) or []
        )
        buffered = list(session_data.get("buffer", []) or [])
        buffered_turn_ids = list(session_data.get("buffered_turn_ids", []) or [])
        combined = [*in_flight, *buffered]
        if not combined:
            return None
        return {
            "buffer": combined,
            "buffered_turn_ids": list(
                dict.fromkeys([*in_flight_turn_ids, *buffered_turn_ids])
            ),
            "last_update": float(session_data.get("last_update", time.time()) or time.time()),
            "cooldown_until": float(session_data.get("cooldown_until", 0.0) or 0.0),
            "failures": int(session_data.get("failures", 0) or 0),
            "last_run_at": float(session_data.get("last_run_at", 0.0) or 0.0),
        }

    async def _restore_checkpoints(self) -> None:
        store = self.checkpoint_store
        if store is None:
            return
        try:
            restored = await store.load_all()
        except Exception as exc:
            logger.warning(f"[MemoryTurnPipeline] checkpoint restore degraded: {exc}")
            return
        for chat_id, session_data in restored.items():
            if not list((session_data or {}).get("buffer", []) or []):
                continue
            self._session_history_buffer[str(chat_id)] = dict(session_data)
        if restored:
            await self._observe_global(
                "memory_pipeline",
                "checkpoint_restored",
                summary=f"restored_chats={len(restored)}",
                payload={"restored_chats": len(restored)},
            )

    async def _persist_checkpoint(self, chat_id: str) -> bool:
        store = self.checkpoint_store
        if store is None:
            return True
        snapshot = self._checkpoint_session(chat_id)
        try:
            if snapshot is None:
                await store.delete(chat_id)
            else:
                await store.upsert(chat_id, snapshot)
            return True
        except Exception as exc:
            logger.warning(f"[MemoryTurnPipeline] checkpoint persist degraded for {chat_id}: {exc}")
            return False

    async def _persist_all_checkpoints(self) -> None:
        store = self.checkpoint_store
        if store is None:
            return
        snapshots = {
            str(chat_id): snapshot
            for chat_id in set(self._session_history_buffer) | set(self._inflight_maintenance)
            if (snapshot := self._checkpoint_session(str(chat_id))) is not None
        }
        try:
            timeout = self._timing_value("shutdown_snapshot_timeout_sec", 0.5)
            await asyncio.wait_for(store.save_many(snapshots), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                f"[MemoryTurnPipeline] checkpoint snapshot timed out after {timeout:.3f}s; shutdown will continue"
            )
        except Exception as exc:
            logger.warning(f"[MemoryTurnPipeline] checkpoint snapshot degraded: {exc}")

    def build_turn(
        self,
        *,
        chat_id: str,
        user_text: str,
        assistant_text: str,
        sender_id: str = "",
        source: str,
        is_proactive: bool = False,
        think_level: int | None = None,
        persona_id: str = "",
    ) -> CommittedMemoryTurn:
        return CommittedMemoryTurn(
            turn_id=f"turn_{uuid.uuid4().hex[:16]}",
            chat_id=str(chat_id or ""),
            user_text=str(user_text or "").strip(),
            assistant_text=str(assistant_text or "").strip(),
            sender_id=str(sender_id or "").strip(),
            source=str(source or ""),
            is_proactive=bool(is_proactive),
            think_level=think_level if think_level is None else int(think_level),
            persona_id=str(persona_id or ""),
            committed_at=time.time(),
        )

    async def record_turn(self, turn: CommittedMemoryTurn) -> dict[str, Any]:
        if turn.is_proactive:
            await self._observe_turn(turn, "memory_pipeline", "proactive_ignored", level="warning", reason="proactive_ignored")
            return {"performed": False, "reason": "proactive_ignored", "pending_messages": 0}
        if not turn.user_text or not turn.assistant_text:
            await self._observe_turn(turn, "memory_pipeline", "turn_rejected", level="warning", reason="empty_turn")
            return {"performed": False, "reason": "empty_turn", "pending_messages": 0}
        turn_ledger = self.turn_ledger
        lease_token = ""
        if turn_ledger is not None:
            try:
                lease_token = await turn_ledger.claim(turn.turn_id, turn.chat_id)
            except Exception as exc:
                logger.warning(f"[MemoryTurnPipeline] turn ledger degraded: {exc}")
                turn_ledger = None
            if turn_ledger is not None and not lease_token:
                await self._observe_turn(
                    turn,
                    "memory_pipeline",
                    "turn_duplicate_skipped",
                    level="warning",
                    reason="turn_id_already_recorded",
                )
                return {"performed": False, "reason": "duplicate_turn", "pending_messages": 0}
        lock = self._get_memory_lock(turn.chat_id)
        try:
            async with lock:
                now = time.time()
                session_data = self._session_history_buffer.setdefault(
                    turn.chat_id,
                    {"buffer": [], "last_update": now, "cooldown_until": 0.0, "failures": 0, "last_run_at": 0.0},
                )
                buffer = list(session_data.get("buffer", []) or [])
                buffered_turn_ids = list(
                    session_data.get("buffered_turn_ids", []) or []
                )
                already_buffered = turn.turn_id in buffered_turn_ids
                if turn.user_text and not already_buffered:
                    # OPT-05/ML-10: 结构化存 sender——旧格式"用户/旁白：{text}"根本不带
                    # 发送者，摘要解析器只能全落 unknown，群记忆无法归属到人
                    buffer.append(
                        {
                            "sender": str(turn.sender_id or "").strip() or "旁白",
                            "text": str(turn.user_text),
                        }
                    )
                if turn.assistant_text and not already_buffered:
                    buffer.append(
                        {
                            "sender": "Bot",
                            "text": str(turn.assistant_text),
                        }
                    )
                if not already_buffered:
                    buffered_turn_ids.append(turn.turn_id)
                session_data["buffer"] = buffer
                session_data["buffered_turn_ids"] = buffered_turn_ids
                session_data["last_update"] = now
                pending_messages = len(buffer)
                if not await self._persist_checkpoint(turn.chat_id):
                    raise RuntimeError("memory turn checkpoint persist failed")
            if turn_ledger is not None:
                committed = await turn_ledger.mark_committed(
                    turn.turn_id,
                    lease_token=lease_token,
                )
                if not committed:
                    raise RuntimeError("memory turn ledger lease lost")
        except asyncio.CancelledError:
            if turn_ledger is not None:
                try:
                    await asyncio.shield(
                        turn_ledger.release_failed(
                            turn.turn_id,
                            "record_turn_cancelled",
                            lease_token=lease_token,
                        )
                    )
                except Exception:
                    pass
            raise
        except Exception as exc:
            if turn_ledger is not None:
                try:
                    await turn_ledger.release_failed(
                        turn.turn_id,
                        str(exc),
                        lease_token=lease_token,
                    )
                except Exception:
                    pass
            raise
        await self._observe_turn(
            turn,
            "memory_pipeline",
            "turn_recorded",
            summary=f"pending_messages={pending_messages}",
            payload={"pending_messages": pending_messages},
        )
        return {"performed": True, "reason": "recorded", "pending_messages": pending_messages}

    async def process_instant_gate(self, turn: CommittedMemoryTurn) -> InstantGateResult:
        result = await self.instant_gate.process_committed_turn(turn)
        turn.instant_gate_hit = bool(result.hit)
        turn.instant_memory_id = str(result.memory_id or "")
        return result

    async def publish_turn_committed(self, turn: CommittedMemoryTurn) -> None:
        await self.event_bus.publish_memory_turn_committed({"turn": turn})
        await self._observe_turn(
            turn,
            "memory_pipeline",
            "event_published",
            summary="memory.turn_committed published",
        )

    async def on_turn_committed(self, payload: dict[str, Any]) -> None:
        turn = payload.get("turn") if isinstance(payload, dict) else None
        if not isinstance(turn, CommittedMemoryTurn) or turn.is_proactive:
            return
        queue = self._worker_queues.get(turn.chat_id)
        if not self._accepting:
            if queue is None:
                self._started_after_shutdown += 1
            return
        existing = self._worker_tasks.get(turn.chat_id)
        if existing is not None and existing.done() and self._accepting:
            if self._worker_tasks.get(turn.chat_id) is existing:
                self._worker_tasks.pop(turn.chat_id, None)
            existing = None
        if queue is None or existing is None:
            if queue is None:
                queue = asyncio.Queue()
                self._worker_queues[turn.chat_id] = queue
            task = asyncio.create_task(self._chat_worker(turn.chat_id, queue))
            self._worker_tasks[turn.chat_id] = task
            self._background_tasks.add(task)
            self._register_owner_task(
                task,
                task_family="memory.turn.worker",
                scope_id=turn.chat_id,
                run_id=f"memory-worker-{turn.chat_id}-{uuid.uuid4().hex[:8]}",
            )
            task.add_done_callback(
                lambda completed, key=turn.chat_id, expected=task: self._handle_worker_result(
                    key, expected, completed
                )
            )
            await self._observe_turn(
                turn,
                "memory_pipeline",
                "worker_spawned",
                summary=f"worker spawned for {turn.chat_id}",
            )
        await queue.put(turn)

    async def describe_session_eligibility(self, chat_id: str) -> dict[str, Any]:
        threshold_messages = int(getattr(getattr(self.config, "memory", None), "summary_threshold", 30) or 30) * 2
        session_data = self._session_history_buffer.get(chat_id) or {}
        buffer = list(session_data.get("buffer", []) or [])
        pending_messages = len(buffer)
        cooldown_until = float(session_data.get("cooldown_until", 0.0) or 0.0)
        last_update = float(session_data.get("last_update", 0.0) or 0.0)
        now = time.time()
        last_run_at = float(session_data.get("last_run_at", 0.0) or 0.0)
        if last_run_at > 0.0:
            cooldown_until = max(cooldown_until, last_run_at + self._long_term_memory_cooldown())
        candidate_present = pending_messages > 0
        force_due = candidate_present and last_update > 0 and (now - last_update) >= self.TURN_FORCE_SUMMARIZE_AFTER_SECONDS
        eligible = candidate_present and now >= cooldown_until and (pending_messages >= threshold_messages or force_due)
        if not candidate_present:
            reason = "no_buffer"
        elif now < cooldown_until:
            reason = "cooldown"
        elif pending_messages >= threshold_messages:
            reason = "eligible"
        elif force_due:
            reason = "idle_timeout"
        else:
            reason = "below_threshold"
        return {
            "eligible": eligible,
            "candidate_present": candidate_present,
            "reason": reason,
            "pending_messages": pending_messages,
            "history_size": pending_messages,
            "threshold_messages": threshold_messages,
            "cooldown_until": cooldown_until,
            "last_memory_run_at": float(session_data.get("last_run_at", 0.0) or 0.0),
            "last_update": last_update,
        }

    def describe_chat_buffer(self, chat_id: str) -> dict[str, Any]:
        session_data = self._session_history_buffer.get(chat_id) or {}
        buffer = list(session_data.get("buffer", []) or [])
        return {
            "chat_id": str(chat_id or ""),
            "pending_messages": len(buffer),
            "last_update": float(session_data.get("last_update", 0.0) or 0.0),
            "cooldown_until": float(session_data.get("cooldown_until", 0.0) or 0.0),
            "failures": int(session_data.get("failures", 0) or 0),
            "last_memory_run_at": float(session_data.get("last_run_at", 0.0) or 0.0),
        }

    def describe_runtime_status(self) -> dict[str, Any]:
        active_workers = [
            chat_id
            for chat_id, task in self._worker_tasks.items()
            if task is not None and not task.done() and self._accepting
        ]
        stopping_workers = [
            chat_id
            for chat_id, task in self._worker_tasks.items()
            if task is not None and not task.done() and not self._accepting
        ]
        return {
            "running": bool(self._running),
            "accepting_work": bool(self._accepting),
            "shutdown_generation": int(self._shutdown_generation),
            "shutdown_rejected_count": int(self._shutdown_rejected_count),
            "started_after_shutdown": int(self._started_after_shutdown),
            "sweep_task_running": bool(self._sweep_task is not None and not self._sweep_task.done()),
            "buffered_chats": len([chat_id for chat_id, data in self._session_history_buffer.items() if (data or {}).get("buffer")]),
            "tracked_chats": len(self._session_history_buffer),
            "active_worker_count": len(active_workers),
            "stopping_worker_count": len(stopping_workers),
            "pending_worker_count": len(stopping_workers),
            "active_worker_chats": list(active_workers[:50]),
            "maintenance_concurrency": int(self._maintenance_limit),
            "active_maintenance": int(self._active_maintenance),
            "maintenance_available_slots": max(0, int(getattr(self._maintenance_semaphore, "_value", 0) or 0)),
        }

    def is_worker_active(self, chat_id: str) -> bool:
        task = self._worker_tasks.get(str(chat_id or ""))
        return bool(task is not None and not task.done())

    async def run_maintenance_for_session(self, chat_id: str, *, force: bool = False) -> dict[str, Any]:
        threshold = int(getattr(getattr(self.config, "memory", None), "summary_threshold", 30) or 30)
        now = time.time()
        lock = self._get_memory_lock(chat_id)
        shutdown_rejected_pending = 0
        task_lease = None
        async with lock:
            session_data = self._session_history_buffer.setdefault(
                chat_id,
                {"buffer": [], "last_update": now, "cooldown_until": 0.0, "failures": 0, "last_run_at": 0.0},
            )
            buffer = list(session_data.get("buffer", []) or [])
            cooldown_until = float(session_data.get("cooldown_until", 0.0) or 0.0)
            last_update = float(session_data.get("last_update", 0.0) or 0.0)
            if not buffer:
                return {"performed": False, "reason": "no_buffer", "pending_messages": 0}
            if not self._accepting:
                self._shutdown_rejected_count += 1
                shutdown_rejected_pending = len(buffer)
            if not shutdown_rejected_pending:
                last_run_at = float(session_data.get("last_run_at", 0.0) or 0.0)
                hard_cooldown_until = last_run_at + self._long_term_memory_cooldown() if last_run_at > 0.0 else 0.0
                # ``force`` only bypasses the message-count threshold.  The
                # long-term memory cooldown is a hard LLM admission limit and
                # must hold for manual and shutdown-triggered calls alike.
                if now < max(cooldown_until, hard_cooldown_until):
                    return {"performed": False, "reason": "cooldown", "pending_messages": len(buffer), "cooldown_until": max(cooldown_until, hard_cooldown_until)}
                force_due = last_update > 0 and (now - last_update) >= self.TURN_FORCE_SUMMARIZE_AFTER_SECONDS
                if len(buffer) < threshold * 2 and not force_due and not force:
                    await self._observe_chat(
                        chat_id,
                        "memory_pipeline",
                        "maintenance_skipped",
                        reason="below_threshold",
                        summary=f"pending_messages={len(buffer)} threshold={threshold * 2}",
                    )
                    return {
                        "performed": False,
                        "reason": "below_threshold",
                        "pending_messages": len(buffer),
                        "threshold_messages": threshold * 2,
                    }
                if self.task_ledger is not None:
                    fingerprint_payload = json.dumps(
                        buffer,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    task_lease = await self.task_ledger.claim(
                        task_family="long_term_memory",
                        scope_id=str(chat_id),
                        input_fingerprint=hashlib.sha256(
                            fingerprint_payload.encode("utf-8")
                        ).hexdigest()[:32],
                        lease_seconds=1800.0,
                        min_interval_seconds=self._long_term_memory_cooldown(),
                        checkpoint_before={
                            "pending_messages": len(buffer),
                            "last_run_at": last_run_at,
                        },
                        payload={"force": bool(force)},
                    )
                    if task_lease is None:
                        return {
                            "performed": False,
                            "reason": "lease_busy_or_cooldown",
                            "pending_messages": len(buffer),
                        }
                messages_to_process = buffer.copy()
                turn_ids_to_process = list(
                    session_data.get("buffered_turn_ids", []) or []
                )
                session_data["buffer"] = []
                session_data["buffered_turn_ids"] = []
                self._inflight_maintenance[chat_id] = messages_to_process
                self._inflight_maintenance_turn_ids[chat_id] = turn_ids_to_process

        if shutdown_rejected_pending:
            await self._observe_chat(
                chat_id,
                "memory_pipeline",
                "maintenance_rejected",
                level="warning",
                reason="shutdown_rejected",
                summary="maintenance admission fenced during shutdown",
                payload={"pending_messages": shutdown_rejected_pending, "shutdown_generation": self._shutdown_generation},
            )
            return {"performed": False, "reason": "shutdown_rejected", "pending_messages": shutdown_rejected_pending}

        await self._persist_checkpoint(chat_id)

        history_text = "\n".join(
            self._render_buffer_line(index + 1, item) for index, item in enumerate(messages_to_process)
        )
        try:
            await self._observe_chat(
                chat_id,
                "memory_pipeline",
                "maintenance_started",
                summary=f"processing {len(messages_to_process)} messages",
            )
            await self._maintenance_semaphore.acquire()
            self._active_maintenance += 1
            try:
                if not self._accepting:
                    raise BackgroundTaskQueueFull("memory maintenance admission fenced during shutdown")
                if self.background_task_budget is not None:
                    await self.background_task_budget.run(
                        lambda: self.session_summarizer.summarize_session(
                            session_id=chat_id,
                            chat_history_text=history_text,
                        ),
                        task_name="memory_maintenance",
                        scope_id=chat_id,
                        defer_release_on_timeout=True,
                    )
                else:
                    await self.session_summarizer.summarize_session(
                        session_id=chat_id,
                        chat_history_text=history_text,
                    )
            finally:
                self._active_maintenance = max(0, self._active_maintenance - 1)
                self._maintenance_semaphore.release()
            completed_at = time.time()
            async with lock:
                current_data = self._session_history_buffer.setdefault(
                    chat_id,
                    {"buffer": [], "last_update": completed_at, "cooldown_until": 0.0, "failures": 0, "last_run_at": 0.0},
                )
                current_data["failures"] = 0
                current_data["cooldown_until"] = 0.0
                current_data["last_run_at"] = completed_at
                current_data["last_update"] = completed_at
                self._inflight_maintenance.pop(chat_id, None)
                self._inflight_maintenance_turn_ids.pop(chat_id, None)
            await self._persist_checkpoint(chat_id)
            await self._observe_chat(
                chat_id,
                "memory_pipeline",
                "maintenance_summarized",
                summary=f"summarized {len(messages_to_process)} messages",
            )
            if task_lease is not None:
                await self.task_ledger.finish(
                    task_lease,
                    status="succeeded",
                    checkpoint_after={
                        "pending_messages_processed": len(messages_to_process),
                        "last_run_at": completed_at,
                    },
                    llm_call_count=1,
                )
            return {
                "performed": True,
                "reason": "summarized",
                "pending_messages_processed": len(messages_to_process),
                "last_memory_run_at": completed_at,
            }
        except (BackgroundTaskQueueFull, BackgroundTaskQueueTimeout) as exc:
            async with lock:
                current_data = self._session_history_buffer.setdefault(
                    chat_id,
                    {"buffer": [], "last_update": time.time(), "cooldown_until": 0.0, "failures": 0, "last_run_at": 0.0},
                )
                current_data["buffer"] = messages_to_process + list(current_data.get("buffer", []) or [])
                current_data["buffered_turn_ids"] = list(
                    dict.fromkeys(
                        turn_ids_to_process
                        + list(current_data.get("buffered_turn_ids", []) or [])
                    )
                )
                current_data["last_update"] = time.time()
                self._inflight_maintenance.pop(chat_id, None)
                self._inflight_maintenance_turn_ids.pop(chat_id, None)
            await self._persist_checkpoint(chat_id)
            if not self._accepting or "draining" in str(exc).lower() or "fenced" in str(exc).lower():
                self._shutdown_rejected_count += 1
                await self._observe_chat(
                    chat_id,
                    "memory_pipeline",
                    "maintenance_rejected",
                    level="warning",
                    reason="shutdown_rejected",
                    summary="maintenance budget rejected shutdown admission",
                    payload={"pending_messages": len(messages_to_process), "shutdown_generation": self._shutdown_generation},
                )
                if task_lease is not None:
                    await self.task_ledger.finish(
                        task_lease,
                        status="retry_wait",
                        checkpoint_after={"pending_messages_restored": len(messages_to_process)},
                        error="shutdown_rejected",
                        retry_after_seconds=0.0,
                    )
                return {
                    "performed": False,
                    "reason": "shutdown_rejected",
                    "pending_messages": len(current_data.get("buffer", []) or []),
                }
            if task_lease is not None:
                await self.task_ledger.finish(
                    task_lease,
                    status="retry_wait",
                    checkpoint_after={"pending_messages_restored": len(messages_to_process)},
                    error=str(exc),
                    retry_after_seconds=300.0,
                )
            return {
                "performed": False,
                "reason": "budget_retry_wait",
                "pending_messages": len(current_data.get("buffer", []) or []),
                "retry_after_seconds": 300.0,
            }
        except asyncio.CancelledError:
            async with lock:
                current_data = self._session_history_buffer.setdefault(
                    chat_id,
                    {"buffer": [], "last_update": time.time(), "cooldown_until": 0.0, "failures": 0, "last_run_at": 0.0},
                )
                current_data["buffer"] = messages_to_process + list(current_data.get("buffer", []) or [])
                current_data["buffered_turn_ids"] = list(
                    dict.fromkeys(
                        turn_ids_to_process
                        + list(current_data.get("buffered_turn_ids", []) or [])
                    )
                )
                current_data["last_update"] = time.time()
                self._inflight_maintenance.pop(chat_id, None)
                self._inflight_maintenance_turn_ids.pop(chat_id, None)
            await self._persist_checkpoint(chat_id)
            await self._observe_chat(chat_id, "memory_pipeline", "maintenance_cancelled", level="warning", reason="cancelled")
            if task_lease is not None:
                await self.task_ledger.finish(
                    task_lease,
                    status="retry_wait",
                    checkpoint_after={"pending_messages_restored": len(messages_to_process)},
                    error="cancelled",
                    retry_after_seconds=0.0,
                )
            raise
        except Exception as exc:
            logger.error(f"[AstrMai-Memory] memory maintenance degraded for {chat_id}: {exc}")
            async with lock:
                current_data = self._session_history_buffer.setdefault(
                    chat_id,
                    {"buffer": [], "last_update": time.time(), "cooldown_until": 0.0, "failures": 0, "last_run_at": 0.0},
                )
                merged_buffer = messages_to_process + list(current_data.get("buffer", []) or [])
                max_capacity = threshold * 3
                if len(merged_buffer) > max_capacity:
                    merged_buffer = merged_buffer[-max_capacity:]
                failures = int(current_data.get("failures", 0) or 0) + 1
                retry_after_seconds = min(3600, 300 * (2 ** (failures - 1)))
                cooldown_until = time.time() + retry_after_seconds
                current_data["buffer"] = merged_buffer
                current_data["buffered_turn_ids"] = list(
                    dict.fromkeys(
                        turn_ids_to_process
                        + list(current_data.get("buffered_turn_ids", []) or [])
                    )
                )
                current_data["last_update"] = time.time()
                current_data["failures"] = failures
                current_data["cooldown_until"] = cooldown_until
                self._inflight_maintenance.pop(chat_id, None)
                self._inflight_maintenance_turn_ids.pop(chat_id, None)
            await self._persist_checkpoint(chat_id)
            await self._observe_chat(
                chat_id,
                "memory_pipeline",
                "maintenance_rolled_back",
                level="error",
                reason="summary_failed",
                summary=str(exc),
                payload={"restored_messages": len(merged_buffer), "cooldown_until": cooldown_until},
            )
            if task_lease is not None:
                await self.task_ledger.finish(
                    task_lease,
                    status="retry_wait",
                    checkpoint_after={"pending_messages_restored": len(merged_buffer)},
                    llm_call_count=1,
                    error=str(exc),
                    retry_after_seconds=retry_after_seconds,
                )
            return {
                "performed": False,
                "reason": "summary_failed",
                "pending_messages_restored": len(merged_buffer),
                "cooldown_until": cooldown_until,
            }

    async def extract_and_summarize_history(self, session_id: str, days: int = 1):
        return await self.session_summarizer.extract_and_summarize_history(session_id, days=days)

    @staticmethod
    def _render_buffer_line(index: int, entry: Any) -> str:
        # OPT-05/ML-10: 渲染成摘要解析器已认识的 "[序号] 发送者: 内容" 格式
        # （session_memory_summarizer._build_topic_messages 的正则），speaker_ids
        # 得以落到具体 QQ 号；字符串条目为热更前旧数据，原样透传
        if isinstance(entry, dict):
            sender = str(entry.get("sender") or "旁白").strip() or "旁白"
            text = str(entry.get("text") or "")
            return f"[{index}] {sender}: {text}"
        return str(entry)

    async def _chat_worker(self, chat_id: str, queue: asyncio.Queue[CommittedMemoryTurn]) -> None:
        # OPT-02/RT-01: per-chat worker 在某轮 turn 上下文中懒创建，必须斩断继承的
        # telemetry contextvar，否则 instant backfill 等 LLM 调用被陈旧 deadline 钳死
        # （线上实证 17/17 全败 turn_deadline_exhausted）
        detach_turn_telemetry()
        while self._running:
            try:
                turn = await queue.get()
                try:
                    await self._observe_turn(turn, "memory_pipeline", "worker_consumed", summary="worker consumed event")
                    if not turn.instant_gate_hit:
                        await self._maybe_run_llm_backfill(turn)
                    else:
                        await self._observe_turn(turn, "memory_pipeline", "backfill_skipped", reason="instant_gate_hit")
                finally:
                    queue.task_done()
                if queue.empty() and not self._running:
                    break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[MemoryTurnPipeline] worker degraded for {chat_id}: {exc}")
                await self._observe_chat(chat_id, "memory_pipeline", "worker_failed", level="error", reason="worker_failed", summary=str(exc))

    async def _maybe_run_llm_backfill(self, turn: CommittedMemoryTurn) -> None:
        session_data = self._session_history_buffer.get(turn.chat_id) or {}
        session_rounds = len(list(session_data.get("buffer", []) or [])) // 2
        now = asyncio.get_running_loop().time()
        last_check = float(self._instant_llm_last_check.get(turn.chat_id, 0.0) or 0.0)
        if not self.instant_gate.should_run_llm_backfill(turn, session_rounds=session_rounds, last_check=last_check, now=now):
            await self._observe_turn(
                turn,
                "memory_pipeline",
                "backfill_skipped",
                reason="eligibility_false",
                payload={"session_rounds": session_rounds},
            )
            return
        self._instant_llm_last_check[turn.chat_id] = now
        await self._observe_turn(turn, "memory_pipeline", "backfill_started", summary="llm backfill started")
        try:
            if self.background_task_budget is not None:
                await self.background_task_budget.run(
                    lambda: self.instant_gate.run_llm_backfill(turn),
                    task_name="memory_instant_backfill",
                    scope_id=turn.chat_id,
                    defer_release_on_timeout=True,
                )
            else:
                await self.instant_gate.run_llm_backfill(turn)
        except (BackgroundTaskQueueFull, BackgroundTaskQueueTimeout, BackgroundTaskExecutionTimeout) as exc:
            await self._observe_turn(
                turn,
                "memory_pipeline",
                "backfill_skipped",
                level="warning",
                reason=type(exc).__name__,
                summary="instant backfill admission unavailable",
            )
            return
        await self._observe_turn(turn, "memory_pipeline", "backfill_finished", summary="llm backfill finished")

    async def _sweep_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.TURN_IDLE_SWEEP_SECONDS)
                active_sessions = list(self._session_history_buffer.keys())
                for chat_id in active_sessions:
                    payload = await self.describe_session_eligibility(chat_id)
                    if not bool(payload.get("eligible")):
                        continue
                    if str(payload.get("reason", "")) == "idle_timeout":
                        await self._observe_chat(chat_id, "memory_pipeline", "idle_timeout", level="warning", reason="idle_timeout")
                    await self.run_maintenance_for_session(chat_id)
                # ponytail: prune inactive dict entries
                now = time.time()
                stale_cutoff = now - 1800
                for chat_id in list(self._session_history_buffer.keys()):
                    buf = self._session_history_buffer.get(chat_id)
                    last_update = buf.get("last_update", 0) if isinstance(buf, dict) else 0
                    if last_update < stale_cutoff:
                        await self._persist_checkpoint(chat_id)
                        self._session_history_buffer.pop(chat_id, None)
                        self._memory_locks.pop(chat_id, None)
                        stale_task = self._worker_tasks.pop(chat_id, None)
                        if stale_task is not None and not stale_task.done():
                            stale_task.cancel()
                        self._worker_queues.pop(chat_id, None)
                        self._instant_llm_last_check.pop(chat_id, None)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[MemoryTurnPipeline] sweep degraded: {exc}")
                await self._observe_global("memory_pipeline", "sweep_failed", level="error", reason="sweep_failed", summary=str(exc))

    def _get_memory_lock(self, chat_id: str) -> asyncio.Lock:
        lock = self._memory_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._memory_locks[chat_id] = lock
        return lock

    def _handle_task_result(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
        try:
            exc = task.exception()
            if exc:
                logger.error(f"[MemoryTurnPipeline] background task exception: {exc}", exc_info=exc)
        except asyncio.CancelledError:
            pass

    def _handle_worker_result(
        self,
        chat_id: str,
        expected: asyncio.Task[Any],
        task: asyncio.Task[Any],
    ) -> None:
        self._handle_task_result(task)
        if self._worker_tasks.get(chat_id) is not expected:
            return
        self._worker_tasks.pop(chat_id, None)
        queue = self._worker_queues.get(chat_id)
        if self._accepting and queue is not None and not queue.empty():
            replacement = asyncio.create_task(self._chat_worker(chat_id, queue))
            self._worker_tasks[chat_id] = replacement
            self._background_tasks.add(replacement)
            self._register_owner_task(
                replacement,
                task_family="memory.turn.worker",
                scope_id=chat_id,
                run_id=f"memory-worker-{chat_id}-{uuid.uuid4().hex[:8]}",
            )
            replacement.add_done_callback(
                lambda completed, key=chat_id, owner=replacement: self._handle_worker_result(
                    key, owner, completed
                )
            )

    async def _observe_turn(
        self,
        turn: CommittedMemoryTurn,
        component: str,
        stage: str,
        *,
        level: str = "info",
        reason: str = "",
        summary: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        observer = self.observer
        if observer is None or not hasattr(observer, "record"):
            return
        try:
            await observer.record(
                chat_id=turn.chat_id,
                component=component,
                stage=stage,
                level=level,
                turn_id=turn.turn_id,
                memory_id=turn.instant_memory_id,
                reason=reason,
                summary=summary,
                payload=payload or {},
            )
        except Exception:
            logger.debug("[AstrMai-mem-pipeline] observe_turn failed", exc_info=True)
            return

    async def _observe_chat(
        self,
        chat_id: str,
        component: str,
        stage: str,
        *,
        level: str = "info",
        reason: str = "",
        summary: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        observer = self.observer
        if observer is None or not hasattr(observer, "record"):
            return
        try:
            await observer.record(
                chat_id=str(chat_id or ""),
                component=component,
                stage=stage,
                level=level,
                reason=reason,
                summary=summary,
                payload=payload or {},
            )
        except Exception:
            logger.debug("[AstrMai-mem-pipeline] observe_chat failed", exc_info=True)
            return

    async def _observe_global(
        self,
        component: str,
        stage: str,
        *,
        level: str = "info",
        reason: str = "",
        summary: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self._observe_chat("__memory_global__", component, stage, level=level, reason=reason, summary=summary, payload=payload)


__all__ = ["MemoryTurnPipeline"]
