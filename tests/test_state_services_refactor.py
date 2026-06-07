import asyncio
import datetime
import importlib
import sys
import tempfile
import time
import types
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


def _install_state_stubs():
    gateway_mod = types.ModuleType("astrmai.infra.gateway")
    gateway_mod.GlobalModelGateway = type("GlobalModelGateway", (), {})
    sys.modules["astrmai.infra.gateway"] = gateway_mod


class _FakePersistence:
    def __init__(self):
        self.saved_chat_states = []

    async def load_chat_state(self, chat_id):
        return None

    async def save_chat_state(self, chat_id, state):
        self.saved_chat_states.append((chat_id, state.energy, state.mood))
        return None

    async def load_user_profile(self, user_id):
        return None

    async def save_user_profile(self, user_id, profile):
        return None


class _DropPersistence(_FakePersistence):
    async def load_chat_state(self, chat_id):
        return {
            "chat_id": chat_id,
            "energy": 0.05,
            "mood": 0.0,
            "group_config": {},
            "last_reset_date": datetime.date.today().isoformat(),
            "total_replies": 0,
            "last_reply_time": 0.0,
            "last_passive_decay_time": 0.0,
            "last_energy_recovery_time": 0.0,
            "total_messages": 0,
            "judgment_mode": "single",
            "last_msg_info": self._last_msg_info(),
            "last_access_time": 0.0,
            "next_wakeup_timestamp": 0.0,
            "is_dirty": False,
        }

    @staticmethod
    def _last_msg_info():
        return {"sender_id": "", "has_image": False, "image_urls": [], "vl_executed": False}


class StateRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        _install_state_stubs()
        sys.modules.pop("astrmai.state", None)
        sys.modules.pop("astrmai.state.chat_state_service", None)
        sys.modules.pop("astrmai.state.mood.mood_manager", None)
        self.state_mod = importlib.import_module("astrmai.state.chat_state_service")
        self.state_mod = importlib.reload(self.state_mod)
        self.mood_mod = importlib.import_module("astrmai.state.mood.mood_manager")
        self.mood_mod = importlib.reload(self.mood_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_update_mood_keeps_delta_contract(self):
        config = SimpleNamespace(
            energy=SimpleNamespace(cost_per_reply=0.1, min_reply_threshold=0.1, daily_recovery=0.1, recovery_silence_min=60),
            mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
            reply=SimpleNamespace(emotion_mapping=[]),
        )
        engine = self.state_mod.StateEngine(_FakePersistence(), SimpleNamespace(config=config), config=config)

        observed = {}

        async def _get_state(chat_id):
            return SimpleNamespace(mood=0.2, energy=0.5, last_reply_time=0, last_passive_decay_time=0)

        async def _analyze(text, current_mood, user_affection=0.0, chat_id=None):
            observed["text"] = text
            observed["chat_id"] = chat_id
            observed["current_mood"] = current_mood
            return "happy", 0.6

        async def _atomic(chat_id, delta=0.0, absolute_val=None):
            observed["delta"] = delta
            return 0.6

        engine.get_state = _get_state
        engine.mood_manager.analyze_mood = _analyze
        engine.atomic_update_mood = _atomic

        tag, final_mood = asyncio.run(engine.update_mood("chat-1", "hello"))
        self.assertEqual(tag, "happy")
        # CAS: _get_state_inner 返回 mood=0.0 (FakePersistence 默认), snapshot=0.2
        # → abs(0.0-0.2) > 0.001 → delta 路径: 0.0 + (0.6-0.2) = 0.4
        self.assertAlmostEqual(final_mood, 0.4)
        self.assertEqual(observed["chat_id"], "chat-1")
        # delta 不再通过 atomic_update_mood 观测（新 CAS 路径直接 clamp）

    def test_consume_energy_skips_private_chat_by_design(self):
        persistence = _FakePersistence()
        config = SimpleNamespace(
            energy=SimpleNamespace(cost_per_reply=0.1, min_reply_threshold=0.1, daily_recovery=0.1, recovery_silence_min=60),
            mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
            reply=SimpleNamespace(emotion_mapping=[]),
        )
        engine = self.state_mod.StateEngine(persistence, SimpleNamespace(config=config), config=config)

        async def _run():
            private_chat_id = "default:FriendMessage:user-1"
            group_chat_id = "default:GroupMessage:group-1"
            private_before = (await engine.get_state(private_chat_id)).energy
            group_before = (await engine.get_state(group_chat_id)).energy
            await engine.consume_energy(private_chat_id, amount=0.2)
            await engine.consume_energy(group_chat_id, amount=0.2)
            private_after = (await engine.get_state(private_chat_id)).energy
            group_after = (await engine.get_state(group_chat_id)).energy
            return private_before, private_after, group_before, group_after

        private_before, private_after, group_before, group_after = asyncio.run(_run())

        self.assertEqual(private_before, 0.5)
        self.assertEqual(private_after, 0.5)
        self.assertEqual(group_before, 0.5)
        self.assertAlmostEqual(group_after, 0.3)
        self.assertEqual(
            [chat_id for chat_id, _, _ in persistence.saved_chat_states],
            ["default:GroupMessage:group-1"],
        )

    def test_should_drop_by_energy_persists_recovery_side_effect(self):
        persistence = _DropPersistence()
        config = SimpleNamespace(
            energy=SimpleNamespace(cost_per_reply=0.1, min_reply_threshold=0.1, daily_recovery=0.1, recovery_silence_min=60),
            mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
            reply=SimpleNamespace(emotion_mapping=[]),
        )
        engine = self.state_mod.StateEngine(persistence, SimpleNamespace(config=config), config=config)

        original_random = self.state_mod.random.random if hasattr(self.state_mod, "random") else None

        async def _run():
            import astrmai.state.energy.energy_manager as energy_manager_mod
            old_random = energy_manager_mod.random.random
            energy_manager_mod.random.random = lambda: 0.0
            try:
                dropped = await engine.should_drop_by_energy("default:GroupMessage:group-drop", 2)
                state = await engine.get_state("default:GroupMessage:group-drop")
                return dropped, state
            finally:
                energy_manager_mod.random.random = old_random

        dropped, state = asyncio.run(_run())

        self.assertTrue(dropped)
        self.assertAlmostEqual(state.energy, 0.25)
        self.assertFalse(state.is_dirty)
        self.assertEqual(
            [chat_id for chat_id, _, _ in persistence.saved_chat_states],
            ["default:GroupMessage:group-drop"],
        )

    def test_update_mood_snapshot_does_not_mutate_live_state_before_analysis(self):
        config = SimpleNamespace(
            energy=SimpleNamespace(cost_per_reply=0.1, min_reply_threshold=0.1, daily_recovery=0.1, recovery_silence_min=1),
            mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
            reply=SimpleNamespace(emotion_mapping=[]),
        )
        engine = self.state_mod.StateEngine(_FakePersistence(), SimpleNamespace(config=config), config=config)
        live_state = SimpleNamespace(
            mood=0.2,
            energy=0.4,
            last_reply_time=time.time() - 7200.0,
            last_passive_decay_time=time.time() - 7200.0,
            is_dirty=False,
        )
        observed = {}

        async def _get_state(chat_id):
            observed["chat_id"] = chat_id
            return live_state

        async def _get_state_inner(chat_id):
            return live_state

        async def _save_chat_state(chat_id, state):
            observed["saved_energy"] = state.energy
            observed["saved_mood"] = state.mood

        async def _analyze(text, current_mood, user_affection=0.0, chat_id=None):
            observed["energy_during_analysis"] = live_state.energy
            observed["dirty_during_analysis"] = live_state.is_dirty
            return "happy", 0.6

        engine.get_state = _get_state
        engine.chat_state_service._get_state_inner = _get_state_inner
        engine.chat_state_service.persistence.save_chat_state = _save_chat_state
        engine.mood_manager.analyze_mood = _analyze

        tag, final_mood = asyncio.run(engine.update_mood("chat-snapshot", "hello"))

        self.assertEqual(tag, "happy")
        self.assertAlmostEqual(final_mood, 0.6)
        self.assertAlmostEqual(observed["energy_during_analysis"], 0.4)
        self.assertFalse(observed["dirty_during_analysis"])
        self.assertAlmostEqual(observed["saved_energy"], 0.5)
        self.assertAlmostEqual(live_state.energy, 0.5)

    def test_settle_no_send_affection_only_updates_negative_interactions(self):
        config = SimpleNamespace(
            energy=SimpleNamespace(cost_per_reply=0.1, min_reply_threshold=0.1, daily_recovery=0.1, recovery_silence_min=60),
            mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
            reply=SimpleNamespace(emotion_mapping=[]),
        )
        engine = self.state_mod.StateEngine(_FakePersistence(), SimpleNamespace(config=config), config=config)
        observed = []

        async def _publish_change(user_id, old_score, new_score, mood_tag, event_type):
            observed.append((user_id, old_score, new_score, mood_tag, event_type))

        engine.affection_router.publish_change = _publish_change

        async def _run():
            neutral = await engine.settle_no_send_affection(
                "user-1",
                "default:GroupMessage:group-1",
                "hello there",
                skipped_reason="send_failed",
            )
            negative = await engine.settle_no_send_affection(
                "user-1",
                "default:GroupMessage:group-1",
                "你这个废物给我闭嘴",
                skipped_reason="ignore",
            )
            return neutral, negative, await engine.get_user_profile("user-1")

        neutral, negative, profile = asyncio.run(_run())

        self.assertFalse(neutral)
        self.assertTrue(negative)
        self.assertLess(profile.social_score, 0.0)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][-1], self.state_mod.RelationshipEvent.INSULT)

    def test_mood_manager_uses_lane_raw_text_when_parsed_json_is_empty(self):
        class _LaneResult:
            parsed_json = {}
            raw_completion = '```json\n{"mood_tag": "sad", "mood_value": -0.35}\n```'

        class _Gateway:
            def __init__(self):
                self.config = SimpleNamespace(provider=SimpleNamespace(task_models=[]))
                self.lane_manager = object()

            async def chat_in_lane_result(self, **kwargs):
                del kwargs
                return _LaneResult()

        manager = self.mood_mod.MoodManager(_Gateway(), config=SimpleNamespace(reply=SimpleNamespace(emotion_mapping=[]), provider=SimpleNamespace(task_models=[])))

        tag, mood_value = asyncio.run(manager.analyze_mood("对不起，让你担心了", 0.0, chat_id="chat-1"))

        self.assertEqual(tag, "sad")
        self.assertAlmostEqual(mood_value, -0.35)

    def test_mood_manager_fallback_keeps_sarcasm_negative(self):
        tag, mood_value = self.mood_mod.MoodManager._fallback_analyze_local("你可真行啊，又把事情搞砸了，真棒。", 0.0)

        self.assertEqual(tag, "angry")
        self.assertLess(mood_value, 0.0)

    def test_mood_manager_fallback_does_not_flatten_mixed_affect_to_happy(self):
        tag, mood_value = self.mood_mod.MoodManager._fallback_analyze_local("谢谢你，但我还是有点难过。", 0.0)

        self.assertEqual(tag, "sad")
        self.assertLess(mood_value, 0.0)

    def test_mood_manager_fallback_keeps_tool_intent_questions_neutral(self):
        tag, mood_value = self.mood_mod.MoodManager._fallback_analyze_local("帮我查一下明天上海天气？", 0.0)

        self.assertEqual(tag, "neutral")
        self.assertEqual(mood_value, 0.0)

    def test_calculate_and_update_affection_keeps_mixed_affect_from_support_uplift(self):
        config = SimpleNamespace(
            energy=SimpleNamespace(cost_per_reply=0.1, min_reply_threshold=0.1, daily_recovery=0.1, recovery_silence_min=60),
            mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
            reply=SimpleNamespace(emotion_mapping=[]),
        )
        engine = self.state_mod.StateEngine(_FakePersistence(), SimpleNamespace(config=config), config=config)

        async def _run():
            await engine.calculate_and_update_affection(
                user_id="user-mixed",
                group_id="default:FriendMessage:user-mixed",
                mood_tag="sad",
                intensity=1.0,
                message_text="谢谢你，但我还是有点难过。",
            )
            return await engine.get_user_profile("user-mixed")

        profile = asyncio.run(_run())

        self.assertGreater(profile.social_score, 0.0)
        self.assertLess(profile.social_score, 1.0)

    def test_calculate_and_update_affection_softens_comfort_with_complaint(self):
        config = SimpleNamespace(
            energy=SimpleNamespace(cost_per_reply=0.1, min_reply_threshold=0.1, daily_recovery=0.1, recovery_silence_min=60),
            mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
            reply=SimpleNamespace(emotion_mapping=[]),
        )
        engine = self.state_mod.StateEngine(_FakePersistence(), SimpleNamespace(config=config), config=config)

        async def _run():
            await engine.calculate_and_update_affection(
                user_id="user-comfort-complaint",
                group_id="default:FriendMessage:user-comfort-complaint",
                mood_tag="sad",
                intensity=1.0,
                message_text="抱抱，谢谢你愿意安慰我，但你刚才那句还是让我有点受伤。",
            )
            return await engine.get_user_profile("user-comfort-complaint")

        profile = asyncio.run(_run())

        self.assertGreater(profile.social_score, 0.0)
        self.assertLessEqual(profile.social_score, 0.4)

    def test_calculate_and_update_affection_publishes_effective_mood_and_event(self):
        config = SimpleNamespace(
            energy=SimpleNamespace(cost_per_reply=0.1, min_reply_threshold=0.1, daily_recovery=0.1, recovery_silence_min=60),
            mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
            reply=SimpleNamespace(emotion_mapping=[]),
        )
        engine = self.state_mod.StateEngine(_FakePersistence(), SimpleNamespace(config=config), config=config)
        observed = {}

        async def _publish_change(user_id, old_score, new_score, mood_tag, event_type):
            observed.update(
                {
                    "user_id": user_id,
                    "old_score": old_score,
                    "new_score": new_score,
                    "mood_tag": mood_tag,
                    "event_type": event_type,
                }
            )

        engine.affection_router.publish_change = _publish_change

        async def _run():
            await engine.calculate_and_update_affection(
                user_id="user-publish-effective",
                group_id="default:FriendMessage:user-publish-effective",
                mood_tag="sad",
                intensity=1.0,
                message_text="谢谢你，但我还是有点难过。",
            )

        asyncio.run(_run())

        self.assertEqual(observed["mood_tag"], "")
        self.assertEqual(observed["event_type"], self.state_mod.RelationshipEvent.NORMAL_CHAT)

    def test_relationship_engine_classifies_ambiguous_and_cold_boundaries_conservatively(self):
        engine = self.state_mod.RelationshipEngine()

        self.assertEqual(
            engine.classify_interaction_type("晚安呀，早点休息，别太累了。"),
            self.state_mod.RelationshipEvent.GREETING,
        )
        self.assertEqual(
            engine.classify_interaction_type("哦，那你先忙吧，我就不打扰了。"),
            self.state_mod.RelationshipEvent.IGNORE,
        )
        self.assertEqual(
            engine.classify_interaction_type("哦，行吧，就这样。"),
            self.state_mod.RelationshipEvent.IGNORE,
        )
        self.assertEqual(
            engine.classify_interaction_type("行了，别说了，我知道了。"),
            self.state_mod.RelationshipEvent.IGNORE,
        )

    def test_normal_chat_bias_keeps_tool_intent_above_mixed_affect(self):
        config = SimpleNamespace(
            energy=SimpleNamespace(cost_per_reply=0.1, min_reply_threshold=0.1, daily_recovery=0.1, recovery_silence_min=60),
            mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
            reply=SimpleNamespace(emotion_mapping=[]),
        )
        engine = self.state_mod.StateEngine(_FakePersistence(), SimpleNamespace(config=config), config=config)

        async def _run():
            await engine.calculate_and_update_affection(
                user_id="user-mixed-bias",
                group_id="default:FriendMessage:user-mixed-bias",
                mood_tag="sad",
                intensity=1.0,
                message_text="谢谢你，但我还是有点难过。",
            )
            await engine.calculate_and_update_affection(
                user_id="user-tool-bias",
                group_id="default:FriendMessage:user-tool-bias",
                mood_tag="neutral",
                intensity=1.0,
                message_text="帮我查一下明天上海天气。",
            )
            mixed_profile = await engine.get_user_profile("user-mixed-bias")
            tool_profile = await engine.get_user_profile("user-tool-bias")
            return mixed_profile.social_score, tool_profile.social_score

        mixed_score, tool_score = asyncio.run(_run())

        self.assertGreaterEqual(mixed_score, 0.2)
        self.assertLessEqual(mixed_score, 0.4)
        self.assertGreaterEqual(tool_score, 0.3)
        self.assertLessEqual(tool_score, 0.45)
        self.assertGreater(tool_score, mixed_score)

    def test_ignore_bias_creates_three_non_hostile_negative_tiers(self):
        config = SimpleNamespace(
            energy=SimpleNamespace(cost_per_reply=0.1, min_reply_threshold=0.1, daily_recovery=0.1, recovery_silence_min=60),
            mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
            reply=SimpleNamespace(emotion_mapping=[]),
        )
        engine = self.state_mod.StateEngine(_FakePersistence(), SimpleNamespace(config=config), config=config)

        async def _score(user_id: str, text: str) -> float:
            await engine.calculate_and_update_affection(
                user_id=user_id,
                group_id="default:FriendMessage:tier",
                mood_tag="neutral",
                intensity=1.0,
                message_text=text,
            )
            profile = await engine.get_user_profile(user_id)
            return float(profile.social_score)

        cold_score = asyncio.run(_score("user-cold-distance", "哦，那你先忙吧，我就不打扰了。"))
        perfunctory_score = asyncio.run(_score("user-perfunctory", "哦，行吧，就这样。"))
        irritation_score = asyncio.run(_score("user-irritation", "行了，别说了，我知道了。"))

        self.assertGreater(cold_score, perfunctory_score)
        self.assertGreater(perfunctory_score, irritation_score)
        self.assertGreaterEqual(cold_score, -0.30)
        self.assertLessEqual(cold_score, -0.20)
        self.assertGreaterEqual(perfunctory_score, -0.40)
        self.assertLessEqual(perfunctory_score, -0.30)
        self.assertGreaterEqual(irritation_score, -0.55)
        self.assertLessEqual(irritation_score, -0.40)


if __name__ == "__main__":
    unittest.main()
