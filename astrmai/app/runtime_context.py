from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..shared.constants.defaults import InfrastructureSettings, build_infrastructure_settings
from ..infrastructure.runtime.background_task_budget import BackgroundTaskBudget

System2Callback = Callable[[Any, list[Any] | None], Awaitable[Any]]


@dataclass(slots=True)
class CoreServices:
    persistence: Any = None
    db_service: Any = None
    gateway: Any = None
    lane_manager: Any = None
    event_bus: Any = None
    observability_hub: Any = None
    memory_engine: Any = None
    dialogue_store: Any = None
    context_compaction: Any = None
    state_engine: Any = None
    judge: Any = None
    sensors: Any = None
    visual_cortex: Any = None
    image_resolver: Any = None


@dataclass(slots=True)
class WorkModeServices:
    sys3_router: Any = None
    cron_guard: Any = None


@dataclass(slots=True)
class CognitionServices:
    reply_engine: Any = None
    evolution: Any = None
    persona_summarizer: Any = None
    context_engine: Any = None
    react_retriever: Any = None
    prompt_refiner: Any = None
    system2_planner: Any = None
    system2_runner: Any = None


@dataclass(slots=True)
class InteractionServices:
    frequency_controller: Any = None
    private_chat_manager: Any = None
    group_reply_wait_manager: Any = None
    group_social_feedback_observer: Any = None
    post_reply_feedback_coordinator: Any = None
    group_reread_observer: Any = None
    reread_action_dispatcher: Any = None
    attention_gate: Any = None


@dataclass(slots=True)
class LifecycleServices:
    reflector: Any = None
    reflect_tracker: Any = None
    review_service: Any = None
    auto_check_task: Any = None
    expression_governance_runner: Any = None
    proactive_task: Any = None
    manager: Any = None


