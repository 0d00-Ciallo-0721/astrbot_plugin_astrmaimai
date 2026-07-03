import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from astrmai.state.mood.mood_decay import apply_natural_decay
from astrmai.state.mood.mood_manager import MoodManager
from astrmai.state.relationship.affection_router import AffectionRouter
from astrmai.state.relationship.relationship_engine import RelationshipEngine, RelationshipEvent


class StateGapCoverageTests(unittest.TestCase):
    def test_relationship_process_event_negative_event_resets_positive_streak(self):
        engine = RelationshipEngine()
        vec = engine.get_or_create("user-negative")
        vec.trust = 60.0
        vec.emotion_bond = 40.0
        vec.positive_streak = 3
        before_score = vec.social_score

        after_score = engine.process_event("user-negative", RelationshipEvent.INSULT, intensity=2.0)

        self.assertLess(after_score, before_score)
        self.assertEqual(vec.positive_streak, 0)
        self.assertEqual(vec.negative_streak, 1)
        self.assertEqual(vec.total_interactions, 1)
        self.assertLess(vec.trust, 60.0)
        self.assertLess(vec.emotion_bond, 40.0)

    def test_affection_router_hostile_trigger_can_override_context_window(self):
        config = SimpleNamespace(
            attention=SimpleNamespace(
                affection_weights={"trigger": 20.0, "window": 50.0, "history": 30.0},
                adjudication_threshold=50.0,
                sensitive_words=["badword"],
            )
        )
        history = [{"sender_id": "user-a", "content": "long helpful context from user a"}]
        window = [{"sender_id": "user-a", "content": "another normal message from user a"}]
        trigger = {"sender_id": "user-b", "content": "badword"}

        winner = AffectionRouter.route(history, window, trigger, "angry", config)

        self.assertEqual(winner, "user-b")

    def test_affection_router_hostile_trigger_does_not_override_stronger_context(self):
        config = SimpleNamespace(
            attention=SimpleNamespace(
                affection_weights={"trigger": 20.0, "window": 70.0, "history": 40.0},
                adjudication_threshold=50.0,
                sensitive_words=["badword"],
            )
        )
        history = [{"sender_id": "user-a", "content": "long helpful context from user a"}]
        window = [{"sender_id": "user-a", "content": "another normal message from user a"}]
        trigger = {"sender_id": "user-b", "content": "badword"}

        winner = AffectionRouter.route(history, window, trigger, "angry", config)

        self.assertEqual(winner, "user-a")

    def test_affection_router_non_hostile_trigger_keeps_normal_scoring(self):
        config = SimpleNamespace(
            attention=SimpleNamespace(
                affection_weights={"trigger": 20.0, "window": 70.0, "history": 40.0},
                adjudication_threshold=50.0,
                sensitive_words=["badword"],
            )
        )
        history = [{"sender_id": "user-a", "content": "long helpful context from user a"}]
        window = [{"sender_id": "user-a", "content": "another normal message from user a"}]
        trigger = {"sender_id": "user-b", "content": "plain question"}

        winner = AffectionRouter.route(history, window, trigger, "neutral", config)

        self.assertEqual(winner, "user-a")

    def test_mood_manager_lane_result_normalizes_unknown_tag_and_clamps_value(self):
        observed = {}

        class _Gateway:
            lane_manager = object()
            config = SimpleNamespace(
                reply=SimpleNamespace(emotion_mapping=[]),
                provider=SimpleNamespace(task_models=["mood-model"]),
            )

            async def chat_in_lane_result(self, **kwargs):
                observed.update(kwargs)
                return SimpleNamespace(parsed_json={"mood_tag": "joyful", "mood_value": 2.5})

        manager = MoodManager(_Gateway())

        tag, mood_value = asyncio.run(manager.analyze_mood("hello there", 0.2, chat_id="chat-1"))

        self.assertEqual(tag, "neutral")
        self.assertAlmostEqual(mood_value, 1.0)
        self.assertEqual(observed["base_origin"], "chat-1")
        self.assertTrue(observed["is_json"])
        self.assertFalse(observed["use_fallback"])

    def test_apply_natural_decay_epoch0_does_not_catastrophically_decay_mood(self):
        state = SimpleNamespace(
            energy=0.9,
            mood=0.7,
            last_reply_time=0.0,
            last_energy_recovery_time=0.0,
            last_passive_decay_time=0.0,
            is_dirty=False,
        )
        config = SimpleNamespace(
            energy=SimpleNamespace(recovery_silence_min=60),
            mood=SimpleNamespace(decay_interval=60, decay_rate=0.1),
        )

        with patch("astrmai.state.mood.mood_decay.time.time", return_value=1_000_000.0):
            apply_natural_decay(state, config)

        self.assertAlmostEqual(state.mood, 0.7)
        self.assertAlmostEqual(state.last_passive_decay_time, 1_000_000.0)
        self.assertFalse(state.is_dirty)


if __name__ == "__main__":
    unittest.main()
