import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeGateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.config = SimpleNamespace(persona=SimpleNamespace(persona_id="persona-1"))

    async def call_data_process_task(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, "kwargs": kwargs})
        response = self.responses.pop(0)
        if asyncio.iscoroutinefunction(response):
            return await response(prompt, **kwargs)
        if callable(response):
            return await response(prompt, **kwargs)
        return response


class _FakeRelationshipVector:
    def get_context_description(self):
        return "relationship: friendly"


class _FakeRelationshipEngine:
    def get_or_create(self, user_id):
        return _FakeRelationshipVector()


class _FakeStateEngine:
    def __init__(self):
        self.relationship_engine = _FakeRelationshipEngine()

    async def get_user_profile_summary(self, user_id):
        return SimpleNamespace(
            name="Alice",
            nickname="A",
            social_score=42.0,
            persona_analysis="curious",
            memory_points=["likes puzzles"],
        )

    async def get_state(self, chat_id):
        return SimpleNamespace(energy=0.8, mood=0.1, total_replies=3, last_reply_time=0.0)


class _FakeMemoryEngine:
    async def recall_persona_lore(self, query, persona_id=None):
        return f"self-lore:{persona_id}:{query}"

    async def recall(self, query, session_id=None, top_k=None):
        return f"memory:{session_id}:{top_k}:{query}"


class _FakeEvent:
    def __init__(self, text="你还记得刚才说的那个吗？"):
        self.message_str = text
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self._sender_id = "user-1"
        self._sender_name = "Alice"
        self._extra = {}

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name


class CognitiveLoopRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.conversation.planning.cognitive_loop", None)
        self.mod = importlib.import_module("astrmai.conversation.planning.cognitive_loop")
        self.mod = importlib.reload(self.mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_cognitive_loop_rejects_non_readonly_tool_request(self):
        gateway = _FakeGateway(
            [
                {
                    "action": "reply",
                    "intent": "先确认上下文",
                    "memory_policy": "light",
                    "retrieve_keys": ["relations"],
                    "style_policy": "自然一点",
                    "forbid_history_continuation": True,
                    "inner_monologue": "先看一下关系",
                    "need_tool": True,
                    "tool_name": "proactive_poke",
                    "tool_query": "poke Bob",
                },
                {
                    "action": "reply",
                    "intent": "直接回到当前问题",
                    "memory_policy": "light",
                    "retrieve_keys": ["relations"],
                    "style_policy": "短一点",
                    "forbid_history_continuation": True,
                    "inner_monologue": "不能乱动手",
                },
            ]
        )
        loop = self.mod.CognitiveLoop(
            gateway,
            memory_engine=_FakeMemoryEngine(),
            state_engine=_FakeStateEngine(),
        )

        decision = asyncio.run(loop.decide(event=_FakeEvent()))

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, "reply")
        self.assertEqual(len(gateway.calls), 2)
        self.assertIn("Readonly restriction", gateway.calls[1]["prompt"])

    def test_cognitive_loop_timeout_falls_back_to_none(self):
        async def _slow_response(prompt, **kwargs):
            await asyncio.sleep(0.05)
            return {"action": "reply"}

        gateway = _FakeGateway([_slow_response])
        loop = self.mod.CognitiveLoop(gateway)
        loop.SOFT_TIMEOUT_SECONDS = 0.01

        decision = asyncio.run(loop.decide(event=_FakeEvent("为什么会这样？")))

        self.assertIsNone(decision)

    def test_cognitive_loop_should_run_skips_lightweight_fast_legacy_and_trivial_cases(self):
        loop = self.mod.CognitiveLoop(_FakeGateway([]))

        lightweight_event = _FakeEvent("hi")
        lightweight_event.set_extra("astrmai_lightweight_event", True)
        self.assertFalse(loop.should_run(lightweight_event))

        fast_mode_event = _FakeEvent("hello there")
        fast_mode_event.set_extra("is_fast_mode", True)
        self.assertFalse(loop.should_run(fast_mode_event))

        core_only_event = _FakeEvent("hello world")
        core_only_event.set_extra("retrieve_keys", ["CORE_ONLY"])
        self.assertFalse(loop.should_run(core_only_event))

        all_mode_event = _FakeEvent("hello world and enough context")
        all_mode_event.set_extra("retrieve_keys", ["ALL"])
        self.assertTrue(loop.should_run(all_mode_event))

        legacy_wait_event = _FakeEvent("why")
        legacy_wait_event.set_extra("judge_action", "WAIT")
        self.assertFalse(loop.should_run(legacy_wait_event))

        trivial_event = _FakeEvent("ok")
        self.assertFalse(loop.should_run(trivial_event))

        complex_short_event = _FakeEvent("why?")
        self.assertTrue(loop.should_run(complex_short_event))

        budget_zero_event = _FakeEvent("why?")
        budget_zero_event.set_extra("astrmai_think_level", 0)
        self.assertFalse(loop.should_run(budget_zero_event))
        self.assertEqual(budget_zero_event.get_extra("astrmai_cognitive_loop_skipped_reason"), "think_level_0")

        direct_budget_event = _FakeEvent("hello")
        direct_budget_event.set_extra("astrmai_think_level", 1)
        self.assertTrue(loop.should_run(direct_budget_event))

    def test_cognitive_loop_skips_readonly_tool_step_below_think_level_three(self):
        gateway = _FakeGateway(
            [
                {
                    "action": "reply",
                    "intent": "answer from first pass",
                    "memory_policy": "light",
                    "need_tool": True,
                    "tool_name": "light_memory",
                    "tool_query": "topic",
                }
            ]
        )
        loop = self.mod.CognitiveLoop(
            gateway,
            memory_engine=_FakeMemoryEngine(),
            state_engine=_FakeStateEngine(),
        )
        event = _FakeEvent("why should we do this?")
        event.set_extra("astrmai_think_level", 2)

        decision = asyncio.run(loop.decide(event=event))

        self.assertIsNotNone(decision)
        self.assertEqual(decision.intent, "answer from first pass")
        self.assertEqual(len(gateway.calls), 1)
        self.assertFalse(event.get_extra("astrmai_cognitive_loop_readonly_tools_allowed"))
        self.assertEqual(
            event.get_extra("astrmai_cognitive_loop_readonly_skip_reason"),
            "think_level_2_blocks_readonly_tool",
        )

    def test_cognitive_loop_allows_readonly_tool_step_at_think_level_three(self):
        gateway = _FakeGateway(
            [
                {
                    "action": "reply",
                    "intent": "need memory",
                    "memory_policy": "deep",
                    "need_tool": True,
                    "tool_name": "light_memory",
                    "tool_query": "topic",
                },
                {
                    "action": "reply",
                    "intent": "answer after memory",
                    "memory_policy": "deep",
                },
            ]
        )
        loop = self.mod.CognitiveLoop(
            gateway,
            memory_engine=_FakeMemoryEngine(),
            state_engine=_FakeStateEngine(),
        )
        event = _FakeEvent("do you remember what we said before?")
        event.set_extra("astrmai_think_level", 3)

        decision = asyncio.run(loop.decide(event=event))

        self.assertIsNotNone(decision)
        self.assertEqual(decision.intent, "answer after memory")
        self.assertEqual(len(gateway.calls), 2)
        self.assertTrue(event.get_extra("astrmai_cognitive_loop_readonly_tools_allowed"))
        self.assertEqual(event.get_extra("astrmai_cognitive_loop_readonly_skip_reason"), "")

    def test_cognitive_loop_normalizes_agency_fields_and_downgrades_weak_pushback(self):
        gateway = _FakeGateway(
            [
                {
                    "action": "reply",
                    "reply_need": "reply",
                    "intent": "set a boundary",
                    "memory_policy": "none",
                    "social_intent": "pushback",
                    "action_tier": "chat",
                    "allowed_action_families": ["meme", "poke"],
                    "stance": "guarded",
                    "state_bias": "keep it short",
                    "risk_flags": ["low_intensity"],
                    "attack_confidence": 0.4,
                    "inner_monologue": "not enough evidence for pushback",
                }
            ]
        )
        loop = self.mod.CognitiveLoop(gateway)

        decision = asyncio.run(loop.decide(event=_FakeEvent("你这说法不对吧，别乱说了真的")))

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, "reply")
        self.assertEqual(decision.reply_need, "reply")
        self.assertEqual(decision.memory_policy, "none")
        self.assertEqual(decision.social_intent, "boundary")
        self.assertEqual(decision.action_tier, "chat")
        self.assertEqual(decision.allowed_action_families, ["meme", "poke"])
        self.assertEqual(decision.stance, "guarded")
        self.assertEqual(decision.state_bias, "keep it short")
        self.assertIn("pushback_downgraded", decision.risk_flags)
        self.assertEqual(decision.attack_confidence, 0.4)

    def test_cognitive_loop_allows_high_confidence_pushback(self):
        gateway = _FakeGateway(
            [
                {
                    "action": "reply",
                    "reply_need": "reply",
                    "intent": "respond to direct abuse",
                    "memory_policy": "light",
                    "social_intent": "pushback",
                    "action_tier": "none",
                    "risk_flags": ["direct_attack_to_bot"],
                    "attack_confidence": 0.92,
                }
            ]
        )
        loop = self.mod.CognitiveLoop(gateway)

        decision = asyncio.run(loop.decide(event=_FakeEvent("你就是个废物机器人，闭嘴")))

        self.assertIsNotNone(decision)
        self.assertEqual(decision.social_intent, "pushback")
        self.assertEqual(decision.action_tier, "none")
        self.assertAlmostEqual(decision.attack_confidence, 0.92)

    def test_cognitive_loop_requires_direct_attack_flag_for_pushback(self):
        gateway = _FakeGateway(
            [
                {
                    "action": "reply",
                    "reply_need": "reply",
                    "intent": "respond to unclear hostility",
                    "memory_policy": "light",
                    "social_intent": "pushback",
                    "action_tier": "none",
                    "risk_flags": [],
                    "attack_confidence": 0.93,
                }
            ]
        )
        loop = self.mod.CognitiveLoop(gateway)

        decision = asyncio.run(loop.decide(event=_FakeEvent("this is hostile but target is unclear")))

        self.assertIsNotNone(decision)
        self.assertEqual(decision.social_intent, "boundary")
        self.assertIn("pushback_downgraded", decision.risk_flags)

    def test_cognitive_loop_reads_continuity_from_turn_context(self):
        gateway = _FakeGateway(
            [
                {
                    "action": "reply",
                    "reply_need": "reply",
                    "intent": "continue naturally",
                    "memory_policy": "light",
                }
            ]
        )
        loop = self.mod.CognitiveLoop(gateway)
        event = _FakeEvent("continue the same topic please")
        turn_mod = importlib.import_module("astrmai.conversation.contracts.turn_context")
        turn_context = turn_mod.ensure_turn_context(event)
        turn_context.continuity.conversation_summary = (
            "Conversation continuity:\n"
            "current_topic=Alice: puzzle chat\n"
            "current_goal=continue the puzzle gently\n"
            "last_social_intent=tease\n"
            "goal_status=continuing"
        )

        decision = asyncio.run(loop.decide(event=event))

        self.assertIsNotNone(decision)
        self.assertIn("current_goal=continue the puzzle gently", gateway.calls[0]["prompt"])
        self.assertIn("goal_status=continuing", gateway.calls[0]["prompt"])


if __name__ == "__main__":
    unittest.main()
