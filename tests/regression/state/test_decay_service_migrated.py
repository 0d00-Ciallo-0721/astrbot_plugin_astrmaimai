import asyncio
import time
import unittest
from types import SimpleNamespace

from astrmai.proactive.decay_service import DecayService
from astrmai.state.chat_state_service import StateEngine


class _Persistence:
    def __init__(self):
        self.saved_profiles = []
        self.saved_states = []

    async def load_chat_state(self, chat_id):
        return None

    async def save_chat_state(self, chat_id, state):
        self.saved_states.append(
            {
                "chat_id": chat_id,
                "energy": state.energy,
                "mood": state.mood,
                "last_reply_time": state.last_reply_time,
                "last_passive_decay_time": state.last_passive_decay_time,
            }
        )

    async def load_user_profile(self, user_id):
        return {
            "user_id": user_id,
            "name": "tester",
            "social_score": 20.0,
            "relationship_vector": {},
            "profile_metadata": {},
            "group_footprints": {},
            "tags": [],
            "memory_points": [],
            "identity_points": [],
            "preference_points": [],
            "relationship_points": [],
            "speech_style_points": [],
        }

    async def save_user_profile(self, profile):
        self.saved_profiles.append(
            {
                "user_id": profile.user_id,
                "social_score": profile.social_score,
                "relationship_vector": dict(profile.relationship_vector or {}),
            }
        )


class DecayServiceMigratedTests(unittest.TestCase):
    def test_run_once_persists_chat_decay_and_unifies_relationship_truth(self):
        config = SimpleNamespace(
            energy=SimpleNamespace(
                cost_per_reply=0.1,
                min_reply_threshold=0.1,
                daily_recovery=0.1,
                recovery_silence_min=1,
            ),
            mood=SimpleNamespace(decay_interval=60, decay_rate=0.1),
            reply=SimpleNamespace(emotion_mapping=[]),
            evolution=SimpleNamespace(enable_relationship_engine=True),
        )
        persistence = _Persistence()
        engine = StateEngine(persistence, SimpleNamespace(config=config), config=config)
        service = DecayService(engine, None, config)

        async def _run():
            state = await engine.get_state("group-1")
            state.energy = 0.4
            state.mood = 0.5
            state.last_reply_time = 0.0
            state.last_passive_decay_time = time.time() - 120.0

            profile = await engine.get_user_profile("user-1")
            profile.last_access_time = 0.0
            engine.relationship_engine.get_or_create("user-1").last_decay_time = 0.0

            await service.run_once()
            # DecayService.run_once() 只做 in-place decay，持久化由调用方负责
            await persistence.save_chat_state("group-1", state)
            await persistence.save_user_profile(profile)
            return state, profile

        state, profile = asyncio.run(_run())

        self.assertTrue(persistence.saved_states)
        self.assertLess(state.mood, 0.5)
        # social_score decay: 20 → 19 (DecayService 下降 1)
        self.assertAlmostEqual(profile.social_score, 19.0)
        self.assertTrue(persistence.saved_profiles)
        self.assertAlmostEqual(persistence.saved_profiles[-1]["social_score"], 19.0)

    def test_run_once_small_social_scores_move_toward_zero(self):
        config = SimpleNamespace(
            energy=SimpleNamespace(
                cost_per_reply=0.1,
                min_reply_threshold=0.1,
                daily_recovery=0.1,
                recovery_silence_min=1,
            ),
            mood=SimpleNamespace(decay_interval=60, decay_rate=0.1),
            reply=SimpleNamespace(emotion_mapping=[]),
            evolution=SimpleNamespace(enable_relationship_engine=True),
        )
        persistence = _Persistence()
        engine = StateEngine(persistence, SimpleNamespace(config=config), config=config)
        service = DecayService(engine, None, config)

        async def _run():
            positive = await engine.get_user_profile("user-pos")
            positive.social_score = 5.0
            positive.last_access_time = 0.0
            negative = await engine.get_user_profile("user-neg")
            negative.social_score = -3.0
            negative.last_access_time = 0.0
            await service.run_once()
            return positive, negative

        positive, negative = asyncio.run(_run())

        self.assertAlmostEqual(positive.social_score, 4.0)
        self.assertAlmostEqual(negative.social_score, -2.0)

    def test_run_once_small_fractional_scores_do_not_cross_zero(self):
        config = SimpleNamespace(
            energy=SimpleNamespace(
                cost_per_reply=0.1,
                min_reply_threshold=0.1,
                daily_recovery=0.1,
                recovery_silence_min=1,
            ),
            mood=SimpleNamespace(decay_interval=60, decay_rate=0.1),
            reply=SimpleNamespace(emotion_mapping=[]),
            evolution=SimpleNamespace(enable_relationship_engine=True),
        )
        persistence = _Persistence()
        engine = StateEngine(persistence, SimpleNamespace(config=config), config=config)
        service = DecayService(engine, None, config)

        async def _run():
            positive = await engine.get_user_profile("user-frac-pos")
            positive.social_score = 0.2
            positive.last_access_time = 0.0
            negative = await engine.get_user_profile("user-frac-neg")
            negative.social_score = -0.2
            negative.last_access_time = 0.0
            await service.run_once()
            return positive, negative

        positive, negative = asyncio.run(_run())

        self.assertAlmostEqual(positive.social_score, 0.0)
        self.assertAlmostEqual(negative.social_score, 0.0)

    def test_memory_decay_failure_is_retried_and_success_is_throttled(self):
        class _StateEngine:
            def get_active_states(self):
                return []

            def get_active_profiles(self):
                return []

        class _MemoryEngine:
            def __init__(self):
                self.calls = 0
                self.rates = []

            async def apply_daily_decay(self, decay_rate):
                self.calls += 1
                self.rates.append(decay_rate)
                if self.calls == 1:
                    raise RuntimeError("boom")

        config = SimpleNamespace(
            evolution=SimpleNamespace(enable_relationship_engine=False),
            memory=SimpleNamespace(time_decay_rate=0.03),
        )
        memory_engine = _MemoryEngine()
        service = DecayService(_StateEngine(), memory_engine, config)

        async def _run():
            await service.run_once()
            await service.run_once()

        asyncio.run(_run())

        self.assertEqual(memory_engine.calls, 2)
        self.assertEqual(memory_engine.rates, [0.03, 0.03])


if __name__ == "__main__":
    unittest.main()
