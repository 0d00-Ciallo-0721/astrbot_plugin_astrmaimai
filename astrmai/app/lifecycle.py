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
        self._shutdown_pending_drain = False
        self._shutdown_started_monotonic = 0.0
        self._late_shutdown_cleanup_task: asyncio.Task[Any] | None = None
        self._late_shutdown_cleanup_deadline_monotonic = 0.0
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
            if not await self._prepare_reinitialize():
                return
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
        self.runtime.status.startup_blocked_reason = ""
        self.runtime.status.startup_retry_at = 0.0
        self.runtime.set_boot_phase("lifecycle.starting")
        attention_gate = getattr(self.runtime, "attention_gate", None)
        reset_attention = getattr(attention_gate, "reset_runtime_state", None)
        if callable(reset_attention):
            reset_attention()
        self._startup_task = self.track_task(self._complete_startup())

    async def _prepare_reinitialize(self) -> bool:
        if getattr(self, "_shutdown_dependency_close_errors", None):
            logger.warning("[AstrMai] runtime reinitialize deferred: dependency close failed")
            return False
        if getattr(self, "_shutdown_pending_drain", False):
            logger.warning("[AstrMai] runtime reinitialize deferred: shutdown cleanup is pending")
            return False
        late_cleanup = getattr(self, "_late_shutdown_cleanup_task", None)
        if late_cleanup is not None and not late_cleanup.done():
            logger.warning("[AstrMai] runtime reinitialize deferred: late shutdown cleanup is running")
            return False
        reread_dispatcher = getattr(self.runtime, "reread_action_dispatcher", None)
        resume_reread = getattr(reread_dispatcher, "resume", None)
        budget = getattr(self.runtime, "background_task_budget", None)
        can_resume_budget = getattr(budget, "can_resume", None)
        resume_budget = getattr(budget, "resume", None)
        resume_budget_if_idle = getattr(budget, "resume_if_idle", None)
        if callable(can_resume_budget):
            if can_resume_budget() is False:
                logger.warning("[AstrMai] runtime reinitialize deferred: background work is still draining")
                return False
            if callable(resume_reread) and resume_reread() is False:
                logger.warning("[AstrMai] runtime reinitialize deferred: reread dispatcher still has pending work")
                return False
            budget_resume = resume_budget_if_idle if callable(resume_budget_if_idle) else resume_budget
            if callable(budget_resume) and budget_resume() is False:
                logger.warning("[AstrMai] runtime reinitialize deferred: background work is still draining")
                return False
        else:
            if callable(resume_budget) and resume_budget() is False:
                logger.warning("[AstrMai] runtime reinitialize deferred: background work is still draining")
                return False
            if callable(resume_reread) and resume_reread() is False:
                logger.warning("[AstrMai] runtime reinitialize deferred: reread dispatcher still has pending work")
                return False
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
        return True

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

    def _persona_startup_timeout(self) -> float:
        persona_config = getattr(self.runtime.config, "persona", None)
        try:
            return max(0.0, float(getattr(persona_config, "startup_timeout_sec", 900.0) or 0.0))
        except (TypeError, ValueError):
            return 900.0

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

        persona_ready = await self._initialize_persona_core_until_ready()
        if self._shutdown_requested or not persona_ready:
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

    async def _initialize_persona_core_until_ready(self) -> bool:
        summarizer = getattr(self.runtime, "persona_summarizer", None)
        context_engine = getattr(self.runtime, "context_engine", None)
        if summarizer is None or context_engine is None:
            raise RuntimeError("persona runtime is unavailable")
        persona_id, raw_prompt = context_engine.resolve_active_persona()
        cache_key = summarizer._cache_key(persona_id, "global")
        self.runtime.status.persona_cache_key = cache_key
        initial_delay, max_delay = self._persona_retry_bounds()
        startup_timeout = self._persona_startup_timeout()
        startup_started = time.monotonic()
        delay = initial_delay
        while not self._shutdown_requested:
            if startup_timeout > 0 and time.monotonic() - startup_started >= startup_timeout:
                self.runtime.status.persona_state = "core_failed"
                self.runtime.status.persona_persisted = False
                self.runtime.status.startup_blocked_reason = "persona_startup_timeout"
                self.runtime.status.startup_retry_at = 0.0
                self.runtime.set_boot_phase("lifecycle.persona_timeout")
                self.runtime.mark_degraded("persona.core", "startup_timeout")
                return False
            self.runtime.status.persona_state = "core_initializing"
            self.runtime.status.persona_last_error = ""
            self.runtime.set_boot_phase("lifecycle.persona_core")
            try:
                ensure_core_ready = summarizer.ensure_core_ready(
                    raw_prompt,
                    persona_id=persona_id,
                    session_id="global",
                )
                if startup_timeout > 0:
                    remaining = max(0.001, startup_timeout - (time.monotonic() - startup_started))
                    payload = await asyncio.wait_for(ensure_core_ready, timeout=remaining)
                else:
                    payload = await ensure_core_ready
                self.runtime.status.persona_state = "core_ready"
                self.runtime.status.persona_persisted = True
                self.runtime.status.startup_blocked_reason = ""
                self.runtime.status.startup_retry_at = 0.0
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
                return True
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                self.runtime.status.persona_state = "core_failed"
                self.runtime.status.persona_persisted = False
                self.runtime.status.persona_last_error = "persona startup timed out"
                self.runtime.status.startup_blocked_reason = "persona_startup_timeout"
                self.runtime.status.startup_retry_at = 0.0
                self.runtime.set_boot_phase("lifecycle.persona_timeout")
                self.runtime.mark_degraded("persona.core", "startup_timeout")
                return False
            except Exception as exc:
                self.runtime.status.persona_state = "core_failed"
                self.runtime.status.persona_persisted = False
                self.runtime.status.persona_last_error = str(exc)
                self.runtime.status.startup_blocked_reason = "persona_core_unavailable"
                self.runtime.status.startup_retry_at = time.time() + delay
                self.runtime.set_boot_phase("lifecycle.persona_timeout")
                self.runtime.mark_degraded("persona.core", str(exc))
                logger.warning(
                    f"[AstrMai] persona core initialization failed; retrying in {delay:.1f}s: {exc}"
                )
                await asyncio.sleep(delay)
                delay = min(max_delay, delay * 2)
        return False

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
        attention_gate = getattr(self.runtime, "attention_gate", None)
        request_attention_shutdown = getattr(attention_gate, "request_shutdown", None)
        if callable(request_attention_shutdown):
            request_attention_shutdown()
        proactive_task = getattr(self.runtime, "proactive_task", None)
        scheduled_scenarios = getattr(proactive_task, "scheduled_scenario_service", None)
        request_scenario_shutdown = getattr(scheduled_scenarios, "request_shutdown", None)
        if callable(request_scenario_shutdown):
            request_scenario_shutdown()
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
        late_cleanup = getattr(self, "_late_shutdown_cleanup_task", None)
        if (
            task.cancelled()
            and task is late_cleanup
            and int(getattr(self.runtime.status, "shutdown_late_cleanup_task_count", 0) or 0)
        ):
            self._shutdown_pending_drain = True
            self._termination_complete = False
            self.runtime.status.shutdown_late_cleanup_task_count = 0
            self.runtime.status.shutdown_final_status = "degraded"
            self.runtime.status.shutdown_pending_drain = True
            self.runtime.status.shutdown_forced_termination_risk = True
            self.runtime.set_boot_phase("shutdown.degraded")
            self.runtime.mark_degraded("shutdown.cleanup_cancelled", "late_cleanup_cancelled")
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

    async def _force_shutdown_tail_async(self) -> None:
        reread_dispatcher = getattr(self.runtime, "reread_action_dispatcher", None)
        force_shutdown_reread = getattr(reread_dispatcher, "force_shutdown", None)
        if callable(force_shutdown_reread):
            try:
                completed = await force_shutdown_reread()
                status = getattr(reread_dispatcher, "describe_status", lambda: {})()
                if completed is False or status.get("pending_dispatch_shutdown"):
                    self._shutdown_pending_drain = True

                    async def _late_reread_cleanup() -> None:
                        try:
                            await reread_dispatcher.shutdown()
                            self._force_shutdown_tail(wait_dispatcher=False)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            self.runtime.mark_degraded("shutdown.reread_late_cleanup", str(exc))
                            logger.warning(f"[AstrMai] late reread cleanup degraded: {exc}")
                        finally:
                            self._shutdown_pending_drain = False
                            self._reset_runtime_status_flags()
                            elapsed_ms = (time.monotonic() - self._shutdown_started_monotonic) * 1000
                            self.runtime.status.shutdown_completed_at = time.time()
                            self.runtime.status.last_shutdown_elapsed_ms = round(elapsed_ms, 3)
                            self.runtime.set_boot_phase("shutdown.complete")
                            self._termination_complete = True

                    task = asyncio.create_task(_late_reread_cleanup(), name="astrmai:shutdown:reread-late")
                    self._late_shutdown_cleanup_task = task
                    self._isolated_shutdown_tasks.add(task)
                    task.add_done_callback(self._consume_isolated_shutdown_task)
                    return
            except Exception as exc:
                logger.warning(f"[AstrMai] forced reread dispatcher shutdown degraded: {exc}")
        self._force_shutdown_tail(wait_dispatcher=False)

    def _force_shutdown_tail(self, *, wait_dispatcher: bool = True) -> None:
        """Release critical tail resources after a bounded sequence is isolated."""
        started = time.monotonic()
        errors: list[str] = []
        reread_dispatcher = getattr(self.runtime, "reread_action_dispatcher", None)
        force_shutdown_reread = getattr(reread_dispatcher, "force_shutdown", None)
        if wait_dispatcher and callable(force_shutdown_reread):
            self._schedule_forced_shutdown_cleanup("reread_dispatcher", force_shutdown_reread)
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

        budget = getattr(self.runtime, "background_task_budget", None)
        begin_drain = getattr(budget, "begin_drain", None)
        if callable(begin_drain):
            try:
                begin_drain()
            except Exception as exc:
                errors.append(f"budget.begin_drain:{exc}")
        pending_report = self._shutdown_pending_report(budget)
        pending_budget = int(pending_report.get("remaining", 0) or 0)
        if pending_budget:
            self._shutdown_pending_drain = True
            self._schedule_late_shutdown_cleanup(budget)
            self.runtime.status.shutdown_pending_drain = True
            self.runtime.status.shutdown_final_status = "pending_drain"
            elapsed_ms = round((time.monotonic() - started) * 1000, 3)
            self.runtime.status.shutdown_stage_stats["forced_tail"] = {
                "status": "pending_drain",
                "elapsed_ms": elapsed_ms,
                "errors": errors,
                "remaining": pending_budget,
                "remaining_by_kind": dict(pending_report.get("remaining_by_kind", {}) or {}),
                "owner_task_names": list(pending_report.get("owner_task_names", []) or []),
            }
            logger.warning(
                "[AstrMai] forced shutdown tail deferred while background work remains: "
                f"remaining={pending_budget}"
            )
            return

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

    @staticmethod
    def _background_budget_pending(budget: Any) -> int:
        status_fn = getattr(budget, "status", None)
        if not callable(status_fn):
            return 0
        try:
            status = status_fn() or {}
        except Exception:
            return 0
        return max(
            int(status.get("active", 0) or 0),
            int(status.get("queued", 0) or 0),
            int(status.get("deferred_tasks", 0) or 0),
            int(status.get("physical_owner_count", 0) or 0),
        )

    def _shutdown_pending_report(self, budget: Any, *, include_late_task: bool = False) -> dict[str, Any]:
        status_fn = getattr(budget, "status", None)
        budget_status = status_fn() if callable(status_fn) else {}
        if not isinstance(budget_status, dict):
            budget_status = {}
        remaining_by_kind: dict[str, int] = {}
        for field in ("active_by_kind", "queued_by_kind", "deferred_by_kind"):
            for name, count in dict(budget_status.get(field, {}) or {}).items():
                if int(count or 0) > 0:
                    remaining_by_kind[str(name)] = max(
                        remaining_by_kind.get(str(name), 0), int(count or 0)
                    )
        attention = getattr(self.runtime, "attention_gate", None)
        attention_status = {}
        describe_attention = getattr(attention, "describe_status", None)
        if callable(describe_attention):
            try:
                attention_status = describe_attention() or {}
            except Exception:
                attention_status = {}
        worker_count = int(attention_status.get("worker_count", 0) or 0)
        if worker_count:
            remaining_by_kind["attention.worker"] = worker_count
        remaining = max(
            int(budget_status.get("active", 0) or 0),
            int(budget_status.get("queued", 0) or 0),
            int(budget_status.get("deferred", budget_status.get("deferred_tasks", 0)) or 0),
            int(budget_status.get("physical", budget_status.get("physical_owner_count", 0)) or 0),
            worker_count,
        )
        if include_late_task and int(getattr(self.runtime.status, "shutdown_late_cleanup_task_count", 0) or 0):
            remaining = max(remaining, 1)
            remaining_by_kind["shutdown.late_cleanup"] = 1
        return {
            "remaining": remaining,
            "remaining_by_kind": remaining_by_kind,
            "active": int(budget_status.get("active", 0) or 0),
            "queued": int(budget_status.get("queued", 0) or 0),
            "deferred": int(budget_status.get("deferred", budget_status.get("deferred_tasks", 0)) or 0),
            "physical": int(budget_status.get("physical", budget_status.get("physical_owner_count", 0)) or 0),
            "worker_count": worker_count,
            "owner_task_names": list(budget_status.get("owner_task_names", []) or []),
            "oldest_owner_age_ms": float(budget_status.get("oldest_owner_age_ms", 0.0) or 0.0),
            "active_by_kind": dict(budget_status.get("active_by_kind", {}) or {}),
            "queued_by_kind": dict(budget_status.get("queued_by_kind", {}) or {}),
            "deferred_by_kind": dict(budget_status.get("deferred_by_kind", {}) or {}),
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
            if self._shutdown_pending_drain:
                logger.info("[AstrMai] shutdown is waiting for deferred background work")
                return
            logger.info("[AstrMai] 正在终止进程并卸载资源...")
            started = time.monotonic()
            self._shutdown_started_monotonic = started
            self.begin_shutdown()
            self.runtime.status.shutdown_started_at = time.time()
            self.runtime.status.shutdown_stage_stats = {}
            self.runtime.status.shutdown_isolated_tasks = 0
            self.runtime.status.shutdown_final_status = "running"
            self.runtime.status.shutdown_pending_drain = False
            self.runtime.status.shutdown_forced_termination_risk = False
            self.runtime.status.shutdown_late_cleanup_deadline = 0.0
            self.runtime.status.shutdown_late_cleanup_task_count = 0
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
                    await self._force_shutdown_tail_async()
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
                final_budget = getattr(self.runtime, "background_task_budget", None)
                final_pending = self._shutdown_pending_report(
                    final_budget,
                    include_late_task=True,
                )
                dependency_errors = list(getattr(self, "_shutdown_dependency_close_errors", []) or [])
                if dependency_errors:
                    self.runtime.set_boot_phase("shutdown.degraded")
                    self.runtime.status.shutdown_pending_drain = False
                    self.runtime.status.shutdown_final_status = "degraded"
                    self.runtime.status.shutdown_forced_termination_risk = True
                    self.runtime.status.shutdown_stage_stats["dependency_close"] = {
                        "status": "degraded",
                        "errors": dependency_errors,
                    }
                    self._termination_complete = False
                    logger.warning(
                        "[AstrMai] shutdown degraded: dependency close failed "
                        f"errors={dependency_errors}"
                    )
                elif self._shutdown_pending_drain or final_pending.get("remaining"):
                    if final_pending.get("remaining"):
                        self.runtime.status.shutdown_stage_stats["final_quiescence"] = {
                            "status": "pending",
                            "remaining": int(final_pending.get("remaining", 0) or 0),
                            "remaining_by_kind": dict(final_pending.get("remaining_by_kind", {}) or {}),
                            "active_by_kind": dict(final_pending.get("active_by_kind", {}) or {}),
                            "queued_by_kind": dict(final_pending.get("queued_by_kind", {}) or {}),
                            "deferred_by_kind": dict(final_pending.get("deferred_by_kind", {}) or {}),
                            "physical_owner_count": int(final_pending.get("physical", 0) or 0),
                            "worker_count": int(final_pending.get("worker_count", 0) or 0),
                        }
                    self.runtime.set_boot_phase("shutdown.pending_drain")
                    self.runtime.status.shutdown_pending_drain = True
                    if self.runtime.status.shutdown_final_status == "running":
                        self.runtime.status.shutdown_final_status = "pending_drain"
                    self._termination_complete = False
                    logger.warning(
                        "[AstrMai] shutdown pending quiescence drain: "
                        f"remaining={final_pending.get('remaining', 0)} "
                        f"by_kind={final_pending.get('remaining_by_kind', {})}"
                    )
                else:
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
                    self.runtime.status.shutdown_final_status = "complete"
                    self.runtime.status.shutdown_pending_drain = False
                    self.runtime.status.shutdown_forced_termination_risk = False
                    self._termination_complete = True
                    logger.info(
                        f"[AstrMai] shutdown complete elapsed_ms={elapsed_ms:.1f} "
                        f"isolated={self.runtime.status.shutdown_isolated_tasks} "
                        f"slowest={self.runtime.status.last_shutdown_slowest_stage or 'none'}"
                    )

    async def _terminate_impl(self) -> None:
        self._shutdown_dependency_close_errors = []
        budget = getattr(self.runtime, "background_task_budget", None)
        begin_drain = getattr(budget, "begin_drain", None)
        if callable(begin_drain):
            try:
                begin_drain()
            except Exception as exc:
                logger.warning(f"[AstrMai] background budget drain admission degraded: {exc}")

        attention_gate = getattr(self.runtime, "attention_gate", None)
        shutdown_attention = getattr(attention_gate, "shutdown_workers", None)
        if callable(shutdown_attention):
            try:
                await shutdown_attention()
            except Exception as exc:
                logger.warning(f"[AstrMai] Attention worker shutdown degraded: {exc}")

        reread_dispatcher = getattr(self.runtime, "reread_action_dispatcher", None)
        shutdown_reread = getattr(reread_dispatcher, "shutdown", None)
        if callable(shutdown_reread):
            try:
                await shutdown_reread()
            except Exception as exc:
                logger.warning(f"[AstrMai] Reread dispatcher shutdown degraded: {exc}")
        drain_budget = getattr(budget, "drain", None)
        drain_report: dict[str, object] = {}

        # Fence all producer loops before stopping memory/projector workers;
        # their durable outbox remains available for the next bootstrap.
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
            stop_memory = getattr(self.runtime.memory_engine, "stop_background_producers", None)
            if not callable(stop_memory):
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

        if callable(drain_budget):
            try:
                drain_report = await drain_budget(
                    timeout_sec=min(2.0, float(self.SHUTDOWN_TASK_TIMEOUT or 2.0))
                )
                if drain_report.get("observed", 0):
                    logger.info(
                        "[AstrMai] deferred background work drained: "
                        f"observed={drain_report.get('observed', 0)} "
                        f"remaining={drain_report.get('remaining', 0)}"
                    )
            except Exception as exc:
                logger.warning(f"[AstrMai] deferred background drain degraded: {exc}")

        pending_report = self._shutdown_pending_report(budget)
        if drain_report.get("remaining", 0):
            pending_report["remaining"] = max(
                int(pending_report.get("remaining", 0) or 0),
                int(drain_report.get("remaining", 0) or 0),
            )
            for field in ("active_by_kind", "queued_by_kind", "deferred_by_kind"):
                pending_report[field] = {
                    **dict(pending_report.get(field, {}) or {}),
                    **dict(drain_report.get(field, {}) or {}),
                }
            pending_report["owner_task_names"] = sorted(
                set(pending_report.get("owner_task_names", []) or [])
                | set(drain_report.get("owner_task_names", []) or [])
            )
        self.runtime.status.shutdown_stage_stats["budget_drain"] = {
            "status": "pending_drain" if pending_report["remaining"] else "completed",
            **pending_report,
        }
        if pending_report["remaining"]:
            self._shutdown_pending_drain = True
            self._schedule_late_shutdown_cleanup(budget)
            self.runtime.status.shutdown_pending_drain = True
            self.runtime.status.shutdown_final_status = "pending_drain"
            logger.warning(
                "[AstrMai] keeping dependent resources open until background drain completes: "
                f"remaining={pending_report.get('remaining', 0)} "
                f"by_kind={pending_report.get('remaining_by_kind', {})}"
            )
            return

        try:
            close_memory_resources = getattr(
                self.runtime.memory_engine,
                "close_background_resources",
                None,
            )
            if callable(close_memory_resources):
                await close_memory_resources()
        except Exception as exc:
            self._shutdown_dependency_close_errors.append(f"memory:{exc}")
            self.runtime.mark_degraded("shutdown.dependency_close", f"memory:{exc}")
            logger.warning(f"[AstrMai] Memory resource shutdown degraded: {exc}")

        # 停止 EventBus workers
        event_bus = getattr(self.runtime, "event_bus", None)
        if event_bus is not None:
            try:
                await event_bus.stop()
            except Exception as exc:
                self._shutdown_dependency_close_errors.append(f"event_bus:{exc}")
                self.runtime.mark_degraded("shutdown.dependency_close", f"event_bus:{exc}")
                logger.warning(f"[AstrMai] EventBus shutdown degraded: {exc}")

        # 释放 DB 连接池
        persistence = getattr(self.runtime, "persistence", None)
        if persistence is not None:
            try:
                persistence.dispose()
            except Exception as exc:
                self._shutdown_dependency_close_errors.append(f"persistence:{exc}")
                self.runtime.mark_degraded("shutdown.dependency_close", f"persistence:{exc}")
                logger.warning(f"[AstrMai] Persistence dispose degraded: {exc}")

        if self._shutdown_dependency_close_errors:
            self.runtime.status.shutdown_stage_stats["dependency_close"] = {
                "status": "degraded",
                "errors": list(self._shutdown_dependency_close_errors),
            }
            self.runtime.status.shutdown_final_status = "degraded"
            self.runtime.status.shutdown_pending_drain = False
            self.runtime.status.shutdown_forced_termination_risk = True
            self.runtime.set_boot_phase("shutdown.degraded")
            self._termination_complete = False

    def _schedule_late_shutdown_cleanup(self, budget: Any) -> None:
        if not budget or not hasattr(budget, "wait_until_idle"):
            self.runtime.status.shutdown_final_status = "degraded"
            self.runtime.status.shutdown_forced_termination_risk = True
            self.runtime.status.shutdown_late_cleanup_task_count = 0
            self.runtime.set_boot_phase("shutdown.degraded")
            self.runtime.mark_degraded("shutdown.late_cleanup", "budget_idle_waiter_unavailable")
            logger.warning("[AstrMai] late shutdown cleanup unavailable: budget has no idle waiter")
            return
        existing = getattr(self, "_late_shutdown_cleanup_task", None)
        if existing is not None and not existing.done():
            return

        late_budget = self._shutdown_timing("shutdown_late_physical_drain_budget_sec", 30.0)
        late_started = time.monotonic()
        self._late_shutdown_cleanup_deadline_monotonic = late_started + late_budget
        self.runtime.status.shutdown_late_cleanup_deadline = time.time() + late_budget
        self.runtime.status.shutdown_late_cleanup_task_count = 1

        async def _mark_cleanup_cancelled() -> None:
            pending = self._shutdown_pending_report(budget)
            self._shutdown_pending_drain = True
            self._termination_complete = False
            self.runtime.status.shutdown_late_cleanup_task_count = 0
            self.runtime.status.shutdown_final_status = "degraded"
            self.runtime.status.shutdown_pending_drain = True
            self.runtime.status.shutdown_forced_termination_risk = True
            self.runtime.status.shutdown_stage_stats["late_cleanup"] = {
                "status": "cancelled",
                "elapsed_ms": round((time.monotonic() - late_started) * 1000, 3),
                "remaining": int(pending.get("remaining", 0) or 0),
                "remaining_by_kind": dict(pending.get("remaining_by_kind", {}) or {}),
                "owner_task_names": list(pending.get("owner_task_names", []) or []),
                "forced_termination_risk": True,
            }
            self.runtime.set_boot_phase("shutdown.degraded")
            self.runtime.mark_degraded("shutdown.cleanup_cancelled", "late_cleanup_cancelled")
            logger.warning(
                "[AstrMai] late shutdown cleanup cancelled; dependencies remain open: "
                f"remaining={pending.get('remaining', 0)}"
            )

        async def _close_dependencies() -> None:
            close_errors: list[str] = []
            try:
                close_memory_resources = getattr(
                    self.runtime.memory_engine,
                    "close_background_resources",
                    None,
                )
                if callable(close_memory_resources):
                    await close_memory_resources()
            except Exception as exc:
                close_errors.append(f"memory:{exc}")
                self.runtime.mark_degraded("shutdown.dependency_close", f"memory:{exc}")
                logger.warning(f"[AstrMai] late Memory resource shutdown degraded: {exc}")
            try:
                event_bus = getattr(self.runtime, "event_bus", None)
                stop_event_bus = getattr(event_bus, "stop", None)
                if callable(stop_event_bus):
                    await stop_event_bus()
            except Exception as exc:
                close_errors.append(f"event_bus:{exc}")
                self.runtime.mark_degraded("shutdown.dependency_close", f"event_bus:{exc}")
                logger.warning(f"[AstrMai] late EventBus shutdown degraded: {exc}")
            try:
                persistence = getattr(self.runtime, "persistence", None)
                dispose = getattr(persistence, "dispose", None)
                if callable(dispose):
                    dispose()
            except Exception as exc:
                close_errors.append(f"persistence:{exc}")
                self.runtime.mark_degraded("shutdown.dependency_close", f"persistence:{exc}")
                logger.warning(f"[AstrMai] late Persistence dispose degraded: {exc}")
            if close_errors:
                self._shutdown_dependency_close_errors = close_errors
                self._shutdown_pending_drain = False
                self._termination_complete = False
                self.runtime.status.shutdown_pending_drain = False
                self.runtime.status.shutdown_final_status = "degraded"
                self.runtime.status.shutdown_forced_termination_risk = True
                self.runtime.status.shutdown_late_cleanup_task_count = 0
                self.runtime.status.shutdown_stage_stats["dependency_close"] = {
                    "status": "degraded",
                    "errors": list(close_errors),
                }
                self.runtime.set_boot_phase("shutdown.degraded")
                return
            else:
                self._shutdown_pending_drain = False
                self._reset_runtime_status_flags()
                self.runtime.status.shutdown_pending_drain = False
                self.runtime.status.shutdown_final_status = "complete"
                self.runtime.status.shutdown_forced_termination_risk = False
                self.runtime.status.shutdown_late_cleanup_deadline = 0.0
                self.runtime.status.shutdown_late_cleanup_task_count = 0
                self.runtime.status.shutdown_stage_stats["late_cleanup"] = {
                    **dict(self.runtime.status.shutdown_stage_stats.get("late_cleanup", {}) or {}),
                    "status": "completed",
                    "remaining": 0,
                    "physical_owner_count": 0,
                }
                elapsed_ms = (time.monotonic() - self._shutdown_started_monotonic) * 1000
                self.runtime.status.shutdown_completed_at = time.time()
                self.runtime.status.last_shutdown_elapsed_ms = round(elapsed_ms, 3)
                self.runtime.set_boot_phase("shutdown.complete")
                self._termination_complete = True
                logger.info(f"[AstrMai] deferred shutdown cleanup complete elapsed_ms={elapsed_ms:.1f}")

        async def _wait_for_quiescence(timeout_sec: float | None) -> dict[str, Any]:
            deadline = None if timeout_sec is None else time.monotonic() + max(0.0, timeout_sec)
            while True:
                if deadline is None:
                    slice_timeout = 0.5
                else:
                    slice_timeout = min(0.5, max(0.0, deadline - time.monotonic()))
                await budget.wait_until_idle(timeout_sec=slice_timeout)
                pending = self._shutdown_pending_report(budget)
                if not pending["remaining"]:
                    return pending
                if deadline is not None and time.monotonic() >= deadline:
                    return pending
                await asyncio.sleep(0.05)

        async def _watch_late_shutdown_cleanup() -> None:
            try:
                pending = await _wait_for_quiescence(None)
            except asyncio.CancelledError:
                await _mark_cleanup_cancelled()
                raise
            if pending.get("remaining"):
                self.runtime.mark_degraded(
                    "shutdown.late_cleanup",
                    f"physical_background_work_remaining={pending.get('remaining', 0)}",
                )
                return
            await _close_dependencies()

        async def _cleanup() -> None:
            try:
                pending = await _wait_for_quiescence(late_budget)
            except asyncio.CancelledError:
                await _mark_cleanup_cancelled()
                raise
            except Exception as exc:
                self.runtime.mark_degraded("shutdown.late_cleanup", str(exc))
                pending = self._shutdown_pending_report(budget)
            if pending.get("remaining"):
                self.runtime.status.shutdown_stage_stats["late_cleanup"] = {
                    "status": "degraded",
                    "elapsed_ms": round((time.monotonic() - late_started) * 1000, 3),
                    "remaining": int(pending.get("remaining", 0) or 0),
                    "remaining_by_kind": dict(pending.get("remaining_by_kind", {}) or {}),
                    "active_by_kind": dict(pending.get("active_by_kind", {}) or {}),
                    "queued_by_kind": dict(pending.get("queued_by_kind", {}) or {}),
                    "deferred_by_kind": dict(pending.get("deferred_by_kind", {}) or {}),
                    "physical_owner_count": int(pending.get("physical", 0) or 0),
                    "owner_task_names": list(pending.get("owner_task_names", []) or []),
                    "oldest_owner_age_ms": float(pending.get("oldest_owner_age_ms", 0.0) or 0.0),
                    "deadline": self.runtime.status.shutdown_late_cleanup_deadline,
                    "forced_termination_risk": True,
                }
                self.runtime.status.shutdown_final_status = "degraded"
                self.runtime.status.shutdown_pending_drain = True
                self.runtime.status.shutdown_forced_termination_risk = True
                self.runtime.set_boot_phase("shutdown.degraded")
                self.runtime.mark_degraded(
                    "shutdown.late_cleanup",
                    f"physical_background_work_remaining={pending.get('remaining', 0)}",
                )
                logger.warning(
                    "[AstrMai] late shutdown drain deadline reached; dependencies remain open: "
                    f"remaining={pending.get('remaining', 0)} "
                    f"by_kind={pending.get('remaining_by_kind', {})} budget_sec={late_budget:.1f}"
                )
                try:
                    watcher = asyncio.create_task(
                        _watch_late_shutdown_cleanup(),
                        name="astrmai:shutdown:late-cleanup-watcher",
                    )
                except RuntimeError as exc:
                    await _mark_cleanup_cancelled()
                    logger.warning(f"[AstrMai] late shutdown watcher scheduling degraded: {exc}")
                    return
                self._late_shutdown_cleanup_task = watcher
                self._isolated_shutdown_tasks.add(watcher)
                watcher.add_done_callback(self._consume_isolated_shutdown_task)
                return
            await _close_dependencies()

        try:
            task = asyncio.create_task(_cleanup(), name="astrmai:shutdown:late-cleanup")
        except RuntimeError as exc:
            self.runtime.status.shutdown_late_cleanup_task_count = 0
            self.runtime.status.shutdown_final_status = "degraded"
            self.runtime.status.shutdown_pending_drain = True
            self.runtime.status.shutdown_forced_termination_risk = True
            self.runtime.set_boot_phase("shutdown.degraded")
            self.runtime.mark_degraded("shutdown.cleanup_cancelled", "late_cleanup_schedule_failed")
            logger.warning(f"[AstrMai] late shutdown cleanup scheduling degraded: {exc}")
            return
        self._late_shutdown_cleanup_task = task
        self._isolated_shutdown_tasks.add(task)
        task.add_done_callback(self._consume_isolated_shutdown_task)

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
        self.runtime.status.startup_blocked_reason = ""
        self.runtime.status.startup_retry_at = 0.0
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