@dataclass(slots=True)
class RuntimeStatus:
    boot_phase: str = "created"
    is_running: bool = False
    accepting_events: bool = False
    boot_logged: bool = False
    bootstrap_completed: bool = False
    lifecycle_started: bool = False
    work_mode_enabled: bool = False
    memory_initialized: bool = False
    persona_state: str = "pending"
    persona_cache_key: str = ""
    persona_completed_shards: int = 0
    persona_persisted: bool = False
    persona_self_lore_ready: bool = False
    persona_last_error: str = ""
    startup_blocked_reason: str = ""
    startup_retry_at: float = 0.0
    runtime_generation: int = 0
    previous_runtime_generation: int = 0
    reload_wait_ms: float = 0.0
    reload_wait_timeout: bool = False
    reload_wait_error: str = ""
    startup_stage_timings: dict[str, float] = field(default_factory=dict)
    startup_yield_count: int = 0
    startup_loop_lag_max_ms: float = 0.0
    startup_loop_lag_p95_ms: float = 0.0
    startup_loop_lag_p99_ms: float = 0.0
    foreign_commands_loaded: bool = False
    proactive_started: bool = False
    visual_started: bool = False
    cron_guard_started: bool = False
    shutdown_generation: int = 0
    shutdown_started_at: float = 0.0
    shutdown_completed_at: float = 0.0
    last_shutdown_elapsed_ms: float = 0.0
    last_shutdown_slowest_stage: str = ""
    shutdown_stage_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    shutdown_isolated_tasks: int = 0
    shutdown_final_status: str = "idle"
    shutdown_pending_drain: bool = False
    shutdown_forced_termination_risk: bool = False
    shutdown_late_cleanup_deadline: float = 0.0
    shutdown_late_cleanup_task_count: int = 0
    degraded_components: dict[str, str] = field(default_factory=dict)
    # ponytail: threading.Lock is safe here (sync-only during bootstrap, not held across await)
    _degraded_lock: threading.Lock = field(default_factory=threading.Lock)

    def set_phase(self, phase: str) -> None:
        self.boot_phase = phase

    def mark_degraded(self, component: str, reason: str) -> None:
        with self._degraded_lock:
            self.degraded_components[component] = reason

    def as_dict(self) -> dict[str, Any]:
        return {
            "boot_phase": self.boot_phase,
            "is_running": self.is_running,
            "accepting_events": self.accepting_events,
            "boot_logged": self.boot_logged,
            "bootstrap_completed": self.bootstrap_completed,
            "lifecycle_started": self.lifecycle_started,
            "work_mode_enabled": self.work_mode_enabled,
            "memory_initialized": self.memory_initialized,
            "persona_state": self.persona_state,
            "persona_cache_key": self.persona_cache_key,
            "persona_completed_shards": self.persona_completed_shards,
            "persona_persisted": self.persona_persisted,
            "persona_self_lore_ready": self.persona_self_lore_ready,
            "persona_last_error": self.persona_last_error,
            "startup_blocked_reason": self.startup_blocked_reason,
            "startup_retry_at": self.startup_retry_at,
            "runtime_generation": self.runtime_generation,
            "previous_runtime_generation": self.previous_runtime_generation,
            "reload_wait_ms": self.reload_wait_ms,
            "reload_wait_timeout": self.reload_wait_timeout,
            "reload_wait_error": self.reload_wait_error,
            "startup_stage_timings": dict(self.startup_stage_timings),
            "startup_yield_count": self.startup_yield_count,
            "startup_loop_lag_max_ms": self.startup_loop_lag_max_ms,
            "startup_loop_lag_p95_ms": self.startup_loop_lag_p95_ms,
            "startup_loop_lag_p99_ms": self.startup_loop_lag_p99_ms,
            "foreign_commands_loaded": self.foreign_commands_loaded,
            "proactive_started": self.proactive_started,
            "visual_started": self.visual_started,
            "cron_guard_started": self.cron_guard_started,
            "shutdown_generation": self.shutdown_generation,
            "shutdown_started_at": self.shutdown_started_at,
            "shutdown_completed_at": self.shutdown_completed_at,
            "last_shutdown_elapsed_ms": self.last_shutdown_elapsed_ms,
            "last_shutdown_slowest_stage": self.last_shutdown_slowest_stage,
            "shutdown_stage_stats": dict(self.shutdown_stage_stats),
            "shutdown_isolated_tasks": self.shutdown_isolated_tasks,
            "shutdown_final_status": self.shutdown_final_status,
            "shutdown_pending_drain": self.shutdown_pending_drain,
            "shutdown_forced_termination_risk": self.shutdown_forced_termination_risk,
            "shutdown_late_cleanup_deadline": self.shutdown_late_cleanup_deadline,
            "shutdown_late_cleanup_task_count": self.shutdown_late_cleanup_task_count,
            "degraded_components": self._snapshot_degraded(),
        }

    def _snapshot_degraded(self) -> dict[str, str]:
        with self._degraded_lock:
            return dict(self.degraded_components)


