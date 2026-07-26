from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from astrbot.api import logger

from ...infrastructure.runtime.event_bus import EventBus
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
    ) -> None:
        self.context = context
        self.gateway = gateway
        self.engine = engine
        self.session_summarizer = session_summarizer
        self.instant_gate = instant_gate
        self.event_bus = event_bus or EventBus()
        self.config = config if config is not None else getattr(gateway, "config", None)
        self.observer = observer
        self._session_history_buffer: dict[str, dict[str, Any]] = {}
        self._memory_locks: dict[str, asyncio.Lock] = {}
        self._worker_tasks: dict[str, asyncio.Task[Any]] = {}
        self._worker_queues: dict[str, asyncio.Queue[CommittedMemoryTurn]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._instant_llm_last_check: dict[str, float] = {}
        self._running = False
        self._sweep_task: asyncio.Task[Any] | None = None

    def refresh_config(self, config: Any) -> None:
        self.config = config
        for component in (self.session_summarizer, self.instant_gate):
            refresh = getattr(component, "refresh_config", None)
            if callable(refresh):
                refresh(config)
            elif component is not None:
                component.config = config

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.event_bus.subscribe(self.event_bus.TOPIC_MEMORY_TURN_COMMITTED, self.on_turn_committed)
        self._sweep_task = asyncio.create_task(self._sweep_loop())
        self._background_tasks.add(self._sweep_task)
        self._sweep_task.add_done_callback(self._handle_task_result)
        await self._observe_global("memory_pipeline", "pipeline_started", summary="Memory pipeline started")

    async def stop(self) -> None:
        self.event_bus.unsubscribe(self.event_bus.TOPIC_MEMORY_TURN_COMMITTED, self.on_turn_committed)
        await self.flush_pending_sessions()
        self._running = False
        tasks = [task for task in [self._sweep_task, *self._worker_tasks.values()] if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._worker_tasks.clear()
        self._worker_queues.clear()
        self._background_tasks.clear()
        self._sweep_task = None
        await self._observe_global("memory_pipeline", "pipeline_stopped", level="warning", summary="Memory pipeline stopped")

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
        lock = self._get_memory_lock(turn.chat_id)
        async with lock:
            now = time.time()
            session_data = self._session_history_buffer.setdefault(
                turn.chat_id,
                {"buffer": [], "last_update": now, "cooldown_until": 0.0, "failures": 0, "last_run_at": 0.0},
            )
            if turn.user_text:
                # OPT-05/ML-10: 结构化存 sender——旧格式"用户/旁白：{text}"根本不带
                # 发送者，摘要解析器只能全落 unknown，群记忆无法归属到人
                session_data["buffer"].append(
                    {"sender": str(turn.sender_id or "").strip() or "旁白", "text": str(turn.user_text)}
                )
            if turn.assistant_text:
                session_data["buffer"].append({"sender": "Bot", "text": str(turn.assistant_text)})
            session_data["last_update"] = now
            pending_messages = len(session_data["buffer"])
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
        if queue is None:
            queue = asyncio.Queue()
            self._worker_queues[turn.chat_id] = queue
            task = asyncio.create_task(self._chat_worker(turn.chat_id, queue))
            self._worker_tasks[turn.chat_id] = task
            self._background_tasks.add(task)
            task.add_done_callback(self._handle_task_result)
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
        active_workers = [chat_id for chat_id, task in self._worker_tasks.items() if task is not None and not task.done()]
        return {
            "running": bool(self._running),
            "sweep_task_running": bool(self._sweep_task is not None and not self._sweep_task.done()),
            "buffered_chats": len([chat_id for chat_id, data in self._session_history_buffer.items() if (data or {}).get("buffer")]),
            "tracked_chats": len(self._session_history_buffer),
            "active_worker_count": len(active_workers),
            "active_worker_chats": list(active_workers[:50]),
        }

    def is_worker_active(self, chat_id: str) -> bool:
        task = self._worker_tasks.get(str(chat_id or ""))
        return bool(task is not None and not task.done())

    async def run_maintenance_for_session(self, chat_id: str, *, force: bool = False) -> dict[str, Any]:
        threshold = int(getattr(getattr(self.config, "memory", None), "summary_threshold", 30) or 30)
        now = time.time()
        lock = self._get_memory_lock(chat_id)
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
            if now < cooldown_until and not force:
                return {"performed": False, "reason": "cooldown", "pending_messages": len(buffer), "cooldown_until": cooldown_until}
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
            messages_to_process = buffer.copy()
            session_data["buffer"] = []

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
            await self.session_summarizer.summarize_session(session_id=chat_id, chat_history_text=history_text)
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
            await self._observe_chat(
                chat_id,
                "memory_pipeline",
                "maintenance_summarized",
                summary=f"summarized {len(messages_to_process)} messages",
            )
            return {
                "performed": True,
                "reason": "summarized",
                "pending_messages_processed": len(messages_to_process),
                "last_memory_run_at": completed_at,
            }
        except asyncio.CancelledError:
            async with lock:
                current_data = self._session_history_buffer.setdefault(
                    chat_id,
                    {"buffer": [], "last_update": time.time(), "cooldown_until": 0.0, "failures": 0, "last_run_at": 0.0},
                )
                current_data["buffer"] = messages_to_process + list(current_data.get("buffer", []) or [])
                current_data["last_update"] = time.time()
            await self._observe_chat(chat_id, "memory_pipeline", "maintenance_cancelled", level="warning", reason="cancelled")
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
                cooldown_until = time.time() + min(3600, 300 * (2 ** (failures - 1)))
                current_data["buffer"] = merged_buffer
                current_data["last_update"] = time.time()
                current_data["failures"] = failures
                current_data["cooldown_until"] = cooldown_until
            await self._observe_chat(
                chat_id,
                "memory_pipeline",
                "maintenance_rolled_back",
                level="error",
                reason="summary_failed",
                summary=str(exc),
                payload={"restored_messages": len(merged_buffer), "cooldown_until": cooldown_until},
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
        await self.instant_gate.run_llm_backfill(turn)
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
