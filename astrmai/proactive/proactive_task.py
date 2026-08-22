from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
import random
import time
from time import monotonic

from astrbot.api import logger

from ..conversation.runtime.architecture_rollout import rollout_enabled
from ..infrastructure.context_economy import PromptTemplateId
from ..infrastructure.runtime.background_task_budget import (
    BackgroundTaskQueueFull,
    BackgroundTaskQueueTimeout,
)
from ..infrastructure.runtime.lane_manager import LaneKey
from ..learning.profiling.nickname_generator import NicknameGenerator
from ..learning.profiling.profile_generator import ProfileGenerator
from ..memory.dream.dream_agent import DreamAgent
from ..memory.dream.dream_generator import DreamGenerator
from ..memory.dream.promotion_engine import MemoryPromotionEngine
from .decay_service import DecayService
from .diary_service import DiaryService
from .dream_scheduler import DreamScheduler
from .dispatcher import ProactiveDispatcher
from .group_signin_service import GroupSigninService
from .heartflow import HeartflowManager, HeartflowTopicDigestService
from .review_dispatcher import ReviewDispatcher
from .scheduled_scenario_service import ScheduledScenarioService
from .wakeup_service import WakeupService


@dataclass(slots=True)
class ProactiveDeps:
    dream_visible: bool = False
    planner: object | None = None


