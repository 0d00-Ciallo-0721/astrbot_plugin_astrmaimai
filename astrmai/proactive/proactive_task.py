from __future__ import annotations

import asyncio
import random
import time

from astrbot.api import logger

from ..infrastructure.runtime.lane_manager import LaneKey
from ..learning.profiling.nickname_generator import NicknameGenerator
from ..learning.profiling.profile_generator import ProfileGenerator
from ..memory.dream.dream_agent import DreamAgent
from ..memory.dream.dream_generator import DreamGenerator
from .decay_service import DecayService
from .diary_service import DiaryService
from .dream_scheduler import DreamScheduler
from .dispatcher import ProactiveDispatcher
from .heartflow import HeartflowManager, HeartflowTopicDigestService
from .review_dispatcher import ReviewDispatcher
from .wakeup_service import WakeupService


class ProactiveTask:
    """Refactoring-side lifecycle scheduler that delegates concrete jobs to subservices."""

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
    ):
        self.context = context
        self.state_engine = state_engine
        self.gateway = gateway
        self.persistence = persistence
        self.memory_engine = memory_engine
        self.reflector = reflector
        self.auto_check_task = None
        self.reflect_tracker = None
        self.config = config if config else gateway.config
        self._is_running = False
        self._task = None
        self._background_tasks: set[asyncio.Task] = set()
        self._bg_semaphore = asyncio.Semaphore(2)
        self._last_profile_run = 0.0
        self._last_diary_date = ""
        self._db_service = None
        self.profile_generator = ProfileGenerator()
        self.nickname_generator = NicknameGenerator()
        self.proactive_dispatcher = ProactiveDispatcher(
            attention_gate=attention_gate,
            runtime_coordinator=runtime_coordinator,
            state_engine=state_engine,
            config=self.config,
        )

        self.wakeup_service = WakeupService(
            context=context,
            state_engine=state_engine,
            persistence=persistence,
            call_background_lane=self._call_background_lane,
            config=self.config,
            dispatcher=self.proactive_dispatcher,
        )
        self.decay_service = DecayService(state_engine, memory_engine, self.config)
        self.diary_service = DiaryService(
            persistence=persistence,
            memory_engine=memory_engine,
            config=self.config,
            call_background_lane=self._call_background_lane,
            semaphore=self._bg_semaphore,
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
        )
        self.heartflow_topic_digest_service = HeartflowTopicDigestService(
            memory_engine=memory_engine,
            semaphore=self._bg_semaphore,
        )
        self.dream_generator = DreamGenerator(gateway, config=self.config)
        self.dream_agent = None

    async def _call_background_lane(self, task_family: str, scope_id: str, prompt: str, system_prompt: str = "") -> str:
        return await self.gateway.call_proactive_task(
            prompt=prompt,
            system_prompt=system_prompt,
            lane_key=LaneKey(subsystem="bg", task_family=task_family, scope_id=scope_id or "global", scope_kind="global"),
            base_origin="",
            persona_id=getattr(self.config.persona, "persona_id", "") or "global",
        )

    async def start(self):
        if self._is_running:
            return
        self._is_running = True
        if self.dream_agent is None and self._db_service:
            self._bind_dream_dependencies()
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._is_running = False
        if self._task:
            self._task.cancel()

    def set_db_service(self, db_service):
        self._db_service = db_service
        if self._is_running and self.dream_agent is None:
            self._bind_dream_dependencies()

    def _bind_dream_dependencies(self):
        self.dream_agent = DreamAgent(
            gateway=self.gateway,
            db_service=self._db_service,
            memory_engine=self.memory_engine,
            config=self.config,
        )
        self.dream_scheduler.bind_dependencies(self.dream_agent, self.dream_generator, db_service=self._db_service)

    def _fire_background_task(self, coro):
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_task_result)

    def _handle_task_result(self, task: asyncio.Task):
        self._background_tasks.discard(task)
        try:
            exc = task.exception()
            if exc:
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

    async def _generate_persona_analysis(self, profile) -> None:
        summary = await self._load_persona_summary()
        prompt = self.profile_generator.build_prompt(profile, summary)
        result = await self._call_background_lane("profile", str(getattr(profile, "user_id", profile.name)), prompt)
        parsed = self.profile_generator.parse_result(result)
        analysis = parsed["analysis"]
        tags = parsed["tags"]
        memory_points = parsed["memory_points"]

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
        summary = await self._load_persona_summary()
        prompt = self.nickname_generator.build_prompt(profile, summary)
        result = await self._call_background_lane("profile", str(getattr(profile, "user_id", profile.name)), prompt)
        nickname, reason = self.nickname_generator.parse_result(result)
        nickname = self.nickname_generator.choose(getattr(profile, "name", ""), preferred=nickname)
        if not nickname:
            return
        profile.nickname = nickname
        profile.nickname_reason = reason
        profile.is_known = True
        profile.is_dirty = True
        await self._save_user_profile(profile)
        logger.info(f"[Life] nickname generated for {getattr(profile, 'name', '')}: {nickname}")

    async def _run_profiling_task(self):
        async with self._bg_semaphore:
            active_profiles = self.state_engine.get_active_profiles()
            threshold = int(getattr(getattr(self.config, "life", None), "profiling_msg_threshold", 200) or 200)

            for profile in active_profiles:
                profile.know_times = int(getattr(profile, "know_times", 0) or 0) + 1
                if profile.know_times >= 3 and not getattr(profile, "is_known", False):
                    try:
                        await self._generate_nickname(profile)
                    except Exception as exc:
                        logger.error(f"[Life] nickname task degraded for {getattr(profile, 'name', '')}: {exc}")

                if int(getattr(profile, "message_count_for_profiling", 0) or 0) >= threshold:
                    try:
                        await self._generate_persona_analysis(profile)
                    except Exception as exc:
                        logger.error(f"[Life] profiling task degraded for {getattr(profile, 'name', '')}: {exc}")

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

    async def _run_daily_diary_task_with_jitter(self):
        await asyncio.sleep(random.randint(1, 300))
        await self.diary_service.run_once(self.state_engine.get_active_states())

    async def _loop(self):
        while self._is_running:
            try:
                await asyncio.sleep(60)
                await self.decay_service.run_once()
                await self.wakeup_service.run_once()
                await self.heartflow_manager.tick()
                self._fire_background_task(self.heartflow_topic_digest_service.run_once(self.heartflow_manager))

                now = time.time()
                if now - self._last_profile_run > 3600:
                    await self._run_profiling_task()
                    self._last_profile_run = now

                await self._run_reflection_tasks()

                if self.dream_scheduler.should_run(now):
                    self._fire_background_task(self.dream_scheduler.run_once())

                if self.diary_service.should_run(self._last_diary_date, now):
                    self._last_diary_date = time.strftime("%Y-%m-%d", time.localtime(now))
                    self._fire_background_task(self._run_daily_diary_task_with_jitter())
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[ProactiveTask] scheduler loop degraded: {exc}")
                await asyncio.sleep(60)

    def describe_status(self) -> dict:
        return {
            "running": self._is_running,
            "dream_ready": self.dream_agent is not None,
            "last_profile_run": self._last_profile_run,
            "last_diary_date": self._last_diary_date,
            "background_tasks": len(self._background_tasks),
            "dream_scheduler": self.dream_scheduler.describe_status(),
            "heartflow": self.heartflow_manager.describe_status(),
            "heartflow_topic_digest": self.heartflow_topic_digest_service.describe_status(),
            "proactive_dispatcher": self.proactive_dispatcher.describe_status(),
            "review_dispatcher_ready": self.review_dispatcher.reflect_tracker is not None,
        }


__all__ = ["ProactiveTask"]
