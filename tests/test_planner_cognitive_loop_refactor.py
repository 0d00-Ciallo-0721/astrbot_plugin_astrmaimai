import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.planner_stubs import install_planner_stubs
from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeLaneManager:
    async def get_recent_transcript(self, lane_key, base_origin, max_turns=4, max_age_seconds=None):
        return "AstrMai: previous reply"


class _FakeGateway:
    def __init__(self):
        self.config = SimpleNamespace(
            system1=SimpleNamespace(nicknames=["AstrMai"]),
            global_settings=SimpleNamespace(debug_mode=False),
            provider=SimpleNamespace(),
            reply=SimpleNamespace(follow_up_probability=0.0, emotion_mapping={}),
            memory=SimpleNamespace(),
            agent=SimpleNamespace(),
            persona=SimpleNamespace(persona_id="persona-1"),
        )
        self.lane_manager = _FakeLaneManager()


class _FakeContextEngine:
    def __init__(self):
        self.db = SimpleNamespace()
        self.context = SimpleNamespace(shared_dict={})
        self.calls = []

    async def build_prompt(self, **kwargs):
        self.calls.append(kwargs)
        return ("system prompt only", "自然简短", "主动记忆提示")

    def get_last_prefix_hash(self, chat_id):
        return "hash-1"


class _FakePromptRefiner:
    def __init__(self):
        self.calls = []

    async def refine_prompt(
        self,
        event,
        system_prompt,
        prompt="",
        context=None,
        *,
        prompt_envelope=None,
        style_variant="",
        proactive_recall="",
    ):
        self.calls.append(
            {
                "event": event,
                "system_prompt": system_prompt,
                "prompt_envelope": prompt_envelope,
                "style_variant": style_variant,
                "proactive_recall": proactive_recall,
            }
        )
        return system_prompt, "final prompt"


class _FakeExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, event, system_prompt, prompt, tools=None, direct_vision_urls=None):
        self.calls.append(
            {
                "event": event,
                "system_prompt": system_prompt,
                "prompt": prompt,
                "tools": tools,
            }
        )
        return "ok"


class _FakeLoop:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    async def decide(self, *, event, prompt_envelope=None):
        self.calls.append((event, prompt_envelope))
        return self.decision


class _FakeSys3Router:
    async def get_light_tools_for_planner(self):
        return SimpleNamespace(tools=["light-tool"])


class _FakeFeedbackMemoryEngine:
    async def get_cognitive_feedback(self, chat_id, limit=3):
        return [
            SimpleNamespace(
                source="agency",
                summary="recently used meme twice",
                guidance="avoid repeated meme",
                tags=["meme"],
            )
        ]


class _FakeHeartflowManager:
    def get_hidden_context(self, chat_id):
        return "interest=0.82; talk_willingness=0.61; guidance=join carefully"

    def get_state(self, chat_id):
        return SimpleNamespace(interest=0.82, talk_willingness=0.61)

    def get_latest_pulse(self, chat_id):
        return SimpleNamespace(pulse_type="prepare_reply")


class _NamedTool:
    def __init__(self, name):
        self.name = name


class _FakeEvent:
    def __init__(self, sender_id="user-1", sender_name="Alice", text="do you still remember that question?"):
        self.message_str = text
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self.message_obj = None
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._extra = {"retrieve_keys": []}

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_group_id(self):
        return "group-1"


def _install_focus_extras(event):
    event.set_extra("astrmai_focus_event", event)
    event.set_extra("astrmai_focus_reason", "at_bot")
    event.set_extra("astrmai_focus_message_text", "Alice: do you still remember that question?")
    event.set_extra("astrmai_focus_thread_root_event", event)
    event.set_extra("astrmai_focus_thread_core_events", [event])
    event.set_extra("astrmai_focus_thread_related_events", [])
    event.set_extra("astrmai_focus_thread_ambient_events", [])
    event.set_extra("astrmai_focus_thread_reason", "at_bot")


