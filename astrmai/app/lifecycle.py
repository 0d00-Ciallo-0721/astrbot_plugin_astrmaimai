from __future__ import annotations

import asyncio
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
        if self.runtime.status.is_running and self.runtime.status.lifecycle_started:
            logger.debug("[AstrMai] runtime startup skipped reason=already_running")
            return
        if self._startup_task is not None and not self._startup_task.done():
            logger.debug("[AstrMai] runtime startup skipped reason=in_progress")
            return
        logger.info("[AstrMai] Starting system initialization from refactoring workspace...")
        self._shutdown_requested = False
        self.runtime.status.is_running = False
        self.runtime.status.lifecycle_started = False
        self.runtime.status.persona_state = "pending"
        self.runtime.set_boot_phase("lifecycle.starting")
        self._startup_task = self.track_task(self._complete_startup())

    async def _prepare_reinitialize(self) -> None:
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

    async def terminate(self) -> None:
        logger.info("[AstrMai] 正在终止进程并卸载资源...")
        self._terminated = True
        self._shutdown_requested = True
        self.runtime.set_boot_phase("shutdown.start")

        # G4/PL-09: 先落盘上下文快照，再 cancel 在飞任务——顺序关键，
        # coordinator.shutdown 会清空运行态
        await self._persist_dialogue_snapshot()

        try:
            coordinator = getattr(self.runtime, "runtime_coordinator", None)
            shutdown = getattr(coordinator, "shutdown", None)
            if callable(shutdown):
                try:
                    await shutdown()
                except Exception as exc:
                    logger.warning(f"[AstrMai] Runtime coordinator shutdown degraded: {exc}")
            await self._terminate_impl()
        finally:
            coordinator = getattr(self.runtime, "runtime_coordinator", None)
            if coordinator and hasattr(coordinator, "_states"):
                coordinator._states.clear()
            handoff_store = getattr(self.runtime, "cross_session_handoff_store", None)
            clear_handoffs = getattr(handoff_store, "clear", None)
            if callable(clear_handoffs):
                try:
                    await clear_handoffs()
                except Exception as exc:
                    logger.warning(f"[AstrMai] Cross-session handoff cleanup degraded: {exc}")
            self._reset_runtime_status_flags()
            self.runtime.set_boot_phase("shutdown.complete")

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