@dataclass(slots=True)
class PluginRuntimeContext:
    host_context: Any
    raw_config: dict[str, Any]
    config: Any
    runtime_coordinator: Any
    host_bridge: Any
    chat_loop_kernel: Any = None
    cross_session_handoff_store: Any = None
    conversation_history_service: Any = None
    infrastructure_settings: InfrastructureSettings = field(
        default_factory=InfrastructureSettings
    )
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    background_task_budget: BackgroundTaskBudget | None = None
    core: CoreServices = field(default_factory=CoreServices)
    workmode: WorkModeServices = field(default_factory=WorkModeServices)
    cognition: CognitionServices = field(default_factory=CognitionServices)
    interaction: InteractionServices = field(default_factory=InteractionServices)
    lifecycle: LifecycleServices = field(default_factory=LifecycleServices)
    status: RuntimeStatus = field(default_factory=RuntimeStatus)
    # Cross-instance hot-reload registration.  These fields intentionally live
    # on the slotted runtime context because LifecycleManager consumes them to
    # gate shared-resource initialization.
    runtime_generation: int = 0
    runtime_previous_termination: Any = None
    runtime_facade: Any = None
    runtime_registration: Any = None
    runtime_registration_error: str = ""
    system2_callback: System2Callback | None = None
    host_plugin_ref: Any = None
    diagnostics_history: list[dict[str, Any]] = field(default_factory=list)
    diagnostics_sample_interval_sec: float = 60.0
    _last_diagnostics_sample_at: float = 0.0

    def bind_system2_callback(self, callback: System2Callback) -> None:
        self.system2_callback = callback

    def bind_host_plugin(self, host_plugin: Any) -> None:
        import weakref
        self.host_plugin_ref = weakref.ref(host_plugin)

    def sync_host_compat_attrs(self) -> None:
        # ponytail: setattr in loop may partially fail — some attrs set, some not.
        # Add rollback or exception guard if partial injection causes desync.
        ref = self.host_plugin_ref
        if ref is None:
            return
        host_plugin = ref()
        if host_plugin is None:
            return
        for name, value in export_legacy_attrs(self).items():
            setattr(host_plugin, name, value)

    def rebuild_infrastructure_settings(self) -> None:
        self.infrastructure_settings = build_infrastructure_settings(self.config)
        if self.background_task_budget is not None:
            infra = getattr(self.config, "infra", None)
            self.background_task_budget.refresh_limit(
                int(getattr(infra, "background_task_concurrency", 2) or 2),
                max_queue=int(getattr(infra, "background_task_queue_limit", 64) or 0),
                wait_timeout_sec=float(
                    getattr(infra, "background_task_wait_timeout_sec", 120.0) or 120.0
                ),
                execution_timeout_sec=float(
                    getattr(infra, "background_task_execution_timeout_sec", 300.0) or 300.0
                ),
            )

    def set_boot_phase(self, phase: str) -> None:
        self.status.set_phase(phase)

    def mark_degraded(self, component: str, reason: str) -> None:
        self.status.mark_degraded(component, reason)

    @property
    def feature_flags(self):
        return self.infrastructure_settings.features

    @property
    def context(self) -> Any:
        return self.host_context

    @property
    def persistence(self) -> Any:
        return self.core.persistence

    @property
    def db_service(self) -> Any:
        return self.core.db_service

    @property
    def gateway(self) -> Any:
        return self.core.gateway

    @property
    def lane_manager(self) -> Any:
        return self.core.lane_manager

    @property
    def event_bus(self) -> Any:
        return self.core.event_bus

    @property
    def memory_engine(self) -> Any:
        return self.core.memory_engine

    @property
    def observability_hub(self) -> Any:
        return self.core.observability_hub

    @property
    def state_engine(self) -> Any:
        return self.core.state_engine

    @property
    def judge(self) -> Any:
        return self.core.judge

    @property
    def sensors(self) -> Any:
        return self.core.sensors

    @property
    def visual_cortex(self) -> Any:
        return self.core.visual_cortex

    @property
    def image_resolver(self) -> Any:
        return self.core.image_resolver

    @property
    def dialogue_store(self) -> Any:
        return self.core.dialogue_store

    @property
    def context_compaction(self) -> Any:
        return self.core.context_compaction

    @property
    def sys3_router(self) -> Any:
        return self.workmode.sys3_router

    @property
    def cron_guard(self) -> Any:
        return self.workmode.cron_guard

    @property
    def reply_engine(self) -> Any:
        return self.cognition.reply_engine

    @property
    def evolution(self) -> Any:
        return self.cognition.evolution

    @property
    def persona_summarizer(self) -> Any:
        return self.cognition.persona_summarizer

    @property
    def context_engine(self) -> Any:
        return self.cognition.context_engine

    @property
    def react_retriever(self) -> Any:
        return self.cognition.react_retriever

    @property
    def prompt_refiner(self) -> Any:
        return self.cognition.prompt_refiner

    @property
    def system2_planner(self) -> Any:
        return self.cognition.system2_planner

    @property
    def system2_runner(self) -> Any:
        return self.cognition.system2_runner

    @property
    def frequency_controller(self) -> Any:
        return self.interaction.frequency_controller

    @property
    def private_chat_manager(self) -> Any:
        return self.interaction.private_chat_manager

    @property
    def group_reply_wait_manager(self) -> Any:
        return self.interaction.group_reply_wait_manager

    @property
    def group_social_feedback_observer(self) -> Any:
        return self.interaction.group_social_feedback_observer

    @property
    def post_reply_feedback_coordinator(self) -> Any:
        return self.interaction.post_reply_feedback_coordinator

    @property
    def group_reread_observer(self) -> Any:
        return self.interaction.group_reread_observer

    @property
    def reread_action_dispatcher(self) -> Any:
        return self.interaction.reread_action_dispatcher

    @property
    def attention_gate(self) -> Any:
        return self.interaction.attention_gate

    @property
    def reflector(self) -> Any:
        return self.lifecycle.reflector

    @property
    def reflect_tracker(self) -> Any:
        return self.lifecycle.reflect_tracker

    @property
    def review_service(self) -> Any:
        return self.lifecycle.review_service

    @property
    def auto_check_task(self) -> Any:
        return self.lifecycle.auto_check_task

    @property
    def expression_governance_runner(self) -> Any:
        return self.lifecycle.expression_governance_runner

    @property
    def proactive_task(self) -> Any:
        return self.lifecycle.proactive_task

    @property
    def chat_loop_kernel_with_fallback(self) -> Any:
        """Return the chat loop kernel, falling back to proactive_task's copy.

        The primary kernel lives on ``self.chat_loop_kernel`` (set during
        bootstrap).  When that is None the proactive task may still hold a
        reference — this property encapsulates the fallback so callers do not
        need to reach into ProactiveTask internals.
        """
        if self.chat_loop_kernel is not None:
            return self.chat_loop_kernel
        task = self.proactive_task
        return getattr(task, "chat_loop_kernel", None) if task is not None else None

    def iter_task_owners(self) -> tuple[Any, ...]:
        return (
            self.lifecycle.manager,
            self.attention_gate,
            self.evolution,
            self.expression_governance_runner,
            self.proactive_task,
            getattr(self.memory_engine, "index_projector", None),
            self.group_social_feedback_observer,
            self.group_reread_observer,
            self.reread_action_dispatcher,
            self.event_bus,
        )

    def build_diagnostics(self) -> dict[str, Any]:
        component_errors: list[dict[str, str]] = []

        def safe_float(value: Any, default: float = 0.0) -> float:
            try:
                return float(value or default)
            except (TypeError, ValueError):
                return float(default)

        def safe_int(value: Any, default: int = 0) -> int:
            try:
                return int(value or default)
            except (TypeError, ValueError):
                return int(default)

        def safe_component(name: str, producer, fallback: dict[str, Any]) -> dict[str, Any]:
            try:
                value = producer()
                return value if isinstance(value, dict) else dict(fallback)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                component_errors.append({"component": name, "error": error})
                return {
                    **fallback,
                    "available": False,
                    "diagnostics_status": "error",
                    "error": error,
                }

        describe_vector = getattr(self.memory_engine, "describe_vector_status", None)
        vector_status = safe_component(
            "vector_retrieval",
            describe_vector
            if callable(describe_vector)
            else lambda: (
                self.memory_engine.vec_retriever.describe_status()
                if getattr(self.memory_engine, "vec_retriever", None) is not None
                and hasattr(self.memory_engine.vec_retriever, "describe_status")
                else {"available": False}
            ),
            {"available": False},
        )
        attention_router = getattr(self.attention_gate, "decision_router", None)
        attention_status = safe_component(
            "attention",
            attention_router.describe_status
            if attention_router is not None and hasattr(attention_router, "describe_status")
            else lambda: {"available": False},
            {"available": False},
        )
        attention_gate_status = safe_component(
            "attention_gate",
            self.attention_gate.describe_status
            if self.attention_gate is not None and hasattr(self.attention_gate, "describe_status")
            else lambda: {"available": False},
            {"available": False},
        )
        if isinstance(attention_gate_status, dict):
            attention_status = {**attention_status, **attention_gate_status}
        proactive_status = safe_component(
            "proactive",
            self.proactive_task.describe_status
            if self.proactive_task is not None and hasattr(self.proactive_task, "describe_status")
            else lambda: {"running": False},
            {"running": False},
        )
        reread_status = safe_component(
            "group_reread_observer",
            self.group_reread_observer.describe_status
            if self.group_reread_observer is not None and hasattr(self.group_reread_observer, "describe_status")
            else lambda: {"active_groups": 0},
            {"active_groups": 0},
        )
        budget_status = safe_component(
            "background_task_budget",
            self.background_task_budget.status
            if self.background_task_budget is not None and hasattr(self.background_task_budget, "status")
            else lambda: {"limit": 0, "active": 0, "available_slots": 0},
            {"limit": 0, "active": 0, "available_slots": 0},
        )
        chat_loop_status = safe_component(
            "chat_loop",
            self.chat_loop_kernel.describe_status_sync
            if self.chat_loop_kernel is not None and hasattr(self.chat_loop_kernel, "describe_status_sync")
            else lambda: {"enabled": False, "tracked_chats": 0},
            {"enabled": False, "tracked_chats": 0},
        )
        planner = self.system2_planner
        try:
            raw_traces = getattr(planner, "turn_trace_history", []) or []
            traces = list(raw_traces)[-300:] if isinstance(raw_traces, (list, tuple)) else []
        except Exception:
            traces = []
        elapsed = []
        timeout_count = 0
        budget_exhausted = 0
        incomplete_timing = 0
        failed_count = 0
        skipped_count = 0
        completed_count = 0

        def trace_flag(value: Any, names: set[str], seen: set[int] | None = None, depth: int = 0) -> bool:
            if depth > 12:
                return False
            if isinstance(value, dict):
                seen = seen if seen is not None else set()
                value_id = id(value)
                if value_id in seen:
                    return False
                seen.add(value_id)
                for key, item in value.items():
                    normalized_key = str(key or "").strip().lower()
                    if normalized_key in names and item is True:
                        return True
                    if trace_flag(item, names, seen, depth + 1):
                        return True
            elif isinstance(value, (list, tuple)):
                seen = seen if seen is not None else set()
                value_id = id(value)
                if value_id in seen:
                    return False
                seen.add(value_id)
                return any(trace_flag(item, names, seen, depth + 1) for item in value)
            return False

        for trace in traces:
            if not isinstance(trace, dict):
                continue
            try:
                value = float(trace.get("turn_total_elapsed_ms", trace.get("elapsed_ms", 0.0)) or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                elapsed.append(value)
            status = str(trace.get("status", "") or "").lower()
            is_timeout = "timeout" in status or trace_flag(
                trace, {"timeout", "judge_timeout", "turn_timeout"}
            )
            is_budget_exhausted = "budget_exhausted" in status or trace_flag(
                trace, {"budget_exhausted", "turn_budget_exhausted", "judge_budget_exhausted"}
            )
            is_skipped = status.startswith("skipped") or status in {"ignored", "cancelled"}
            is_failed = status in {"failed", "error", "exception", "degraded"} or trace_flag(
                trace, {"failed", "error"}
            )
            # A trace belongs to exactly one terminal bucket. Keep the most
            # specific exhaustion/timeout outcome ahead of generic failures.
            if is_budget_exhausted:
                budget_exhausted += 1
            elif is_timeout:
                timeout_count += 1
            elif is_failed:
                failed_count += 1
            elif is_skipped:
                skipped_count += 1
            else:
                completed_count += 1
            coverage = trace.get("timing_coverage", {}) or {}
            if isinstance(coverage, dict) and coverage.get("complete") is False:
                incomplete_timing += 1

        def percentile(values: list[float], ratio: float) -> float:
            if not values:
                return 0.0
            ordered = sorted(values)
            index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
            return round(ordered[index], 2)

        long_turn_status = {
            "active": safe_int(chat_loop_status.get("active_turn_task_count", 0)),
            "active_scope": "coordinator_registered_turns",
            "completed": completed_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "timeout": timeout_count,
            "budget_exhausted": budget_exhausted,
            "timing_incomplete": incomplete_timing,
            "elapsed_ms_p50": percentile(elapsed, 0.50),
            "elapsed_ms_p95": percentile(elapsed, 0.95),
            "elapsed_ms_p99": percentile(elapsed, 0.99),
            "sample_size": len(elapsed),
        }
        projection_status = vector_status.get("projection", {}) if isinstance(vector_status, dict) else {}
        diagnostics_degraded = bool(
            component_errors
            or (
                isinstance(projection_status, dict)
                and projection_status.get("repair_required")
            )
            or self.status.shutdown_final_status == "degraded"
        )
        snapshot = {
            "snapshot_at": time.time(),
            "diagnostics_status": "degraded" if diagnostics_degraded else "ok",
            "component_errors": component_errors,
            "status": self.status.as_dict(),
            "infrastructure": {
                "gateway": {
                    "max_concurrent_llm_calls": self.infrastructure_settings.gateway.max_concurrent_llm_calls,
                    "llm_retries": self.infrastructure_settings.gateway.llm_retries,
                    "backoff_factor": self.infrastructure_settings.gateway.backoff_factor,
                    "api_timeout": self.infrastructure_settings.gateway.api_timeout,
                    "debug_mode": self.infrastructure_settings.gateway.debug_mode,
                },
                "background_task_budget": budget_status,
                "features": {
                    "work_mode_enabled": self.infrastructure_settings.features.work_mode_enabled,
                    "private_chat_enabled": self.infrastructure_settings.features.private_chat_enabled,
                    "vision_enabled": self.infrastructure_settings.features.vision_enabled,
                    "proactive_enabled": self.infrastructure_settings.features.proactive_enabled,
                    "dream_visible": self.infrastructure_settings.features.dream_visible,
                    "meme_enabled": self.infrastructure_settings.features.meme_enabled,
                    "dialogue_store_enabled": self.infrastructure_settings.features.dialogue_store_enabled,
                    "context_compaction_enabled": self.infrastructure_settings.features.context_compaction_enabled,
                    "prefix_caching_enabled": self.infrastructure_settings.features.prefix_caching_enabled,
                },
            },
            "components": {
                "gateway": self.gateway is not None,
                "memory_engine": self.memory_engine is not None,
                "observability_hub": self.observability_hub is not None,
                "state_engine": self.state_engine is not None,
                "attention_gate": self.attention_gate is not None,
                "planner": self.system2_planner is not None,
                "system2_runner": self.system2_runner is not None,
                "reply_engine": self.reply_engine is not None,
                "review_service": self.review_service is not None,
                "sys3_router": self.sys3_router is not None,
                "cron_guard": self.cron_guard is not None,
                "visual_cortex": self.visual_cortex is not None,
                "proactive_task": self.proactive_task is not None,
                "dialogue_store": self.dialogue_store is not None,
                "context_compaction": self.context_compaction is not None,
                "chat_loop_kernel": self.chat_loop_kernel is not None,
            },
            "chat_loop": chat_loop_status,
            "memory": {
                "vector_retrieval": vector_status,
            },
            "attention": attention_status,
            "proactive": proactive_status,
            "group_reread_observer": reread_status,
            "long_turn": long_turn_status,
        }
        history_sample = {
            "snapshot_at": snapshot["snapshot_at"],
            "diagnostics_status": snapshot["diagnostics_status"],
            "component_error_count": len(component_errors),
            "background_active": safe_int(budget_status.get("active", 0)),
            "background_queued": safe_int(budget_status.get("queued", 0)),
            "vector_degraded_ratio": safe_float(vector_status.get("degraded_ratio", 0.0)),
            "vector_active_queries": safe_int(vector_status.get("active_queries", 0)),
            "vector_circuit_open": bool(vector_status.get("circuit_open", False)),
            "attention_judge_active": safe_int(attention_status.get("judge_requests_active", 0)),
            "attention_judge_timeout_count": safe_int(attention_status.get("judge_timeout_count", 0)),
            "attention_judge_degraded_count": safe_int(attention_status.get("judge_degraded_count", 0)),
            "attention_judge_success_count": safe_int(attention_status.get("judge_success_count", 0)),
            "attention_judge_latency_ms_p95": safe_float(attention_status.get("judge_latency_ms_p95", 0.0)),
            "attention_prefilter_avoided": safe_int(attention_status.get("prefilter_avoided_judge_count", 0)),
            "attention_shadow_success": safe_int(attention_status.get("shadow_judge_success_count", 0)),
            "reread_active_groups": safe_int(reread_status.get("active_groups", 0)),
            "long_turn_p95_ms": safe_float(long_turn_status.get("elapsed_ms_p95", 0.0)),
            "long_turn_timeout": safe_int(long_turn_status.get("timeout", 0)),
            "long_turn_budget_exhausted": safe_int(long_turn_status.get("budget_exhausted", 0)),
        }
        sample_interval = max(0.0, safe_float(self.diagnostics_sample_interval_sec, 60.0))
        if (
            not self.diagnostics_history
            or snapshot["snapshot_at"] - self._last_diagnostics_sample_at >= sample_interval
        ):
            self.diagnostics_history.append(history_sample)
            self.diagnostics_history = self.diagnostics_history[-360:]
            self._last_diagnostics_sample_at = snapshot["snapshot_at"]
        snapshot["history"] = list(self.diagnostics_history[-60:])
        return snapshot

    def build_capability_overview_sync(self) -> dict[str, Any]:
        from .. import multimodal as multimodal_mod
        try:
            cron_guard_status = self.cron_guard.describe_status() if self.cron_guard else {"running": False}
        except Exception as exc:
            cron_guard_status = {"running": False, "error": str(exc)}
        try:
            task_status = (
                self.proactive_task.describe_status()
                if self.proactive_task and hasattr(self.proactive_task, "describe_status")
                else {"running": False}
            )
        except Exception as exc:
            task_status = {"running": False, "error": str(exc)}
        try:
            dream_scheduler_status = (
                self.proactive_task.dream_scheduler.describe_status()
                if self.proactive_task and getattr(self.proactive_task, "dream_scheduler", None)
                else {
                    "dream_visible": self.feature_flags.dream_visible,
                    "interval_seconds": 0,
                    "last_dream_time": 0.0,
                    "dream_agent_bound": False,
                    "dream_generator_bound": False,
                }
            )
        except Exception as exc:
            dream_scheduler_status = {"dream_visible": self.feature_flags.dream_visible, "error": str(exc)}

        return {
            "workmode": {
                "enabled": self.feature_flags.work_mode_enabled,
                "agents": self.sys3_router.get_static_agent_names() if self.sys3_router else [],
                "router": {},
                "cron_guard": cron_guard_status,
            },
            "multimodal": multimodal_mod.describe_multimodal_capabilities(
                self.visual_cortex,
                vision_enabled=self.feature_flags.vision_enabled,
                meme_enabled=self.feature_flags.meme_enabled,
            ),
            "proactive": {
                "enabled": self.feature_flags.proactive_enabled,
                "dream_visible": self.feature_flags.dream_visible,
                "task_status": task_status,
                "dream_scheduler": dream_scheduler_status,
                "review_dispatcher": {"ready": False, "pending": 0},
            },
        }

    async def build_capability_overview(self) -> dict[str, Any]:
        from .. import proactive as proactive_mod
        from .. import workmode as workmode_mod

        overview = self.build_capability_overview_sync()
        overview["workmode"] = await workmode_mod.describe_workmode_capabilities(
            self.sys3_router,
            self.cron_guard,
            enabled=self.feature_flags.work_mode_enabled,
        )
        overview["proactive"] = await proactive_mod.describe_proactive_capabilities(
            self.proactive_task,
            enabled=self.feature_flags.proactive_enabled,
            dream_visible=self.feature_flags.dream_visible,
        )
        return overview


# Legacy attrs are exported only for host/runtime compatibility.
# New refactor-side code should depend on PluginRuntimeContext directly instead
# of consuming these names as first-class interfaces.
LEGACY_RUNTIME_ATTRS = (
    "persistence",
    "db_service",
    "gateway",
    "lane_manager",
    "event_bus",
    "memory_engine",
    "state_engine",
    "judge",
    "sensors",
    "visual_cortex",
    "dialogue_store",
    "context_compaction",
    "sys3_router",
    "cron_guard",
    "reply_engine",
    "evolution",
    "persona_summarizer",
    "context_engine",
    "react_retriever",
    "prompt_refiner",
    "system2_planner",
    "system2_runner",
    "frequency_controller",
    "private_chat_manager",
    "group_reply_wait_manager",
    "attention_gate",
    "reflector",
    "reflect_tracker",
    "review_service",
    "auto_check_task",
    "proactive_task",
    "chat_loop_kernel",
)


def export_legacy_attrs(runtime: PluginRuntimeContext) -> dict[str, Any]:
    # Keep the legacy surface centralized here so compatibility does not
    # spread back into production modules.
    attrs = {
        "raw_config": runtime.raw_config,
        "config": runtime.config,
        "_background_tasks": runtime.background_tasks,
        "runtime_coordinator": runtime.runtime_coordinator,
        "cross_session_handoff_store": runtime.cross_session_handoff_store,
        "conversation_history_service": runtime.conversation_history_service,
        "host_bridge": runtime.host_bridge,
    }
    for name in LEGACY_RUNTIME_ATTRS:
        value = getattr(runtime, name)
        if value is not None:
            attrs[name] = value
    return attrs
