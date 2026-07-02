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

    async def on_program_start(self) -> None:
        logger.info("[AstrMai] AstrBot Loaded. Starting system initialization from refactoring workspace...")
        logger.info("[AstrMai] Initializing Memory Engine...")
        await self.initialize_memory()
        logger.info("[AstrMai] boot phase: memory initialized")

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
        self.track_task(self._memory_gc_task())
        self.track_task(self._db_sync_task())

    async def start_workmode_guard(self) -> None:
        self.runtime.set_boot_phase("lifecycle.workmode")
        if not self.runtime.cron_guard:
            return
        try:
            await self.runtime.cron_guard.reload_all_lost_jobs()
        except Exception as exc:
            self.runtime.mark_degraded("workmode.cron_guard_reload", str(exc))
            logger.warning(f"[AstrMai] Workmode guard reload degraded: {exc}")
            return
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
        self.runtime.set_boot_phase("shutdown.start")

        try:
            await self._terminate_impl()
        finally:
            # ponytail: clear ChatRuntimeCoordinator states to free memory
            coordinator = getattr(self.runtime, "runtime_coordinator", None)
            if coordinator and hasattr(coordinator, "_states"):
                coordinator._states.clear()
            self._reset_runtime_status_flags()
            self.runtime.set_boot_phase("shutdown.complete")

    async def _terminate_impl(self) -> None:
        try:
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
            await event_bus.stop()

        # 释放 DB 连接池
        persistence = getattr(self.runtime, "persistence", None)
        if persistence is not None:
            persistence.dispose()

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