class ProactiveTask:
    """Refactoring-side lifecycle scheduler that delegates concrete jobs to subservices."""

    FAST_POLL_INTERVAL_SECONDS = 5.0
    NORMAL_POLL_INTERVAL_SECONDS = 10.0
    IDLE_POLL_INTERVAL_SECONDS = 15.0
    GLOBAL_MAINTENANCE_INTERVAL_SECONDS = 60.0
    HEARTBEAT_DUE_HORIZON_SECONDS = 2.0
    HEARTBEAT_MAX_BATCH = 32

    def __init__(
        self,
        context,
        state_engine,
        gateway,
        persistence,
        memory_engine=None,
        reflector=None,
        config=None,
        runtime_coordinator=None,
        attention_gate=None,
        background_task_budget=None,
    ):
        self.context = context
        self.state_engine = state_engine
        self.gateway = gateway
        self.persistence = persistence
        self.memory_engine = memory_engine
        self.reflector = reflector
        self.runtime_coordinator = runtime_coordinator
        self.background_task_budget = background_task_budget
        self.auto_check_task = None
        self.reflect_tracker = None
        self.config = config if config else gateway.config
        self._is_running = False
        self._task = None
        self._background_tasks: set[asyncio.Task] = set()
        self._bg_semaphore = asyncio.Semaphore(2)
        self._profile_semaphore = asyncio.Semaphore(1)
        self._profiling_user_ids: set[str] = set()
        self._last_profile_run = 0.0
        self._last_diary_date = ""
        self._diary_pending_date = ""
        self._last_global_maintenance_run = 0.0
        self._last_due_chat_count = 0
        self._last_skipped_not_due_count = 0
        self._last_active_candidate_count = 0
        self._last_persistent_due_candidate_count = 0
        self._last_merged_candidate_count = 0
        self._last_selected_persistent_due_count = 0
        self._persistent_due_scan_enabled = False
        self._persistent_due_scan_degraded_reason = ""
        self._scheduler_poll_mode = "FAST"
        self._scheduler_poll_interval_seconds = self.FAST_POLL_INTERVAL_SECONDS
        self._last_due_phase_mix: dict[str, int] = {}
        self._last_maintenance_budget_total = 0
        self._last_maintenance_budget_used = 0
        self._last_maintenance_budget_remaining = 0
        self._scheduler_batch_limit = self.HEARTBEAT_MAX_BATCH
        self._last_scheduler_batch_plan: dict[str, int] = {}
        self._last_batch_fill_rate = 0.0
        self._last_forced_promotion_count = 0
        self._last_quota_skip_counts: dict[str, int] = {}
        self._last_selection_summary: dict[str, Any] = {}
        self._busy_backpressure_active = False
        self._maintenance_backpressure_active = False
        self._last_poll_mode_transition = {
            "previous": "FAST",
            "current": "FAST",
            "reason": "startup",
        }
        self._db_service = None
        self.chat_loop_kernel = None
        self.profile_generator = ProfileGenerator()
        self.nickname_generator = NicknameGenerator()
        self.prompt_registry = getattr(getattr(gateway, "context_economy", None), "templates", None)
        self.proactive_dispatcher = ProactiveDispatcher(
            attention_gate=attention_gate,
            runtime_coordinator=runtime_coordinator,
            state_engine=state_engine,
            config=self.config,
        )
        self.scheduled_scenario_service = ScheduledScenarioService(
            state_engine=state_engine,
            dispatcher=self.proactive_dispatcher,
            config=self.config,
            db_path=getattr(persistence, "db_path", None),
            call_background_lane=self._call_background_lane,
            task_launcher=self._fire_background_task,
        )

        self.wakeup_service = WakeupService(
            context=context,
            state_engine=state_engine,
            persistence=persistence,
            call_background_lane=self._call_background_lane,
            config=self.config,
            dispatcher=self.proactive_dispatcher,
            memory_engine=memory_engine,
            prompt_registry=self.prompt_registry,
        )
        self.group_signin_service = GroupSigninService(
            state_engine=state_engine,
            persistence=persistence,
            dispatcher=self.proactive_dispatcher,
            config=self.config,
        )
        self.decay_service = DecayService(state_engine, memory_engine, self.config)
        self.diary_service = DiaryService(
            persistence=persistence,
            memory_engine=memory_engine,
            config=self.config,
            call_background_lane=self._call_background_lane,
            semaphore=self._bg_semaphore,
            prompt_registry=self.prompt_registry,
        )
        self.dream_scheduler = DreamScheduler(
            context=context,
            memory_engine=memory_engine,
            config=self.config,
            semaphore=self._bg_semaphore,
            dream_visible=False,
        )
        self.review_dispatcher = ReviewDispatcher(context, None)
        self.heartflow_manager = HeartflowManager(
            runtime_coordinator=runtime_coordinator,
            state_engine=state_engine,
            memory_engine=memory_engine,
            semaphore=self._bg_semaphore,
            dispatcher=self.proactive_dispatcher,
            config=self.config,
        )
        self.heartflow_topic_digest_service = HeartflowTopicDigestService(
            memory_engine=memory_engine,
            semaphore=self._bg_semaphore,
        )
        self.dream_generator = DreamGenerator(gateway, config=self.config)
        self.dream_agent = None

    def refresh_config(self, config) -> None:
        self.config = config if config is not None else getattr(self.gateway, "config", None)
        if self.gateway is not None:
            self.gateway.config = self.config
        for service_name in (
            "proactive_dispatcher",
            "scheduled_scenario_service",
            "wakeup_service",
            "group_signin_service",
            "decay_service",
            "diary_service",
            "dream_scheduler",
            "heartflow_manager",
            "dream_generator",
            "dream_agent",
        ):
            service = getattr(self, service_name, None)
            refresh = getattr(service, "refresh_config", None)
            if callable(refresh):
                refresh(self.config)
            elif service is not None and hasattr(service, "config"):
                service.config = self.config

    async def _call_background_lane(
        self,
        task_family: str,
        scope_id: str,
        prompt: str,
        system_prompt: str = "",
        template_envelope=None,
    ) -> str:
        return await self.gateway.call_proactive_task(
            prompt=prompt,
            system_prompt=system_prompt,
            lane_key=LaneKey(subsystem="bg", task_family=task_family, scope_id=scope_id or "global", scope_kind="global"),
            base_origin="",
            persona_id=getattr(self.config.persona, "persona_id", "") or "global",
            template_envelope=template_envelope,
        )

    async def start(self):
        if self._is_running:
            return
        self._is_running = True
        self._last_global_maintenance_run = monotonic()
        self._scheduler_poll_mode = "FAST"
        self._scheduler_poll_interval_seconds = self.FAST_POLL_INTERVAL_SECONDS
        if self.dream_agent is None and self._db_service:
            self._bind_dream_dependencies()
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_loop_done)

    def _on_loop_done(self, task):
        """Loop 意外终止时自动重启（正常 stop 不触发）。"""
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            if self._is_running:
                logger.error("[ProactiveTask] loop unexpectedly cancelled, restarting in 5s")
                loop = asyncio.get_running_loop()
                loop.call_later(5, lambda: self._restart_if_still_running())
            return
        if exc and self._is_running:
            logger.exception("[ProactiveTask] loop crashed, restarting in 5s")
            loop = asyncio.get_running_loop()
            loop.call_later(5, lambda: self._restart_if_still_running())

    def _restart_if_still_running(self):
        """ponytail: re-check _is_running after 5s delay to avoid reanimating stopped scheduler"""
        if self._is_running:
            self._is_running = False  # reset so start() will actually restart
            from ..shared.helpers.plugin_helpers import safe_create_task
            safe_create_task(self.start())

    async def stop(self):
        self._is_running = False
        try:
            await self.proactive_dispatcher.shutdown()
        except Exception as exc:
            logger.warning(f"[ProactiveTask] dispatcher shutdown degraded: {type(exc).__name__}")
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._background_tasks:
            tasks = list(self._background_tasks)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._background_tasks.difference_update(tasks)

    def configure(
        self,
        deps: ProactiveDeps | None = None,
        *,
        dream_visible: bool | None = None,
    ) -> None:
        """Apply post-construction configuration (encapsulates bootstrap wiring)."""
        resolved_deps = deps or ProactiveDeps()
        if dream_visible is not None:
            resolved_deps = ProactiveDeps(
                dream_visible=bool(dream_visible),
                planner=resolved_deps.planner,
            )
        self.dream_scheduler.dream_visible = bool(resolved_deps.dream_visible)
        if resolved_deps.planner is not None:
            resolved_deps.planner.heartflow_manager = self.heartflow_manager
        self.auto_check_task = None
        self.reflect_tracker = None
        self.review_dispatcher.reflect_tracker = None

    def set_db_service(self, db_service):
        self._db_service = db_service
        if self._is_running and self.dream_agent is None:
            self._bind_dream_dependencies()

    def bind_chat_loop_kernel(self, chat_loop_kernel) -> None:
        self.chat_loop_kernel = chat_loop_kernel
        if chat_loop_kernel is None:
            return
        if hasattr(chat_loop_kernel, "bind_heartbeat_handler"):
            chat_loop_kernel.bind_heartbeat_handler(self.handle_chat_heartbeat)
        if hasattr(chat_loop_kernel, "bind_signal_sources"):
            chat_loop_kernel.bind_signal_sources(
                wakeup_service=self.wakeup_service,
                heartflow_manager=self.heartflow_manager,
                memory_service=self.memory_engine,
                dream_scheduler=self.dream_scheduler,
                context_compaction=getattr(self.state_engine, "context_compaction", None),
            )
        if hasattr(chat_loop_kernel, "bind_dispatch_bridge"):
            chat_loop_kernel.bind_dispatch_bridge("PROACTIVE_WAKEUP", self.handle_wakeup_signal)
            chat_loop_kernel.bind_dispatch_bridge("HEARTFLOW_EVALUATE", self.handle_heartflow_signal)
            chat_loop_kernel.bind_dispatch_bridge("DREAM_MAINTENANCE", self.handle_dream_signal)
            chat_loop_kernel.bind_dispatch_bridge("MEMORY_MAINTENANCE", self.handle_memory_signal)
            chat_loop_kernel.bind_dispatch_bridge("COMPACTION_EVALUATE", self.handle_compaction_signal)

    def _bind_dream_dependencies(self):
        self.dream_agent = DreamAgent(
            gateway=self.gateway,
            db_service=self._db_service,
            memory_engine=self.memory_engine,
            config=self.config,
        )
        promotion_engine = MemoryPromotionEngine(self.memory_engine)
        self.dream_scheduler.bind_dependencies(
            self.dream_agent,
            self.dream_generator,
            db_service=self._db_service,
            promotion_engine=promotion_engine,
        )

    def _fire_background_task(
        self,
        awaitable_factory,
        *,
        task_name: str = "proactive",
        scope_id: str = "GLOBAL",
    ):
        budget = getattr(self, "background_task_budget", None)
        if budget is not None:
            coro = budget.run(
                awaitable_factory,
                task_name=task_name,
                scope_id=scope_id,
                defer_release_on_timeout=True,
            )
        else:
            coro = awaitable_factory()
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_task_result)

    def _handle_task_result(self, task: asyncio.Task):
        self._background_tasks.discard(task)
        try:
            exc = task.exception()
            if exc:
                if isinstance(exc, (BackgroundTaskQueueFull, BackgroundTaskQueueTimeout)):
                    logger.warning(f"[ProactiveTask] background task skipped: {exc}")
                else:
                    logger.error(f"[Proactive Task Error] {exc}", exc_info=exc)
        except asyncio.CancelledError:
            pass

    async def _load_persona_summary(self) -> str:
        persona_id = getattr(getattr(self.config, "persona", None), "persona_id", "") or "global"
        try:
            if hasattr(self.persistence, "load_persona_cache_async"):
                cache = await self.persistence.load_persona_cache_async()
            else:
                cache = self.persistence.load_persona_cache()
        except Exception as exc:
            logger.debug(f"[ProactiveTask] load persona cache degraded: {exc}")
            return ""
        persona_data = cache.get(persona_id, {}) if isinstance(cache, dict) else {}
        return str(persona_data.get("summary", "") or "").strip()

    async def _save_user_profile(self, profile) -> None:
        try:
            await self.persistence.save_user_profile(profile)
        except TypeError:
            await self.persistence.save_user_profile(getattr(profile, "user_id", ""), profile)

    async def _select_group_profile_target(self, chat_id: str) -> tuple[str, str, int] | None:
        db_service = self._db_service
        if not db_service or not hasattr(db_service, "get_recent_message_logs_async"):
            return None
        try:
            logs = await db_service.get_recent_message_logs_async(
                chat_id,
                limit=80,
                max_age_seconds=3600,
                include_processed=True,
            )
        except TypeError:
            logs = await db_service.get_recent_message_logs_async(chat_id, limit=80)
        except Exception as exc:
            logger.debug(f"[Life] group profile target degraded for {chat_id}: {exc}")
            return None
        if not logs:
            return None

        counts: Counter[str] = Counter()
        display_names: dict[str, str] = {}
        bot_id = str(getattr(self.state_engine, "bot_id", "") or "")
        for item in logs:
            sender_id = str(getattr(item, "sender_id", "") or "")
            sender_name = str(getattr(item, "sender_name", "") or "")
            content = str(getattr(item, "content", "") or "")
            if not sender_id or sender_name == "SELF":
                continue
            if bot_id and sender_id == bot_id:
                continue
            if not content.strip():
                continue
            counts[sender_id] += 1
            if sender_name.strip():
                display_names[sender_id] = sender_name.strip()
        if not counts:
            return None
        top_user_id, top_count = counts.most_common(1)[0]
        return top_user_id, display_names.get(top_user_id, ""), int(top_count)

    async def _generate_persona_analysis(self, profile) -> None:
        recent_summary = ""
        if hasattr(self.state_engine, "user_profile_service"):
            recent_summary = self.state_engine.user_profile_service.build_recent_interaction_summary(profile)
        if recent_summary:
            setattr(profile, "recent_interaction_summary", recent_summary)
        summary = await self._load_persona_summary()
        if self.prompt_registry is not None:
            envelope = self.prompt_registry.render_template(
                PromptTemplateId.PROFILE_GENERATION,
                self.profile_generator.build_template_payload(profile, summary),
            )
            result = await self._call_background_lane(
                "profile",
                str(getattr(profile, "user_id", profile.name)),
                envelope.prompt,
                system_prompt=envelope.system_prompt,
                template_envelope=envelope,
            )
        else:
            prompt = self.profile_generator.build_prompt(profile, summary)
            if prompt is None:  # ponytail: skip when no new messages
                return
            result = await self._call_background_lane("profile", str(getattr(profile, "user_id", profile.name)), prompt)
        parsed = self.profile_generator.parse_result(result)
        analysis = parsed["analysis"]
        tags = parsed["tags"]
        memory_points = parsed["memory_points"]
        if hasattr(self.state_engine, "user_profile_service"):
            self.state_engine.user_profile_service.refresh_profile_from_generation(
                profile,
                analysis=analysis,
                tags=tags,
                memory_points=memory_points,
                source="auto_profile_generation",
            )
        else:
            if analysis:
                profile.persona_analysis = analysis.strip()
            if tags:
                profile.tags = tags
            if memory_points:
                profile.memory_points = memory_points
                categorized = self.profile_generator.categorize_memory_points(memory_points)
                profile.identity_points = categorized["identity_points"]
                profile.preference_points = categorized["preference_points"]
                profile.relationship_points = categorized["relationship_points"]
                profile.speech_style_points = categorized["speech_style_points"]
            profile.message_count_for_profiling = 0
            profile.last_persona_gen_time = time.time()
            profile.is_dirty = True
        await self._save_user_profile(profile)
        logger.info(
            f"[Life] persona profiling completed for {getattr(profile, 'name', '')}: "
            f"tags={len(tags)} memory_points={len(memory_points)}"
        )

    async def _generate_nickname(self, profile) -> None:
        if not profile or getattr(profile, "is_known", False):
            return
        if hasattr(self.state_engine, "user_profile_service") and not self.state_engine.user_profile_service.can_auto_update_nickname(profile):
            return
        summary = await self._load_persona_summary()
        if self.prompt_registry is not None:
            envelope = self.prompt_registry.render_template(
                PromptTemplateId.PROFILE_NICKNAME_GENERATION,
                self.nickname_generator.build_template_payload(profile, summary),
            )
            result = await self._call_background_lane(
                "profile",
                str(getattr(profile, "user_id", profile.name)),
                envelope.prompt,
                system_prompt=envelope.system_prompt,
                template_envelope=envelope,
            )
        else:
            prompt = self.nickname_generator.build_prompt(profile, summary)
            result = await self._call_background_lane("profile", str(getattr(profile, "user_id", profile.name)), prompt)
        nickname, reason = self.nickname_generator.parse_result(result)
        nickname = self.nickname_generator.choose(getattr(profile, "name", ""), preferred=nickname)
        if not nickname:
            return
        if hasattr(self.state_engine, "user_profile_service"):
            applied = self.state_engine.user_profile_service.set_auto_nickname(profile, nickname, reason)
            if not applied:
                return
        else:
            profile.nickname = nickname
            profile.nickname_reason = reason
            profile.is_known = True
            profile.is_dirty = True
        await self._save_user_profile(profile)
        logger.info(f"[Life] nickname generated for {getattr(profile, 'name', '')}: {nickname}")

    async def _run_profiling_task(self):
        async with self._profile_semaphore:
            for state in self.state_engine.get_active_states():
                chat_id = str(getattr(state, "chat_id", "") or "")
                if not chat_id or chat_id.startswith("FriendMessage:"):
                    continue
                target = await self._select_group_profile_target(chat_id)
                if not target:
                    continue
                user_id, sender_name, weight = target
                await self.state_engine.record_profile_learning_touch(
                    user_id,
                    chat_id=chat_id,
                    source="group_periodic",
                    weight=weight,
                    sender_name=sender_name,
                    increment_know_times=True,
                )

            active_profiles = self.state_engine.get_active_profiles()
            threshold = int(getattr(getattr(self.config, "life", None), "profiling_msg_threshold", 200) or 200)
            cooldown_sec = max(
                0,
                int(getattr(getattr(self.config, "life", None), "profiling_user_cooldown_sec", 21600) or 0),
            )
            active_user_ids = getattr(self, "_profiling_user_ids", None)
            if active_user_ids is None:
                active_user_ids = set()
                self._profiling_user_ids = active_user_ids

            for profile in active_profiles:
                user_id = str(getattr(profile, "user_id", getattr(profile, "name", "")) or "")
                if not user_id or user_id in active_user_ids:
                    continue
                last_generated_at = float(getattr(profile, "last_persona_gen_time", 0.0) or 0.0)
                analysis_due = int(getattr(profile, "message_count_for_profiling", 0) or 0) >= threshold
                if analysis_due and cooldown_sec > 0 and time.time() - last_generated_at < cooldown_sec:
                    analysis_due = False
                active_user_ids.add(user_id)
                try:
                    if profile.know_times >= 3 and not getattr(profile, "is_known", False):
                        try:
                            await self._generate_nickname(profile)
                        except Exception as exc:
                            logger.error(f"[Life] nickname task degraded for {getattr(profile, 'name', '')}: {exc}")
                    if analysis_due:
                        try:
                            await self._generate_persona_analysis(profile)
                        except Exception as exc:
                            logger.error(f"[Life] profiling task degraded for {getattr(profile, 'name', '')}: {exc}")
                finally:
                    active_user_ids.discard(user_id)

    async def _run_reflection_tasks(self):
        enable_exp_mine = getattr(self.config.evolution, "enable_expression_mining", True) if hasattr(self.config, "evolution") else True
        if not self.reflector or not enable_exp_mine:
            return
        for state in self.state_engine.get_active_states():
            if not getattr(state, "chat_id", None):
                continue
            await self.reflector.reflect_batch(state.chat_id)
            await self.reflector.auto_audit(state.chat_id)
            if self.auto_check_task:
                await self.auto_check_task.run_once(state.chat_id)
        await self.review_dispatcher.dispatch_pending()

    async def _run_daily_diary_task_with_jitter(self, diary_date: str):
        try:
            await asyncio.sleep(random.randint(1, 300))
            report = await self.diary_service.run_once(
                self.state_engine.get_active_states(),
                diary_date=diary_date,
            )
            report = dict(report or {})
            if int(report.get("failed", 0) or 0) == 0:
                self._last_diary_date = diary_date
        finally:
            if self._diary_pending_date == diary_date:
                self._diary_pending_date = ""

    async def handle_chat_heartbeat(self, chat_id: str, snapshot, decision) -> dict:
        decision.metadata["dispatch_mode"] = "observe_only"
        hidden_context = ""
        manager = getattr(self, "heartflow_manager", None)
        if manager is not None and hasattr(manager, "get_hidden_context"):
            try:
                hidden_context = str(manager.get_hidden_context(chat_id) or "")
            except Exception as exc:
                logger.debug(f"[ProactiveTask] heartflow hidden context degraded for {chat_id}: {exc}")
        return {
            "chat_id": chat_id,
            "action": decision.action,
            "reason": decision.reason,
            "executor_pending": snapshot.executor_pending,
            "wait_targets_count": len(snapshot.wait_targets),
            "has_hidden_context": bool(hidden_context.strip()),
            "dispatch_mode": "observe_only",
        }

    async def handle_wakeup_signal(self, chat_id: str, snapshot, decision) -> dict:
        decision.metadata["dispatch_mode"] = "kernel_mediated"
        decision.metadata["dispatch_bridge"] = "PROACTIVE_WAKEUP"
        decision.metadata["wakeup_candidate_present"] = bool(getattr(snapshot, "proactive_summary", {}).get("candidate_present", False))
        decision.metadata["wakeup_cooldown_until"] = float(getattr(snapshot, "proactive_summary", {}).get("next_wakeup_timestamp", 0.0) or 0.0)
        result = await self.wakeup_service.run_for_chat(chat_id)
        cooldown_until = 0.0
        proactive_summary = dict(getattr(snapshot, "latest_activity", {}).get("proactive_summary", {}) or {})
        if bool(result.get("performed", False)) and not bool(result.get("reason") == "quiet_hours"):
            cooldown_until = float(proactive_summary.get("next_wakeup_timestamp", 0.0) or 0.0)
            if cooldown_until <= time.time():
                cooldown_seconds = float(proactive_summary.get("wakeup_cooldown", 0.0) or 0.0)
                if cooldown_seconds > 0:
                    cooldown_until = time.time() + cooldown_seconds
            if cooldown_until > time.time() and self.chat_loop_kernel is not None:
                await self.chat_loop_kernel.set_cooldown(chat_id, "wakeup", cooldown_until, reason=str(result.get("reason", "") or "wakeup_dispatch"))
        return {
            "chat_id": chat_id,
            "action": decision.action,
            "reason": decision.reason,
            "dispatch_mode": "kernel_mediated",
            "bridge": "PROACTIVE_WAKEUP",
            "result": result,
            "cooldown_until": cooldown_until,
            "cooldown_reason": str(result.get("reason", "") or "wakeup_dispatch"),
        }

    async def handle_heartflow_signal(self, chat_id: str, snapshot, decision) -> dict:
        decision.metadata["dispatch_mode"] = "kernel_mediated"
        decision.metadata["dispatch_bridge"] = "HEARTFLOW_EVALUATE"
        result = await self.heartflow_manager.tick_chat(chat_id, snapshot=dict(getattr(snapshot, "latest_activity", {}) or {}))
        cooldown_until = 0.0
        visible_dispatch_performed = bool(
            result.get("visible_dispatch_performed", False) or result.get("synthetic_event_queued", False)
        )
        decision.metadata["heartflow_dispatch_performed"] = bool(result.get("performed", False))
        decision.metadata["heartflow_visible_dispatch"] = visible_dispatch_performed
        if visible_dispatch_performed:
            cooldown_seconds = float(getattr(self.heartflow_manager, "VISIBLE_CANDIDATE_COOLDOWN_SECONDS", 0.0) or 0.0)
            if cooldown_seconds > 0:
                cooldown_until = time.time() + cooldown_seconds
                if self.chat_loop_kernel is not None:
                    await self.chat_loop_kernel.set_cooldown(chat_id, "heartflow", cooldown_until, reason="heartflow_dispatch")
        return {
            "chat_id": chat_id,
            "action": decision.action,
            "reason": decision.reason,
            "dispatch_mode": "kernel_mediated",
            "bridge": "HEARTFLOW_EVALUATE",
            "result": result,
            "cooldown_until": cooldown_until,
            "cooldown_reason": "heartflow_dispatch" if cooldown_until > 0 else "",
            "visible_dispatch_performed": visible_dispatch_performed,
        }

    async def handle_dream_signal(self, chat_id: str, snapshot, decision) -> dict:
        decision.metadata["dispatch_mode"] = "kernel_mediated"
        decision.metadata["dispatch_bridge"] = "DREAM_MAINTENANCE"
        result = await self.dream_scheduler.run_once_for_session(chat_id)
        if isinstance(result, dict):
            result.setdefault("throttle_scope", "global")
        return {
            "chat_id": chat_id,
            "action": decision.action,
            "reason": decision.reason,
            "dispatch_mode": "kernel_mediated",
            "bridge": "DREAM_MAINTENANCE",
            "result": result,
        }

    async def handle_memory_signal(self, chat_id: str, snapshot, decision) -> dict:
        decision.metadata["dispatch_mode"] = "kernel_mediated"
        decision.metadata["dispatch_bridge"] = "MEMORY_MAINTENANCE"
        pipeline = getattr(self.memory_engine, "memory_pipeline", None)
        if pipeline is None or not hasattr(pipeline, "run_maintenance_for_session"):
            result = {"performed": False, "reason": "memory_summarizer_unavailable"}
        else:
            result = await pipeline.run_maintenance_for_session(chat_id)
        return {
            "chat_id": chat_id,
            "action": decision.action,
            "reason": decision.reason,
            "dispatch_mode": "kernel_mediated",
            "bridge": "MEMORY_MAINTENANCE",
            "result": result,
        }

    async def handle_compaction_signal(self, chat_id: str, snapshot, decision) -> dict:
        decision.metadata["dispatch_mode"] = "kernel_mediated"
        decision.metadata["dispatch_bridge"] = "COMPACTION_EVALUATE"
        engine = getattr(self.state_engine, "context_compaction", None)
        if engine is None or not hasattr(engine, "maybe_compact"):
            return {
                "chat_id": chat_id,
                "action": decision.action,
                "reason": decision.reason,
                "dispatch_mode": "kernel_mediated",
                "bridge": "COMPACTION_EVALUATE",
                "result": {"performed": False, "reason": "compaction_unavailable"},
            }
        result = await engine.maybe_compact(chat_id)
        cooldown_until = 0.0
        if hasattr(engine, "get_cooldown_until"):
            try:
                cooldown_until = float(await engine.get_cooldown_until(chat_id) if asyncio.iscoroutinefunction(engine.get_cooldown_until) else engine.get_cooldown_until(chat_id) or 0.0)
            except Exception as exc:
                logger.debug(f"[ProactiveTask] compaction cooldown lookup degraded for {chat_id}: {exc}")
                cooldown_until = 0.0
        if cooldown_until > time.time() and self.chat_loop_kernel is not None:
            await self.chat_loop_kernel.set_cooldown(chat_id, "compaction", cooldown_until, reason=str(getattr(result, "reason", "") or getattr(result, "skipped_reason", "") or "compaction_dispatch"))
        return {
            "chat_id": chat_id,
            "action": decision.action,
            "reason": decision.reason,
            "dispatch_mode": "kernel_mediated",
            "bridge": "COMPACTION_EVALUATE",
            "result": result,
            "cooldown_until": cooldown_until,
            "cooldown_reason": str(getattr(result, "reason", "") or getattr(result, "skipped_reason", "") or "compaction_dispatch"),
        }

    async def _run_chat_heartbeat_pass(self) -> list[dict]:
        if self.chat_loop_kernel is None:
            self._last_due_chat_count = 0
            self._last_skipped_not_due_count = 0
            self._last_due_phase_mix = {}
            self._last_active_candidate_count = 0
            self._last_persistent_due_candidate_count = 0
            self._last_merged_candidate_count = 0
            self._last_selected_persistent_due_count = 0
            self._persistent_due_scan_enabled = False
            self._persistent_due_scan_degraded_reason = "kernel_unavailable"
            self._set_scheduler_poll_mode("IDLE")
            return []
        active_chat_ids: list[str] = []
        if self.runtime_coordinator is not None and hasattr(self.runtime_coordinator, "list_active_chats"):
            try:
                active_chat_ids = list(await self.runtime_coordinator.list_active_chats() or [])
            except Exception as exc:
                logger.debug(f"[ProactiveTask] active chat scan degraded for kernel heartbeat: {exc}")

        self._persistent_due_scan_enabled = rollout_enabled(self.config, "proactive_due_enabled", True)
        self._persistent_due_scan_degraded_reason = ""
        persistent_due_chat_ids: list[str] = []
        due_loader = getattr(self.state_engine, "list_due_proactive_chat_ids", None)
        if self._persistent_due_scan_enabled and callable(due_loader):
            try:
                persistent_due_chat_ids = list(
                    await due_loader(now=time.time(), limit=self.HEARTBEAT_MAX_BATCH * 4) or []
                )
            except Exception as exc:
                self._persistent_due_scan_degraded_reason = exc.__class__.__name__
                logger.warning(f"[ProactiveTask] persisted proactive due scan degraded: {exc}")
        elif self._persistent_due_scan_enabled:
            self._persistent_due_scan_degraded_reason = "due_loader_unavailable"

        candidate_sources: dict[str, str] = {}
        chat_ids: list[str] = []
        for chat_id in active_chat_ids:
            normalized = str(chat_id or "").strip()
            if normalized and normalized not in candidate_sources:
                candidate_sources[normalized] = "active"
                chat_ids.append(normalized)
        for chat_id in persistent_due_chat_ids:
            normalized = str(chat_id or "").strip()
            if not normalized:
                continue
            if normalized not in candidate_sources:
                chat_ids.append(normalized)
            candidate_sources[normalized] = "persistent_due"
        self._last_active_candidate_count = len({str(chat_id or "").strip() for chat_id in active_chat_ids if str(chat_id or "").strip()})
        self._last_persistent_due_candidate_count = len({str(chat_id or "").strip() for chat_id in persistent_due_chat_ids if str(chat_id or "").strip()})
        self._last_merged_candidate_count = len(chat_ids)
        try:
            selection_report = await self.chat_loop_kernel.describe_due_selection(
                chat_ids,
                now=time.time(),
                horizon_seconds=self.HEARTBEAT_DUE_HORIZON_SECONDS,
                max_batch=self.HEARTBEAT_MAX_BATCH,
                candidate_sources=candidate_sources,
            )
        except Exception as exc:
            logger.debug(f"[ProactiveTask] due chat selection degraded for kernel heartbeat: {exc}")
            selection_report = {
                "selected": list(chat_ids or []),
                "skipped_not_due": [],
                "score_breakdown": {},
                "due_phase_mix": {},
                "poll_mode": "FAST",
                "maintenance_budget_total": 0,
                "maintenance_budget_used": 0,
                "maintenance_budget_remaining": 0,
                "maintenance_blocked_by_budget": [],
                "persistent_due_selected": [],
            }
        due_chat_ids = list(selection_report.get("selected", []) or [])
        previous_poll_mode = self._scheduler_poll_mode
        self._last_due_chat_count = len(due_chat_ids)
        self._last_skipped_not_due_count = len(selection_report.get("skipped_not_due", []) or [])
        self._last_due_phase_mix = dict(selection_report.get("due_phase_mix", {}) or {})
        self._last_maintenance_budget_total = int(selection_report.get("maintenance_budget_total", 0) or 0)
        self._last_maintenance_budget_used = int(selection_report.get("maintenance_budget_used", 0) or 0)
        self._last_maintenance_budget_remaining = int(selection_report.get("maintenance_budget_remaining", 0) or 0)
        self._scheduler_batch_limit = int(dict(selection_report.get("batch_plan", {}) or {}).get("total_limit", self.HEARTBEAT_MAX_BATCH) or self.HEARTBEAT_MAX_BATCH)
        self._last_scheduler_batch_plan = dict(selection_report.get("batch_plan", {}) or {})
        self._last_batch_fill_rate = float(selection_report.get("batch_fill_rate", 0.0) or 0.0)
        self._last_forced_promotion_count = len(list(selection_report.get("forced_promotions_selected", []) or []))
        self._last_selected_persistent_due_count = len(list(selection_report.get("persistent_due_selected", []) or []))
        self._last_quota_skip_counts = dict(selection_report.get("quota_skip_counts", {}) or {})
        self._busy_backpressure_active = bool(selection_report.get("busy_backpressure_active", False))
        self._maintenance_backpressure_active = bool(selection_report.get("maintenance_backpressure_active", False))
        self._last_selection_summary = {
            "selected_count": len(due_chat_ids),
            "forced_promotion_count": self._last_forced_promotion_count,
            "dialogue_selected_count": len(list(selection_report.get("dialogue_selected", []) or [])),
            "maintenance_selected_count": len(list(selection_report.get("maintenance_selected", []) or [])),
            "persistent_due_selected_count": self._last_selected_persistent_due_count,
            "skipped_by_batch_count": len(list(selection_report.get("skipped_by_batch", []) or [])),
        }
        self._set_scheduler_poll_mode(str(selection_report.get("poll_mode", "IDLE") or "IDLE"))
        self._last_poll_mode_transition = {
            "previous": previous_poll_mode,
            "current": self._scheduler_poll_mode,
            "reason": str(selection_report.get("poll_mode_reason", "") or ""),
        }
        if hasattr(self.chat_loop_kernel, "commit_due_selection_counters"):
            try:
                await self.chat_loop_kernel.commit_due_selection_counters(selection_report)
            except Exception as exc:
                logger.debug(f"[ProactiveTask] due selection counter commit degraded: {exc}")
        results: list[dict] = []
        if hasattr(self.chat_loop_kernel, "set_heartbeat_scheduler_context"):
            self.chat_loop_kernel.set_heartbeat_scheduler_context(selection_report)
        try:
            for chat_id in due_chat_ids:
                try:
                    tick = await self.chat_loop_kernel.tick(chat_id=str(chat_id or ""), trigger="heartbeat")
                    results.append(
                        {
                            "chat_id": str(chat_id or ""),
                            "action": tick.decision.action,
                            "reason": tick.decision.reason,
                        }
                    )
                except Exception as exc:
                    logger.debug(f"[ProactiveTask] kernel heartbeat degraded for {chat_id}: {exc}")
        finally:
            final_context = {}
            if hasattr(self.chat_loop_kernel, "get_heartbeat_scheduler_context"):
                try:
                    final_context = dict(self.chat_loop_kernel.get_heartbeat_scheduler_context() or {})
                except Exception:  # ponytail: heartbeat context is non-critical debug info
                    final_context = {}
            if hasattr(self.chat_loop_kernel, "clear_heartbeat_scheduler_context"):
                self.chat_loop_kernel.clear_heartbeat_scheduler_context()
        if final_context:
            self._last_maintenance_budget_used = int(final_context.get("maintenance_budget_used", 0) or 0)
            self._last_maintenance_budget_remaining = int(final_context.get("maintenance_budget_remaining", 0) or 0)
        else:
            self._last_maintenance_budget_used = int(selection_report.get("maintenance_budget_used", 0) or 0)
            self._last_maintenance_budget_remaining = int(selection_report.get("maintenance_budget_remaining", 0) or 0)
        return results

    async def _run_maintenance_cycle(self) -> None:
        try:
            await self.decay_service.run_once()
        except Exception as exc:
            logger.error(f"[ProactiveTask] decay maintenance degraded: {exc}")
        try:
            await self._run_memory_store_maintenance()
        except Exception as exc:
            logger.error(f"[ProactiveTask] memory store maintenance degraded: {exc}")
        try:
            await self.group_signin_service.run_once()
        except Exception as exc:
            logger.error(f"[ProactiveTask] group signin maintenance degraded: {exc}")
        try:
            self._fire_background_task(
                lambda: self.heartflow_topic_digest_service.run_once(self.heartflow_manager),
                task_name="proactive.heartflow",
            )
        except Exception as exc:
            logger.error(f"[ProactiveTask] heartflow topic digest scheduling degraded: {exc}")

    _MEMORY_STORE_MAINTENANCE_INTERVAL_SEC = 24 * 3600.0
    # OPT-05/WU-04 分步启用：purge 未开时把各类宽限期推到天文数字——索引一致性
    # 修复照常运行，但不产生任何物理删除（首周观察期的保守策略）
    _MAINTENANCE_NO_PURGE_POLICY = {
        "stale_grace_seconds": 1e12,
        "pending_jargon_grace_seconds": 1e12,
        "pending_human_jargon_grace_seconds": 1e12,
        "rejected_jargon_grace_seconds": 1e12,
        "pending_expression_grace_seconds": 1e12,
        "rejected_expression_grace_seconds": 1e12,
    }

    async def _run_memory_store_maintenance(self) -> None:
        # OPT-05/WU-04: MemoryMaintenanceService.run_once（索引一致性修复 + 黑话/
        # 表达积压过期清理 + 墓碑 purge）此前无任何调度方——唯一入口是前端从不
        # 调用的 WebUI 端点，待审积压只增不减、投影缺口重启前不自愈
        memory_cfg = getattr(self.config, "memory", None)
        if not bool(getattr(memory_cfg, "maintenance_schedule_enabled", True)):
            return
        engine = getattr(self, "memory_engine", None)
        maintenance = getattr(engine, "maintenance_service", None) if engine else None
        if maintenance is None or not hasattr(maintenance, "run_once"):
            return
        now = time.time()
        last_run = float(getattr(self, "_last_store_maintenance_at", 0.0) or 0.0)
        if now - last_run < self._MEMORY_STORE_MAINTENANCE_INTERVAL_SEC:
            return
        self._last_store_maintenance_at = now
        purge_enabled = bool(getattr(memory_cfg, "maintenance_purge_enabled", False))
        policy = {} if purge_enabled else dict(self._MAINTENANCE_NO_PURGE_POLICY)
        report = await maintenance.run_once(policy=policy)
        logger.info(
            "[ProactiveTask] memory store maintenance completed: "
            f"purge_enabled={purge_enabled} "
            f"physically_deleted={report.get('physically_deleted', 0)} "
            f"projection_deleted={report.get('projection_deleted', 0)} "
            f"marked_stale={report.get('marked_stale', 0)} "
            f"index_repair={report.get('index_repair', {})} "
            f"errors={len(report.get('errors', []) or [])}"
        )

    async def _loop(self):
        while self._is_running:
            try:
                await asyncio.sleep(self._scheduler_poll_interval_seconds)
                await self._run_chat_heartbeat_pass()
                now = time.time()
                await self.scheduled_scenario_service.tick(now=now)
                # ponytail: M1 — only skip maintenance block, not profiling/reflection/diary
                run_maintenance = (now - self._last_global_maintenance_run) >= self.GLOBAL_MAINTENANCE_INTERVAL_SECONDS

                if run_maintenance:
                    self._last_global_maintenance_run = now
                    await self._run_maintenance_cycle()

                if now - self._last_profile_run > 3600:
                    self._last_profile_run = now
                    self._fire_background_task(self._run_profiling_task, task_name="proactive.profile")

                if run_maintenance and self.diary_service.should_run(self._last_diary_date, now):
                    diary_date = time.strftime("%Y-%m-%d", time.localtime(now))
                    if self._diary_pending_date != diary_date:
                        self._diary_pending_date = diary_date
                        self._fire_background_task(
                            lambda: self._run_daily_diary_task_with_jitter(diary_date),
                            task_name="proactive.diary",
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[ProactiveTask] scheduler loop degraded: {exc}")
                await asyncio.sleep(self._scheduler_poll_interval_seconds)

    def _set_scheduler_poll_mode(self, mode: str) -> None:
        normalized = str(mode or "IDLE").strip().upper()
        if normalized == "FAST":
            self._scheduler_poll_mode = "FAST"
            self._scheduler_poll_interval_seconds = self.FAST_POLL_INTERVAL_SECONDS
            return
        if normalized == "NORMAL":
            self._scheduler_poll_mode = "NORMAL"
            self._scheduler_poll_interval_seconds = self.NORMAL_POLL_INTERVAL_SECONDS
            return
        self._scheduler_poll_mode = "IDLE"
        self._scheduler_poll_interval_seconds = self.IDLE_POLL_INTERVAL_SECONDS

    def describe_status(self) -> dict:
        bridge_status = {}
        kernel_status = {}
        if self.chat_loop_kernel is not None and hasattr(self.chat_loop_kernel, "describe_status_sync"):
            try:
                kernel_status = dict(self.chat_loop_kernel.describe_status_sync() or {})
                bridge_status = dict(kernel_status.get("dispatch_bridges", {}) or {})
            except Exception:  # ponytail: kernel status is best-effort observability
                kernel_status = {}
                bridge_status = {}
        return {
            "running": self._is_running,
            "dream_ready": self.dream_agent is not None,
            "last_profile_run": self._last_profile_run,
            "last_diary_date": self._last_diary_date,
            "diary_pending_date": self._diary_pending_date,
            "background_tasks": len(self._background_tasks),
            "profiling_active_users": len(getattr(self, "_profiling_user_ids", set())),
            "dream_scheduler": self.dream_scheduler.describe_status(),
            "heartflow": self.heartflow_manager.describe_status(),
            "heartflow_topic_digest": self.heartflow_topic_digest_service.describe_status(),
            "proactive_dispatcher": self.proactive_dispatcher.describe_status(),
            "scheduled_scenarios": self.scheduled_scenario_service.describe_status(),
            "group_signin": self.group_signin_service.describe_status(),
            "review_dispatcher_ready": self.review_dispatcher.reflect_tracker is not None,
            "chat_loop_kernel_bound": self.chat_loop_kernel is not None,
            "heartbeat_mode": "kernel_mediated",
            "scheduler_poll_mode": self._scheduler_poll_mode,
            "scheduler_poll_interval": self._scheduler_poll_interval_seconds,
            "global_maintenance_interval": self.GLOBAL_MAINTENANCE_INTERVAL_SECONDS,
            "due_chat_count": self._last_due_chat_count,
            "skipped_not_due_count": self._last_skipped_not_due_count,
            "active_candidate_count": self._last_active_candidate_count,
            "persistent_due_candidate_count": self._last_persistent_due_candidate_count,
            "merged_candidate_count": self._last_merged_candidate_count,
            "selected_persistent_due_count": self._last_selected_persistent_due_count,
            "persistent_due_scan_enabled": self._persistent_due_scan_enabled,
            "persistent_due_scan_degraded_reason": self._persistent_due_scan_degraded_reason,
            "due_phase_mix": dict(self._last_due_phase_mix),
            "scheduler_batch_limit": self._scheduler_batch_limit,
            "scheduler_batch_plan": dict(self._last_scheduler_batch_plan),
            "batch_fill_rate": self._last_batch_fill_rate,
            "forced_promotion_count": self._last_forced_promotion_count,
            "quota_skip_counts": dict(self._last_quota_skip_counts),
            "busy_backpressure_active": self._busy_backpressure_active,
            "maintenance_backpressure_active": self._maintenance_backpressure_active,
            "last_selection_summary": dict(self._last_selection_summary),
            "poll_mode_transition": dict(self._last_poll_mode_transition),
            "maintenance_budget_total": self._last_maintenance_budget_total,
            "maintenance_budget_used": self._last_maintenance_budget_used,
            "maintenance_budget_remaining": self._last_maintenance_budget_remaining,
            "scheduler_policy": dict(kernel_status.get("scheduler_policy", {}) or {}),
            "kernel_due_selection_summary": dict(kernel_status.get("last_due_selection_summary", {}) or {}),
            "private_wait_visible_in_heartbeat": True,
            "heartflow_preview_readonly": True,
            "dream_scope": "global_throttle",
            "wakeup_bridge_ready": bool(bridge_status.get("PROACTIVE_WAKEUP", False)),
            "heartflow_bridge_ready": bool(bridge_status.get("HEARTFLOW_EVALUATE", False)),
            "dream_bridge_ready": bool(bridge_status.get("DREAM_MAINTENANCE", False)),
            "memory_bridge_ready": bool(bridge_status.get("MEMORY_MAINTENANCE", False)),
            "compaction_bridge_ready": bool(bridge_status.get("COMPACTION_EVALUATE", False)),
        }


__all__ = ["ProactiveTask"]
