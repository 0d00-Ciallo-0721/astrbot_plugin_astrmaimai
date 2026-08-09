from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

from astrbot.api import logger

from ..multimodal import init_meme_storage
from ..shared.helpers.plugin_helpers import cleanup_stale_focus_pools, collect_background_tasks, safe_create_task
from .runtime_context import PluginRuntimeContext


class PluginLifecycleManager:
    SHUTDOWN_TASK_TIMEOUT: float = 8.0

    def __init__(self, runtime: PluginRuntimeContext):
        self.runtime = runtime
        self._background_tasks = runtime.background_tasks
        self._startup_task: asyncio.Task[Any] | None = None
        self._shutdown_requested = False
        self._terminated = False
        self._terminate_lock = asyncio.Lock()
        self._termination_complete = False
        self._isolated_shutdown_tasks: set[asyncio.Task[Any]] = set()
        self.runtime.lifecycle.manager = self

    def track_task(self, coro: Any) -> asyncio.Task[Any]:
        # ponytail: prune done tasks to prevent unbounded set growth
        done_tasks = {t for t in self._background_tasks if t.done()}
        self._background_tasks -= done_tasks
        task = safe_create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_task_result)
        return task

    def _handle_task_result(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
        try:
            exc = task.exception()
            if exc:
                logger.error(f"[AstrMai-Background] 后台任务异常: {exc}", exc_info=exc)
        except asyncio.CancelledError:
            logger.debug(f"[AstrMai-Background] task cancelled: {task.get_name()}")

    async def initialize_memory(self) -> None:
        self.runtime.set_boot_phase("lifecycle.memory")
        try:
            await self.runtime.memory_engine.initialize()
            await self.runtime.memory_engine.start_background_tasks()
            self.runtime.status.memory_initialized = True
        except Exception as exc:
            self.runtime.mark_degraded("memory.engine", str(exc))
            logger.warning(f"[AstrMai] Memory engine start degraded: {exc}")

    # G4/PL-10: 只有"显式重新初始化插件实例"才允许复位终止闩锁。
    # 两个场景有真实张力，必须按来源区分：
    #   plugin_initialize —— 面板禁用→启用复用同一实例，旧实现会让插件静默死到
    #                        进程重启（PL-10 要修的就是它）
    #   astrbot_loaded / 无来源 —— shutdown 期间迟到的框架 hook，绝不能复活插件
    #                        （既有 test_terminated_lifecycle_cannot_be_restarted_by_late_hook 守护）
    _LATCH_RESET_SOURCES = frozenset({"plugin_initialize"})

    async def on_program_start(self, *, source: str = "") -> None:
        if self._terminated:
            if str(source or "").strip() not in self._LATCH_RESET_SOURCES:
                logger.warning(
                    f"[AstrMai] runtime startup rejected reason=terminated source={source or 'unknown'}"
                )
                return
            logger.info("[AstrMai] runtime re-initialized after terminate; resetting shutdown latch")
            await self._prepare_reinitialize()
            self._terminated = False
            self._termination_complete = False
        if self.runtime.status.is_running and self.runtime.status.lifecycle_started:
            logger.debug("[AstrMai] runtime startup skipped reason=already_running")
            return
        if self._startup_task is not None and not self._startup_task.done():
            logger.debug("[AstrMai] runtime startup skipped reason=in_progress")
            return
        logger.info("[AstrMai] Starting system initialization from refactoring workspace...")
        self._shutdown_requested = False
        event_bus = getattr(self.runtime, "event_bus", None)
        reset_abort = getattr(event_bus, "reset_abort", None)
        if callable(reset_abort):
            reset_abort()
        self.runtime.status.accepting_events = False
        self.runtime.status.is_running = False
        self.runtime.status.lifecycle_started = False
        self.runtime.status.persona_state = "pending"
        self.runtime.set_boot_phase("lifecycle.starting")
        self._startup_task = self.track_task(self._complete_startup())

    async def _prepare_reinitialize(self) -> None:
        event_bus = getattr(self.runtime, "event_bus", None)
        reset_abort = getattr(event_bus, "reset_abort", None)
        if callable(reset_abort):
            reset_abort()

        coordinator = getattr(self.runtime, "runtime_coordinator", None)
        reopen_coordinator = getattr(coordinator, "reopen", None)
        if callable(reopen_coordinator):
            await reopen_coordinator()

        persona_summarizer = getattr(self.runtime, "persona_summarizer", None)
        reopen_persona = getattr(persona_summarizer, "reopen", None)
        if callable(reopen_persona):
            reopen_persona()

        cron_guard = getattr(self.runtime, "cron_guard", None)
        start_cron_guard = getattr(cron_guard, "start", None)
        if callable(start_cron_guard):
            start_cron_guard()

        self._bind_learning_collaboration()

    def _bind_learning_collaboration(self) -> None:
        event_bus = getattr(self.runtime, "event_bus", None)
        state_engine = getattr(self.runtime, "state_engine", None)
        memory_engine = getattr(self.runtime, "memory_engine", None)
        if event_bus is None or state_engine is None or memory_engine is None:
            return
        bindings = (
            (
                event_bus.TOPIC_LEARNING_MESSAGE_RECORDED,
                getattr(state_engine, "on_learning_message_recorded", None),
            ),
            (
                event_bus.TOPIC_LEARNING_BOT_REPLY_RECORDED,
                getattr(memory_engine, "on_learning_bot_reply_recorded", None),
            ),
            (
                event_bus.TOPIC_LEARNING_MINING_COMPLETED,
                getattr(memory_engine, "on_learning_mining_completed", None),
            ),
        )
        for topic, callback in bindings:
            if callable(callback):
                event_bus.subscribe(topic, callback)

    def _persona_retry_bounds(self) -> tuple[float, float]:
        persona_config = getattr(self.runtime.config, "persona", None)
        initial = max(0.1, float(getattr(persona_config, "retry_interval_sec", 15.0) or 15.0))
        maximum = max(initial, float(getattr(persona_config, "retry_max_interval_sec", 300.0) or 300.0))
        return initial, maximum

    async def _restore_dialogue_snapshot(self) -> None:
        # G4/PL-09: 重载后恢复群对话热/温区（TTL 与 schema 版本双重约束在 store 内部）
        store = getattr(self.runtime, "dialogue_store", None)
        restore = getattr(store, "restore_snapshot", None) if store is not None else None
        if not callable(restore):
            return
        try:
            await restore()
        except Exception as exc:
            logger.warning(f"[AstrMai] dialogue snapshot restore degraded: {exc}")

    async def _persist_dialogue_snapshot(self) -> None:
        store = getattr(self.runtime, "dialogue_store", None)
        persist = getattr(store, "persist_snapshot", None) if store is not None else None
        if not callable(persist):
            return
        try:
            await persist()
        except Exception as exc:
            logger.warning(f"[AstrMai] dialogue snapshot persist degraded: {exc}")

    async def _complete_startup(self) -> None:
        logger.info("[AstrMai] Initializing Memory Engine...")
        await self._restore_dialogue_snapshot()
        await self.initialize_memory()
        if self._shutdown_requested:
            return
        logger.info("[AstrMai] boot phase: memory initialization completed")

        await self._initialize_persona_core_until_ready()
        if self._shutdown_requested:
            return
        logger.info("[AstrMai] boot phase: persona core ready and persisted")

        init_meme_storage()
        await self.load_command_metadata()
        logger.info("[AstrMai] boot phase: commands loaded")

        await self._wire_private_chat_plugin()
        await self.start_expression_governance_services()
        await self.start_proactive_services()
        logger.info("[AstrMai] boot phase: proactive services started")

        await self.start_visual_services()
        logger.info("[AstrMai] boot phase: visual services started")

        self.start_background_services()
        self.runtime.status.is_running = True
        await self.start_workmode_guard()
        logger.info("[AstrMai] boot phase: workmode guard started")

        self.runtime.status.lifecycle_started = True
        self.runtime.status.accepting_events = True
        self.runtime.set_boot_phase("runtime.running")
        logger.info("[AstrMai] boot complete — runtime running")

    async def _initialize_persona_core_until_ready(self) -> None:
        summarizer = getattr(self.runtime, "persona_summarizer", None)
        context_engine = getattr(self.runtime, "context_engine", None)
        if summarizer is None or context_engine is None:
            raise RuntimeError("persona runtime is unavailable")
        persona_id, raw_prompt = context_engine.resolve_active_persona()
        cache_key = summarizer._cache_key(persona_id, "global")
        self.runtime.status.persona_cache_key = cache_key
        initial_delay, max_delay = self._persona_retry_bounds()
        delay = initial_delay
        while not self._shutdown_requested:
            self.runtime.status.persona_state = "core_initializing"
            self.runtime.status.persona_last_error = ""
            self.runtime.set_boot_phase("lifecycle.persona_core")
            try:
                payload = await summarizer.ensure_core_ready(
                    raw_prompt,
                    persona_id=persona_id,
                    session_id="global",
                )
                self.runtime.status.persona_state = "core_ready"
                self.runtime.status.persona_persisted = True
                self.runtime.status.persona_self_lore_ready = bool(payload.get("self_lore_ready", False))
                shards = payload.get("shard_status", {})
                self.runtime.status.persona_completed_shards = sum(
                    1 for name in summarizer.REQUIRED_SHARDS if shards.get(name) == "completed"
                )
                enrichment_task = summarizer.pending_tasks.get(cache_key)
                if not payload.get("is_full_ready", False) and enrichment_task is None:
                    enrichment_task = summarizer._start_shard_task(raw_prompt, cache_key)
                if enrichment_task is not None:
                    self.track_task(self._monitor_persona_enrichment(cache_key, enrichment_task))
                elif payload.get("is_full_ready", False):
                    self.runtime.status.persona_state = "full_ready"
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.runtime.status.persona_state = "core_failed"
                self.runtime.status.persona_persisted = False
                self.runtime.status.persona_last_error = str(exc)
                self.runtime.mark_degraded("persona.core", str(exc))
                logger.warning(
                    f"[AstrMai] persona core initialization failed; retrying in {delay:.1f}s: {exc}"
                )
                await asyncio.sleep(delay)
                delay = min(max_delay, delay * 2)

    async def _monitor_persona_enrichment(self, cache_key: str, task: asyncio.Task[Any]) -> None:
        self.runtime.status.persona_state = "enriching"
        try:
            while not task.done() and not self._shutdown_requested:
                payload = self.runtime.persona_summarizer.cache.get(cache_key, {})
                shard_status = payload.get("shard_status", {}) if isinstance(payload, dict) else {}
                self.runtime.status.persona_completed_shards = sum(
                    1
                    for name in self.runtime.persona_summarizer.REQUIRED_SHARDS
                    if shard_status.get(name) == "completed"
                )
                self.runtime.status.persona_self_lore_ready = bool(payload.get("self_lore_ready", False))
                await asyncio.sleep(2)
            await asyncio.gather(task)
            payload = self.runtime.persona_summarizer.cache.get(cache_key, {})
            shard_status = payload.get("shard_status", {}) if isinstance(payload, dict) else {}
            self.runtime.status.persona_completed_shards = sum(
                1
                for name in self.runtime.persona_summarizer.REQUIRED_SHARDS
                if shard_status.get(name) == "completed"
            )
            self.runtime.status.persona_self_lore_ready = bool(payload.get("self_lore_ready", False))
            self.runtime.status.persona_state = "full_ready" if payload.get("is_full_ready", False) else "enrichment_degraded"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.runtime.status.persona_state = "enrichment_degraded"
            self.runtime.status.persona_last_error = str(exc)
            self.runtime.mark_degraded("persona.enrichment", str(exc))
            logger.warning(f"[AstrMai] persona enrichment monitor degraded: {exc}")

    async def load_command_metadata(self) -> None:
        self.runtime.set_boot_phase("lifecycle.commands")
        if not self.runtime.sensors or not hasattr(self.runtime.sensors, "load_foreign_commands"):
            return
        try:
            await self.runtime.sensors.load_foreign_commands()
            self.runtime.status.foreign_commands_loaded = True
        except Exception as exc:
            self.runtime.mark_degraded("conversation.foreign_commands", str(exc))
            logger.warning(f"[AstrMai] Foreign command metadata degraded: {exc}")

    async def _wire_private_chat_plugin(self) -> None:
        """Inject host plugin into PrivateChatManager for KV storage access."""
        pcm = self.runtime.private_chat_manager
        host = self.runtime.host_plugin_ref
        # ponytail: host() deref is called twice — first check, then set. Store ref
        # to avoid a spurious GC race between the two calls.
        if pcm and host:
            host_plugin = host()
            if host_plugin:
                pcm.set_host_plugin(host_plugin)
                await pcm._cleanup_stale_pending_sessions()

    async def start_proactive_services(self) -> None:
        self.runtime.set_boot_phase("lifecycle.proactive")
        if not self.runtime.proactive_task:
            return
        try:
            await self.runtime.proactive_task.start()
            self.runtime.status.proactive_started = True
        except Exception as exc:
            self.runtime.mark_degraded("proactive.runtime", str(exc))
            logger.warning(f"[AstrMai] Proactive services degraded: {exc}")

    async def start_expression_governance_services(self) -> None:
        runner = getattr(self.runtime, "expression_governance_runner", None)
        if not runner:
            return
        self.runtime.set_boot_phase("lifecycle.expression_governance")
        try:
            await runner.start()
        except Exception as exc:
            self.runtime.mark_degraded("learning.expression_governance", str(exc))
            logger.warning(f"[AstrMai] Expression governance degraded: {exc}")

    async def start_visual_services(self) -> None:
        self.runtime.set_boot_phase("lifecycle.visual")
        if not self.runtime.visual_cortex:
            return
        try:
            self.runtime.visual_cortex.start()  # sync method, confirmed
            self.runtime.status.visual_started = True
        except Exception as exc:
            self.runtime.mark_degraded("multimodal.visual_runtime", str(exc))
            logger.warning(f"[AstrMai] Visual service degraded: {exc}")

    def start_background_services(self) -> None:
        # ponytail: fires tasks without confirming they started successfully.
        # Tasks are tracked via track_task() for shutdown; add health probe if
        # silent stall is observed in the field.
        self.runtime.set_boot_phase("lifecycle.background")
        evolution = getattr(self.runtime, "evolution", None)
        if evolution is not None and hasattr(evolution, "start_background_tasks"):
            self.track_task(evolution.start_background_tasks())
        reply_engine = getattr(self.runtime, "reply_engine", None)
        repair_worker = getattr(
            reply_engine,
            "run_reply_commit_repair_worker",
            None,
        )
        if callable(repair_worker):
            self.track_task(repair_worker())
        self.track_task(self._memory_gc_task())
        self.track_task(self._db_sync_task())

    async def start_workmode_guard(self) -> None:
        self.runtime.set_boot_phase("lifecycle.workmode")
        if not self.runtime.cron_guard:
            return
        start_guard = getattr(self.runtime.cron_guard, "start", None)
        if callable(start_guard):
            start_guard()
        try:
            await self.runtime.cron_guard.reload_all_lost_jobs()
        except Exception as exc:
            self.runtime.mark_degraded("workmode.cron_guard_reload", str(exc))
            logger.warning(f"[AstrMai] Workmode guard reload degraded: {exc}")
        try:
            self.track_task(self.runtime.cron_guard.run_heartbeat())
            self.runtime.status.cron_guard_started = True
            logger.info("[AstrMai] Sys3 CronHeartbeatGuard started.")
        except Exception as exc:
            self.runtime.mark_degraded("workmode.cron_guard", str(exc))
            logger.warning(f"[AstrMai] Workmode guard heartbeat degraded: {exc}")

    async def _db_sync_task(self) -> None:
        while self.runtime.status.is_running:
            try:
                await asyncio.sleep(5)  # ponytail: 5s flush interval (was 15s) to reduce data loss on crash
                if hasattr(self.runtime.state_engine, "flush_message_counters"):
                    await self.runtime.state_engine.flush_message_counters()
            except asyncio.CancelledError:
                logger.info("[AstrMai-DB-Sync] 收到终止信号，执行最后一次提交后退出。")
                try:
                    if hasattr(self.runtime.state_engine, "flush_message_counters"):
                        await self.runtime.state_engine.flush_message_counters()
                except Exception:
                    logger.warning("[AstrMai] shutdown flush failed", exc_info=True)  # ponytail: R19
                raise
            except Exception as exc:
                logger.error(f"[AstrMai-DB-Sync] 数据库批量同步任务异常: {exc}")

    async def _memory_gc_task(self) -> None:
        while self.runtime.status.is_running:
            try:
                await asyncio.sleep(3600)
                attention_stale_count = await cleanup_stale_focus_pools(
                    self.runtime.attention_gate,
                    ttl_seconds=86400.0,
                    now=time.time(),
                )
                if attention_stale_count > 0:
                    logger.info(f"[AstrMai-GC] cleaned {attention_stale_count} stale focus pools.")
            except asyncio.CancelledError:
                logger.info("[AstrMai-GC] 内存 GC 任务收到终止信号，正在安全退出。")
                raise
            except Exception as exc:
                logger.error(f"[AstrMai-GC] 内存 GC 任务异常: {exc}")

    def begin_shutdown(self) -> None:
        """Fence new ingress synchronously before asynchronous cleanup."""
        if self._shutdown_requested:
            return
        self._terminated = True
        self._shutdown_requested = True
        self.runtime.status.accepting_events = False
        self.runtime.status.is_running = False
        self.runtime.status.shutdown_generation = int(
            getattr(self.runtime.status, "shutdown_generation", 0) or 0
        ) + 1
        self.runtime.set_boot_phase("shutdown.start")
        event_bus = getattr(self.runtime, "event_bus", None)
        trigger_abort = getattr(event_bus, "trigger_abort", None)
        if callable(trigger_abort):
            trigger_abort()

    def _shutdown_timing(self, name: str, default: float) -> float:
        timing = getattr(self.runtime.config, "timing", None)
        try:
            return max(0.0, float(getattr(timing, name, default) or default))
        except (TypeError, ValueError):
            return default

    def _consume_isolated_shutdown_task(self, task: asyncio.Task[Any]) -> None:
        self._isolated_shutdown_tasks.discard(task)
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    def _schedule_forced_shutdown_cleanup(self, name: str, operation: Any) -> None:
        """Schedule best-effort tail cleanup without extending the reload wait."""
        try:
            result = operation() if callable(operation) else operation
            if not inspect.isawaitable(result):
                return
            task = asyncio.create_task(result, name=f"astrmai:shutdown:forced:{name}")
            self._isolated_shutdown_tasks.add(task)
            task.add_done_callback(self._consume_isolated_shutdown_task)
        except Exception as exc:
            logger.warning(f"[AstrMai] forced shutdown cleanup degraded name={name}: {exc}")

    def _force_shutdown_tail(self) -> None:
        """Release critical tail resources after a bounded sequence is isolated."""
        started = time.monotonic()
        errors: list[str] = []
        try:
            self.stop_visual_services()
        except Exception as exc:
            errors.append(f"visual:{exc}")

        try:
            tasks = collect_background_tasks(*self.runtime.iter_task_owners())
            for task in dict.fromkeys(task for task in tasks if task is not None):
                if not task.done():
                    task.cancel()
        except Exception as exc:
            errors.append(f"tasks:{exc}")

        event_bus = getattr(self.runtime, "event_bus", None)
        stop_event_bus = getattr(event_bus, "stop", None)
        if callable(stop_event_bus):
            self._schedule_forced_shutdown_cleanup("event_bus", stop_event_bus)

        persistence = getattr(self.runtime, "persistence", None)
        dispose = getattr(persistence, "dispose", None)
        if callable(dispose):
            try:
                dispose()
            except Exception as exc:
                errors.append(f"persistence:{exc}")

        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        self.runtime.status.shutdown_stage_stats["forced_tail"] = {
            "status": "degraded" if errors else "completed",
            "elapsed_ms": elapsed_ms,
            "errors": errors,
        }

    async def _run_bounded_shutdown_stage(
        self,
        name: str,
        operation: Any,
        *,
        deadline: float,
        timeout_sec: float | None = None,
    ) -> bool:
        started = time.monotonic()
        remaining = max(0.0, deadline - started)
        limit = timeout_sec if timeout_sec is not None else self._shutdown_timing("shutdown_component_timeout_sec", 1.5)
        stage_timeout = min(remaining, max(0.0, limit))
        if stage_timeout <= 0:
            self.runtime.status.shutdown_stage_stats[name] = {
                "status": "skipped_budget_exhausted",
                "elapsed_ms": 0.0,
            }
            return False
        try:
            result = operation() if callable(operation) else operation
            if not inspect.isawaitable(result):
                self.runtime.status.shutdown_stage_stats[name] = {
                    "status": "completed",
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                }
                return True
            task = asyncio.create_task(result, name=f"astrmai:shutdown:{name}")
            done, pending = await asyncio.wait({task}, timeout=stage_timeout)
            if pending:
                task.cancel()
                self._isolated_shutdown_tasks.add(task)
                task.add_done_callback(self._consume_isolated_shutdown_task)
                self.runtime.status.shutdown_isolated_tasks += 1
                self.runtime.status.shutdown_stage_stats[name] = {
                    "status": "isolated_timeout",
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                }
                logger.warning(f"[AstrMai] shutdown stage isolated name={name} timeout_sec={stage_timeout:.3f}")
                return False
            task.result()
            self.runtime.status.shutdown_stage_stats[name] = {
                "status": "completed",
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            }
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.runtime.status.shutdown_stage_stats[name] = {
                "status": "failed",
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                "error": str(exc),
            }
            logger.warning(f"[AstrMai] shutdown stage degraded name={name}: {exc}")
            return False

    async def terminate(self) -> None:
        async with self._terminate_lock:
            if self._termination_complete:
                return
            logger.info("[AstrMai] 正在终止进程并卸载资源...")
            started = time.monotonic()
            self.begin_shutdown()
            self.runtime.status.shutdown_started_at = time.time()
            self.runtime.status.shutdown_stage_stats = {}
            self.runtime.status.shutdown_isolated_tasks = 0
            deadline = started + self._shutdown_timing("hot_reload_shutdown_budget_sec", 5.0)

            try:
                await self._run_bounded_shutdown_stage(
                    "dialogue_snapshot",
                    self._persist_dialogue_snapshot,
                    deadline=deadline,
                    timeout_sec=self._shutdown_timing("shutdown_snapshot_timeout_sec", 0.5),
                )
                coordinator = getattr(self.runtime, "runtime_coordinator", None)
                shutdown_coordinator = getattr(coordinator, "shutdown", None)
                if callable(shutdown_coordinator):
                    async def _shutdown_runtime_coordinator() -> None:
                        try:
                            await shutdown_coordinator(
                                timeout_sec=self._shutdown_timing("shutdown_cancel_grace_sec", 1.0)
                            )
                        except TypeError:
                            await shutdown_coordinator()

                    await self._run_bounded_shutdown_stage(
                        "runtime_coordinator",
                        _shutdown_runtime_coordinator,
                        deadline=deadline,
                    )
                shutdown_completed = await self._run_bounded_shutdown_stage(
                    "shutdown_sequence",
                    self._terminate_impl,
                    deadline=deadline,
                    timeout_sec=max(0.0, deadline - time.monotonic()),
                )
                if not shutdown_completed:
                    self._force_shutdown_tail()
            finally:
                coordinator = getattr(self.runtime, "runtime_coordinator", None)
                if coordinator and hasattr(coordinator, "_states"):
                    coordinator._states.clear()
                handoff_store = getattr(self.runtime, "cross_session_handoff_store", None)
                clear_handoffs = getattr(handoff_store, "clear", None)
                if callable(clear_handoffs):
                    await self._run_bounded_shutdown_stage(
                        "cross_session_handoffs",
                        clear_handoffs,
                        deadline=deadline,
                    )
                self._reset_runtime_status_flags()
                elapsed_ms = (time.monotonic() - started) * 1000
                self.runtime.status.shutdown_completed_at = time.time()
                self.runtime.status.last_shutdown_elapsed_ms = round(elapsed_ms, 3)
                stage_stats = self.runtime.status.shutdown_stage_stats
                self.runtime.status.last_shutdown_slowest_stage = max(
                    stage_stats,
                    key=lambda key: float(stage_stats[key].get("elapsed_ms", 0.0) or 0.0),
                    default="",
                )
                self.runtime.set_boot_phase("shutdown.complete")
                self._termination_complete = True
                logger.info(
                    f"[AstrMai] shutdown complete elapsed_ms={elapsed_ms:.1f} "
                    f"isolated={self.runtime.status.shutdown_isolated_tasks} "
                    f"slowest={self.runtime.status.last_shutdown_slowest_stage or 'none'}"
                )

    async def _terminate_impl(self) -> None:
        try:
            stop_memory = getattr(self.runtime.memory_engine, "stop_background_tasks", None)
            if callable(stop_memory):
                await stop_memory()
            else:
                memory_pipeline = getattr(self.runtime.memory_engine, "memory_pipeline", None)
                if memory_pipeline:
                    await memory_pipeline.stop()
        except Exception as exc:
            logger.warning(f"[AstrMai] Memory pipeline shutdown degraded: {exc}")

        try:
            pcm = self.runtime.private_chat_manager
            if pcm:
                await pcm._persist_pending_sessions()
        except Exception as exc:
            logger.warning(f"[AstrMai] PrivateChat persist shutdown degraded: {exc}")

        try:
            await self.stop_proactive_services()
        except Exception as exc:
            logger.warning(f"[AstrMai] Proactive shutdown degraded: {exc}")

        try:
            await self.stop_expression_governance_services()
        except Exception as exc:
            logger.warning(f"[AstrMai] Expression governance shutdown degraded: {exc}")

        try:
            evolution = getattr(self.runtime, "evolution", None)
            if evolution is not None and hasattr(evolution, "stop_background_tasks"):
                await evolution.stop_background_tasks()
        except Exception as exc:
            logger.warning(f"[AstrMai] Evolution background shutdown degraded: {exc}")

        try:
            persona_summarizer = getattr(self.runtime, "persona_summarizer", None)
            if persona_summarizer and hasattr(persona_summarizer, "stop"):
                await persona_summarizer.stop()
        except Exception as exc:
            logger.warning(f"[AstrMai] Persona summarizer shutdown degraded: {exc}")

        try:
            if self.runtime.cron_guard:
                self.runtime.cron_guard.stop()
        except Exception as exc:
            logger.warning(f"[AstrMai] Cron guard shutdown degraded: {exc}")

        try:
            tasks_to_wait = collect_background_tasks(*self.runtime.iter_task_owners())
        except Exception as exc:
            logger.warning(f"[AstrMai] Shutdown task collection degraded: {exc}")
            tasks_to_wait = []

        try:
            self.stop_visual_services()
        except Exception as exc:
            logger.warning(f"[AstrMai] Visual shutdown degraded: {exc}")

        if tasks_to_wait:
            logger.info(f"[AstrMai] 正在等待 {len(tasks_to_wait)} 个后台协程安全结束...")
            # ponytail: dict.fromkeys dedup relies on asyncio.Task being hashable
            # (CPython detail). Use explicit id() dedup if this breaks on another runtime.
            unique_tasks = [task for task in dict.fromkeys(tasks_to_wait) if task is not None]
            for task in unique_tasks:
                if not task.done():
                    task.cancel()
            try:
                _, pending = await asyncio.wait(unique_tasks, timeout=self.SHUTDOWN_TASK_TIMEOUT)
                if pending:
                    logger.warning(f"[AstrMai] {len(pending)} background tasks did not exit gracefully before timeout.")
                else:
                    logger.info("[AstrMai] all background tasks were cleaned up safely.")
            except Exception as exc:
                logger.warning(f"[AstrMai] Background task cleanup degraded: {exc}")

        # 停止 EventBus workers
        event_bus = getattr(self.runtime, "event_bus", None)
        if event_bus is not None:
            try:
                await event_bus.stop()
            except Exception as exc:
                logger.warning(f"[AstrMai] EventBus shutdown degraded: {exc}")

        # 释放 DB 连接池
        persistence = getattr(self.runtime, "persistence", None)
        if persistence is not None:
            try:
                persistence.dispose()
            except Exception as exc:
                logger.warning(f"[AstrMai] Persistence dispose degraded: {exc}")

    # ponytail: 10+ flags set sequentially with no atomicity guarantee.
    # Acceptable because terminate() is called once at shutdown and flags are
    # only read by the next bootstrap cycle. Add a bulk-reset if parallel boot
    # is needed.
    def _reset_runtime_status_flags(self) -> None:
        """Reset all runtime status flags for a clean shutdown slate.

        Called at the end of terminate() so all flags are consistently
        managed in one place.
        """
        # Runtime lifecycle flags
        self.runtime.status.is_running = False
        self.runtime.status.accepting_events = False
        self.runtime.status.lifecycle_started = False
        # Bootstrap / startup flags
        self.runtime.status.bootstrap_completed = False
        self.runtime.status.boot_logged = False
        self.runtime.status.work_mode_enabled = False
        # Subsystem initialization flags
        self.runtime.status.memory_initialized = False
        self.runtime.status.persona_state = "pending"
        self.runtime.status.persona_cache_key = ""
        self.runtime.status.persona_completed_shards = 0
        self.runtime.status.persona_persisted = False
        self.runtime.status.persona_self_lore_ready = False
        self.runtime.status.persona_last_error = ""
        self.runtime.status.proactive_started = False
        self.runtime.status.visual_started = False
        self.runtime.status.cron_guard_started = False
        self.runtime.status.foreign_commands_loaded = False

    async def stop_proactive_services(self) -> None:
        self.runtime.set_boot_phase("shutdown.proactive")
        if not self.runtime.proactive_task:
            return
        try:
            await self.runtime.proactive_task.stop()
        except Exception as exc:
            self.runtime.mark_degraded("proactive.shutdown", str(exc))
            logger.warning(f"[AstrMai] Proactive shutdown degraded: {exc}")

    async def stop_expression_governance_services(self) -> None:
        runner = getattr(self.runtime, "expression_governance_runner", None)
        if not runner:
            return
        self.runtime.set_boot_phase("shutdown.expression_governance")
        try:
            await runner.stop()
        except Exception as exc:
            self.runtime.mark_degraded("learning.expression_governance_shutdown", str(exc))
            logger.warning(f"[AstrMai] Expression governance shutdown degraded: {exc}")

    def stop_visual_services(self) -> None:
        self.runtime.set_boot_phase("shutdown.visual")
        if not self.runtime.visual_cortex:
            return
        try:
            self.runtime.visual_cortex.stop()
        except Exception as exc:
            self.runtime.mark_degraded("multimodal.visual_shutdown", str(exc))
            logger.warning(f"[AstrMai] Visual shutdown degraded: {exc}")
