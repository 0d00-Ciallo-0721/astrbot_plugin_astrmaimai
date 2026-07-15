from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..shared.constants.defaults import InfrastructureSettings, build_infrastructure_settings

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
    foreign_commands_loaded: bool = False
    proactive_started: bool = False
    visual_started: bool = False
    cron_guard_started: bool = False
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
            "foreign_commands_loaded": self.foreign_commands_loaded,
            "proactive_started": self.proactive_started,
            "visual_started": self.visual_started,
            "cron_guard_started": self.cron_guard_started,
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
    infrastructure_settings: InfrastructureSettings = field(
        default_factory=InfrastructureSettings
    )
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    core: CoreServices = field(default_factory=CoreServices)
    workmode: WorkModeServices = field(default_factory=WorkModeServices)
    cognition: CognitionServices = field(default_factory=CognitionServices)
    interaction: InteractionServices = field(default_factory=InteractionServices)
    lifecycle: LifecycleServices = field(default_factory=LifecycleServices)
    status: RuntimeStatus = field(default_factory=RuntimeStatus)
    system2_callback: System2Callback | None = None
    host_plugin_ref: Any = None

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
        )

    def build_diagnostics(self) -> dict[str, Any]:
        return {
            "status": self.status.as_dict(),
            "infrastructure": {
                "gateway": {
                    "max_concurrent_llm_calls": self.infrastructure_settings.gateway.max_concurrent_llm_calls,
                    "llm_retries": self.infrastructure_settings.gateway.llm_retries,
                    "backoff_factor": self.infrastructure_settings.gateway.backoff_factor,
                    "api_timeout": self.infrastructure_settings.gateway.api_timeout,
                    "debug_mode": self.infrastructure_settings.gateway.debug_mode,
                },
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
            "chat_loop": self.chat_loop_kernel.describe_status_sync()
            if self.chat_loop_kernel is not None and hasattr(self.chat_loop_kernel, "describe_status_sync")
            else {"enabled": False, "tracked_chats": 0},
        }

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
        "host_bridge": runtime.host_bridge,
    }
    for name in LEGACY_RUNTIME_ATTRS:
        value = getattr(runtime, name)
        if value is not None:
            attrs[name] = value
    return attrs
