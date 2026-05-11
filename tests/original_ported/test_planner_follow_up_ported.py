import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.original_ported.helpers import _install_astrbot_stubs
from tests.helpers.planner_stubs import install_planner_stubs


class PlannerFollowUpPortedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        install_planner_stubs()
        sys.modules.pop("astrmai.conversation.planning.planner", None)
        self.planner_mod = importlib.import_module("astrmai.conversation.planning.planner")
        self.planner_mod = importlib.reload(self.planner_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    class _Event:
        def __init__(self, *, group_id="", extras=None):
            self._group_id = group_id
            self._extra = dict(extras or {})

        def get_extra(self, key, default=None):
            return self._extra.get(key, default)

        def set_extra(self, key, value):
            self._extra[key] = value

        def get_group_id(self):
            return self._group_id

    class _Gateway:
        def __init__(self, probability=1.0, result=None):
            self.config = SimpleNamespace(reply=SimpleNamespace(follow_up_probability=probability))
            self.calls = 0
            self.result = result if result is not None else {"follow": True, "reason": "extra_detail"}

        async def call_data_process_task(self, *args, **kwargs):
            self.calls += 1
            return self.result

    def _planner(self, *, gateway=None, state_engine=None):
        return self.planner_mod.Planner(
            context=SimpleNamespace(),
            gateway=gateway or self._Gateway(),
            context_engine=SimpleNamespace(db=SimpleNamespace()),
            reply_engine=SimpleNamespace(),
            memory_engine=SimpleNamespace(),
            evolution_manager=SimpleNamespace(),
            state_engine=state_engine,
            prompt_refiner=SimpleNamespace(),
            sys3_router=None,
        )

    def test_should_follow_up_awaits_state_engine(self):
        called = {"awaited": False}

        async def _get_state(chat_id):
            called["awaited"] = True
            return SimpleNamespace(energy=0.2)

        planner = self._planner(gateway=SimpleNamespace(config=SimpleNamespace()), state_engine=SimpleNamespace(get_state=_get_state))

        result = asyncio.run(
            planner._should_follow_up(
                "chat-1",
                "This is a deliberately long reply body so the follow-up checker does not short-circuit on length.",
            )
        )

        self.assertIsNone(result)
        self.assertTrue(called["awaited"])

    def test_follow_up_skips_lightweight_group_tools_and_boundary_without_llm(self):
        cases = [
            (self._Event(extras={"astrmai_lightweight_event": True}), None, None, "lightweight_event"),
            (self._Event(group_id="group-1", extras={"astrmai_focus_reason": "ambient"}), None, None, "group_non_direct"),
            (self._Event(extras={"astrmai_reply_need": "wait"}), None, None, "reply_need_blocked"),
            (self._Event(extras={"astrmai_action_tier": "none"}), None, None, "action_tier_none"),
            (self._Event(), [SimpleNamespace(name="proactive_meme")], None, "tools_used"),
            (self._Event(), None, SimpleNamespace(social_intent="boundary"), "social_intent_blocked"),
        ]
        for event, tools, decision, reason in cases:
            gateway = self._Gateway(probability=1.0)
            planner = self._planner(gateway=gateway)
            result = asyncio.run(
                planner._should_follow_up(
                    "chat-1",
                    "This reply leaves a small natural opening",
                    event=event,
                    tools=tools,
                    decision=decision,
                )
            )
            snapshot = event.get_extra("astrmai_turn_context").follow_up
            self.assertIsNone(result)
            self.assertEqual(snapshot.skipped_reason, reason)
            self.assertEqual(gateway.calls, 0)

    def test_follow_up_skips_cooldown_length_and_question(self):
        cases = [
            (self._Event(extras={"astrmai_agency_cooldown_tags": ["meme"]}), "This reply leaves room", "agency_cooldown"),
            (self._Event(), "short", "reply_length_out_of_range"),
            (self._Event(), "This reply is already inviting you?", "reply_already_invites_response"),
        ]
        for event, reply, reason in cases:
            gateway = self._Gateway(probability=1.0)
            planner = self._planner(gateway=gateway)
            result = asyncio.run(planner._should_follow_up("chat-1", reply, event=event))
            snapshot = event.get_extra("astrmai_turn_context").follow_up
            self.assertIsNone(result)
            self.assertEqual(snapshot.skipped_reason, reason)
            self.assertEqual(gateway.calls, 0)

    def test_follow_up_probability_gate_avoids_llm(self):
        event = self._Event(extras={"astrmai_social_intent": "answer"})
        gateway = self._Gateway(probability=1.0)
        planner = self._planner(gateway=gateway)

        with patch("astrmai.conversation.planning.planner_side_inputs.random.random", return_value=0.5):
            result = asyncio.run(
                planner._should_follow_up(
                    "chat-1",
                    "This reply leaves a small natural opening",
                    event=event,
                )
            )

        snapshot = event.get_extra("astrmai_turn_context").follow_up
        self.assertIsNone(result)
        self.assertEqual(snapshot.skipped_reason, "probability_gate")
        self.assertEqual(snapshot.probability, 0.08)
        self.assertFalse(snapshot.llm_checked)
        self.assertEqual(gateway.calls, 0)

    def test_comfort_short_reply_uses_rule_and_sets_cooldown(self):
        event = self._Event(extras={"astrmai_social_intent": "comfort"})
        gateway = self._Gateway(probability=1.0)
        planner = self._planner(gateway=gateway)

        with patch("astrmai.conversation.planning.planner_side_inputs.random.random", return_value=0.0):
            result = asyncio.run(planner._should_follow_up("chat-1", "慢慢来，我在这里", event=event))

        snapshot = event.get_extra("astrmai_turn_context").follow_up
        self.assertEqual(result, "gentle_support")
        self.assertTrue(snapshot.followed)
        self.assertFalse(snapshot.llm_checked)
        self.assertEqual(gateway.calls, 0)

        next_event = self._Event(extras={"astrmai_social_intent": "comfort"})
        with patch("astrmai.conversation.planning.planner_side_inputs.random.random", return_value=0.0):
            second = asyncio.run(planner._should_follow_up("chat-1", "慢慢来，我在这里", event=next_event))
        next_snapshot = next_event.get_extra("astrmai_turn_context").follow_up
        self.assertIsNone(second)
        self.assertEqual(next_snapshot.skipped_reason, "follow_up_cooldown")

    def test_follow_up_llm_path_records_checked_and_cooldown(self):
        event = self._Event(extras={"astrmai_social_intent": "tease"})
        gateway = self._Gateway(probability=1.0, result={"follow": True, "reason": "tiny_extra"})
        planner = self._planner(gateway=gateway)

        with patch("astrmai.conversation.planning.planner_side_inputs.random.random", return_value=0.0):
            result = asyncio.run(
                planner._should_follow_up(
                    "chat-2",
                    "That was honestly kind of funny",
                    event=event,
                )
            )

        snapshot = event.get_extra("astrmai_turn_context").follow_up
        self.assertEqual(result, "tiny_extra")
        self.assertTrue(snapshot.llm_checked)
        self.assertTrue(snapshot.followed)
        self.assertEqual(gateway.calls, 1)


if __name__ == "__main__":
    unittest.main()