def _install_ambient_focus_extras(event):
    event.set_extra("astrmai_focus_event", event)
    event.set_extra("astrmai_focus_reason", "latest_user_message")
    event.set_extra("astrmai_focus_message_text", f"Alice: {event.message_str}")
    event.set_extra("astrmai_focus_thread_root_event", event)
    event.set_extra("astrmai_focus_thread_core_events", [event])
    event.set_extra("astrmai_focus_thread_related_events", [])
    event.set_extra("astrmai_focus_thread_ambient_events", [])
    event.set_extra("astrmai_focus_thread_reason", "latest_user_message")


class PlannerCognitiveLoopRefactorTests(unittest.TestCase):
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

    def _make_planner(self, decision, *, sys3_router=None, memory_engine=None):
        planner = self.planner_mod.Planner(
            context=SimpleNamespace(),
            gateway=_FakeGateway(),
            context_engine=_FakeContextEngine(),
            reply_engine=SimpleNamespace(config=SimpleNamespace(reply=SimpleNamespace(emotion_mapping={}))),
            memory_engine=memory_engine or SimpleNamespace(),
            evolution_manager=SimpleNamespace(get_active_patterns=lambda chat_id: ""),
            state_engine=None,
            prompt_refiner=_FakePromptRefiner(),
            sys3_router=sys3_router,
        )
        planner.executor = _FakeExecutor()
        planner.cognitive_loop = _FakeLoop(decision)

        async def _no_follow(*args, **kwargs):
            return None

        planner._should_follow_up = _no_follow
        return planner

    def test_planner_skips_executor_for_wait_and_ignore_actions(self):
        for action in ("wait", "ignore"):
            with self.subTest(action=action):
                decision = self.planner_mod.CognitiveDecision(
                    action=action,
                    intent="hold",
                    memory_policy="light",
                )
                planner = self._make_planner(decision)
                event = _FakeEvent()
                _install_focus_extras(event)

                result = asyncio.run(planner.plan_and_execute(event, [event]))

                self.assertEqual(result, "")
                self.assertEqual(planner.executor.calls, [])
                self.assertEqual(event.get_extra("astrmai_cognitive_action"), action)
                self.assertEqual(len(planner.turn_trace_history), 1)
                self.assertEqual(planner.turn_trace_history[0]["status"], f"skipped_{action}")
                self.assertEqual(planner.turn_trace_history[0]["cognitive"]["action"], action)

    def test_planner_settles_no_send_relationship_for_negative_ignore_turn(self):
        observed = {}

        class _StateEngine:
            async def settle_no_send_affection(self, **kwargs):
                observed.update(kwargs)
                return True

        decision = self.planner_mod.CognitiveDecision(
            action="ignore",
            reply_need="ignore",
            intent="hold",
            memory_policy="light",
            social_intent="boundary",
            action_tier="none",
            risk_flags=["direct_attack_to_bot"],
            attack_confidence=0.91,
        )
        planner = self._make_planner(decision)
        planner.state_engine = _StateEngine()
        planner.cognitive_loop = _FakeLoop(decision)
        event = _FakeEvent(text="你这个废物闭嘴")
        _install_focus_extras(event)

        result = asyncio.run(planner.plan_and_execute(event, [event]))

        self.assertEqual(result, "")
        self.assertEqual(observed["user_id"], "user-1")
        self.assertEqual(observed["group_id"], event.unified_msg_origin)
        self.assertEqual(observed["message_text"], "你这个废物闭嘴")
        self.assertEqual(observed["skipped_reason"], "ignore")

    def test_planner_routes_tool_call_decision_into_sys3_tool_mode(self):
        decision = self.planner_mod.CognitiveDecision(
            action="tool_call",
            intent="need lookup",
            memory_policy="light",
            style_policy="check first",
        )
        planner = self._make_planner(decision, sys3_router=_FakeSys3Router())
        event = _FakeEvent(text="help me check this setting")
        _install_focus_extras(event)

        asyncio.run(planner.plan_and_execute(event, [event]))

        self.assertEqual(event.get_extra("judge_action"), "TOOL_CALL")
        self.assertEqual(len(planner.executor.calls), 1)
        self.assertIn("light-tool", planner.executor.calls[0]["tools"])
        self.assertEqual(planner.prompt_refiner.calls[0]["style_variant"], "自然简短")
        self.assertEqual(planner.prompt_refiner.calls[0]["proactive_recall"], "主动记忆提示")

    def test_planner_applies_reply_memory_policy_and_guidance(self):
        decision = self.planner_mod.CognitiveDecision(
            action="reply",
            intent="reply to the current clue",
            memory_policy="none",
            retrieve_keys=["relations"],
            style_policy="confirm briefly before answering",
            forbid_history_continuation=True,
            inner_monologue="focus on this sentence first",
        )
        planner = self._make_planner(decision)
        event = _FakeEvent()
        _install_focus_extras(event)

        asyncio.run(planner.plan_and_execute(event, [event]))

        envelope = event.get_extra("astrmai_prompt_envelope")
        self.assertIsNotNone(envelope)
        self.assertIn("confirm briefly before answering", "".join(envelope.guidance_lines))
        self.assertIn("只接当前线索", "".join(envelope.guidance_lines))
        self.assertTrue(planner.context_engine.context.shared_dict.get("disable_rag_injection"))
        self.assertEqual(event.get_extra("retrieve_keys"), [])
        self.assertIn("focus on this sentence first", event.get_extra("sys1_thought"))
        self.assertEqual(planner.executor.calls[0]["prompt"], "final prompt")
        self.assertEqual(len(planner.turn_trace_history), 1)
        turn_trace = planner.turn_trace_history[0]
        self.assertEqual(turn_trace["status"], "executed")
        self.assertEqual(turn_trace["cognitive"]["memory_policy"], "none")
        self.assertGreaterEqual(turn_trace["continuity"]["system_prompt_length"], 0)
        self.assertGreaterEqual(turn_trace["continuity"]["prompt_length"], 0)
        self.assertGreaterEqual(turn_trace["continuity"]["frozen_prefix_length"], 0)
        rendered_trace = str(turn_trace)
        self.assertNotIn("focus on this sentence first", rendered_trace)
        self.assertNotIn("final prompt", rendered_trace)

    def test_planner_moves_long_reply_and_mode_runtime_instructions_to_prompt_blocks(self):
        planner = self._make_planner(
            self.planner_mod.CognitiveDecision(
                action="reply",
                intent="reply to current clue",
                memory_policy="light",
                social_intent="answer",
                action_tier="none",
            )
        )
        stable, dynamic = planner._adjust_expression_habits_for_behavior(
            "Use short fragments.",
            planner.cognitive_loop.decision,
            ["long_reply"],
        )
        self.assertEqual(stable, "Use short fragments.")
        self.assertEqual(dynamic, "Keep this turn short; avoid another long reply.")

        decision = self.planner_mod.CognitiveDecision(
            action="reply",
            intent="reply to current clue",
            memory_policy="light",
            social_intent="answer",
            action_tier="sys3",
        )
        planner = self._make_planner(decision)
        event = _FakeEvent(text="请直接说重点")
        _install_focus_extras(event)
        event.set_extra("retrieve_keys", ["ALL"])
        event.set_extra("judge_action", "TOOL_CALL")

        asyncio.run(planner.plan_and_execute(event, [event]))

        envelope = planner.prompt_refiner.calls[0]["prompt_envelope"]
        self.assertEqual(planner.executor.calls[0]["system_prompt"], "system prompt only")
        self.assertIn("soft_background", planner.turn_trace_history[0]["continuity"]["dynamic_prompt_blocks"])
        self.assertGreaterEqual(
            planner.turn_trace_history[0]["continuity"]["dynamic_prompt_length"],
            len(envelope.cognitive_drive_block),
        )

    def test_planner_uses_planner_reasoning_when_cognitive_drive_fallback_needs_it(self):
        decision = self.planner_mod.CognitiveDecision(
            action="reply",
            intent="reply to current clue",
            memory_policy="light",
            social_intent="answer",
            action_tier="none",
        )
        planner = self._make_planner(decision)
        planner._agency_posture_guidance = lambda *_args, **_kwargs: ""
        event = _FakeEvent(text="继续这个点")
        _install_focus_extras(event)

        asyncio.run(planner.plan_and_execute(event, [event]))

        envelope = event.get_extra("astrmai_prompt_envelope")
        planner_reasoning = planner.context_engine.calls[0]["planner_reasoning"]
        self.assertEqual(envelope.cognitive_drive_block, planner_reasoning)

    def test_planner_writes_agency_extras_and_reflection(self):
        decision = self.planner_mod.CognitiveDecision(
            action="reply",
            intent="respond with a boundary",
            memory_policy="light",
            social_intent="pushback",
            action_tier="none",
            allowed_action_families=[],
            stance="guarded",
            state_bias="keep the line short",
            risk_flags=["direct_attack_to_bot"],
            attack_confidence=0.91,
        )
        planner = self._make_planner(decision)
        event = _FakeEvent(text="你就是个废物机器人，闭嘴")
        _install_focus_extras(event)

        asyncio.run(planner.plan_and_execute(event, [event]))

        self.assertEqual(event.get_extra("astrmai_reply_need"), "reply")
        self.assertEqual(event.get_extra("astrmai_social_intent"), "pushback")
        self.assertEqual(event.get_extra("astrmai_action_tier"), "none")
        self.assertEqual(event.get_extra("astrmai_stance"), "guarded")
        self.assertEqual(event.get_extra("astrmai_state_bias"), "keep the line short")
        self.assertAlmostEqual(event.get_extra("astrmai_attack_confidence"), 0.91)
        turn_context = event.get_extra("astrmai_turn_context")
        self.assertIsNotNone(turn_context)
        self.assertEqual(turn_context.cognitive.social_intent, "pushback")
        self.assertEqual(turn_context.cognitive.action_tier, "none")
        self.assertAlmostEqual(turn_context.cognitive.attack_confidence, 0.91)
        envelope = event.get_extra("astrmai_prompt_envelope")
        guidance_text = "\n".join(envelope.guidance_lines)
        self.assertIn("克制反驳", guidance_text)
        self.assertIn("不辱骂", guidance_text)
        self.assertIn("Keep this turn brief and avoid proactive expansion.", guidance_text)
        summary = planner.agency_runtime.summary(event.unified_msg_origin)
        self.assertIn("pushback", summary)
        self.assertIn("sharp_reply", summary)
        self.assertEqual(planner.turn_trace_history[-1]["tools"]["final_tier"], "none")

    def test_planner_waits_for_short_non_direct_group_ambient_message(self):
        decision = self.planner_mod.CognitiveDecision(
            action="reply",
            reply_need="reply",
            intent="join ambient group chat",
            memory_policy="light",
            social_intent="join",
            action_tier="chat",
        )
        planner = self._make_planner(decision)
        event = _FakeEvent(text="ok")
        _install_ambient_focus_extras(event)

        result = asyncio.run(planner.plan_and_execute(event, [event]))

        self.assertEqual(result, "")
        self.assertEqual(planner.executor.calls, [])
        self.assertEqual(event.get_extra("astrmai_reply_need"), "wait")
        self.assertEqual(event.get_extra("astrmai_social_intent"), "observe")
        self.assertIn("group_ambient_short_wait", event.get_extra("astrmai_risk_flags"))
        self.assertEqual(planner.turn_trace_history[-1]["status"], "skipped_wait")

    def test_planner_downgrades_pushback_during_sharp_reply_cooldown(self):
        decision = self.planner_mod.CognitiveDecision(
            action="reply",
            reply_need="reply",
            intent="direct attack but cooling down",
            memory_policy="light",
            social_intent="pushback",
            action_tier="none",
            stance="guarded",
            risk_flags=["direct_attack_to_bot"],
            attack_confidence=0.92,
        )
        planner = self._make_planner(decision)
        planner.agency_runtime.record(
            chat_id="default:GroupMessage:group-1",
            reply_need="reply",
            social_intent="pushback",
            action_tier="none",
            action_taken="reply",
            reply_preview="boundary",
            cooldown_tags=["sharp_reply"],
        )
        event = _FakeEvent(text="direct abuse again")
        _install_focus_extras(event)

        asyncio.run(planner.plan_and_execute(event, [event]))

        self.assertEqual(event.get_extra("astrmai_social_intent"), "boundary")
        self.assertIn("sharp_reply_cooldown", event.get_extra("astrmai_risk_flags"))
        self.assertIn("pushback_downgraded", event.get_extra("astrmai_risk_flags"))

    def test_planner_feeds_previous_agency_reflection_to_cognitive_loop(self):
        first_decision = self.planner_mod.CognitiveDecision(
            action="reply",
            intent="first",
            memory_policy="light",
            social_intent="tease",
            action_tier="chat",
        )
        planner = self._make_planner(first_decision)
        first_event = _FakeEvent(text="今天好开心")
        _install_focus_extras(first_event)
        asyncio.run(planner.plan_and_execute(first_event, [first_event]))

        second_decision = self.planner_mod.CognitiveDecision(
            action="reply",
            intent="second",
            memory_policy="light",
        )
        planner.cognitive_loop = _FakeLoop(second_decision)
        second_event = _FakeEvent(text="继续聊")
        _install_focus_extras(second_event)
        asyncio.run(planner.plan_and_execute(second_event, [second_event]))

        self.assertIn("最近我的短期行动残留", second_event.get_extra("astrmai_agency_reflection_summary"))
        continuity = second_event.get_extra("astrmai_conversation_continuity_summary")
        self.assertIn("Conversation continuity", continuity)
        self.assertIn("current_topic=", continuity)
        self.assertIn("goal_status=", continuity)
        self.assertIn("last_social_intent=", continuity)
        self.assertIn("tease", continuity)
        self.assertIn("reply", continuity)
        turn_context = second_event.get_extra("astrmai_turn_context")
        self.assertEqual(turn_context.continuity.conversation_summary, continuity)
        self.assertTrue(turn_context.continuity.current_topic)
        self.assertTrue(turn_context.continuity.goal_status)
        self.assertNotIn("current_goal=", planner.executor.calls[-1]["prompt"])
        self.assertNotIn("goal_status=", planner.executor.calls[-1]["prompt"])

    def test_planner_wait_decision_does_not_refresh_conversation_goal(self):
        first_decision = self.planner_mod.CognitiveDecision(
            action="reply",
            intent="first",
            memory_policy="light",
            social_intent="answer",
            action_tier="chat",
        )
        planner = self._make_planner(first_decision)
        first_event = _FakeEvent(text="first topic")
        _install_focus_extras(first_event)
        asyncio.run(planner.plan_and_execute(first_event, [first_event]))
        before = planner.conversation_continuity.snapshot(first_event.unified_msg_origin)

        wait_decision = self.planner_mod.CognitiveDecision(
            action="wait",
            reply_need="wait",
            intent="hold",
            memory_policy="light",
            social_intent="observe",
            action_tier="none",
        )
        planner.cognitive_loop = _FakeLoop(wait_decision)
        wait_event = _FakeEvent(text="unrelated interruption")
        _install_focus_extras(wait_event)
        asyncio.run(planner.plan_and_execute(wait_event, [wait_event]))
        after = planner.conversation_continuity.snapshot(wait_event.unified_msg_origin)

        self.assertEqual(after["current_topic"], before["current_topic"])
        self.assertEqual(after["current_goal"], before["current_goal"])
        self.assertEqual(after["turn_count"], before["turn_count"])

    def test_planner_feeds_long_term_memory_feedback_to_cognitive_loop(self):
        decision = self.planner_mod.CognitiveDecision(
            action="reply",
            intent="use feedback quietly",
            memory_policy="light",
        )
        planner = self._make_planner(decision, memory_engine=_FakeFeedbackMemoryEngine())
        event = _FakeEvent(text="tell me something interesting about this")
        _install_focus_extras(event)

        asyncio.run(planner.plan_and_execute(event, [event]))

        feedback = event.get_extra("astrmai_memory_feedback_summary")
        self.assertIn("Long-term behavior and memory feedback", feedback)
        self.assertIn("avoid repeated meme", feedback)
        self.assertEqual(planner.cognitive_loop.calls[0][0].get_extra("astrmai_memory_feedback_summary"), feedback)

    def test_planner_feeds_heartflow_context_to_cognitive_loop(self):
        decision = self.planner_mod.CognitiveDecision(
            action="reply",
            intent="use heartflow quietly",
            memory_policy="light",
        )
        planner = self._make_planner(decision)
        planner.heartflow_manager = _FakeHeartflowManager()
        event = _FakeEvent(text="tell me something interesting about this")
        _install_focus_extras(event)

        asyncio.run(planner.plan_and_execute(event, [event]))

        self.assertIn("join carefully", event.get_extra("astrmai_heartflow_context"))
        self.assertEqual(event.get_extra("astrmai_heartflow_pulse"), "prepare_reply")
        self.assertAlmostEqual(event.get_extra("astrmai_heartflow_interest"), 0.82)
        turn_context = event.get_extra("astrmai_turn_context")
        self.assertIn("join carefully", turn_context.continuity.heartflow_context)
        self.assertAlmostEqual(turn_context.continuity.heartflow_interest, 0.82)
        self.assertEqual(planner.cognitive_loop.calls[0][0].get_extra("astrmai_heartflow_context"), event.get_extra("astrmai_heartflow_context"))

    def test_planner_injects_dynamic_tool_guidance_when_tools_are_available(self):
        decision = self.planner_mod.CognitiveDecision(
            action="reply",
            intent="use a helpful tool",
            memory_policy="light",
        )
        planner = self._make_planner(decision)

        async def _fake_build_tools(*args, **kwargs):
            return [
                _NamedTool("omni_perception_query"),
                _NamedTool("self_lore_query"),
                _NamedTool("proactive_poke"),
                _NamedTool("omni_perception_query"),
            ]

        planner._build_execution_tools = _fake_build_tools
        event = _FakeEvent(text="帮我查一下这个人是谁")
        _install_focus_extras(event)

        asyncio.run(planner.plan_and_execute(event, [event]))

        envelope = event.get_extra("astrmai_prompt_envelope")
        guidance_text = "\n".join(envelope.guidance_lines)
        self.assertIn("本轮可用动作：查询记忆/画像、查询自我设定、戳一戳。只有确实合适时才使用，普通闲聊直接回复。", guidance_text)
        self.assertIn("等待只在对方明显没说完", guidance_text)
        self.assertIn("撤回只在用户明确要求", guidance_text)
        self.assertEqual(guidance_text.count("查询记忆/画像"), 1)
        self.assertIn("本轮可用动作", "\n".join(planner.prompt_refiner.calls[0]["prompt_envelope"].guidance_lines))

    def test_planner_injects_chat_tier_tool_guidance(self):
        decision = self.planner_mod.CognitiveDecision(
            action="reply",
            intent="plain social reply",
            memory_policy="light",
        )
        planner = self._make_planner(decision)

        async def _fake_build_tools(*args, **kwargs):
            event = args[1]
            event.set_extra("astrmai_tool_tier", "chat")
            return [
                _NamedTool("proactive_meme"),
                _NamedTool("message_reaction_action"),
                _NamedTool("proactive_like_action"),
                _NamedTool("proactive_poke"),
            ]

        planner._build_execution_tools = _fake_build_tools
        event = _FakeEvent(text="今天好开心啊")
        _install_focus_extras(event)

        asyncio.run(planner.plan_and_execute(event, [event]))

        envelope = event.get_extra("astrmai_prompt_envelope")
        guidance_text = "\n".join(envelope.guidance_lines)
        self.assertIn("如果气氛合适，可以顺手发表情包、轻轻互动或点个赞", guidance_text)
        self.assertIn("戳人或@别人只在非常自然、明确相关时使用", guidance_text)
        self.assertNotIn("等待只在对方明显没说完", guidance_text)
        self.assertNotIn("撤回只在用户明确要求", guidance_text)
        self.assertNotIn("本轮可用动作：", guidance_text)

    def test_planner_does_not_inject_dynamic_tool_guidance_without_tools(self):
        decision = self.planner_mod.CognitiveDecision(
            action="reply",
            intent="plain reply",
            memory_policy="light",
        )
        planner = self._make_planner(decision)

        async def _fake_build_tools(*args, **kwargs):
            return None

        planner._build_execution_tools = _fake_build_tools
        event = _FakeEvent(text="你好呀")
        _install_focus_extras(event)

        asyncio.run(planner.plan_and_execute(event, [event]))

        envelope = event.get_extra("astrmai_prompt_envelope")
        self.assertNotIn("本轮可用动作", "\n".join(envelope.guidance_lines))


if __name__ == "__main__":
    unittest.main()
