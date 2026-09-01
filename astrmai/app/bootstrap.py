from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context

from ..conversation.ingress.sensors import PreFilters
from ..conversation.history import ConversationHistoryService
from ..conversation.attention.context_compaction import ContextCompactionEngine
from ..conversation.attention.gate import AttentionGate
from ..conversation.attention.group_dialogue_store import GroupDialogueStore
from ..conversation.attention.group_social_feedback_observer import GroupSocialFeedbackObserver
from ..conversation.attention.group_reread_observer import GroupRereadObserver
from ..conversation.attention.private_turn_coordinator import PrivateTurnCoordinator
from ..conversation.decision.judge import Judge
from ..conversation.execution.reply_service import ReplyService
from ..conversation.execution.reply_commit_service import ReplyCommitService
from ..conversation.execution.post_reply_feedback_coordinator import PostReplyFeedbackCoordinator
from ..conversation.execution.reread_action_dispatcher import RereadActionDispatcher
from ..conversation.execution.system2_runner import System2Runner
from ..conversation.loop import ChatLoopKernel
from ..conversation.planning.context_engine import ContextEngine
from ..conversation.planning.planner import Planner
from ..conversation.planning.prompt_refiner import PromptRefiner
from ..infrastructure.gateway.model_gateway import GlobalModelGateway
from ..infrastructure.persistence.database_service import DatabaseService
from ..infrastructure.persistence.persistence_manager import PersistenceManager
from ..infrastructure.persistence.reply_commit_outbox import ReplyCommitOutboxStore
from ..infrastructure.persistence.memory_turn_ledger import MemoryTurnLedgerStore
from ..infrastructure.runtime.chat_runtime_coordinator import ChatRuntimeCoordinator
from ..infrastructure.runtime.cross_session_handoff_store import CrossSessionHandoffStore
from ..infrastructure.runtime.context_economy_benchmark_store import ContextEconomyBenchmarkSampleStore
from ..infrastructure.runtime.event_bus import EventBus
from ..infrastructure.runtime.host_bridge import HostBridge
from ..infrastructure.runtime.background_task_budget import BackgroundTaskBudget
from ..infrastructure.runtime.lane_manager import LaneManager
from ..infrastructure.runtime.observability import RuntimeObservabilityHub
from ..infrastructure.runtime.raw_trace_store import RawTraceEventStore
from ..infrastructure.runtime.turn_trace_store import TurnTraceSampleStore
from ..learning import (
    EvolutionManager,
    ExpressionAutoCheckTask,
    ExpressionReflector,
    ExpressionReviewService,
    JargonAutoCheckTask,
    ReflectTracker,
)
from ..learning.review.expression_governance_runner import ExpressionGovernanceRunner
from ..memory import MemoryEngine, PersonaSummarizer, ReActRetriever
from ..multimodal.visual_cortex import VisualCortex
from ..multimodal.napcat_image_resolver import NapCatImageResolver
from ..proactive import ProactiveTask
from ..proactive.proactive_task import ProactiveDeps
from ..proactive.review_dispatcher import ReviewDispatcher
from ..shared.constants.defaults import build_infrastructure_settings
from ..state import FrequencyController, GroupReplyWaitManager, PrivateChatManager, StateEngine
from ..workmode import CronHeartbeatGuard, Sys3Router
from .runtime_context import (
    CognitionServices,
    CoreServices,
    InteractionServices,
    LifecycleServices,
    PluginRuntimeContext,
    WorkModeServices,
)


