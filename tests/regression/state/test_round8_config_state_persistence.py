from __future__ import annotations

import asyncio
import math
import time
import unittest
from types import SimpleNamespace

from config import AstrMaiConfig


class _Persistence:
    def __init__(self):
        self.saved_states = []
        self.saved_profiles = []

    async def load_chat_state(self, chat_id):
        return None

    async def save_chat_state(self, chat_id, state):
        self.saved_states.append(
            {
                "chat_id": chat_id,
                "energy": state.energy,
                "mood": state.mood,
                "total_replies": state.total_replies,
                "next_wakeup_timestamp": state.next_wakeup_timestamp,
            }
        )

    async def load_user_profile(self, user_id):
        return None

    async def save_user_profile(self, profile):
        self.saved_profiles.append(
            {
                "user_id": profile.user_id,
                "social_score": profile.social_score,
                "last_seen": profile.last_seen,
                "relationship_vector": dict(profile.relationship_vector or {}),
            }
        )


class _RefreshRecorder:
    def __init__(self):
        self.config = None
        self.seen = []

    def refresh_config(self, config):
        self.config = config
        self.seen.append(config)


class Round8ConfigStatePersistenceTests(unittest.TestCase):
    def test_memory_engine_refreshes_all_owned_runtime_children(self):
        from astrmai.memory.services.memory_engine import MemoryEngine

        old_config = AstrMaiConfig(memory={"summary_threshold": 10})
        new_config = AstrMaiConfig(memory={"summary_threshold": 41})
        engine = MemoryEngine.__new__(MemoryEngine)
        engine.config = old_config
        engine.embedding_models = []
        children = {}
        for name in (
            "injection_service",
            "retrieval_service",
            "write_service",
            "retriever",
            "session_summarizer",
            "instant_gate",
            "memory_pipeline",
            "maintenance_service",
            "tool_service",
        ):
            children[name] = _RefreshRecorder()
            setattr(engine, name, children[name])

        def _refresh_pipeline(config):
            children["memory_pipeline"].config = config
            children["memory_pipeline"].seen.append(config)
            children["session_summarizer"].refresh_config(config)
            children["instant_gate"].refresh_config(config)

        children["memory_pipeline"].refresh_config = _refresh_pipeline

        engine.refresh_config(new_config)

        self.assertIs(engine.config, new_config)
        for child in children.values():
            self.assertIs(child.config, new_config)

    def test_session_summarizer_and_maintenance_rebuild_derived_config(self):
        from astrmai.memory.services.memory_maintenance_service import MemoryMaintenanceService
        from astrmai.memory.services.session_memory_summarizer import SessionMemorySummarizer

        old_config = AstrMaiConfig(memory={"summary_threshold": 10, "cleanup_interval": 60})
        new_config = AstrMaiConfig(
            memory={
                "summary_threshold": 44,
                "cleanup_interval": 99,
                "maintenance_temporal_stale_hot_threshold": 0.73,
            }
        )
        summarizer = SessionMemorySummarizer.__new__(SessionMemorySummarizer)
        summarizer.topic_summarizer = SimpleNamespace(config=old_config)
        summarizer.refresh_config(new_config)
        maintenance = MemoryMaintenanceService(SimpleNamespace(), config=old_config)
        maintenance.refresh_config(new_config)

        self.assertEqual(summarizer.msg_threshold, 44)
        self.assertEqual(summarizer.check_interval, 99)
        self.assertIs(summarizer.topic_summarizer.config, new_config)
        self.assertAlmostEqual(maintenance.scoring.maintenance_temporal_stale_hot_threshold, 0.73)

    def test_lane_and_state_hot_refresh_rebuild_derived_values_without_resetting_state(self):
        from astrmai.infrastructure.runtime.lane_manager import LaneManager
        from astrmai.state.chat_state_service import StateEngine
        from astrmai.state.private_chat.private_chat_manager import PrivateChatManager

        old_config = AstrMaiConfig(
            system1={"nicknames": ["old"]},
            global_settings={"debug_mode": False},
            private_chat={"wait_timeout_sec": 30},
            reply={"emotion_mapping": ["happy:old happy"]},
        )
        new_config = AstrMaiConfig(
            system1={"nicknames": ["new", "mai"]},
            global_settings={"debug_mode": True},
            private_chat={"wait_timeout_sec": 77},
            reply={"emotion_mapping": ["happy:new happy"]},
        )
        lane = LaneManager(SimpleNamespace(), old_config)
        lane.refresh_config(new_config)
        persistence = _Persistence()
        engine = StateEngine(persistence, SimpleNamespace(config=old_config), old_config)
        private = PrivateChatManager(old_config)

        async def _run():
            state = await engine.get_state("group-1")
            private_session = private._get_or_create_session("user-1", "chat-1")
            engine.refresh_config(new_config)
            private.refresh_config(new_config)
            return state, private_session

        state, private_session = asyncio.run(_run())

        self.assertEqual(lane.settings.nicknames, ("new", "mai"))
        self.assertTrue(lane.settings.debug_mode)
        self.assertIs(engine.chat_state_service.config, new_config)
        self.assertEqual(engine.mood_manager.emotion_mapping["happy"], "new happy")
        self.assertIs(engine.chat_states["group-1"], state)
        self.assertEqual(private.timeout_sec, 77)
        self.assertIs(private._get_or_create_session("user-1", "chat-1"), private_session)

    def test_profile_activity_flush_uses_one_argument_contract_and_clears_dirty(self):
        from astrmai.state.user_profile_service import UserProfileService

        persistence = _Persistence()
        service = UserProfileService(persistence)

        async def _run():
            await service.observe_user_activity(
                "user-1",
                chat_id="group-1",
                sender_name="Alice",
                content="hello",
            )
            return await service.get_user_profile("user-1")

        profile = asyncio.run(_run())

        self.assertEqual(len(persistence.saved_profiles), 1)
        self.assertEqual(persistence.saved_profiles[0]["user_id"], "user-1")
        self.assertFalse(profile.is_dirty)

    def test_wakeup_settlement_persists_energy_reply_metadata_and_cooldown_once(self):
        from astrmai.state.chat_state_service import StateEngine

        config = AstrMaiConfig()
        persistence = _Persistence()
        engine = StateEngine(persistence, SimpleNamespace(config=config), config)

        async def _run():
            state = await engine.get_state("group-1")
            state.energy = 0.8
            await engine.settle_proactive_wakeup(
                "group-1",
                amount=0.2,
                next_wakeup_timestamp=12345.0,
            )
            return state

        state = asyncio.run(_run())

        self.assertEqual(len(persistence.saved_states), 1)
        self.assertAlmostEqual(state.energy, 0.6)
        self.assertEqual(state.total_replies, 1)
        self.assertEqual(state.next_wakeup_timestamp, 12345.0)
        self.assertEqual(persistence.saved_states[0]["next_wakeup_timestamp"], 12345.0)

    def test_wakeup_failed_send_does_not_settle_energy_or_cooldown(self):
        from astrmai.proactive.wakeup_service import WakeupService

        state = SimpleNamespace(
            chat_id="group-1",
            energy=0.8,
            last_reply_time=0.0,
            next_wakeup_timestamp=0.0,
        )
        settlements = []

        class _Dispatcher:
            async def dispatch(self, intent, *, on_complete=None):
                await on_complete(False, "")
                return SimpleNamespace(allowed=True, blocked_reason="", intent_id="test")

        async def _settle(*args, **kwargs):
            settlements.append((args, kwargs))

        config = AstrMaiConfig()
        service = WakeupService(
            context=SimpleNamespace(),
            state_engine=SimpleNamespace(settle_proactive_wakeup=_settle),
            persistence=_Persistence(),
            call_background_lane=None,
            config=config,
            dispatcher=_Dispatcher(),
        )
        service.build_wakeup_intent = lambda *args, **kwargs: asyncio.sleep(
            0,
            result=SimpleNamespace(),
        )

        result = asyncio.run(
            service.run_for_chat(
                "group-1",
                signal={
                    "eligible": True,
                    "state": state,
                    "wakeup_cost": 0.2,
                    "wakeup_cooldown": 60.0,
                },
            )
        )

        self.assertTrue(result["performed"])
        self.assertEqual(settlements, [])
        self.assertEqual(state.energy, 0.8)
        self.assertEqual(state.next_wakeup_timestamp, 0.0)

    def test_event_bus_stop_discards_old_queue_and_subscriber_generation(self):
        from astrmai.infrastructure.runtime.event_bus import EventBus

        async def _run():
            bus = EventBus()
            bus._init_bus()
            old_seen = []
            new_seen = []
            bus.subscribe("topic", lambda payload: old_seen.append(payload))
            bus._event_queue.put_nowait((bus._generation, "topic", {"old": True}))
            bus.trigger_abort()
            bus.response_sent.set()
            await bus.stop()
            shutdown_fence_held = bus.abort_signal.is_set() and not bus.response_sent.is_set()
            bus.reset_abort()
            bus.subscribe("topic", lambda payload: new_seen.append(payload))
            await bus.publish("topic", {"new": True})
            await asyncio.sleep(0.05)
            await bus.stop()
            return old_seen, new_seen, shutdown_fence_held

        old_seen, new_seen, shutdown_fence_held = asyncio.run(_run())
        self.assertEqual(old_seen, [])
        self.assertEqual(new_seen, [{"new": True}])
        self.assertTrue(shutdown_fence_held)

    def test_group_clear_keeps_lock_identity_and_invalidates_inflight_mood_write(self):
        from astrmai.state.chat_state_service import StateEngine

        config = AstrMaiConfig()
        persistence = _Persistence()
        engine = StateEngine(persistence, SimpleNamespace(config=config), config)
        analysis_started = asyncio.Event()
        release_analysis = asyncio.Event()

        async def _analyze(*args, **kwargs):
            analysis_started.set()
            await release_analysis.wait()
            return "happy", 0.9

        engine.mood_manager.analyze_mood = _analyze

        async def _run():
            lock = engine.chat_state_service._get_chat_lock("group-1")
            task = asyncio.create_task(engine.update_mood("group-1", "hello"))
            await analysis_started.wait()
            await engine.clear_chat_state("group-1")
            same_lock = engine.chat_state_service._get_chat_lock("group-1") is lock
            release_analysis.set()
            await task
            return same_lock

        same_lock = asyncio.run(_run())

        self.assertTrue(same_lock)
        self.assertNotIn("group-1", engine.chat_states)
        self.assertEqual(persistence.saved_states, [])

    def test_non_finite_mood_results_do_not_change_or_persist_state(self):
        from astrmai.state.chat_state_service import StateEngine
        from astrmai.state.mood.mood_manager import MoodManager

        normalized = MoodManager._normalize_result(
            MoodManager.__new__(MoodManager),
            {"mood_tag": "happy", "mood_value": math.nan},
            math.nan,
        )
        self.assertEqual(normalized, ("happy", 0.0))

        for invalid in (math.nan, math.inf, -math.inf):
            with self.subTest(invalid=invalid):
                config = AstrMaiConfig()
                persistence = _Persistence()
                engine = StateEngine(persistence, SimpleNamespace(config=config), config)

                async def _analyze(*args, **kwargs):
                    return "happy", invalid

                engine.mood_manager.analyze_mood = _analyze
                tag, mood = asyncio.run(engine.update_mood("group-1", "hello"))

                self.assertEqual(tag, "happy")
                self.assertEqual(mood, 0.0)
                self.assertEqual(engine.chat_states["group-1"].mood, 0.0)
                self.assertEqual(persistence.saved_states, [])

    def test_relationship_decay_runs_once_and_persists_matching_vector_without_touching_last_seen(self):
        from astrmai.proactive.decay_service import DecayService
        from astrmai.state.chat_state_service import StateEngine

        config = AstrMaiConfig()
        persistence = _Persistence()
        engine = StateEngine(persistence, SimpleNamespace(config=config), config)
        service = DecayService(engine, None, config)

        async def _run():
            profile = await engine.get_user_profile("user-1")
            profile.social_score = 20.0
            profile.last_access_time = 0.0
            original_last_seen = profile.last_seen
            engine.relationship_engine.align_social_score("user-1", 20.0)
            engine.relationship_engine.apply_global_decay = lambda: (_ for _ in ()).throw(
                AssertionError("global decay must not run in the same maintenance cycle")
            )
            await service.run_once()
            first_score = profile.social_score
            await service.run_once()
            return profile, original_last_seen, first_score

        profile, original_last_seen, first_score = asyncio.run(_run())

        self.assertEqual(first_score, 19.0)
        self.assertEqual(profile.social_score, 19.0)
        self.assertEqual(profile.last_seen, original_last_seen)
        self.assertEqual(engine.relationship_engine.get_social_score("user-1"), 19.0)
        self.assertEqual(persistence.saved_profiles[-1]["social_score"], 19.0)
        self.assertEqual(persistence.saved_profiles[-1]["relationship_vector"]["social_score"], 19.0)


if __name__ == "__main__":
    unittest.main()
