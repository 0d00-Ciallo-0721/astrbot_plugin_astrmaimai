from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

from astrbot.api import logger

from ..multimodal import init_meme_storage
from ..shared.helpers.plugin_helpers import cleanup_stale_focus_pools, collect_background_tasks, safe_create_task
from ..infrastructure.runtime.outbound_send_guard import OUTBOUND_SEND_GATE
from .runtime_instance_coordinator import RUNTIME_INSTANCE_COORDINATOR
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
        self._shutdown_fence_errors: dict[str, str] = {}
        self._persistence_dispose_task: asyncio.Task[Any] | None = None
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
        started = time.monotonic()
        try:
            await self.runtime.memory_engine.initialize()
            await self.runtime.memory_engine.start_background_tasks()
            self.runtime.status.memory_initialized = True
            self.runtime.status.startup_stage_timings["memory_initialize_ms"] = round(
                (time.monotonic() - started) * 1000.0, 1
            )
            self.runtime.status.startup_yield_count = int(
                getattr(self.runtime.memory_engine, "_startup_yield_count", 0) or 0
            )
        except Exception as exc:
            self.runtime.status.startup_stage_timings["memory_initialize_ms"] = round(
                (time.monotonic() - started) * 1000.0, 1
            )
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
        self.runtime.status.previous_runtime_generation = 0
        self.runtime.status.reload_wait_ms = 0.0
        self.runtime.status.reload_wait_timeout = False
        self.runtime.status.reload_wait_error = ""
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
        begin_budget_drain = getattr(budget, "begin_drain", None)
        budget_resume = resume_budget_if_idle if callable(resume_budget_if_idle) else resume_budget
        reopened = {
            "budget": False,
            "reread": False,
            "event_bus": False,
            "coordinator": False,
            "persona": False,
            "cron": False,
        }
        bound_collaboration: list[tuple[Any, str, Any]] = []

        async def _await_result(result: Any) -> Any:
            return await result if inspect.isawaitable(result) else result

        async def _rollback(stage: str, error: BaseException | str) -> bool:
            rollback_errors: list[str] = []
            status = getattr(self.runtime, "status", None)
            if status is not None:
                status.accepting_events = False
                status.startup_blocked_reason = f"reinitialize_failed:{stage}"
            if reopened["budget"] and callable(begin_budget_drain):
                try:
                    begin_budget_drain()
                except Exception as exc:
                    rollback_errors.append(f"budget:{type(exc).__name__}: {exc}")
            event_bus = getattr(self.runtime, "event_bus", None)
            trigger_abort = getattr(event_bus, "trigger_abort", None)
            if reopened["event_bus"] and callable(trigger_abort):
                try:
                    trigger_abort()
                except Exception as exc:
                    rollback_errors.append(f"event_bus:{type(exc).__name__}: {exc}")
            if reopened["reread"]:
                shutdown_reread = getattr(reread_dispatcher, "shutdown", None)
                if callable(shutdown_reread):
                    try:
                        await _await_result(
                            self._invoke_with_optional_keyword(
                                shutdown_reread,
                                "wait_for_active",
                                False,
                            )
                        )
                    except Exception as exc:
                        rollback_errors.append(f"reread:{type(exc).__name__}: {exc}")
            if reopened["coordinator"]:
                shutdown_coordinator = getattr(
                    getattr(self.runtime, "runtime_coordinator", None),
                    "shutdown",
                    None,
                )
                if callable(shutdown_coordinator):
                    try:
                        await _await_result(
                            self._invoke_with_optional_keyword(
                                shutdown_coordinator,
                                "timeout_sec",
                                self._shutdown_timing("shutdown_cancel_grace_sec", 1.0),
                            )
                        )
                    except Exception as exc:
                        rollback_errors.append(f"coordinator:{type(exc).__name__}: {exc}")
            if reopened["persona"]:
                stop_persona = getattr(
                    getattr(self.runtime, "persona_summarizer", None),
                    "stop",
                    None,
                )
                if callable(stop_persona):
                    try:
                        await _await_result(stop_persona())
                    except Exception as exc:
                        rollback_errors.append(f"persona:{type(exc).__name__}: {exc}")
            if reopened["cron"]:
                stop_cron = getattr(getattr(self.runtime, "cron_guard", None), "stop", None)
                if callable(stop_cron):
                    try:
                        await _await_result(stop_cron())
                    except Exception as exc:
                        rollback_errors.append(f"cron:{type(exc).__name__}: {exc}")
            event_bus = getattr(self.runtime, "event_bus", None)
            unsubscribe = getattr(event_bus, "unsubscribe", None)
            if callable(unsubscribe):
                for bus, topic, callback in reversed(bound_collaboration):
                    try:
                        unsubscribe(topic, callback)
                    except Exception as exc:
                        rollback_errors.append(
                            f"collaboration:{topic}:{type(exc).__name__}: {exc}"
                        )
            detail = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
            stage_stats = getattr(status, "shutdown_stage_stats", None)
            if isinstance(stage_stats, dict):
                stage_stats["reinitialize"] = {
                    "status": "rolled_back",
                    "failed_stage": stage,
                    "error": detail[:500],
                    "rollback_errors": rollback_errors,
                }
            mark_degraded = getattr(self.runtime, "mark_degraded", None)
            if callable(mark_degraded):
                mark_degraded("lifecycle.reinitialize", f"{stage}: {detail}"[:500])
            logger.warning(
                f"[AstrMai] runtime reinitialize rolled back stage={stage} "
                f"error={detail} rollback_errors={rollback_errors}"
            )
            return False
        if callable(can_resume_budget):
            try:
                budget_ready = can_resume_budget()
            except Exception as exc:
                logger.warning(f"[AstrMai] runtime reinitialize deferred: budget status failed: {exc}")
                return False
            if budget_ready is False:
                logger.warning("[AstrMai] runtime reinitialize deferred: background work is still draining")
                return False
            reopened["budget"] = callable(budget_resume)
            try:
                budget_resumed = budget_resume() if callable(budget_resume) else True
            except Exception as exc:
                return await _rollback("budget.resume", exc)
            if budget_resumed is False:
                logger.warning("[AstrMai] runtime reinitialize deferred: background work is still draining")
                return False
        else:
            reopened["budget"] = callable(budget_resume)
            try:
                budget_resumed = budget_resume() if callable(budget_resume) else True
            except Exception as exc:
                return await _rollback("budget.resume", exc)
            if budget_resumed is False:
                logger.warning("[AstrMai] runtime reinitialize deferred: background work is still draining")
                return False
        reopened["reread"] = callable(resume_reread)
        try:
            reread_resumed = resume_reread() if callable(resume_reread) else True
        except Exception as exc:
            return await _rollback("reread.resume", exc)
        if reread_resumed is False:
            return await _rollback("reread.resume", "dispatcher still has pending work")
        event_bus = getattr(self.runtime, "event_bus", None)
        reset_abort = getattr(event_bus, "reset_abort", None)
        if callable(reset_abort):
            reopened["event_bus"] = True
            try:
                reset_abort()
            except Exception as exc:
                return await _rollback("event_bus.reset_abort", exc)

        coordinator = getattr(self.runtime, "runtime_coordinator", None)
        reopen_coordinator = getattr(coordinator, "reopen", None)
        if callable(reopen_coordinator):
            reopened["coordinator"] = True
            try:
                result = await _await_result(reopen_coordinator())
                if result is False:
                    raise RuntimeError("coordinator reopen returned false")
            except Exception as exc:
                return await _rollback("coordinator.reopen", exc)

        persona_summarizer = getattr(self.runtime, "persona_summarizer", None)
        reopen_persona = getattr(persona_summarizer, "reopen", None)
        if callable(reopen_persona):
            reopened["persona"] = True
            try:
                result = await _await_result(reopen_persona())
                if result is False:
                    raise RuntimeError("persona reopen returned false")
            except Exception as exc:
                return await _rollback("persona.reopen", exc)

        cron_guard = getattr(self.runtime, "cron_guard", None)
        start_cron_guard = getattr(cron_guard, "start", None)
        if callable(start_cron_guard):
            reopened["cron"] = True
            try:
                result = await _await_result(start_cron_guard())
                if result is False:
                    raise RuntimeError("cron start returned false")
            except Exception as exc:
                return await _rollback("cron.start", exc)

        try:
            bound_collaboration = self._bind_learning_collaboration()
        except Exception as exc:
            return await _rollback("learning_collaboration.bind", exc)
        return True

    def _bind_learning_collaboration(self) -> list[tuple[Any, str, Any]]:
        event_bus = getattr(self.runtime, "event_bus", None)
        state_engine = getattr(self.runtime, "state_engine", None)
        memory_engine = getattr(self.runtime, "memory_engine", None)
        if event_bus is None or state_engine is None or memory_engine is None:
            return []
        bound: list[tuple[Any, str, Any]] = []
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
        try:
            for topic, callback in bindings:
                if callable(callback):
                    event_bus.subscribe(topic, callback)
                    bound.append((event_bus, topic, callback))
        except Exception:
            unsubscribe = getattr(event_bus, "unsubscribe", None)
            if callable(unsubscribe):
                for _bus, topic, callback in reversed(bound):
                    try:
                        unsubscribe(topic, callback)
                    except Exception as rollback_exc:
                        logger.warning(
                            "[AstrMai] learning collaboration partial bind rollback degraded "
                            f"topic={topic}: {rollback_exc}"
                        )
            raise
        return bound

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

    async def _await_runtime_reload_fence(self) -> bool:
        """Wait for the previous facade before touching shared resources."""
        handle = getattr(self.runtime, "runtime_previous_termination", None)
        generation = int(getattr(self.runtime, "runtime_generation", 0) or 0)
        facade = getattr(self.runtime, "runtime_facade", None)
        status = self.runtime.status
        registration_error = str(getattr(self.runtime, "runtime_registration_error", "") or "").strip()
        registration = getattr(self.runtime, "runtime_registration", None)
        status.runtime_generation = generation
        if registration_error:
            status.accepting_events = False
            status.is_running = False
            status.lifecycle_started = False
            status.startup_blocked_reason = "runtime_registration_failed"
            status.reload_wait_error = registration_error
            self.runtime.set_boot_phase("lifecycle.reload_deferred")
            self.runtime.mark_degraded("runtime.reload_fence", registration_error)
            logger.warning("[AstrMai] startup deferred: runtime registration failed: %s", registration_error)
            return False
        if facade is not None and registration is None:
            reason = "runtime registration metadata is missing"
            status.accepting_events = False
            status.is_running = False
            status.lifecycle_started = False
            status.startup_blocked_reason = "runtime_registration_unknown"
            status.reload_wait_error = reason
            self.runtime.set_boot_phase("lifecycle.reload_deferred")
            self.runtime.mark_degraded("runtime.reload_fence", reason)
            logger.warning("[AstrMai] startup deferred: %s", reason)
            return False
        if registration is not None:
            try:
                generation = int(getattr(registration, "generation", generation) or generation)
                handle = getattr(registration, "previous_termination", handle)
                status.runtime_generation = generation
            except (TypeError, ValueError):
                reason = "runtime registration generation is invalid"
                status.accepting_events = False
                status.startup_blocked_reason = "runtime_registration_unknown"
                status.reload_wait_error = reason
                self.runtime.set_boot_phase("lifecycle.reload_deferred")
                self.runtime.mark_degraded("runtime.reload_fence", reason)
                return False
        if handle is not None:
            timeout = self._shutdown_timing("hot_reload_startup_wait_sec", 10.0)
            previous_meta = RUNTIME_INSTANCE_COORDINATOR.describe().get("terminations", [])
            if previous_meta:
                try:
                    status.previous_runtime_generation = int(previous_meta[-1].get("generation", 0) or 0)
                except (TypeError, ValueError):
                    status.previous_runtime_generation = 0
            wait_started = time.monotonic()
            ok, reason = await RUNTIME_INSTANCE_COORDINATOR.wait_for_previous_termination(
                handle,
                timeout_sec=timeout,
            )
            status.reload_wait_ms = round((time.monotonic() - wait_started) * 1000, 3)
            if not ok:
                status.accepting_events = False
                status.is_running = False
                status.lifecycle_started = False
                status.reload_wait_timeout = "timed out" in reason.lower()
                status.reload_wait_error = reason
                status.startup_blocked_reason = "previous_facade_termination_pending"
                status.startup_retry_at = time.time() + max(1.0, timeout)
                self.runtime.set_boot_phase("lifecycle.reload_deferred")
                self.runtime.mark_degraded("runtime.reload_fence", reason)
                logger.warning("[AstrMai] startup deferred until previous facade terminates: %s", reason)
                return False
            status.reload_wait_timeout = False
            status.reload_wait_error = ""
        if facade is not None and generation:
            if not RUNTIME_INSTANCE_COORDINATOR.claim_resource_owner(facade, generation):
                self.runtime.status.accepting_events = False
                self.runtime.status.startup_blocked_reason = "runtime_generation_not_current"
                self.runtime.set_boot_phase("lifecycle.reload_deferred")
                self.runtime.mark_degraded("runtime.reload_fence", "facade generation was superseded")
                return False
            store = getattr(self.runtime, "dialogue_store", None)
            if store is not None:
                try:
                    store.runtime_generation = generation
                    store.runtime_resource_guard = lambda: RUNTIME_INSTANCE_COORDINATOR.can_use_shared_resources(
                        facade, generation
                    )
                except Exception as exc:
                    self.runtime.mark_degraded("runtime.reload_fence", f"dialogue store lease bind failed: {exc}")
                    return False
        return True

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
        if not await self._await_runtime_reload_fence():
            return
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
        attention_gate = getattr(self.runtime, "attention_gate", None)
        mark_attention_started = getattr(attention_gate, "mark_runtime_started", None)
        if callable(mark_attention_started):
            mark_attention_started()
        OUTBOUND_SEND_GATE.open()
        self.runtime.set_boot_phase("runtime.running")
        schedule_vector = getattr(getattr(self.runtime, "memory_engine", None), "schedule_vector_bootstrap_after_startup", None)
        if callable(schedule_vector):
            schedule_vector(delay_sec=0.25)
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

    def _apply_shutdown_fences(self) -> dict[str, str]:
        errors = dict(getattr(self, "_shutdown_fence_errors", {}) or {})
        memory_engine = getattr(self.runtime, "memory_engine", None)
        fences = (
            (
                "background_budget.begin_drain",
                getattr(getattr(self.runtime, "background_task_budget", None), "begin_drain", None),
            ),
            ("memory.engine.begin_shutdown", getattr(memory_engine, "begin_shutdown", None)),
            (
                "memory.pipeline.begin_shutdown",
                getattr(getattr(memory_engine, "memory_pipeline", None), "begin_shutdown", None),
            ),
            (
                "memory.projector.begin_shutdown",
                getattr(getattr(memory_engine, "index_projector", None), "begin_shutdown", None),
            ),
        )
        for name, fence in fences:
            if not callable(fence):
                continue
            try:
                fence()
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"[:500]
                logger.warning(f"[AstrMai] shutdown fence degraded name={name}: {exc}")
            else:
                errors.pop(name, None)
        self._shutdown_fence_errors = errors
        if errors:
            self.runtime.status.shutdown_stage_stats["shutdown_fence"] = {
                "status": "pending_drain",
                "errors": dict(errors),
            }
        return dict(errors)

    def _apply_fence_errors_to_pending_report(self, report: dict[str, Any]) -> dict[str, Any]:
        errors = dict(getattr(self, "_shutdown_fence_errors", {}) or {})
        if not errors:
            return report
        remaining_by_kind = dict(report.get("remaining_by_kind", {}) or {})
        for name in errors:
            remaining_by_kind[f"shutdown_fence.{name}"] = 1
        report["remaining"] = max(1, int(report.get("remaining", 0) or 0))
        report["has_pending"] = True
        report["remaining_by_kind"] = remaining_by_kind
        report["shutdown_fence_errors"] = errors
        return self._recompute_pending_report(report)

    @staticmethod
    def _recompute_pending_report(report: dict[str, Any]) -> dict[str, Any]:
        """Re-derive pending fields after merging component diagnostics."""
        scalar_fields = (
            "active",
            "queued",
            "deferred",
            "physical",
            "worker_count",
            "projection_count",
            "pipeline_count",
            "physical_index_future_count",
            "vector_retirement_count",
            "vector_candidate_build_count",
            "vector_candidate_physical_count",
            "vector_candidate_path_count",
            "vector_sync_retirement_count",
            "vector_close_owner_count",
            "retired_vector_stack_count",
            "isolated_shutdown_task_count",
        )
        values = []
        for field in scalar_fields:
            value = report.get(field, 0)
            if isinstance(value, bool):
                value = int(value)
            if isinstance(value, int) and value >= 0:
                values.append(value)
        nested_max = 0
        remaining_by_kind = report.get("remaining_by_kind")
        if isinstance(remaining_by_kind, dict):
            for value in remaining_by_kind.values():
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    nested_max = max(nested_max, value)
        owner_names = report.get("owner_task_names")
        owner_count = len(owner_names) if isinstance(owner_names, (list, tuple, set, frozenset)) else 0
        unknown = bool(report.get("unknown_components"))
        remaining = max(
            [int(report.get("remaining", 0) or 0), nested_max, owner_count, int(unknown), *values]
        )
        report["remaining"] = remaining
        report["has_pending"] = bool(remaining)
        if isinstance(remaining_by_kind, dict):
            report["remaining_total"] = sum(
                value
                for value in remaining_by_kind.values()
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            )
            report["remaining_by_category_total"] = report["remaining_total"]
            report["category_sum"] = report["remaining_total"]
            report["category_count_sum"] = report["remaining_total"]
            report["remaining_max_by_category"] = nested_max
        report["unique_remaining_owner_count"] = max(remaining, owner_count)
        report["unique_owner_count_estimate"] = report["unique_remaining_owner_count"]
        return report

    @staticmethod
    def _validate_budget_drain_report(report: Any) -> dict[str, Any]:
        if not isinstance(report, dict):
            raise TypeError(f"report must be dict, got {type(report).__name__}")
        normalized = dict(report)

        def _count(value: Any, field: str) -> int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field} must be a non-negative integer")
            if value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
            return value

        for field in (
            "observed",
            "remaining",
            "active",
            "queued",
            "queued_waiters",
            "deferred",
            "physical",
            "physical_owner_count",
        ):
            if field in normalized:
                normalized[field] = _count(normalized[field], field)
        normalized.setdefault("observed", 0)
        normalized.setdefault("remaining", 0)
        for field in ("active_by_kind", "queued_by_kind", "deferred_by_kind"):
            value = normalized.get(field)
            if value is None:
                continue
            if not isinstance(value, dict):
                raise TypeError(f"{field} must be dict, got {type(value).__name__}")
            normalized[field] = {
                str(name): _count(count, f"{field}.{name}")
                for name, count in value.items()
            }
        owner_names = normalized.get("owner_task_names")
        if owner_names is not None:
            if not isinstance(owner_names, (list, tuple, set, frozenset)):
                raise TypeError(
                    "owner_task_names must be a collection, "
                    f"got {type(owner_names).__name__}"
                )
            if any(not isinstance(name, str) for name in owner_names):
                raise TypeError("owner_task_names entries must be strings")
            normalized["owner_task_names"] = list(owner_names)
        return normalized

    @staticmethod
    def _invoke_with_optional_keyword(callable_obj: Any, name: str, value: Any) -> Any:
        kwargs: dict[str, Any] = {}
        try:
            signature = inspect.signature(callable_obj)
        except (TypeError, ValueError):
            pass
        else:
            try:
                signature.bind(**{name: value})
            except TypeError:
                pass
            else:
                kwargs[name] = value
        return callable_obj(**kwargs)

    def begin_shutdown(self) -> None:
        """Fence new ingress synchronously before asynchronous cleanup."""
        if self._shutdown_requested:
            return
        self._terminated = True
        self._shutdown_requested = True
        # Close outbound side effects synchronously.  Late cleanup callbacks
        # may finish internal work, but must not send after unload begins.
        OUTBOUND_SEND_GATE.close(enforce_provider=True)
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
        self._apply_shutdown_fences()
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
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            self._shutdown_pending_drain = True
                            self._termination_complete = False
                            self.runtime.status.shutdown_pending_drain = True
                            self.runtime.status.shutdown_final_status = "degraded"
                            self.runtime.status.shutdown_forced_termination_risk = True
                            self.runtime.set_boot_phase("shutdown.degraded")
                            self.runtime.mark_degraded("shutdown.reread_late_cleanup", str(exc))
                            logger.warning(f"[AstrMai] late reread cleanup degraded: {exc}")
                            return

                        try:
                            self.stop_visual_services()
                            tasks = collect_background_tasks(*self.runtime.iter_task_owners())
                            current = asyncio.current_task()
                            for pending_task in dict.fromkeys(
                                task for task in tasks if task is not None and task is not current
                            ):
                                if not pending_task.done():
                                    pending_task.cancel()
                        except Exception as exc:
                            logger.warning(f"[AstrMai] late reread tail fencing degraded: {exc}")
                        budget = getattr(self.runtime, "background_task_budget", None)
                        begin_drain = getattr(budget, "begin_drain", None)
                        if callable(begin_drain):
                            try:
                                begin_drain()
                            except Exception as exc:
                                logger.warning(f"[AstrMai] late reread budget drain degraded: {exc}")
                        if self._late_shutdown_cleanup_task is asyncio.current_task():
                            self._late_shutdown_cleanup_task = None
                        self._schedule_late_shutdown_cleanup(budget)

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
        fence_errors = self._apply_shutdown_fences()
        errors.extend(f"{name}:{error}" for name, error in fence_errors.items())
        pending_report = self._apply_fence_errors_to_pending_report(
            self._shutdown_pending_report(budget)
        )
        pending_budget = int(pending_report.get("remaining", 0) or 0)
        if pending_report.get("vector_stack_present"):
            self._shutdown_pending_drain = True
            self.runtime.status.shutdown_pending_drain = True
            self.runtime.status.shutdown_final_status = "pending_drain"
            if budget is not None and hasattr(budget, "wait_until_idle"):
                self._schedule_late_shutdown_cleanup(budget)
            else:
                close_memory_resources = getattr(
                    getattr(self.runtime, "memory_engine", None),
                    "close_background_resources",
                    None,
                )
                if callable(close_memory_resources):
                    async def _close_memory_then_resume() -> None:
                        closed = await close_memory_resources()
                        if closed is not False:
                            self._force_shutdown_tail(wait_dispatcher=False)

                    self._schedule_forced_shutdown_cleanup(
                        "memory_resources",
                        _close_memory_then_resume,
                    )
            logger.warning("[AstrMai] vector stack remains open; dependency close deferred")
            return
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
        persistence = getattr(self.runtime, "persistence", None)
        dispose = getattr(persistence, "dispose", None)
        if callable(stop_event_bus) or callable(dispose):
            async def _close_tail_dependencies() -> None:
                if callable(stop_event_bus):
                    await stop_event_bus()
                    describe_event_bus = getattr(event_bus, "describe_status", None)
                    if callable(describe_event_bus):
                        status = describe_event_bus() or {}
                        pending_count = status.get("pending_stop_task_count", 0)
                        if (
                            isinstance(pending_count, bool)
                            or not isinstance(pending_count, int)
                            or pending_count < 0
                        ):
                            raise RuntimeError("event_bus pending_stop_task_count is invalid")
                        if pending_count:
                            self._shutdown_pending_drain = True
                            self.runtime.status.shutdown_pending_drain = True
                            self.runtime.status.shutdown_final_status = "pending_drain"
                            self.runtime.status.shutdown_forced_termination_risk = True
                            raise RuntimeError(
                                f"event_bus pending_stop_tasks={pending_count}"
                            )
                if callable(dispose):
                    disposed = await self._dispose_persistence_bounded(
                        persistence,
                        timeout_sec=self._shutdown_timing("shutdown_cancel_grace_sec", 1.0),
                    )
                    if not disposed:
                        self._shutdown_pending_drain = True
                        self.runtime.status.shutdown_pending_drain = True
                        self.runtime.status.shutdown_final_status = "pending_drain"
                        self.runtime.status.shutdown_forced_termination_risk = True
                        raise RuntimeError("persistence dispose pending")

            self._schedule_forced_shutdown_cleanup("tail_dependencies", _close_tail_dependencies)

        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        self.runtime.status.shutdown_stage_stats["forced_tail"] = {
            "status": "degraded" if errors else "completed",
            "elapsed_ms": elapsed_ms,
            "errors": errors,
        }

    @staticmethod
    def _background_budget_pending(budget: Any) -> int:
        if budget is None:
            return 0
        status_fn = getattr(budget, "status", None)
        if not callable(status_fn):
            return 1
        try:
            status = status_fn() or {}
        except Exception:
            return 1
        if not isinstance(status, dict):
            return 1
        try:
            return max(
                max(0, int(status.get("active", 0) or 0)),
                max(0, int(status.get("queued", 0) or 0)),
                max(0, int(status.get("deferred_tasks", 0) or 0)),
                max(0, int(status.get("physical_owner_count", 0) or 0)),
            )
        except (TypeError, ValueError, OverflowError):
            return 1

    def _shutdown_pending_report(self, budget: Any, *, include_late_task: bool = False) -> dict[str, Any]:
        diagnostics_errors: dict[str, str] = {}
        unknown_components: set[str] = set()

        def _mark_unknown(component: str, reason: str) -> None:
            unknown_components.add(component)
            diagnostics_errors.setdefault(component, str(reason)[:500])

        def _safe_int(value: Any, component: str, field: str) -> int:
            if value is None:
                return 0
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                _mark_unknown(component, f"invalid {field}: {type(value).__name__}")
                return 0
            return value

        def _safe_float(value: Any, component: str, field: str) -> float:
            try:
                return max(0.0, float(value or 0.0))
            except (TypeError, ValueError, OverflowError):
                _mark_unknown(component, f"invalid {field}: {type(value).__name__}")
                return 0.0

        def _safe_mapping(value: Any, component: str, field: str) -> dict[str, Any]:
            if value is None:
                return {}
            if not isinstance(value, dict):
                _mark_unknown(component, f"invalid {field}: {type(value).__name__}")
                return {}
            return value

        def _read_status(component: str, target: Any, method_name: str) -> dict[str, Any]:
            if target is None:
                return {}
            status_fn = getattr(target, method_name, None)
            if not callable(status_fn):
                _mark_unknown(component, f"missing {method_name}()")
                return {}
            try:
                status = status_fn()
            except Exception as exc:
                _mark_unknown(component, f"{type(exc).__name__}: {exc}")
                return {}
            if not isinstance(status, dict):
                _mark_unknown(component, f"invalid status: {type(status).__name__}")
                return {}
            return status

        def _read_vector_owner_status(target: Any) -> dict[str, Any]:
            if target is None:
                return {}
            describe = getattr(target, "describe_shutdown_owners", None)
            if callable(describe):
                return _read_status("memory.vector_owners", target, "describe_shutdown_owners")
            legacy_fields = (
                "_vector_retirement_tasks",
                "_vector_candidate_build_tasks",
                "_vector_candidate_futures",
                "_vector_candidate_paths",
                "_vector_sync_retirement_futures",
                "_vector_close_tasks",
                "_retired_vector_stacks",
            )
            if not any(hasattr(target, field) for field in legacy_fields):
                return {}
            registry_lock = getattr(target, "_vector_registry_lock", None)
            if registry_lock is None or not hasattr(registry_lock, "__enter__"):
                _mark_unknown(
                    "memory.vector_owners",
                    "legacy vector owner registry is missing _vector_registry_lock",
                )
                return {}
            try:
                with registry_lock:
                    retirement_tasks = list(getattr(target, "_vector_retirement_tasks", set()) or set())
                    candidate_tasks = list(getattr(target, "_vector_candidate_build_tasks", set()) or set())
                    candidate_futures = list(getattr(target, "_vector_candidate_futures", set()) or set())
                    sync_futures = list(getattr(target, "_vector_sync_retirement_futures", set()) or set())
                    candidate_path_count = len(
                        getattr(target, "_vector_candidate_paths", set()) or set()
                    )
                    retired_stack_count = len(
                        getattr(target, "_retired_vector_stacks", {}) or {}
                    )
                    close_owners = [
                        owner
                        for _resource, owner in (
                            getattr(target, "_vector_close_tasks", {}) or {}
                        ).values()
                    ]
                running_retirements = [task for task in retirement_tasks if task is not None and not task.done()]
                running_candidates = [task for task in candidate_tasks if task is not None and not task.done()]
                running_candidate_futures = [
                    future for future in candidate_futures if future is not None and not future.done()
                ]
                running_sync_futures = [
                    future for future in sync_futures if future is not None and not future.done()
                ]
                running_close_owners = [
                    owner for owner in close_owners if owner is not None and not owner.done()
                ]
                owner_names = []
                for owner, fallback in (
                    *((task, "memory.vector_retirement") for task in running_retirements),
                    *((task, "memory.vector_candidate_build") for task in running_candidates),
                    *((owner, "memory.vector_close_owner") for owner in running_close_owners),
                ):
                    get_name = getattr(owner, "get_name", None)
                    owner_names.append(str(get_name()) if callable(get_name) else fallback)
                owner_names.extend(
                    "astrmai-vector-candidate-physical" for _future in running_candidate_futures
                )
                owner_names.extend(
                    "astrmai-vector-sync-retirement" for _future in running_sync_futures
                )
                return {
                    "vector_retirement_count": len(running_retirements),
                    "vector_candidate_build_count": len(running_candidates),
                    "vector_candidate_physical_count": len(running_candidate_futures),
                    "vector_candidate_path_count": candidate_path_count,
                    "vector_sync_retirement_count": len(running_sync_futures),
                    "vector_close_owner_count": len(running_close_owners),
                    "retired_vector_stack_count": retired_stack_count,
                    "owner_task_names": owner_names,
                }
            except Exception as exc:
                _mark_unknown(
                    "memory.vector_owners",
                    f"legacy snapshot {type(exc).__name__}: {exc}",
                )
                return {}

        budget_status = _read_status("background_budget", budget, "status")
        budget_state_unknown = "background_budget" in unknown_components
        remaining_by_kind: dict[str, int] = {}
        for field in ("active_by_kind", "queued_by_kind", "deferred_by_kind"):
            for name, count in _safe_mapping(
                budget_status.get(field), "background_budget", field
            ).items():
                normalized_count = _safe_int(count, "background_budget", f"{field}.{name}")
                if normalized_count > 0:
                    remaining_by_kind[str(name)] = max(
                        remaining_by_kind.get(str(name), 0), normalized_count
                    )
        attention = getattr(self.runtime, "attention_gate", None)
        attention_status = _read_status("attention", attention, "describe_status")
        worker_count = _safe_int(
            attention_status.get("worker_count"), "attention", "worker_count"
        )
        if worker_count:
            remaining_by_kind["attention.worker"] = worker_count
        memory_engine = getattr(self.runtime, "memory_engine", None)
        projector = getattr(memory_engine, "index_projector", None)
        projector_status = _read_status(
            "memory.projector", projector, "describe_status"
        )
        projection_count = max(
            _safe_int(projector_status.get("projection_inflight_count"), "memory.projector", "projection_inflight_count"),
            _safe_int(projector_status.get("projection_background_task_count"), "memory.projector", "projection_background_task_count"),
            _safe_int(projector_status.get("durable_cleanup_task_count"), "memory.projector", "durable_cleanup_task_count"),
        )
        if projection_count:
            remaining_by_kind["memory.projection"] = projection_count
        pipeline = getattr(memory_engine, "memory_pipeline", None)
        pipeline_status = _read_status(
            "memory.pipeline", pipeline, "describe_runtime_status"
        )
        pipeline_count = max(
            _safe_int(pipeline_status.get("active_worker_count"), "memory.pipeline", "active_worker_count"),
            _safe_int(pipeline_status.get("stopping_worker_count"), "memory.pipeline", "stopping_worker_count"),
            int(bool(pipeline_status.get("sweep_task_running"))),
        )
        if pipeline_count:
            remaining_by_kind["memory.pipeline"] = pipeline_count
        vector_retriever = getattr(memory_engine, "vec_retriever", None)
        vector_stack_present = bool(
            getattr(memory_engine, "faiss_db", None) is not None
            or vector_retriever is not None
            or getattr(memory_engine, "retriever", None) is not None
        )
        vector_status = _read_status(
            "memory.vector", vector_retriever, "describe_status"
        )
        physical_future_count = _safe_int(
            vector_status.get("physical_index_future_count"),
            "memory.vector",
            "physical_index_future_count",
        )
        if physical_future_count:
            remaining_by_kind["memory.faiss_physical"] = physical_future_count
        if vector_stack_present:
            remaining_by_kind["memory.vector_stack"] = 1
        event_bus = getattr(self.runtime, "event_bus", None)
        event_bus_status = {}
        if event_bus is not None and (
            callable(getattr(event_bus, "describe_status", None))
            or hasattr(event_bus, "_pending_stop_tasks")
        ):
            event_bus_status = _read_status("event_bus", event_bus, "describe_status")
            event_task_count = max(
                _safe_int(event_bus_status.get("background_task_count"), "event_bus", "background_task_count"),
                _safe_int(event_bus_status.get("pending_stop_task_count"), "event_bus", "pending_stop_task_count"),
            )
            if event_task_count:
                remaining_by_kind["event_bus.task"] = event_task_count
        vector_owner_status = _read_vector_owner_status(memory_engine)
        retirement_count = _safe_int(
            vector_owner_status.get("vector_retirement_count"),
            "memory.vector_owners",
            "vector_retirement_count",
        )
        candidate_build_count = _safe_int(
            vector_owner_status.get("vector_candidate_build_count"),
            "memory.vector_owners",
            "vector_candidate_build_count",
        )
        candidate_physical_count = _safe_int(
            vector_owner_status.get("vector_candidate_physical_count"),
            "memory.vector_owners",
            "vector_candidate_physical_count",
        )
        candidate_path_count = _safe_int(
            vector_owner_status.get("vector_candidate_path_count"),
            "memory.vector_owners",
            "vector_candidate_path_count",
        )
        sync_retirement_count = _safe_int(
            vector_owner_status.get("vector_sync_retirement_count"),
            "memory.vector_owners",
            "vector_sync_retirement_count",
        )
        close_owner_count = _safe_int(
            vector_owner_status.get("vector_close_owner_count"),
            "memory.vector_owners",
            "vector_close_owner_count",
        )
        retired_stack_count = _safe_int(
            vector_owner_status.get("retired_vector_stack_count"),
            "memory.vector_owners",
            "retired_vector_stack_count",
        )
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        isolated_shutdown_tasks = [
            task
            for task in (getattr(self, "_isolated_shutdown_tasks", set()) or set())
            if task is not None and task is not current_task and not task.done()
        ]
        isolated_shutdown_count = len(isolated_shutdown_tasks)
        if retirement_count:
            remaining_by_kind["memory.vector_retirement"] = retirement_count
        if candidate_build_count:
            remaining_by_kind["memory.vector_candidate_build"] = candidate_build_count
        if candidate_physical_count:
            remaining_by_kind["memory.vector_candidate_physical"] = candidate_physical_count
        if candidate_path_count:
            remaining_by_kind["memory.vector_candidate_path"] = candidate_path_count
        if sync_retirement_count:
            remaining_by_kind["memory.vector_sync_retirement"] = sync_retirement_count
        if close_owner_count:
            remaining_by_kind["memory.vector_close_owner"] = close_owner_count
        if retired_stack_count:
            remaining_by_kind["memory.retired_vector_stack"] = retired_stack_count
        if isolated_shutdown_count:
            remaining_by_kind["shutdown.isolated_task"] = isolated_shutdown_count
        lifecycle_tasks = {
            "memory.vector_bootstrap": getattr(memory_engine, "_vector_bootstrap_task", None),
            "memory.projection_replay": getattr(memory_engine, "_projection_ready_replay_task", None),
        }
        for kind, task in lifecycle_tasks.items():
            if task is not None and not task.done():
                remaining_by_kind[kind] = 1
        budget_active = _safe_int(budget_status.get("active"), "background_budget", "active")
        budget_queued = _safe_int(budget_status.get("queued"), "background_budget", "queued")
        budget_deferred = _safe_int(
            budget_status.get("deferred", budget_status.get("deferred_tasks")),
            "background_budget",
            "deferred",
        )
        budget_physical = _safe_int(
            budget_status.get("physical", budget_status.get("physical_owner_count")),
            "background_budget",
            "physical",
        )
        for component in sorted(unknown_components):
            remaining_by_kind[f"{component}.state_unknown"] = 1
        remaining = max(
            budget_active,
            budget_queued,
            budget_deferred,
            budget_physical,
            worker_count,
            projection_count,
            pipeline_count,
            physical_future_count,
            int(vector_stack_present),
            retirement_count,
            candidate_build_count,
            candidate_physical_count,
            candidate_path_count,
            sync_retirement_count,
            close_owner_count,
            retired_stack_count,
            isolated_shutdown_count,
            int(bool(unknown_components)),
            *(1 for task in lifecycle_tasks.values() if task is not None and not task.done()),
        )
        if include_late_task and int(getattr(self.runtime.status, "shutdown_late_cleanup_task_count", 0) or 0):
            remaining = max(remaining, 1)
            remaining_by_kind["shutdown.late_cleanup"] = 1
        raw_owner_names = budget_status.get("owner_task_names")
        if raw_owner_names is None:
            owner_names = []
        elif isinstance(raw_owner_names, (list, tuple, set, frozenset)):
            owner_names = raw_owner_names
        else:
            _mark_unknown("background_budget", f"invalid owner_task_names: {type(raw_owner_names).__name__}")
            owner_names = []
            remaining_by_kind["background_budget.state_unknown"] = 1
            remaining = max(remaining, 1)
        owner_task_names = set(str(name) for name in owner_names)
        for task in (getattr(projector, "_background_tasks", set()) or set()):
            if task is not None and not task.done():
                owner_task_names.add(str(task.get_name() if hasattr(task, "get_name") else "memory.projection"))
        raw_vector_owner_names = vector_owner_status.get("owner_task_names")
        if raw_vector_owner_names is None:
            vector_owner_names = []
        elif isinstance(raw_vector_owner_names, (list, tuple, set, frozenset)):
            vector_owner_names = raw_vector_owner_names
        else:
            _mark_unknown(
                "memory.vector_owners",
                f"invalid owner_task_names: {type(raw_vector_owner_names).__name__}",
            )
            vector_owner_names = []
        owner_task_names.update(str(name) for name in vector_owner_names)
        if event_bus_status.get("pending_stop_task_count"):
            owner_task_names.update(
                f"eventbus:pending-stop:{index}"
                for index in range(
                    int(event_bus_status.get("pending_stop_task_count", 0) or 0)
                )
            )
        for task in lifecycle_tasks.values():
            if task is not None and not task.done():
                owner_task_names.add(str(task.get_name() if hasattr(task, "get_name") else "memory.lifecycle"))
        for task in isolated_shutdown_tasks:
            owner_task_names.add(
                str(task.get_name() if hasattr(task, "get_name") else "shutdown.isolated_task")
            )
        oldest_owner_age_ms = _safe_float(
            budget_status.get("oldest_owner_age_ms"),
            "background_budget",
            "oldest_owner_age_ms",
        )
        active_by_kind = _safe_mapping(
            budget_status.get("active_by_kind"), "background_budget", "active_by_kind"
        )
        queued_by_kind = _safe_mapping(
            budget_status.get("queued_by_kind"), "background_budget", "queued_by_kind"
        )
        deferred_by_kind = _safe_mapping(
            budget_status.get("deferred_by_kind"), "background_budget", "deferred_by_kind"
        )
        for component in sorted(unknown_components):
            remaining_by_kind[f"{component}.state_unknown"] = 1
        remaining = max(
            remaining,
            int(bool(unknown_components)),
            max(remaining_by_kind.values(), default=0),
            len(owner_task_names),
        )
        category_sum = sum(remaining_by_kind.values())
        unique_remaining_owner_count = max(int(remaining or 0), len(owner_task_names))
        diagnostics_error = "; ".join(
            f"{component}: {reason}"
            for component, reason in sorted(diagnostics_errors.items())
        )[:1000]
        budget_state_unknown = "background_budget" in unknown_components
        return {
            "remaining": remaining,
            "remaining_by_kind": remaining_by_kind,
            "active": budget_active,
            "queued": budget_queued,
            "deferred": budget_deferred,
            "physical": budget_physical,
            "worker_count": worker_count,
            "vector_stack_present": vector_stack_present,
            "has_pending": bool(remaining),
            "remaining_total": category_sum,
            "remaining_by_category_total": category_sum,
            "category_sum": category_sum,
            "remaining_max_by_category": remaining,
            "category_count_sum": category_sum,
            "unique_remaining_owner_count": unique_remaining_owner_count,
            "unique_remaining_owner_count_basis": "max_category_or_named_owner",
            "unique_owner_count_estimate": unique_remaining_owner_count,
            "diagnostics_error": diagnostics_error,
            "diagnostics_errors": dict(sorted(diagnostics_errors.items())),
            "unknown_components": sorted(unknown_components),
            "budget_state_unknown": budget_state_unknown,
            "projection_count": projection_count,
            "pipeline_count": pipeline_count,
            "physical_index_future_count": physical_future_count,
            "vector_retirement_count": retirement_count,
            "vector_candidate_build_count": candidate_build_count,
            "vector_candidate_physical_count": candidate_physical_count,
            "vector_candidate_path_count": candidate_path_count,
            "vector_sync_retirement_count": sync_retirement_count,
            "vector_close_owner_count": close_owner_count,
            "retired_vector_stack_count": retired_stack_count,
            "isolated_shutdown_task_count": isolated_shutdown_count,
            "projector_status": projector_status,
            "pipeline_status": pipeline_status,
            "vector_status": vector_status,
            "owner_task_names": sorted(owner_task_names),
            "oldest_owner_age_ms": oldest_owner_age_ms,
            "active_by_kind": active_by_kind,
            "queued_by_kind": queued_by_kind,
            "deferred_by_kind": deferred_by_kind,
            "event_bus_status": event_bus_status,
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

    async def _dispose_persistence_bounded(self, persistence: Any, *, timeout_sec: float) -> bool:
        dispose = getattr(persistence, "dispose", None)
        if not callable(dispose):
            return True
        task = self._persistence_dispose_task
        if task is None or task.done():
            if task is not None and task.done():
                try:
                    task.result()
                    return True
                except Exception:
                    self._persistence_dispose_task = None
            task = asyncio.create_task(
                asyncio.to_thread(dispose),
                name="astrmai:shutdown:persistence-dispose",
            )
            self._persistence_dispose_task = task
        done, pending = await asyncio.wait(
            {task},
            timeout=max(0.0, float(timeout_sec or 0.0)),
        )
        if pending:
            self._isolated_shutdown_tasks.add(task)
            task.add_done_callback(self._consume_isolated_shutdown_task)
            self.runtime.status.shutdown_isolated_tasks += 1
            self.runtime.status.shutdown_stage_stats["persistence_dispose"] = {
                "status": "isolated_timeout",
                "timeout_sec": max(0.0, float(timeout_sec or 0.0)),
            }
            return False
        try:
            task.result()
        except Exception as exc:
            logger.warning(f"[AstrMai] persistence dispose degraded: {exc}")
            return False
        return True

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
                        result = self._invoke_with_optional_keyword(
                            shutdown_coordinator,
                            "timeout_sec",
                            self._shutdown_timing("shutdown_cancel_grace_sec", 1.0),
                        )
                        if inspect.isawaitable(result):
                            await result

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
                final_pending = self._apply_fence_errors_to_pending_report(
                    self._shutdown_pending_report(
                        final_budget,
                        include_late_task=True,
                    )
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
        initial_fence_errors = self._apply_shutdown_fences()
        if initial_fence_errors:
            self._shutdown_pending_drain = True
            self._termination_complete = False
            self.runtime.status.shutdown_pending_drain = True
            self.runtime.status.shutdown_final_status = "pending_drain"
            self.runtime.status.shutdown_forced_termination_risk = True
            self.runtime.status.shutdown_stage_stats["budget_drain"] = {
                "status": "pending_drain",
                "remaining": 1,
                "has_pending": True,
                "shutdown_fence_errors": dict(initial_fence_errors),
                "remaining_by_kind": {
                    f"shutdown_fence.{name}": 1
                    for name in initial_fence_errors
                },
            }
            self._schedule_late_shutdown_cleanup(budget)
            logger.warning(
                "[AstrMai] shutdown fence failed; dependent resources remain open: "
                f"errors={initial_fence_errors}"
            )
            return

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
        budget_drain_error = ""

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

        projector = None
        try:
            tasks_to_wait = collect_background_tasks(*self.runtime.iter_task_owners())
            durable_tasks: set[Any] = set()
            projector = getattr(getattr(self.runtime, "memory_engine", None), "index_projector", None)
            durable_tasks.update(getattr(projector, "_durable_cleanup_tasks", set()) or set())
            tasks_to_wait = [task for task in tasks_to_wait if task not in durable_tasks]
        except Exception as exc:
            logger.warning(f"[AstrMai] Shutdown task collection degraded: {exc}")
            tasks_to_wait = []
            durable_tasks = set()

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

        try:
            wait_durable = getattr(projector, "wait_durable_cleanup", None)
            if callable(wait_durable):
                pending_durable = await wait_durable(
                    timeout_sec=min(2.0, float(self.SHUTDOWN_TASK_TIMEOUT or 2.0))
                )
                if pending_durable:
                    logger.warning(
                        f"[AstrMai] durable projection cleanup remains pending={pending_durable}"
                    )
        except Exception as exc:
            logger.warning(f"[AstrMai] durable projection cleanup degraded: {exc}")

        if callable(drain_budget):
            try:
                drain_report = await drain_budget(
                    timeout_sec=min(2.0, float(self.SHUTDOWN_TASK_TIMEOUT or 2.0))
                )
                try:
                    drain_report = self._validate_budget_drain_report(drain_report)
                except (TypeError, ValueError, OverflowError) as exc:
                    budget_drain_error = f"invalid drain report: {exc}"[:500]
                    drain_report = {}
                if drain_report.get("observed", 0):
                    logger.info(
                        "[AstrMai] deferred background work drained: "
                        f"observed={drain_report.get('observed', 0)} "
                        f"remaining={drain_report.get('remaining', 0)}"
                    )
            except Exception as exc:
                budget_drain_error = f"{type(exc).__name__}: {exc}"[:500]
                logger.warning(f"[AstrMai] deferred background drain degraded: {exc}")

        pending_report = self._apply_fence_errors_to_pending_report(
            self._shutdown_pending_report(budget)
        )
        if budget_drain_error:
            pending_report["remaining"] = max(
                1, int(pending_report.get("remaining", 0) or 0)
            )
            pending_report["has_pending"] = True
            pending_report["budget_drain_error"] = budget_drain_error
            pending_report["remaining_by_kind"] = {
                **dict(pending_report.get("remaining_by_kind", {}) or {}),
                "background_budget.drain_unknown": 1,
            }
        drain_remaining = int(drain_report.get("remaining", 0) or 0)
        if drain_remaining:
            pending_report["remaining"] = max(
                int(pending_report.get("remaining", 0) or 0),
                drain_remaining,
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
        pending_report = self._recompute_pending_report(pending_report)
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
                close_result = await close_memory_resources()
                if close_result is False:
                    pending = self._shutdown_pending_report(budget)
                    self._shutdown_pending_drain = True
                    self.runtime.status.shutdown_pending_drain = True
                    self.runtime.status.shutdown_final_status = "pending_drain"
                    self._schedule_late_shutdown_cleanup(budget)
                    logger.warning(
                        "[AstrMai] keeping dependent resources open after bounded vector close failure: "
                        f"remaining={pending.get('remaining', 0)}"
                    )
                    return
        except Exception as exc:
            self._shutdown_pending_drain = True
            self.runtime.status.shutdown_pending_drain = True
            self.runtime.status.shutdown_final_status = "pending_drain"
            self._schedule_late_shutdown_cleanup(budget)
            logger.warning(
                "[AstrMai] keeping dependent resources open after Memory close exception: "
                f"{type(exc).__name__}: {exc}"
            )
            return

        # 停止 EventBus workers
        event_bus = getattr(self.runtime, "event_bus", None)
        if event_bus is not None:
            try:
                await event_bus.stop()
                event_bus_status = {}
                describe_event_bus = getattr(event_bus, "describe_status", None)
                if callable(describe_event_bus):
                    event_bus_status = describe_event_bus() or {}
                pending_event_tasks = event_bus_status.get("pending_stop_task_count", 0)
                if isinstance(pending_event_tasks, bool) or not isinstance(pending_event_tasks, int):
                    raise RuntimeError("event_bus pending_stop_task_count is invalid")
                if pending_event_tasks > 0:
                    self._shutdown_dependency_close_errors.append(
                        f"event_bus:pending_stop_tasks={pending_event_tasks}"
                    )
                    self._shutdown_pending_drain = True
                    self.runtime.status.shutdown_pending_drain = True
                    self.runtime.status.shutdown_final_status = "pending_drain"
                    self.runtime.status.shutdown_forced_termination_risk = True
                    self._schedule_late_shutdown_cleanup(budget)
                    logger.warning(
                        "[AstrMai] EventBus stop still has physical tasks; "
                        f"persistence dispose deferred count={pending_event_tasks}"
                    )
                    return
            except Exception as exc:
                self._shutdown_dependency_close_errors.append(f"event_bus:{exc}")
                self.runtime.mark_degraded("shutdown.dependency_close", f"event_bus:{exc}")
                self.runtime.status.shutdown_forced_termination_risk = True
                logger.warning(f"[AstrMai] EventBus shutdown degraded: {exc}")

        # 释放 DB 连接池
        persistence = getattr(self.runtime, "persistence", None)
        if persistence is not None:
            disposed = await self._dispose_persistence_bounded(
                persistence,
                timeout_sec=min(2.0, float(self.SHUTDOWN_TASK_TIMEOUT or 2.0)),
            )
            if not disposed:
                self._shutdown_dependency_close_errors.append("persistence:dispose pending")
                self.runtime.mark_degraded("shutdown.dependency_close", "persistence dispose pending")
                self._shutdown_pending_drain = True
                self.runtime.status.shutdown_pending_drain = True
                self.runtime.status.shutdown_final_status = "pending_drain"
                self._schedule_late_shutdown_cleanup(budget)
                return

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

        async def _close_dependencies() -> bool:
            close_errors: list[str] = []
            retryable_pending = False
            event_bus_blocked = False
            fence_errors = self._apply_shutdown_fences()
            if fence_errors:
                self._shutdown_pending_drain = True
                self.runtime.status.shutdown_pending_drain = True
                self.runtime.status.shutdown_final_status = "pending_drain"
                self.runtime.status.shutdown_forced_termination_risk = True
                self.runtime.status.shutdown_stage_stats["shutdown_fence"] = {
                    "status": "pending_drain",
                    "errors": dict(fence_errors),
                }
                return False
            try:
                close_memory_resources = getattr(
                    self.runtime.memory_engine,
                    "close_background_resources",
                    None,
                )
                if callable(close_memory_resources):
                    close_result = await close_memory_resources()
                    if close_result is False:
                        close_errors.append("memory:vector resources remain open")
                        self.runtime.mark_degraded("shutdown.dependency_close", "vector resources remain open")
                        self.runtime.status.shutdown_pending_drain = True
                        self.runtime.status.shutdown_final_status = "pending_drain"
                        self.runtime.status.shutdown_forced_termination_risk = True
                        self.runtime.status.shutdown_stage_stats["dependency_close"] = {
                            "status": "pending_drain",
                            "errors": list(close_errors),
                        }
                        return False
            except Exception as exc:
                close_errors.append(f"memory:{exc}")
                self.runtime.mark_degraded("shutdown.dependency_close", f"memory:{exc}")
                logger.warning(f"[AstrMai] late Memory resource shutdown degraded: {exc}")
                self.runtime.status.shutdown_pending_drain = True
                self.runtime.status.shutdown_final_status = "pending_drain"
                self.runtime.status.shutdown_forced_termination_risk = True
                return False
            try:
                event_bus = getattr(self.runtime, "event_bus", None)
                stop_event_bus = getattr(event_bus, "stop", None)
                if callable(stop_event_bus):
                    await stop_event_bus()
                    describe_event_bus = getattr(event_bus, "describe_status", None)
                    if callable(describe_event_bus):
                        event_bus_status = describe_event_bus() or {}
                        pending_event_tasks = event_bus_status.get("pending_stop_task_count", 0)
                        if (
                            isinstance(pending_event_tasks, bool)
                            or not isinstance(pending_event_tasks, int)
                            or pending_event_tasks < 0
                        ):
                            raise RuntimeError("event_bus pending_stop_task_count is invalid")
                        if pending_event_tasks:
                            close_errors.append(
                                f"event_bus:pending_stop_tasks={pending_event_tasks}"
                            )
                            retryable_pending = True
                            event_bus_blocked = True
            except Exception as exc:
                close_errors.append(f"event_bus:{exc}")
                self.runtime.mark_degraded("shutdown.dependency_close", f"event_bus:{exc}")
                logger.warning(f"[AstrMai] late EventBus shutdown degraded: {exc}")
            if not event_bus_blocked:
                try:
                    persistence = getattr(self.runtime, "persistence", None)
                    dispose = getattr(persistence, "dispose", None)
                    if callable(dispose):
                        disposed = await self._dispose_persistence_bounded(
                            persistence,
                            timeout_sec=self._shutdown_timing("shutdown_cancel_grace_sec", 1.0),
                        )
                        if not disposed:
                            close_errors.append("persistence:dispose pending")
                            retryable_pending = True
                except Exception as exc:
                    close_errors.append(f"persistence:{exc}")
                    retryable_pending = True
                    self.runtime.mark_degraded("shutdown.dependency_close", f"persistence:{exc}")
                    logger.warning(f"[AstrMai] late Persistence dispose degraded: {exc}")
            if close_errors:
                self._shutdown_dependency_close_errors = close_errors
                self._shutdown_pending_drain = retryable_pending
                self._termination_complete = False
                self.runtime.status.shutdown_pending_drain = retryable_pending
                self.runtime.status.shutdown_final_status = (
                    "pending_drain" if retryable_pending else "degraded"
                )
                self.runtime.status.shutdown_forced_termination_risk = True
                self.runtime.status.shutdown_stage_stats["dependency_close"] = {
                    "status": "pending_drain" if retryable_pending else "degraded",
                    "errors": list(close_errors),
                }
                self.runtime.set_boot_phase(
                    "shutdown.pending_drain" if retryable_pending else "shutdown.degraded"
                )
                return False
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
                return True

        async def _retry_close_dependencies(deadline: float | None) -> bool:
            while True:
                if await _close_dependencies():
                    return True
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(0.05)

        async def _wait_for_quiescence(timeout_sec: float | None) -> dict[str, Any]:
            deadline = None if timeout_sec is None else time.monotonic() + max(0.0, timeout_sec)
            wait_until_idle = getattr(budget, "wait_until_idle", None)
            while True:
                fence_errors = self._apply_shutdown_fences()
                if deadline is None:
                    slice_timeout = 0.5
                else:
                    slice_timeout = min(0.5, max(0.0, deadline - time.monotonic()))
                if callable(wait_until_idle):
                    await wait_until_idle(timeout_sec=slice_timeout)
                pending = self._apply_fence_errors_to_pending_report(
                    self._shutdown_pending_report(budget)
                )
                if not fence_errors and (pending.get("vector_stack_present") or int(
                    pending.get("retired_vector_stack_count", 0) or 0
                )):
                    close_memory_resources = getattr(
                        getattr(self.runtime, "memory_engine", None),
                        "close_background_resources",
                        None,
                    )
                    if callable(close_memory_resources):
                        try:
                            await close_memory_resources()
                        except Exception as exc:
                            logger.warning(f"[AstrMai] late vector close attempt degraded: {exc}")
                        pending = self._apply_fence_errors_to_pending_report(
                            self._shutdown_pending_report(budget)
                        )
                if not pending["remaining"]:
                    return pending
                if deadline is not None and time.monotonic() >= deadline:
                    return pending
                await asyncio.sleep(0.05)

        async def _watch_late_shutdown_cleanup() -> None:
            while True:
                try:
                    pending = await _wait_for_quiescence(None)
                    if pending.get("remaining"):
                        self.runtime.mark_degraded(
                            "shutdown.late_cleanup",
                            f"physical_background_work_remaining={pending.get('remaining', 0)}",
                        )
                        await asyncio.sleep(0.1)
                        continue
                    if await _retry_close_dependencies(None):
                        return
                except asyncio.CancelledError:
                    await _mark_cleanup_cancelled()
                    raise
                except Exception as exc:
                    self._shutdown_pending_drain = True
                    self._termination_complete = False
                    self.runtime.status.shutdown_pending_drain = True
                    self.runtime.status.shutdown_final_status = "degraded"
                    self.runtime.status.shutdown_forced_termination_risk = True
                    self.runtime.status.shutdown_stage_stats["late_cleanup_watcher"] = {
                        "status": "retrying_after_error",
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                    }
                    self.runtime.set_boot_phase("shutdown.degraded")
                    self.runtime.mark_degraded("shutdown.late_cleanup_watcher", str(exc))
                    logger.warning(f"[AstrMai] late shutdown watcher degraded; retrying: {exc}")
                    await asyncio.sleep(0.1)

        async def _cleanup() -> None:
            try:
                pending = await _wait_for_quiescence(late_budget)
            except asyncio.CancelledError:
                await _mark_cleanup_cancelled()
                raise
            except Exception as exc:
                self.runtime.mark_degraded("shutdown.late_cleanup", str(exc))
                pending = self._apply_fence_errors_to_pending_report(
                    self._shutdown_pending_report(budget)
                )
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
            close_deadline = late_started + late_budget
            if not await _retry_close_dependencies(close_deadline):
                self.runtime.status.shutdown_final_status = "degraded"
                self.runtime.status.shutdown_pending_drain = True
                self.runtime.status.shutdown_forced_termination_risk = True
                self.runtime.set_boot_phase("shutdown.degraded")
                watcher = asyncio.create_task(
                    _watch_late_shutdown_cleanup(),
                    name="astrmai:shutdown:late-cleanup-watcher",
                )
                self._late_shutdown_cleanup_task = watcher
                self._isolated_shutdown_tasks.add(watcher)
                watcher.add_done_callback(self._consume_isolated_shutdown_task)

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
