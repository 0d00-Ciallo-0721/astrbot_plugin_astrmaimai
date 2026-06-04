import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrmai.infrastructure.persistence.orm_models import UserProfile
from astrmai.infrastructure.persistence.persistence_schema import PersistenceSchemaMixin
from astrmai.infrastructure.persistence.state_profile_persistence import StateProfilePersistenceMixin
from astrmai.state.chat_state_service import StateEngine
from astrmai.state.relationship.relationship_engine import RelationshipEvent


class _ProfilePersistence:
    def __init__(self):
        self.saved_profiles = []

    async def load_chat_state(self, chat_id):
        return None

    async def save_chat_state(self, chat_id, state):
        return None

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
        self.saved_profiles.append(profile)


class _SqliteProfilePersistence(PersistenceSchemaMixin, StateProfilePersistenceMixin):
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db_sync()


class RelationshipProfileRoundtripMigratedTests(unittest.TestCase):
    def test_get_user_profile_does_not_rebuild_runtime_vector_from_social_score(self):
        config = SimpleNamespace(
            energy=SimpleNamespace(cost_per_reply=0.1, min_reply_threshold=0.1, daily_recovery=0.1, recovery_silence_min=60),
            mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
            reply=SimpleNamespace(emotion_mapping=[]),
        )
        engine = StateEngine(_ProfilePersistence(), SimpleNamespace(config=config), config=config)

        async def _run():
            first_profile = await engine.get_user_profile("user-1")
            first_trust = first_profile.relationship_vector["trust"]
            initial_score = engine.relationship_engine.get_social_score("user-1")
            updated_score = engine.relationship_engine.process_event(
                "user-1",
                RelationshipEvent.HELPFUL_REPLY,
                intensity=1.0,
            )
            second_profile = await engine.get_user_profile("user-1")
            return first_trust, second_profile, initial_score, updated_score

        first_trust, second_profile, initial_score, updated_score = asyncio.run(_run())

        self.assertGreater(updated_score, initial_score)
        self.assertAlmostEqual(engine.relationship_engine.get_social_score("user-1"), updated_score)
        self.assertAlmostEqual(second_profile.social_score, updated_score)
        self.assertIn("trust", second_profile.relationship_vector)
        self.assertGreater(second_profile.relationship_vector["trust"], first_trust)

    def test_update_social_score_from_fact_keeps_runtime_vector_in_sync(self):
        config = SimpleNamespace(
            energy=SimpleNamespace(cost_per_reply=0.1, min_reply_threshold=0.1, daily_recovery=0.1, recovery_silence_min=60),
            mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
            reply=SimpleNamespace(emotion_mapping=[]),
        )
        engine = StateEngine(_ProfilePersistence(), SimpleNamespace(config=config), config=config)

        async def _run():
            before = await engine.get_user_profile("user-2")
            before_score = before.social_score
            await engine.update_social_score_from_fact("user-2", 3.0)
            after = await engine.get_user_profile("user-2")
            return before_score, after.social_score, engine.relationship_engine.get_social_score("user-2")

        before_score, after_score, vector_score = asyncio.run(_run())

        self.assertAlmostEqual(after_score, before_score + 3.0)
        self.assertAlmostEqual(vector_score, after_score)
        vector = engine.relationship_engine.get_or_create("user-2")
        self.assertGreater(vector.trust, 6.0)
        self.assertGreater(vector.familiarity, 5.0)
        self.assertGreater(vector.emotion_bond, 6.0)
        self.assertGreater(vector.respect, 3.0)

    def test_relationship_vector_roundtrip_preserves_last_decay_time(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

        async def _run():
            persistence = _SqliteProfilePersistence(Path(temp_dir.name) / "state.db")
            profile = UserProfile(
                user_id="user-3",
                name="tester",
                social_score=12.0,
                relationship_vector={
                    "trust": 10.0,
                    "familiarity": 11.0,
                    "emotion_bond": 12.0,
                    "respect": 13.0,
                    "social_score": 11.4,
                    "total_interactions": 3,
                    "positive_streak": 2,
                    "negative_streak": 0,
                    "first_seen": 111.0,
                    "last_interaction": 222.0,
                    "last_decay_time": 123456.0,
                },
            )
            await persistence.save_user_profile(profile)
            loaded = await persistence.load_user_profile("user-3")
            return loaded

        try:
            loaded = asyncio.run(_run())
        finally:
            temp_dir.cleanup()

        self.assertEqual(loaded["relationship_vector"]["last_decay_time"], 123456.0)
        self.assertEqual(loaded["relationship_vector"]["trust"], 10.0)


if __name__ == "__main__":
    unittest.main()