class PluginBootstrap:
    def __init__(self, context: Context, config: Any, raw_config: dict[str, Any] | None = None):
        self.context = context
        self.config = config
        self.raw_config = raw_config or {}

    def build(self) -> PluginRuntimeContext:
        runtime = PluginRuntimeContext(
            host_context=self.context,
            raw_config=self.raw_config,
            config=self.config,
            runtime_coordinator=ChatRuntimeCoordinator(),
            host_bridge=HostBridge(),
            infrastructure_settings=build_infrastructure_settings(self.config),
            background_task_budget=BackgroundTaskBudget(
                int(getattr(getattr(self.config, "infra", None), "background_task_concurrency", 2) or 2),
                max_queue=int(
                    getattr(getattr(self.config, "infra", None), "background_task_queue_limit", 64)
                    or 0
                ),
                wait_timeout_sec=float(
                    getattr(
                        getattr(self.config, "infra", None),
                        "background_task_wait_timeout_sec",
                        120.0,
                    )
                    or 120.0
                ),
                execution_timeout_sec=float(
                    getattr(
                        getattr(self.config, "infra", None),
                        "background_task_execution_timeout_sec",
                        300.0,
                    )
                    or 300.0
                ),
            ),
        )
        bind_budget_registry = getattr(
            runtime.background_task_budget,
            "bind_owner_registry",
            None,
        )
        if callable(bind_budget_registry):
            bind_budget_registry(runtime.owner_registry)
        runtime.set_boot_phase("bootstrap.logging")
        self._log_boot_status(runtime)
        runtime.set_boot_phase("bootstrap.core")
        runtime.core = self._build_core_services(runtime)
        db_service = getattr(runtime.core, "db_service", None)
        db_path = getattr(db_service, "db_path", None)
        runtime.cross_session_handoff_store = CrossSessionHandoffStore(db_path)
        runtime.conversation_history_service = ConversationHistoryService(self.context, runtime.config)
        runtime.set_boot_phase("bootstrap.workmode")
        runtime.workmode = self._build_work_mode(runtime)
        runtime.set_boot_phase("bootstrap.cognition")
        runtime.cognition = self._build_cognition_stack(runtime)
        runtime.set_boot_phase("bootstrap.interaction")
        runtime.interaction = self._build_interaction_stack(runtime)
        runtime.chat_loop_kernel = self._build_chat_loop_kernel(runtime)
        runtime.set_boot_phase("bootstrap.lifecycle")
        runtime.lifecycle = self._build_lifecycle_stack(runtime)
        runtime.status.bootstrap_completed = True
        runtime.set_boot_phase("bootstrap.ready")
        return runtime

    def _log_boot_status(self, runtime: PluginRuntimeContext) -> None:
        task_models = getattr(runtime.config.provider, "task_models", [])
        agent_models = getattr(runtime.config.provider, "agent_models", [])
        task_model = (task_models or ["Unconfigured"])[0]
        agent_model = (agent_models or ["Unconfigured"])[0]
        logger.info(
            f"[AstrMai] Booting refactoring workspace... Task(Judge): {task_model} | Agent: {agent_model}"
        )
        runtime.status.boot_logged = True

    # D24/D25 note: these builders still carry cross-service wiring; helper
    # extraction improves readability without changing lifecycle order.

    def _build_core_services(self, runtime: PluginRuntimeContext) -> CoreServices:
        persistence, db_service = self._build_persistence_services(runtime)
        gateway, lane_manager = self._build_gateway_lane_services(runtime)
        event_bus, memory_engine, observability_hub = self._build_memory_observability_services(
            runtime,
            persistence,
            db_service,
            gateway,
        )
        state_engine, dialogue_store = self._build_state_dialogue_services(
            runtime,
            persistence,
            db_service,
            gateway,
            event_bus,
        )
        compaction = self._build_context_compaction_service(
            runtime,
            dialogue_store,
            gateway,
            db_service,
            state_engine,
        )
        judge, sensors, visual_cortex, image_resolver = self._build_judge_sensor_vision_services(
            runtime,
            gateway,
            state_engine,
            db_service,
        )
        return CoreServices(
            persistence=persistence,
            db_service=db_service,
            gateway=gateway,
            lane_manager=lane_manager,
            event_bus=event_bus,
            observability_hub=observability_hub,
            memory_engine=memory_engine,
            dialogue_store=dialogue_store,
            context_compaction=compaction,
            state_engine=state_engine,
            judge=judge,
            sensors=sensors,
            visual_cortex=visual_cortex,
            image_resolver=image_resolver,
        )

    def _build_persistence_services(
        self,
        runtime: PluginRuntimeContext,
    ) -> tuple[PersistenceManager, DatabaseService]:
        persistence = PersistenceManager()
        bind_owner_registry = getattr(persistence, "bind_owner_registry", None)
        if callable(bind_owner_registry):
            bind_owner_registry(getattr(runtime, "owner_registry", None))
        db_service = DatabaseService(persistence)
        return persistence, db_service

    def _build_gateway_lane_services(self, runtime: PluginRuntimeContext) -> tuple[GlobalModelGateway, LaneManager]:
        gateway = GlobalModelGateway(
            self.context,
            runtime.config,
            settings=runtime.infrastructure_settings.gateway,
        )
        lane_manager = LaneManager(
            self.context.conversation_manager,
            config=runtime.config,
            settings=runtime.infrastructure_settings.lane,
        )
        gateway.set_lane_manager(lane_manager)
        return gateway, lane_manager

    def _build_memory_observability_services(
        self,
        runtime: PluginRuntimeContext,
        persistence: PersistenceManager,
        db_service: DatabaseService,
        gateway: GlobalModelGateway,
    ) -> tuple[EventBus, MemoryEngine, RuntimeObservabilityHub]:
        embedding_models = getattr(runtime.config.provider, "embedding_models", [])
        event_bus = EventBus()
        bind_event_bus_registry = getattr(event_bus, "bind_owner_registry", None)
        if callable(bind_event_bus_registry):
            bind_event_bus_registry(getattr(runtime, "owner_registry", None))
        db_service.owner_registry = getattr(runtime, "owner_registry", None)
        memory_engine = MemoryEngine(
            self.context,
            gateway,
            embedding_models=embedding_models,
            owner_registry=getattr(runtime, "owner_registry", None),
        )
        memory_engine.background_task_budget = getattr(runtime, "background_task_budget", None)
        self._wire_memory_database_services(persistence, db_service, gateway, memory_engine)
        observability_hub = RuntimeObservabilityHub(db_service.raw_trace_store)
        db_service.observability_hub = observability_hub
        memory_engine.observability_hub = observability_hub
        return event_bus, memory_engine, observability_hub

    def _wire_memory_database_services(
        self,
        persistence: PersistenceManager,
        db_service: DatabaseService,
        gateway: GlobalModelGateway,
        memory_engine: MemoryEngine,
    ) -> None:
        memory_engine.db_service = db_service
        db_service.memory_engine = memory_engine
        # ponytail: trace_cache_dir default path guess. Verify actual cache dir on disk
        # if turn_trace_store / raw_trace_store produce file-not-found at runtime.
        trace_cache_dir = Path(getattr(persistence, "cache_dir", Path("data") / "plugin_data" / "astrmai" / "cache"))
        db_service.turn_trace_store = TurnTraceSampleStore(trace_cache_dir)
        db_service.raw_trace_store = RawTraceEventStore(trace_cache_dir)
        db_service.context_economy_benchmark_store = ContextEconomyBenchmarkSampleStore(trace_cache_dir)
        gateway.benchmark_sample_store = db_service.context_economy_benchmark_store
        if getattr(memory_engine, "tool_service", None) is not None:
            memory_engine.tool_service.db_service = db_service

    def _build_state_dialogue_services(
        self,
        runtime: PluginRuntimeContext,
        persistence: PersistenceManager,
        db_service: DatabaseService,
        gateway: GlobalModelGateway,
        event_bus: EventBus,
    ) -> tuple[StateEngine, GroupDialogueStore | None]:
        conversation_settings = getattr(runtime.config, "conversation", None)
        state_engine = StateEngine(persistence, gateway, event_bus=event_bus)
        dialogue_store = None
        if runtime.feature_flags.dialogue_store_enabled:
            # G4/PL-09: 传入 cache 目录以支持跨重载的上下文快照（开关见
            # conversation.dialogue_store_persist_enabled；关闭时不传目录即完全禁用）
            snapshot_dir = None
            if bool(getattr(conversation_settings, "dialogue_store_persist_enabled", True)):
                snapshot_dir = getattr(getattr(persistence, "persistence", persistence), "cache_dir", None)
            dialogue_store = GroupDialogueStore(
                hot_zone_ttl_seconds=getattr(conversation_settings, "hot_zone_ttl_seconds", 30.0),
                warm_zone_ttl_seconds=getattr(conversation_settings, "warm_zone_ttl_seconds", 300.0),
                warm_zone_max_tokens=getattr(conversation_settings, "warm_zone_max_tokens", 1200),
                snapshot_dir=snapshot_dir,
            )
        db_service.dialogue_store = dialogue_store
        gateway.dialogue_store = dialogue_store
        gateway.db_service = db_service
        state_engine.dialogue_store = dialogue_store
        return state_engine, dialogue_store

    def _build_context_compaction_service(
        self,
        runtime: PluginRuntimeContext,
        dialogue_store: GroupDialogueStore | None,
        gateway: GlobalModelGateway,
        db_service: DatabaseService,
        state_engine: StateEngine,
    ) -> ContextCompactionEngine | None:
        conversation_settings = getattr(runtime.config, "conversation", None)
        compaction = None
        if runtime.feature_flags.context_compaction_enabled and dialogue_store is not None:
            compaction = ContextCompactionEngine(
                dialogue_store,
                compaction_trigger_segments=getattr(conversation_settings, "compaction_trigger_segments", 40),
                compaction_trigger_tokens=getattr(conversation_settings, "compaction_trigger_tokens", 1800),
                compaction_keep_recent_segments=getattr(conversation_settings, "compaction_keep_recent_segments", 16),
                compaction_summary_max_tokens=getattr(conversation_settings, "compaction_summary_max_tokens", 450),
                provider_id=getattr(conversation_settings, "compaction_provider_id", ""),
                gateway=gateway,
                background_task_budget=getattr(runtime, "background_task_budget", None),
                owner_registry=getattr(runtime, "owner_registry", None),
            )
        db_service.context_compaction = compaction
        gateway.context_compaction = compaction
        state_engine.context_compaction = compaction
        return compaction

    def _build_judge_sensor_vision_services(
        self,
        runtime: PluginRuntimeContext,
        gateway: GlobalModelGateway,
        state_engine: StateEngine,
        db_service: DatabaseService,
    ) -> tuple[Judge, PreFilters, VisualCortex | None, NapCatImageResolver | None]:
        judge = Judge(gateway, state_engine)
        sensors = PreFilters(runtime.config)
        visual_cortex = None
        vision_cache_dir = Path(
            getattr(
                db_service.persistence,
                "cache_dir",
                Path("data") / "plugin_data" / "astrmai" / "cache",
            )
        ) / "vision"
        image_resolver = NapCatImageResolver(
            vision_cache_dir,
            config=runtime.config,
        )
        if runtime.feature_flags.vision_enabled:
            try:
                visual_asset_dir = (
                    Path(
                        getattr(
                            db_service.persistence,
                            "cache_dir",
                            Path("data") / "plugin_data" / "astrmai" / "cache",
                        )
                    ).parent
                    / "visual_assets"
                )
                visual_cortex = VisualCortex(
                    gateway,
                    db_service,
                    config=runtime.config,
                    asset_dir=visual_asset_dir,
                    owner_registry=getattr(runtime, "owner_registry", None),
                )
            except Exception as exc:
                self._record_optional_failure(runtime, "multimodal.visual_cortex", exc)
                logger.warning(f"[AstrMai] VisualCortex init failed, vision disabled: {exc}", exc_info=True)
        return judge, sensors, visual_cortex, image_resolver

    def _build_work_mode(self, runtime: PluginRuntimeContext) -> WorkModeServices:
        enabled = runtime.feature_flags.work_mode_enabled
        runtime.status.work_mode_enabled = enabled
        if enabled:
            logger.info("[AstrMai] Sys3 (Work) enabled by config.")
            try:
                return WorkModeServices(
                    sys3_router=Sys3Router(
                        runtime.config,
                        self.context,
                        runtime.db_service,
                        gateway=runtime.gateway,
                    ),
                    cron_guard=CronHeartbeatGuard(runtime.db_service, self.context),
                )
            except Exception as exc:
                runtime.status.work_mode_enabled = False
                self._record_optional_failure(runtime, "workmode.sys3", exc)
                return WorkModeServices()
        logger.info("[AstrMai] Sys3 (Work) disabled; running in chat-only mode.")
        return WorkModeServices()

    def _build_cognition_stack(self, runtime: PluginRuntimeContext) -> CognitionServices:
        evolution = EvolutionManager(
            runtime.db_service,
            runtime.gateway,
            event_bus=runtime.event_bus,
            background_task_budget=getattr(runtime, "background_task_budget", None),
            owner_registry=getattr(runtime, "owner_registry", None),
        )
        reply_engine = ReplyService(
            runtime.state_engine,
            runtime.state_engine.mood_manager,
            runtime_coordinator=runtime.runtime_coordinator,
            memory_engine=runtime.memory_engine,
            dialogue_store=runtime.dialogue_store,
            evolution_manager=evolution,
            reply_commit_service=ReplyCommitService(
                ReplyCommitOutboxStore(runtime.db_service.db_path),
                owner_registry=getattr(runtime, "owner_registry", None),
            ),
        )
        persona_summarizer = PersonaSummarizer(
            runtime.persistence,
            runtime.gateway,
            memory_engine=runtime.memory_engine,
            background_task_budget=getattr(runtime, "background_task_budget", None),
            owner_registry=getattr(runtime, "owner_registry", None),
        )
        context_engine = ContextEngine(runtime.db_service, persona_summarizer)
        react_retriever = ReActRetriever(
            memory_engine=runtime.memory_engine,
            db_service=runtime.db_service,
            gateway=runtime.gateway,
            config=runtime.config,
        )
        prompt_refiner = PromptRefiner(
            runtime.memory_engine,
            runtime.db_service,
            runtime.config,
            react_retriever=react_retriever,
        )
        system2_planner = Planner(
            self.context,
            runtime.gateway,
            context_engine,
            reply_engine,
            runtime.memory_engine,
            evolution,
            state_engine=runtime.state_engine,
            prompt_refiner=prompt_refiner,
            sys3_router=runtime.sys3_router,
            runtime_coordinator=runtime.runtime_coordinator,
            cross_session_handoff_store=runtime.cross_session_handoff_store,
            conversation_history_service=runtime.conversation_history_service,
            visual_cortex=runtime.visual_cortex,
            image_resolver=runtime.image_resolver,
            owner_registry=getattr(runtime, "owner_registry", None),
        )
        system2_runner = System2Runner(runtime)
        self._bind_learning_collaboration(runtime, evolution)
        return CognitionServices(
            reply_engine=reply_engine,
            evolution=evolution,
            persona_summarizer=persona_summarizer,
            context_engine=context_engine,
            react_retriever=react_retriever,
            prompt_refiner=prompt_refiner,
            system2_planner=system2_planner,
            system2_runner=system2_runner,
        )

    def _build_interaction_stack(self, runtime: PluginRuntimeContext) -> InteractionServices:
        frequency_controller = FrequencyController(config=runtime.config)
        private_chat_manager = PrivateChatManager(config=runtime.config)
        private_turn_coordinator = PrivateTurnCoordinator(
            config=runtime.config,
            image_resolver=runtime.image_resolver,
            visual_cortex=runtime.visual_cortex,
            persistence=runtime.persistence,
        )
        conversation_config = getattr(runtime.config, "conversation", None)
        group_reply_wait_manager = GroupReplyWaitManager(
            threaded_enabled=bool(
                getattr(conversation_config, "group_thread_wait_enabled", True)
            )
        )
        group_social_feedback_observer = GroupSocialFeedbackObserver(
            config=runtime.config,
            dialogue_store=runtime.dialogue_store,
            gateway=runtime.gateway,
            owner_registry=getattr(runtime, "owner_registry", None),
        )
        group_reread_observer = GroupRereadObserver(config=runtime.config)
        reread_action_dispatcher = RereadActionDispatcher(
            context=runtime.context,
            config=runtime.config,
            runtime_coordinator=runtime.runtime_coordinator,
            dialogue_store=runtime.dialogue_store,
            reread_observer=group_reread_observer,
            owner_registry=getattr(runtime, "owner_registry", None),
        )
        post_reply_feedback_coordinator = PostReplyFeedbackCoordinator(
            private_chat_manager=private_chat_manager,
            group_social_feedback_observer=group_social_feedback_observer,
        )
        reply_engine = runtime.reply_engine
        bind_feedback_coordinator = getattr(
            reply_engine,
            "bind_post_reply_feedback_coordinator",
            None,
        )
        if callable(bind_feedback_coordinator):
            bind_feedback_coordinator(post_reply_feedback_coordinator)
        bind_reread_observer = getattr(reply_engine, "bind_group_reread_observer", None)
        if callable(bind_reread_observer):
            bind_reread_observer(group_reread_observer)
        executor = getattr(getattr(runtime, "system2_planner", None), "executor", None)
        bind_reread_dispatcher = getattr(executor, "bind_reread_action_dispatcher", None)
        if callable(bind_reread_dispatcher):
            bind_reread_dispatcher(reread_action_dispatcher)
        attention_gate = AttentionGate(
            state_engine=runtime.state_engine,
            judge=runtime.judge,
            sensors=runtime.sensors,
            system2_callback=self._build_system2_bridge(runtime),
            config=runtime.config,
            persona_summarizer=runtime.persona_summarizer,
            visual_cortex=runtime.visual_cortex,
            frequency_controller=frequency_controller,
            private_chat_manager=private_chat_manager,
            private_turn_coordinator=private_turn_coordinator,
            runtime_coordinator=runtime.runtime_coordinator,
            conversation_continuity=getattr(
                getattr(runtime, "system2_planner", None),
                "conversation_continuity",
                None,
            ),
            turn_trace_callback=getattr(
                getattr(runtime, "system2_planner", None),
                "record_turn_trace",
                None,
            ),
            background_task_budget=getattr(runtime, "background_task_budget", None),
            owner_registry=getattr(runtime, "owner_registry", None),
        )
        return InteractionServices(
            frequency_controller=frequency_controller,
            private_chat_manager=private_chat_manager,
            group_reply_wait_manager=group_reply_wait_manager,
            group_social_feedback_observer=group_social_feedback_observer,
            post_reply_feedback_coordinator=post_reply_feedback_coordinator,
            group_reread_observer=group_reread_observer,
            reread_action_dispatcher=reread_action_dispatcher,
            attention_gate=attention_gate,
        )

    def _build_chat_loop_kernel(self, runtime: PluginRuntimeContext) -> ChatLoopKernel:
        if runtime.attention_gate is None:
            logger.warning("[Bootstrap] attention_gate is None; ChatLoopKernel will have no message handler")
        kernel = ChatLoopKernel(
            runtime_coordinator=runtime.runtime_coordinator,
            message_handler=runtime.attention_gate.process_event if runtime.attention_gate is not None else None,
            observability_hub=runtime.observability_hub,
            owner_registry=getattr(runtime, "owner_registry", None),
        )
        kernel.bind_signal_sources(
            group_reply_wait_manager=runtime.group_reply_wait_manager,
            private_chat_manager=runtime.private_chat_manager,
            context_compaction=runtime.context_compaction,
        )
        if runtime.attention_gate is not None and hasattr(runtime.attention_gate, "bind_chat_loop_kernel"):
            runtime.attention_gate.bind_chat_loop_kernel(kernel)
        return kernel

    def _build_lifecycle_stack(self, runtime: PluginRuntimeContext) -> LifecycleServices:
        reflector, reflect_tracker, review_service, review_dispatcher = self._build_reflection_services(runtime)
        auto_check_task, jargon_auto_check_task = self._build_learning_tasks(runtime, reflect_tracker)
        expression_governance_runner = self._build_expression_governance_runner(
            runtime,
            reflector,
            auto_check_task,
            jargon_auto_check_task,
            review_dispatcher,
        )
        proactive_task = self._build_proactive_task(runtime, reflector)
        if proactive_task is not None:
            proactive_task.expression_governance_runner = expression_governance_runner
        return LifecycleServices(
            reflector=reflector,
            reflect_tracker=reflect_tracker,
            review_service=review_service,
            auto_check_task=auto_check_task,
            expression_governance_runner=expression_governance_runner,
            proactive_task=proactive_task,
        )

    def _build_reflection_services(
        self,
        runtime: PluginRuntimeContext,
    ) -> tuple[ExpressionReflector, ReflectTracker, ExpressionReviewService, ReviewDispatcher]:
        reflector = ExpressionReflector(
            db_service=runtime.db_service,
            gateway=runtime.gateway,
            config=runtime.config,
            background_task_budget=getattr(runtime, "background_task_budget", None),
        )
        reflect_tracker = ReflectTracker(
            db_service=runtime.db_service,
            gateway=runtime.gateway,
            config=runtime.config,
            background_task_budget=getattr(runtime, "background_task_budget", None),
        )
        review_service = ExpressionReviewService(runtime.db_service)
        review_dispatcher = ReviewDispatcher(self.context, reflect_tracker)
        return reflector, reflect_tracker, review_service, review_dispatcher

    def _build_learning_tasks(
        self,
        runtime: PluginRuntimeContext,
        reflect_tracker: ReflectTracker,
    ) -> tuple[ExpressionAutoCheckTask | None, JargonAutoCheckTask | None]:
        auto_check_task = None
        jargon_auto_check_task = None
        try:
            auto_check_task = ExpressionAutoCheckTask(
                db_service=runtime.db_service,
                gateway=runtime.gateway,
                tracker=reflect_tracker,
                config=runtime.config,
                background_task_budget=getattr(runtime, "background_task_budget", None),
            )
        except Exception as exc:
            self._record_optional_failure(runtime, "learning.auto_check_task", exc)
        try:
            jargon_auto_check_task = JargonAutoCheckTask(
                db_service=runtime.db_service,
                gateway=runtime.gateway,
                config=runtime.config,
                background_task_budget=getattr(runtime, "background_task_budget", None),
            )
        except Exception as exc:
            self._record_optional_failure(runtime, "learning.jargon_auto_check_task", exc)
        return auto_check_task, jargon_auto_check_task

    def _build_expression_governance_runner(
        self,
        runtime: PluginRuntimeContext,
        reflector: ExpressionReflector,
        auto_check_task: ExpressionAutoCheckTask | None,
        jargon_auto_check_task: JargonAutoCheckTask | None,
        review_dispatcher: ReviewDispatcher,
    ) -> ExpressionGovernanceRunner:
        runner = ExpressionGovernanceRunner(
            state_engine=runtime.state_engine,
            pattern_service=getattr(runtime.memory_engine, "expression_pattern_service", None),
            reflector=reflector,
            auto_check_task=auto_check_task,
            jargon_auto_check_task=jargon_auto_check_task,
            review_dispatcher=review_dispatcher,
            interval_seconds=getattr(getattr(runtime.config, "evolution", None), "review_runner_interval_sec", 60),
            config=runtime.config,
            background_task_budget=getattr(runtime, "background_task_budget", None),
            owner_registry=getattr(runtime, "owner_registry", None),
        )
        if runtime.system2_planner is not None:
            runtime.system2_planner.reflector = reflector
        return runner

    def _build_proactive_task(
        self,
        runtime: PluginRuntimeContext,
        reflector: ExpressionReflector,
    ) -> ProactiveTask | None:
        if not runtime.feature_flags.proactive_enabled:
            return None
        try:
            proactive_task = ProactiveTask(
                context=self.context,
                state_engine=runtime.state_engine,
                gateway=runtime.gateway,
                persistence=runtime.persistence,
                memory_engine=runtime.memory_engine,
                reflector=reflector,
                config=runtime.config,
                runtime_coordinator=runtime.runtime_coordinator,
                attention_gate=runtime.attention_gate,
                background_task_budget=getattr(runtime, "background_task_budget", None),
                owner_registry=getattr(runtime, "owner_registry", None),
            )
            proactive_task.configure(
                ProactiveDeps(
                    dream_visible=runtime.feature_flags.dream_visible,
                    planner=runtime.system2_planner,
                )
            )
            proactive_task.set_db_service(runtime.db_service)
            proactive_task.bind_chat_loop_kernel(runtime.chat_loop_kernel)
            return proactive_task
        except Exception as exc:
            self._record_optional_failure(runtime, "proactive.task", exc)
            logger.warning(f"[Bootstrap] ProactiveTask creation failed: {type(exc).__name__}: {exc}")
            logger.warning("[Bootstrap] 主动发言、梦境整理等功能将不可用")
            return None

    def _bind_learning_collaboration(self, runtime: PluginRuntimeContext, evolution: EvolutionManager) -> None:
        if runtime.event_bus is None:
            return
        runtime.event_bus.subscribe(
            runtime.event_bus.TOPIC_LEARNING_MESSAGE_RECORDED,
            runtime.state_engine.on_learning_message_recorded,
        )
        runtime.event_bus.subscribe(
            runtime.event_bus.TOPIC_LEARNING_BOT_REPLY_RECORDED,
            runtime.memory_engine.on_learning_bot_reply_recorded,
        )
        runtime.event_bus.subscribe(
            runtime.event_bus.TOPIC_LEARNING_MINING_COMPLETED,
            runtime.memory_engine.on_learning_mining_completed,
        )

    def _record_optional_failure(self, runtime: PluginRuntimeContext, component: str, exc: Exception) -> None:
        runtime.mark_degraded(component, str(exc))
        logger.warning(f"[AstrMai] Optional component degraded: {component} -> {exc}")

    def _build_system2_bridge(self, runtime: PluginRuntimeContext):
        async def _bridge(main_event: Any, events_to_process: list[Any] | None = None) -> Any:
            if runtime.system2_callback is None:
                raise RuntimeError("System2 callback has not been bound yet.")
            return await runtime.system2_callback(main_event, events_to_process)

        return _bridge


def build_runtime_context(context: Context, config: Any, raw_config: dict[str, Any] | None = None) -> PluginRuntimeContext:
    return PluginBootstrap(context=context, config=config, raw_config=raw_config).build()
