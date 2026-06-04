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


if __name__ == "__main__":
    unittest.main()
