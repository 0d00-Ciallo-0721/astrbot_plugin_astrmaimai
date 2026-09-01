from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
import inspect
import json
from pathlib import Path
import random
import time
from time import monotonic
import uuid

from astrbot.api import logger

from ..conversation.runtime.architecture_rollout import rollout_enabled
from ..infrastructure.context_economy import PromptTemplateId
from ..infrastructure.runtime.background_task_budget import (
    BackgroundTaskBudget,
    BackgroundTaskQueueFull,
    BackgroundTaskQueueTimeout,
)
from ..infrastructure.runtime.lane_manager import LaneKey
from ..infrastructure.runtime.background_task_ledger import (
    BackgroundTaskLedger,
    TaskLease,
)
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
    PROFILE_CLAIM_LEASE_SEC = 1800.0
    PROFILE_FAILURE_BASE_BACKOFF_SEC = 3600.0
    PROFILE_FAILURE_MAX_BACKOFF_SEC = 86400.0
    MAX_PENDING_PROFILE_CLAIM_RELEASES = 1024

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
        self.config = config if config else gateway.config
        self.background_task_budget = background_task_budget or self._build_local_background_budget(
            self.config
        )
        task_db_path = getattr(persistence, "db_path", None)
        self._task_ledger = BackgroundTaskLedger(task_db_path) if task_db_path else None
        self.expression_governance_runner = None
        self.auto_check_task = None
        self.reflect_tracker = None
        self._is_running = False
        self._task = None
        self._background_tasks: set[asyncio.Task] = set()
        self._lease_settlement_tasks: set[asyncio.Task] = set()
        self._maintenance_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._kernel_signal_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._bg_semaphore = asyncio.Semaphore(2)
        self._profile_semaphore = asyncio.Semaphore(1)
        self._profiling_user_ids: set[str] = set()
        self._local_profile_claims: set[str] = set()
        self._profile_claim_release_pending_keys: set[tuple[str, str]] = set()
        self._profiling_stats: dict[str, int] = {
            "profile_cooldown_skipped": 0,
            "profile_duplicate_skipped": 0,
            "nickname_cooldown_skipped": 0,
            "profile_generation_failed": 0,
            "profile_generation_inflight": 0,
            "profile_generation_requests": 0,
            "profile_budget_rejected": 0,
            "profile_claim_release_failed": 0,
            "profile_claim_release_pending": 0,
        }
        self._last_profile_run = 0.0
        self._last_proactive_scan = 0.0
        self._background_task_stats: dict[str, dict[str, int | float]] = {}
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
            history_db_path=getattr(persistence, "db_path", None),
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
        self.dream_generator = DreamGenerator(
            gateway,
            config=self.config,
            background_task_budget=getattr(self, "background_task_budget", None),
        )
        self.dream_agent = None

    def _profile_scheduler_state_path(self) -> Path | None:
        cache_dir = getattr(getattr(self, "persistence", None), "cache_dir", None)
        if cache_dir is None:
            return None
        return Path(cache_dir) / "proactive_scheduler_state.json"

    def _load_profile_scheduler_state(self) -> None:
        path = self._profile_scheduler_state_path()
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self._last_profile_run = float(payload.get("last_profile_run", 0.0) or 0.0)
            self._last_proactive_scan = float(payload.get("last_proactive_scan", 0.0) or 0.0)
        except Exception as exc:
            logger.debug(f"[ProactiveTask] scheduler state restore degraded: {exc}")

    async def _persist_profile_scheduler_state(self) -> None:
        path = self._profile_scheduler_state_path()
        if path is None:
            return
        payload = {
            "last_profile_run": self._last_profile_run,
            "last_proactive_scan": self._last_proactive_scan,
            "updated_at": time.time(),
        }

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)

        try:
            await asyncio.to_thread(_write)
        except Exception as exc:
            logger.debug(f"[ProactiveTask] scheduler state persist degraded: {exc}")

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
        if task_family == "profile":
            self._profile_stat_inc("profile_generation_requests")
        async def _call():
            return await self.gateway.call_proactive_task(
                prompt=prompt,
                system_prompt=system_prompt,
                lane_key=LaneKey(subsystem="bg", task_family=task_family, scope_id=scope_id or "global", scope_kind="global"),
                base_origin="",
                persona_id=getattr(self.config.persona, "persona_id", "") or "global",
                template_envelope=template_envelope,
            )

        budget = getattr(self, "background_task_budget", None)
        if budget is None:
            return await _call()
        return await budget.run(
            _call,
            task_name=f"proactive.{task_family}",
            scope_id=str(scope_id or "global"),
            defer_release_on_timeout=True,
        )

    async def _call_background_lane_with_metadata(
        self,
        task_family: str,
        scope_id: str,
        prompt: str,
        system_prompt: str = "",
        template_envelope=None,
    ) -> tuple[str, str]:
        gateway = getattr(self, "gateway", None)
        caller = getattr(gateway, "call_proactive_task_result", None)
        if not callable(caller):
            text = await self._call_background_lane(
                task_family,
                scope_id,
                prompt,
                system_prompt=system_prompt,
                template_envelope=template_envelope,
            )
            return text, ""
        if task_family == "profile":
            self._profile_stat_inc("profile_generation_requests")
        async def _call():
            return await caller(
                prompt=prompt,
                system_prompt=system_prompt,
                lane_key=LaneKey(
                    subsystem="bg",
                    task_family=task_family,
                    scope_id=scope_id or "global",
                    scope_kind="global",
                ),
                base_origin="",
                persona_id=getattr(self.config.persona, "persona_id", "") or "global",
                template_envelope=template_envelope,
            )

        budget = getattr(self, "background_task_budget", None)
        result = (
            await _call()
            if budget is None
            else await budget.run(
                _call,
                task_name=f"proactive.{task_family}",
                scope_id=str(scope_id or "global"),
                defer_release_on_timeout=True,
            )
        )
        if isinstance(result, str):
            return result, ""
        return str(getattr(result, "text", "") or ""), str(getattr(result, "model_id", "") or "")

    def _profile_stat_inc(self, key: str, amount: int = 1) -> None:
        stats = getattr(self, "_profiling_stats", None)
        if not isinstance(stats, dict):
            stats = {}
            self._profiling_stats = stats
        stats[key] = int(stats.get(key, 0) or 0) + int(amount or 0)

    @staticmethod
    def _profile_metadata(profile) -> dict:
        metadata = getattr(profile, "profile_metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            setattr(profile, "profile_metadata", metadata)
        return metadata

    @staticmethod
    def _profile_timestamp(value, default: float = 0.0) -> float:
        """读取画像时间字段时容忍旧数据或手工编辑产生的脏值。"""
        try:
            return float(value or default)
        except (TypeError, ValueError):
            return float(default)

    def _profile_backoff_active(self, profile, kind: str, now: float) -> bool:
        service = getattr(self.state_engine, "user_profile_service", None)
        checker = getattr(service, "profile_generation_backoff_active", None)
        if callable(checker):
            return bool(checker(profile, kind, now=now))
        key = "nickname_generation_failed_until" if kind == "nickname" else "profile_generation_failed_until"
        try:
            return float(self._profile_metadata(profile).get(key, 0.0) or 0.0) > now
        except (TypeError, ValueError):
            return False

    async def _record_profile_generation_failure(self, profile, kind: str, exc: BaseException | None = None) -> None:
        self._profile_stat_inc("profile_generation_failed")
        if isinstance(exc, (BackgroundTaskQueueFull, BackgroundTaskQueueTimeout)):
            self._profile_stat_inc("profile_budget_rejected")
        service = getattr(self.state_engine, "user_profile_service", None)
        recorder = getattr(service, "record_profile_generation_failure", None)
        if callable(recorder):
            recorder(
                profile,
                kind,
                base_backoff_sec=self.PROFILE_FAILURE_BASE_BACKOFF_SEC,
                max_backoff_sec=self.PROFILE_FAILURE_MAX_BACKOFF_SEC,
            )
        else:
            metadata = self._profile_metadata(profile)
            count_key = "nickname_generation_failure_count" if kind == "nickname" else "profile_generation_failure_count"
            until_key = "nickname_generation_failed_until" if kind == "nickname" else "profile_generation_failed_until"
            count = int(metadata.get(count_key, 0) or 0) + 1
            metadata[count_key] = count
            metadata[until_key] = time.time() + min(
                self.PROFILE_FAILURE_MAX_BACKOFF_SEC,
                self.PROFILE_FAILURE_BASE_BACKOFF_SEC * (2 ** (count - 1)),
            )
            setattr(profile, "is_dirty", True)
        try:
            await self._save_user_profile(profile)
        except Exception as save_exc:
            logger.warning("[Life] profile failure backoff persistence degraded: %s", save_exc)

    async def _claim_profile_generation(self, user_id: str) -> str | None:
        service = getattr(self.state_engine, "user_profile_service", None)
        claim = getattr(service, "claim_profile_generation", None)
        if callable(claim):
            return await claim(user_id, lease_sec=self.PROFILE_CLAIM_LEASE_SEC)
        local_claims = getattr(self, "_local_profile_claims", None)
        if local_claims is None:
            local_claims = set()
            self._local_profile_claims = local_claims
        if user_id in local_claims:
            return None
        local_claims.add(user_id)
        return user_id

    async def _release_profile_generation(self, user_id: str, claim_token: str | None) -> bool:
        if not claim_token:
            return False
        pending_keys = getattr(self, "_profile_claim_release_pending_keys", None)
        if not isinstance(pending_keys, set):
            pending_keys = set()
            self._profile_claim_release_pending_keys = pending_keys
        pending_key = (str(user_id), str(claim_token))
        service = getattr(self.state_engine, "user_profile_service", None)
        release = getattr(service, "release_profile_generation", None)
        if callable(release):
            for attempt in range(3):
                try:
                    if await release(user_id, claim_token):
                        pending_keys.discard(pending_key)
                        return True
                    getter = getattr(service, "get_profile_generation_claim", None)
                    if callable(getter):
                        current_token = await getter(user_id)
                        if current_token is None or str(current_token) != str(claim_token):
                            pending_keys.discard(pending_key)
                            return True
                except Exception as exc:
                    if attempt == 2:
                        logger.warning("[Life] profile generation claim release degraded for %s: %s", user_id, exc)
                if attempt < 2:
                    await asyncio.sleep(0)
            self._profile_stat_inc("profile_claim_release_failed")
            if len(pending_keys) >= self.MAX_PENDING_PROFILE_CLAIM_RELEASES:
                pending_keys.pop()
            pending_keys.add(pending_key)
            return False
        local_claims = getattr(self, "_local_profile_claims", None)
        if isinstance(local_claims, set):
            local_claims.discard(user_id)
        pending_keys.discard(pending_key)
        return True

    async def _retry_pending_profile_claim_releases(self) -> None:
        pending_keys = getattr(self, "_profile_claim_release_pending_keys", set())
        for user_id, claim_token in list(pending_keys):
            await self._release_profile_generation(user_id, claim_token)

    async def start(self):
        if self._is_running:
            return
        if self._task_ledger is not None:
            try:
                recovered = await self._task_ledger.recover_expired_leases()
                if recovered:
                    logger.info(
                        "[ProactiveTask] recovered expired background leases count=%s",
                        recovered,
                    )
            except Exception as exc:
                logger.warning("[ProactiveTask] background lease recovery degraded: %s", exc)
        resume_scenarios = getattr(
            getattr(self, "scheduled_scenario_service", None),
            "resume",
            None,
        )
        if callable(resume_scenarios):
            resume_scenarios()
        resume_dispatcher = getattr(self.proactive_dispatcher, "resume", None)
        if callable(resume_dispatcher) and resume_dispatcher() is False:
            logger.warning("[ProactiveTask] dispatcher is still draining; start deferred")
            return
        self._is_running = True
        self._load_profile_scheduler_state()
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
        request_shutdown = getattr(
            getattr(self, "scheduled_scenario_service", None),
            "request_shutdown",
            None,
        )
        if callable(request_shutdown):
            request_shutdown()
        self._is_running = False
        await self._persist_profile_scheduler_state()
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
        maintenance_tasks = getattr(self, "_maintenance_tasks", None)
        if isinstance(maintenance_tasks, dict):
            maintenance_tasks.clear()
        settlement_tasks = getattr(self, "_lease_settlement_tasks", None)
        if isinstance(settlement_tasks, set) and settlement_tasks:
            await asyncio.gather(*list(settlement_tasks), return_exceptions=True)
            settlement_tasks.clear()
        kernel_signal_tasks = getattr(self, "_kernel_signal_tasks", None)
        if isinstance(kernel_signal_tasks, dict):
            kernel_signal_tasks.clear()
        clear_memory_claims = getattr(getattr(self, "scheduled_scenario_service", None), "clear_memory_claims", None)
        if callable(clear_memory_claims):
            clear_memory_claims()

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
        if self._task_ledger is None:
            db_path = getattr(getattr(db_service, "persistence", None), "db_path", None)
            if db_path:
                self._task_ledger = BackgroundTaskLedger(db_path)
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
            chat_loop_kernel.bind_dispatch_bridge("PROACTIVE_WAKEUP", self.enqueue_wakeup_signal)
            chat_loop_kernel.bind_dispatch_bridge("HEARTFLOW_EVALUATE", self.enqueue_heartflow_signal)
            chat_loop_kernel.bind_dispatch_bridge("DREAM_MAINTENANCE", self.enqueue_dream_signal)
            chat_loop_kernel.bind_dispatch_bridge("MEMORY_MAINTENANCE", self.enqueue_memory_signal)
            chat_loop_kernel.bind_dispatch_bridge("COMPACTION_EVALUATE", self.enqueue_compaction_signal)

    def _bind_dream_dependencies(self):
        self.dream_agent = DreamAgent(
            gateway=self.gateway,
            db_service=self._db_service,
            memory_engine=self.memory_engine,
            config=self.config,
            background_task_budget=getattr(self, "background_task_budget", None),
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
        task_lease: TaskLease | None = None,
        checkpoint_after: Any = None,
        llm_call_count: Any = 0,
        cancel_status: str = "retry_wait",
    ):
        budget = getattr(self, "background_task_budget", None)
        if budget is None:
            budget = self._build_local_background_budget(getattr(self, "config", None))
            self.background_task_budget = budget
        async def _run_budgeted():
            return await budget.run(
                awaitable_factory,
                task_name=task_name,
                scope_id=scope_id,
                defer_release_on_timeout=True,
            )

        coro = _run_budgeted()
        base_coro = coro
        if task_lease is not None and self._task_ledger is not None:
            async def _finish_lease(**kwargs) -> bool:
                for attempt in range(3):
                    try:
                        changed = await self._task_ledger.finish(task_lease, **kwargs)
                        if changed or attempt == 2:
                            return bool(changed)
                    except Exception as exc:
                        if attempt == 2:
                            logger.warning(
                                "[ProactiveTask] background lease settlement failed task=%s: %s",
                                getattr(task_lease, "task_id", ""),
                                exc,
                            )
                            return False
                    await asyncio.sleep(0)
                return False

            async def _run_and_settle_lease():
                try:
                    result = await base_coro
                except asyncio.CancelledError:
                    await _finish_lease(
                        status=cancel_status,
                        error="cancelled",
                    )
                    raise
                except (BackgroundTaskQueueFull, BackgroundTaskQueueTimeout) as exc:
                    await _finish_lease(
                        status="retry_wait",
                        error=str(exc),
                        retry_after_seconds=300.0,
                    )
                    raise
                except Exception as exc:
                    await _finish_lease(
                        status="retry_wait",
                        error=str(exc),
                        retry_after_seconds=900.0,
                    )
                    raise
                resolved_checkpoint = (
                    checkpoint_after(result)
                    if callable(checkpoint_after)
                    else checkpoint_after or {"completed": True}
                )
                resolved_llm_calls = (
                    llm_call_count(result)
                    if callable(llm_call_count)
                    else llm_call_count
                )
                await _finish_lease(
                    status="succeeded",
                    checkpoint_after=resolved_checkpoint,
                    llm_call_count=max(0, int(resolved_llm_calls or 0)),
                )
                return result

            coro = _run_and_settle_lease()
        task = asyncio.create_task(coro)
        task._astrmai_task_name = task_name
        task._astrmai_scope_id = str(scope_id or "")
        task._astrmai_started_at = time.time()
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_task_result)
        if task_lease is not None and cancel_status != "retry_wait":
            def _settle_unstarted_cancel(completed: asyncio.Task) -> None:
                if not completed.cancelled() or self._task_ledger is None:
                    return
                close = getattr(coro, "close", None)
                if callable(close):
                    close()
                close_base = getattr(base_coro, "close", None)
                if callable(close_base):
                    close_base()
                async def _settle() -> None:
                    try:
                        await _finish_lease(
                            status=cancel_status,
                            error="cancelled_before_start",
                        )
                    except Exception:
                        logger.debug(
                            "[ProactiveTask] pre-start cancellation settlement degraded task=%s",
                            task_name,
                        )
                settlement = asyncio.create_task(_settle())
                settlement_tasks = getattr(self, "_lease_settlement_tasks", None)
                if not isinstance(settlement_tasks, set):
                    settlement_tasks = set()
                    self._lease_settlement_tasks = settlement_tasks
                settlement_tasks.add(settlement)
                settlement.add_done_callback(settlement_tasks.discard)
            task.add_done_callback(_settle_unstarted_cancel)
        return task

    async def _enqueue_managed_maintenance(
        self,
        *,
        task_family: str,
        scope_id: str,
        awaitable_factory,
        lease_seconds: float = 900.0,
    ) -> dict:
        """Submit a maintenance job through the shared budget and ledger.

        The scheduler is called frequently, so this helper owns the per-scope
        de-duplication and keeps the actual service coroutine out of the
        scheduler's critical path.  A ledger lease is authoritative when
        available; the in-process map covers hosts without a persistence DB.
        """
        family = str(task_family or "").strip() or "maintenance"
        scope = str(scope_id or "").strip() or "global"
        key = (family, scope)
        tasks = getattr(self, "_maintenance_tasks", None)
        if not isinstance(tasks, dict):
            tasks = {}
            self._maintenance_tasks = tasks
        active = tasks.get(key)
        if active is not None and not active.done():
            return {
                "queued": False,
                "reason": "already_queued",
                "task_family": family,
                "scope_id": scope,
            }
        run_id = f"{family}_{uuid.uuid4().hex}"
        if getattr(self, "_is_running", True) is False:
            task_ledger = getattr(self, "_task_ledger", None)
            task_id = ""
            if task_ledger is not None and hasattr(task_ledger, "record_rejected"):
                try:
                    task_id = await task_ledger.record_rejected(
                        task_family=family,
                        scope_id=scope,
                        run_id=run_id,
                        error="shutdown",
                    )
                except Exception as exc:
                    logger.debug(
                        "[ProactiveTask] maintenance rejection recording degraded task=%s: %s",
                        family,
                        exc,
                    )
            return {
                "queued": False,
                "reason": "shutdown_rejected",
                "status": "rejected",
                "task_family": family,
                "scope_id": scope,
                "run_id": run_id,
                "task_id": task_id,
            }

        task_ledger = getattr(self, "_task_ledger", None)
        task_lease = None
        if task_ledger is not None:
            try:
                task_lease = await task_ledger.claim(
                    task_family=family,
                    scope_id=scope,
                    input_fingerprint=f"{family}:{scope}",
                    lease_seconds=lease_seconds,
                    payload={"run_id": run_id, "task_family": family, "scope_id": scope},
                    checkpoint_before={"run_id": run_id},
                )
            except Exception as exc:
                logger.warning(
                    "[ProactiveTask] maintenance lease claim degraded task=%s scope=%s: %s",
                    family,
                    scope,
                    exc,
                )
                return {
                    "queued": False,
                    "reason": "lease_error",
                    "status": "rejected",
                    "task_family": family,
                    "scope_id": scope,
                    "run_id": run_id,
                }
            if task_lease is None:
                return {
                    "queued": False,
                    "reason": "lease_busy",
                    "status": "retry_wait",
                    "task_family": family,
                    "scope_id": scope,
                }
            if getattr(self, "_is_running", True) is False:
                try:
                    await task_ledger.finish(
                        task_lease,
                        status="shutdown",
                        error="shutdown_after_claim",
                    )
                except Exception as exc:
                    logger.debug(
                        "[ProactiveTask] post-claim shutdown settlement degraded task=%s: %s",
                        family,
                        exc,
                    )
                return {
                    "queued": False,
                    "reason": "shutdown_rejected",
                    "status": "shutdown",
                    "task_family": family,
                    "scope_id": scope,
                    "run_id": run_id,
                    "task_id": task_lease.task_id,
                }

        def _checkpoint(result):
            checkpoint = {"run_id": run_id, "completed": True}
            if isinstance(result, dict):
                checkpoint["result"] = {
                    key: value for key, value in result.items()
                    if key in {"processed", "errors", "physically_deleted", "projection_deleted"}
                }
            return checkpoint

        try:
            task = self._fire_background_task(
                awaitable_factory,
                task_name=family,
                scope_id=scope,
                task_lease=task_lease,
                checkpoint_after=_checkpoint,
                cancel_status="cancelled",
            )
        except Exception as exc:
            if task_lease is not None and task_ledger is not None:
                try:
                    await task_ledger.finish(
                        task_lease,
                        status="failed",
                        error=str(exc),
                    )
                except Exception:
                    logger.debug(
                        "[ProactiveTask] maintenance lease settlement degraded task=%s",
                        family,
                    )
            return {
                "queued": False,
                "reason": "schedule_error",
                "status": "failed",
                "task_family": family,
                "scope_id": scope,
                "run_id": run_id,
            }
        task._astrmai_run_id = run_id
        task._astrmai_task_family = family
        tasks[key] = task

        def _clear_maintenance_task(completed: asyncio.Task) -> None:
            if tasks.get(key) is completed:
                tasks.pop(key, None)

        task.add_done_callback(_clear_maintenance_task)
        return {
            "queued": True,
            "reason": "background_dispatch_queued",
            "status": "running",
            "task_family": family,
            "scope_id": scope,
            "run_id": run_id,
            "task_id": getattr(task_lease, "task_id", "") if task_lease else "",
        }

    @staticmethod
    def _build_local_background_budget(config) -> BackgroundTaskBudget:
        infra = getattr(config, "infra", None)
        return BackgroundTaskBudget(
            int(getattr(infra, "background_task_concurrency", 2) or 2),
            max_queue=int(getattr(infra, "background_task_queue_limit", 64) or 0),
            wait_timeout_sec=float(
                getattr(infra, "background_task_wait_timeout_sec", 120.0) or 120.0
            ),
            execution_timeout_sec=float(
                getattr(infra, "background_task_execution_timeout_sec", 300.0) or 300.0
            ),
        )

    async def _enqueue_kernel_signal(
        self,
        action: str,
        handler,
        chat_id: str,
        snapshot,
        decision,
    ) -> dict:
        key = (str(action), str(chat_id or ""))
        tasks = getattr(self, "_kernel_signal_tasks", None)
        if not isinstance(tasks, dict):
            tasks = {}
            self._kernel_signal_tasks = tasks
        active = tasks.get(key)
        if active is not None and not active.done():
            return {
                "chat_id": str(chat_id or ""),
                "action": str(action),
                "dispatch_mode": "queued_background",
                "bridge": str(action),
                "queued": False,
                "reason": "already_queued",
            }

        task_name = "proactive." + str(action or "signal").lower()
        task_lease = None
        task_ledger = getattr(self, "_task_ledger", None)
        if task_ledger is not None:
            task_lease = await task_ledger.claim(
                task_family=task_name,
                scope_id=str(chat_id or ""),
                input_fingerprint="",
                lease_seconds=300.0,
                payload={"action": str(action or ""), "chat_id": str(chat_id or "")},
            )
            if task_lease is None:
                return {
                    "chat_id": str(chat_id or ""),
                    "action": str(action),
                    "dispatch_mode": "queued_background",
                    "bridge": str(action),
                    "queued": False,
                    "reason": "lease_busy",
                }
        task = self._fire_background_task(
            lambda: handler(chat_id, snapshot, decision),
            task_name=task_name,
            scope_id=str(chat_id or ""),
            task_lease=task_lease,
            cancel_status="cancelled",
        )
        tasks[key] = task

        def clear_signal_task(completed: asyncio.Task) -> None:
            if tasks.get(key) is completed:
                tasks.pop(key, None)

        task.add_done_callback(clear_signal_task)
        return {
            "chat_id": str(chat_id or ""),
            "action": str(action),
            "dispatch_mode": "queued_background",
            "bridge": str(action),
            "queued": True,
            "reason": "background_dispatch_queued",
        }

    async def enqueue_wakeup_signal(self, chat_id: str, snapshot, decision) -> dict:
        return await self._enqueue_kernel_signal(
            "PROACTIVE_WAKEUP", self.handle_wakeup_signal, chat_id, snapshot, decision
        )

    async def enqueue_heartflow_signal(self, chat_id: str, snapshot, decision) -> dict:
        return await self._enqueue_kernel_signal(
            "HEARTFLOW_EVALUATE", self.handle_heartflow_signal, chat_id, snapshot, decision
        )

    async def enqueue_dream_signal(self, chat_id: str, snapshot, decision) -> dict:
        return await self._enqueue_kernel_signal(
            "DREAM_MAINTENANCE", self.handle_dream_signal, chat_id, snapshot, decision
        )

    async def enqueue_memory_signal(self, chat_id: str, snapshot, decision) -> dict:
        return await self._enqueue_kernel_signal(
            "MEMORY_MAINTENANCE", self.handle_memory_signal, chat_id, snapshot, decision
        )

    async def enqueue_compaction_signal(self, chat_id: str, snapshot, decision) -> dict:
        return await self._enqueue_kernel_signal(
            "COMPACTION_EVALUATE", self.handle_compaction_signal, chat_id, snapshot, decision
        )

    def _handle_task_result(self, task: asyncio.Task):
        self._background_tasks.discard(task)
        task_name = str(getattr(task, "_astrmai_task_name", "proactive") or "proactive")
        started_at = float(getattr(task, "_astrmai_started_at", 0.0) or 0.0)
        duration_ms = max(0.0, (time.time() - started_at) * 1000.0) if started_at else 0.0
        stats = self._background_task_stats.setdefault(
            task_name,
            {"completed": 0, "failed": 0, "cancelled": 0, "last_duration_ms": 0.0},
        )
        stats["last_duration_ms"] = duration_ms
        try:
            if task.cancelled():
                stats["cancelled"] = int(stats.get("cancelled", 0) or 0) + 1
                return
            exc = task.exception()
            if exc:
                stats["failed"] = int(stats.get("failed", 0) or 0) + 1
                if isinstance(exc, (BackgroundTaskQueueFull, BackgroundTaskQueueTimeout)):
                    if getattr(task, "_astrmai_task_name", "") == "proactive.profile":
                        self._profile_stat_inc("profile_budget_rejected")
                    budget = getattr(self, "background_task_budget", None)
                    budget_status = getattr(budget, "status", None)
                    draining = bool(
                        callable(budget_status)
                        and (budget_status() or {}).get("draining") is True
                    )
                    if draining:
                        logger.info(
                            f"[ProactiveTask] background task shutdown_rejected: {exc}"
                        )
                    else:
                        logger.warning(f"[ProactiveTask] background task skipped: {exc}")
                else:
                    logger.error(f"[Proactive Task Error] {exc}", exc_info=exc)
            else:
                stats["completed"] = int(stats.get("completed", 0) or 0) + 1
                logger.info(
                    f"[ProactiveTask] background job completed task={task_name} "
                    f"scope={getattr(task, '_astrmai_scope_id', '')} duration_ms={duration_ms:.1f}"
                )
        except asyncio.CancelledError:
            stats["cancelled"] = int(stats.get("cancelled", 0) or 0) + 1

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

    async def _save_user_profile(self, profile, *, revision: dict | None = None) -> None:
        try:
            await self.persistence.save_user_profile(profile)
        except TypeError:
            await self.persistence.save_user_profile(getattr(profile, "user_id", ""), profile)
        recorder = getattr(getattr(self, "_db_service", None), "record_user_profile_revision_async", None)
        if revision is not None and callable(recorder):
            try:
                payload = {
                    "user_id": str(getattr(profile, "user_id", "") or ""),
                    "source": "proactive_profile",
                    "summary": str(getattr(profile, "persona_analysis", "") or ""),
                    "tags": list(getattr(profile, "tags", []) or []),
                    "memory_points": list(getattr(profile, "memory_points", []) or []),
                    "created_at": time.time(),
                    **revision,
                }
                await recorder(payload)
                purger = getattr(getattr(self, "_db_service", None), "purge_user_profile_revisions_async", None)
                if callable(purger):
                    await purger(
                        user_id=payload["user_id"],
                        keep_latest=12,
                        max_age_days=180,
                        batch_size=100,
                    )
            except Exception as exc:
                logger.debug("[Life] profile revision persistence degraded: %s", exc)

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

    async def _generate_persona_analysis(self, profile) -> bool | None:
        profile_run_id = f"profile_{uuid.uuid4().hex[:20]}"
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
            result, model_id = await self._call_background_lane_with_metadata(
                "profile",
                str(getattr(profile, "user_id", profile.name)),
                envelope.prompt,
                system_prompt=envelope.system_prompt,
                template_envelope=envelope,
            )
        else:
            prompt = self.profile_generator.build_prompt(profile, summary)
            if prompt is None:  # ponytail: skip when no new messages
                return None
            result, model_id = await self._call_background_lane_with_metadata(
                "profile",
                str(getattr(profile, "user_id", profile.name)),
                prompt,
            )
        parsed = self.profile_generator.parse_result(result)
        if str(parsed.get("parse_status", "parsed") or "parsed") != "parsed":
            self._profile_stat_inc("profile_schema_invalid")
            logger.warning(
                "[Life] persona profiling output rejected: "
                f"status={parsed.get('parse_status', 'unknown')} user={getattr(profile, 'user_id', '')}"
            )
            return False
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
        await self._save_user_profile(
            profile,
            revision={
                "run_id": profile_run_id,
                "model_id": model_id,
                "changed_fields": ["persona_analysis", "tags", "memory_points"],
            },
        )
        logger.info(
            f"[Life] persona profiling completed for {getattr(profile, 'name', '')}: "
            f"tags={len(tags)} memory_points={len(memory_points)}"
        )
        return True

    async def _generate_nickname(self, profile) -> bool:
        if not profile or getattr(profile, "is_known", False):
            return False
        if hasattr(self.state_engine, "user_profile_service") and not self.state_engine.user_profile_service.can_auto_update_nickname(profile):
            return False
        summary = await self._load_persona_summary()
        if self.prompt_registry is not None:
            envelope = self.prompt_registry.render_template(
                PromptTemplateId.PROFILE_NICKNAME_GENERATION,
                self.nickname_generator.build_template_payload(profile, summary),
            )
            result, model_id = await self._call_background_lane_with_metadata(
                "profile",
                str(getattr(profile, "user_id", profile.name)),
                envelope.prompt,
                system_prompt=envelope.system_prompt,
                template_envelope=envelope,
            )
        else:
            prompt = self.nickname_generator.build_prompt(profile, summary)
            result, model_id = await self._call_background_lane_with_metadata(
                "profile",
                str(getattr(profile, "user_id", profile.name)),
                prompt,
            )
        nickname, reason = self.nickname_generator.parse_result(result)
        if str(getattr(self.nickname_generator, "last_parse_status", "parsed") or "parsed") != "parsed":
            self._profile_stat_inc("profile_schema_invalid")
            logger.warning(
                "[Life] nickname output rejected: "
                f"status={getattr(self.nickname_generator, 'last_parse_status', 'unknown')} "
                f"user={getattr(profile, 'user_id', '')}"
            )
            return False
        nickname = self.nickname_generator.choose(getattr(profile, "name", ""), preferred=nickname)
        if not nickname:
            return False
        if hasattr(self.state_engine, "user_profile_service"):
            applied = self.state_engine.user_profile_service.set_auto_nickname(profile, nickname, reason)
            if not applied:
                return False
        else:
            profile.nickname = nickname
            profile.nickname_reason = reason
            profile.is_known = True
            profile.is_dirty = True
        await self._save_user_profile(
            profile,
            revision={
                "run_id": f"profile_{uuid.uuid4().hex[:20]}",
                "model_id": model_id,
                "changed_fields": ["nickname", "nickname_reason", "is_known"],
            },
        )
        logger.info(f"[Life] nickname generated for {getattr(profile, 'name', '')}: {nickname}")
        return True

    async def _run_profiling_task(self):
        report = {
            "profile_count": 0,
            "nickname_count": 0,
            "llm_call_count": 0,
        }
        async with self._profile_semaphore:
            await self._retry_pending_profile_claim_releases()
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

            run_seen_user_ids: set[str] = set()
            for profile in active_profiles:
                user_id = str(getattr(profile, "user_id", getattr(profile, "name", "")) or "")
                if not user_id:
                    continue
                if user_id in active_user_ids or user_id in run_seen_user_ids:
                    self._profile_stat_inc("profile_duplicate_skipped")
                    continue
                run_seen_user_ids.add(user_id)
                now = time.time()
                metadata = self._profile_metadata(profile)
                last_profile_generated_at = max(
                    self._profile_timestamp(getattr(profile, "last_persona_gen_time", 0.0)),
                    self._profile_timestamp(metadata.get("last_nickname_gen_time", 0.0)),
                )
                analysis_due = int(getattr(profile, "message_count_for_profiling", 0) or 0) >= threshold
                if analysis_due:
                    if cooldown_sec > 0 and now - last_profile_generated_at < cooldown_sec:
                        analysis_due = False
                        self._profile_stat_inc("profile_cooldown_skipped")
                    elif self._profile_backoff_active(profile, "profile", now):
                        analysis_due = False

                nickname_candidate = (
                    int(getattr(profile, "know_times", 0) or 0) >= 3
                    and not getattr(profile, "is_known", False)
                )
                if nickname_candidate:
                    service = getattr(self.state_engine, "user_profile_service", None)
                    can_update = getattr(service, "can_auto_update_nickname", None)
                    if callable(can_update) and not can_update(profile):
                        nickname_candidate = False
                nickname_due = nickname_candidate
                if nickname_due:
                    if cooldown_sec > 0 and now - last_profile_generated_at < cooldown_sec:
                        nickname_due = False
                        self._profile_stat_inc("nickname_cooldown_skipped")
                    elif self._profile_backoff_active(profile, "nickname", now):
                        nickname_due = False

                if not analysis_due and not nickname_due:
                    continue
                try:
                    claim_token = await self._claim_profile_generation(user_id)
                except Exception as exc:
                    self._profile_stat_inc("profile_claim_degraded")
                    logger.warning("[Life] profile generation claim degraded for %s: %s", user_id, exc)
                    continue
                if not claim_token:
                    self._profile_stat_inc("profile_duplicate_skipped")
                    continue
                active_user_ids.add(user_id)
                self._profile_stat_inc("profile_generation_inflight")
                try:
                    if nickname_due:
                        try:
                            report["llm_call_count"] += 1
                            nickname_ok = await self._generate_nickname(profile)
                            if nickname_ok is False:
                                await self._record_profile_generation_failure(profile, "nickname")
                            elif nickname_ok:
                                report["nickname_count"] += 1
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            await self._record_profile_generation_failure(profile, "nickname", exc)
                            logger.error(f"[Life] nickname task degraded for {getattr(profile, 'name', '')}: {exc}")
                    if analysis_due:
                        try:
                            report["llm_call_count"] += 1
                            analysis_ok = await self._generate_persona_analysis(profile)
                            if analysis_ok is False:
                                await self._record_profile_generation_failure(profile, "profile")
                            elif analysis_ok:
                                report["profile_count"] += 1
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            await self._record_profile_generation_failure(profile, "profile", exc)
                            logger.error(f"[Life] profiling task degraded for {getattr(profile, 'name', '')}: {exc}")
                finally:
                    try:
                        await asyncio.shield(self._release_profile_generation(user_id, claim_token))
                    except BaseException as exc:
                        logger.warning("[Life] profile generation claim cleanup degraded for %s: %s", user_id, exc)
                    finally:
                        active_user_ids.discard(user_id)
                        self._profile_stat_inc("profile_generation_inflight", -1)
        return report

    async def _run_reflection_tasks(self):
        enable_exp_mine = getattr(self.config.evolution, "enable_expression_mining", True) if hasattr(self.config, "evolution") else True
        runner = getattr(self, "expression_governance_runner", None)
        if runner is None or not enable_exp_mine:
            return
        await runner.run_once()

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
        now_ts = time.time()
        proactive_interval = max(
            300.0,
            float(getattr(getattr(self.config, "life", None), "proactive_scan_interval_sec", 3600) or 3600),
        )
        proactive_scan_lease = None
        proactive_scan_due = now_ts - self._last_proactive_scan >= proactive_interval
        if self._task_ledger is not None:
            try:
                proactive_scan_lease = await self._task_ledger.claim(
                    task_family="proactive_scan",
                    scope_id="__global__",
                    input_fingerprint="",
                    lease_seconds=300.0,
                    min_interval_seconds=proactive_interval,
                    checkpoint_before={
                        "last_scan_at": self._last_proactive_scan,
                    },
                    payload={"interval_seconds": proactive_interval},
                )
                proactive_scan_due = proactive_scan_lease is not None
            except Exception as exc:
                logger.debug(
                    f"[ProactiveTask] proactive scan ledger degraded: {exc}"
                )
        if proactive_scan_due:
            self._last_proactive_scan = now_ts
            await self._persist_profile_scheduler_state()
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
        if proactive_scan_due and self._persistent_due_scan_enabled and callable(due_loader):
            try:
                persistent_due_chat_ids = list(
                    await due_loader(now=time.time(), limit=self.HEARTBEAT_MAX_BATCH * 4) or []
                )
            except Exception as exc:
                self._persistent_due_scan_degraded_reason = exc.__class__.__name__
                logger.warning(f"[ProactiveTask] persisted proactive due scan degraded: {exc}")
        elif proactive_scan_due and self._persistent_due_scan_enabled:
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
                    tick_kwargs = {
                        "chat_id": str(chat_id or ""),
                        "trigger": "heartbeat",
                    }
                    # Keep compatibility with host/test kernels predating the
                    # optional proactive-signal suppression flag.
                    try:
                        supports_proactive_flag = "include_proactive" in inspect.signature(self.chat_loop_kernel.tick).parameters
                    except (TypeError, ValueError):
                        # Unknown signatures are treated as legacy kernels so
                        # a compatibility adapter is never broken by a new kwarg.
                        supports_proactive_flag = False
                    if supports_proactive_flag:
                        tick_kwargs["include_proactive"] = proactive_scan_due
                    try:
                        supports_maintenance_flag = "include_maintenance" in inspect.signature(self.chat_loop_kernel.tick).parameters
                    except (TypeError, ValueError):
                        supports_maintenance_flag = False
                    if supports_maintenance_flag:
                        # Heartbeats only inspect maintenance state on the
                        # configured proactive scan cadence; they never run
                        # Memory/Dream/Compaction on every poll tick.
                        tick_kwargs["include_maintenance"] = proactive_scan_due
                    tick = await self.chat_loop_kernel.tick(**tick_kwargs)
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
        if proactive_scan_lease is not None:
            await self._task_ledger.finish(
                proactive_scan_lease,
                status="succeeded",
                checkpoint_after={
                    "active_candidates": self._last_active_candidate_count,
                    "persistent_due_candidates": self._last_persistent_due_candidate_count,
                    "selected": len(due_chat_ids),
                    "completed_at": time.time(),
                },
                llm_call_count=0,
            )
        return results

    async def _run_maintenance_cycle(self) -> None:
        try:
            await self._enqueue_managed_maintenance(
                task_family="decay",
                scope_id="global",
                awaitable_factory=self.decay_service.run_once,
            )
        except Exception as exc:
            logger.error(f"[ProactiveTask] decay maintenance degraded: {exc}")
        try:
            await self._enqueue_managed_maintenance(
                task_family="memory_maintenance",
                scope_id="global",
                awaitable_factory=self._run_memory_store_maintenance,
            )
        except Exception as exc:
            logger.error(f"[ProactiveTask] memory store maintenance degraded: {exc}")
        try:
            await self._enqueue_managed_maintenance(
                task_family="group_signin",
                scope_id="global",
                awaitable_factory=self.group_signin_service.run_once,
            )
        except Exception as exc:
            logger.error(f"[ProactiveTask] group signin maintenance degraded: {exc}")
        try:
            await self._enqueue_managed_maintenance(
                task_family="heartflow_digest",
                scope_id="global",
                awaitable_factory=lambda: self.heartflow_topic_digest_service.run_once(
                    self.heartflow_manager
                ),
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

                profile_interval = max(
                    300.0,
                    float(getattr(getattr(self.config, "life", None), "profile_scan_interval_sec", 7200) or 7200),
                )
                profile_scan_due = now - self._last_profile_run >= profile_interval
                profile_scan_lease = None
                if profile_scan_due and self._task_ledger is not None:
                    try:
                        profile_scan_lease = await self._task_ledger.claim(
                            task_family="profile_scan",
                            scope_id="__global__",
                            input_fingerprint="",
                            lease_seconds=1800.0,
                            min_interval_seconds=profile_interval,
                            checkpoint_before={
                                "last_scan_at": self._last_profile_run,
                            },
                            payload={"interval_seconds": profile_interval},
                        )
                        profile_scan_due = profile_scan_lease is not None
                    except Exception as exc:
                        logger.debug(
                            f"[ProactiveTask] profile scan ledger degraded: {exc}"
                        )
                if profile_scan_due:
                    self._last_profile_run = now
                    await self._persist_profile_scheduler_state()
                    self._fire_background_task(
                        self._run_profiling_task,
                        task_name="proactive.profile",
                        task_lease=profile_scan_lease,
                        checkpoint_after=lambda result: dict(result or {}),
                        llm_call_count=lambda result: int(
                            dict(result or {}).get("llm_call_count", 0) or 0
                        ),
                    )

                if run_maintenance and self.diary_service.should_run(self._last_diary_date, now):
                    diary_date = time.strftime("%Y-%m-%d", time.localtime(now))
                    if self._diary_pending_date != diary_date:
                        diary_dispatch = await self._enqueue_managed_maintenance(
                            task_family="diary",
                            scope_id="global",
                            awaitable_factory=lambda: self._run_daily_diary_task_with_jitter(
                                diary_date
                            ),
                        )
                        if diary_dispatch.get("queued"):
                            self._diary_pending_date = diary_date
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
            "background_task_stats": {
                key: dict(value) for key, value in self._background_task_stats.items()
            },
            "profiling_active_users": len(getattr(self, "_profiling_user_ids", set())),
            "profile_cooldown_skipped": int(getattr(self, "_profiling_stats", {}).get("profile_cooldown_skipped", 0) or 0),
            "profile_duplicate_skipped": int(getattr(self, "_profiling_stats", {}).get("profile_duplicate_skipped", 0) or 0),
            "nickname_cooldown_skipped": int(getattr(self, "_profiling_stats", {}).get("nickname_cooldown_skipped", 0) or 0),
            "profile_generation_failed": int(getattr(self, "_profiling_stats", {}).get("profile_generation_failed", 0) or 0),
            "profile_generation_inflight": int(getattr(self, "_profiling_stats", {}).get("profile_generation_inflight", 0) or 0),
            "profile_generation_requests": int(getattr(self, "_profiling_stats", {}).get("profile_generation_requests", 0) or 0),
            "profile_budget_rejected": int(getattr(self, "_profiling_stats", {}).get("profile_budget_rejected", 0) or 0),
            "profile_claim_degraded": int(getattr(self, "_profiling_stats", {}).get("profile_claim_degraded", 0) or 0),
            "profile_claim_release_failed": int(getattr(self, "_profiling_stats", {}).get("profile_claim_release_failed", 0) or 0),
            "profile_claim_release_pending": len(getattr(self, "_profile_claim_release_pending_keys", set()) or set()),
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
            "profile_scan_interval_seconds": float(
                getattr(getattr(self.config, "life", None), "profile_scan_interval_sec", 7200) or 7200
            ),
            "proactive_scan_interval_seconds": float(
                getattr(getattr(self.config, "life", None), "proactive_scan_interval_sec", 3600) or 3600
            ),
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
