import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from tests.helpers.planner_stubs import install_planner_stubs
from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeEvent:
    def __init__(self, message="帮我查一下", group_id=None):
        self.message_str = message
        self._group_id = group_id
        self._extra = {}

    def get_group_id(self):
        return self._group_id

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class _Tool:
    def __init__(self, name):
        self.name = name


_STUB_TOOL_NAME_ALIASES = {
    "WaitTool": "wait_and_listen",
    "OmniPerceptionTool": "omni_perception_query",
    "SelfLoreQueryTool": "self_lore_query",
    "ConstructAtEventTool": "construct_at_event",
    "ProactivePokeTool": "proactive_poke",
    "ProactiveMemeTool": "proactive_meme",
    "MemeResonanceTool": "meme_resonance_action",
    "TopicHijackTool": "topic_hijack_action",
    "SpaceTransitionTool": "space_transition_action",
    "RegretAndWithdrawTool": "regret_and_withdraw_action",
    "MessageReactionTool": "message_reaction_action",
    "ProactiveLikeTool": "proactive_like_action",
}


def _normalized_tool_names(tools):
    return {_STUB_TOOL_NAME_ALIASES.get(tool.name, tool.name) for tool in tools}


class _IdentityActionModifier:
    def modify_tools(self, tools, **kwargs):
        return tools


class _FakeSys3Router:
    async def get_light_tools_for_planner(self):
        return SimpleNamespace(tools=[_Tool("sys3_light_tool")])


class _FakeStateEngine:
    def __init__(self, *, energy=0.8, mood=0.0, caution=0.4, social_score=0):
        self._state = SimpleNamespace(energy=energy, mood=mood, caution=caution)
        self._profile = SimpleNamespace(social_score=social_score)
        self.relationship_engine = SimpleNamespace(
            get_or_create=lambda user_id: SimpleNamespace(social_score=social_score, trust=0.0)
        )

    async def get_state(self, chat_id):
        return self._state

    async def get_user_profile(self, user_id):
        return self._profile


class PlannerSideInputsRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        install_planner_stubs()
        sys.modules.pop("astrmai.conversation.planning.planner_side_inputs", None)
        sys.modules.pop("astrmai.conversation.planning.expression_policy", None)
        side_inputs_mod = importlib.import_module("astrmai.conversation.planning.planner_side_inputs")
        self.side_inputs_mod = importlib.reload(side_inputs_mod)
        expression_mod = importlib.import_module("astrmai.conversation.planning.expression_policy")
        self.expression_mod = importlib.reload(expression_mod)
        self.mixin = self.side_inputs_mod.PlannerSideInputMixin()

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_mode_instructions_use_first_person_without_legacy_markers(self):
        system_prompt = self.mixin._append_mode_instructions(
            "base",
            _FakeEvent(message="帮我查一下天气"),
            is_tool_call_mode=True,
            is_all_mode=True,
            is_fast_mode=True,
        )

        self.assertIn("对方这次是在让我帮忙办事。", system_prompt)
        self.assertIn("对方刚才说的是：“帮我查一下天气”。我这轮就先接住这一条来回。", system_prompt)
        self.assertIn("有人在喊我，我得马上用简短直接的话接住这次呼唤，不绕远路。", system_prompt)
        self.assertNotIn(">>>", system_prompt)
        self.assertNotIn("你现在的首要任务是", system_prompt)

    def test_private_jump_context_uses_first_person_memory_recall(self):
        ctx = type(
            "Ctx",
            (),
            {
                "shared_dict": {
                    "astrmai_space_jumps": {
                        "user-1": {
                            "timestamp": time.time(),
                            "private_message": "晚上再接着聊呀",
                        }
                    }
                }
            },
        )()

        result = asyncio.run(
            self.mixin._apply_private_jump_context(
                "base prompt",
                ctx,
                _FakeEvent(group_id=None),
                "user-1",
            )
        )

        self.assertIn("刚才我还在群聊里和大家说话", result)
        self.assertIn("【我刚才的悄悄话】：晚上再接着聊呀", result)
        self.assertIn("对方现在这句，多半就是接着我刚才那次跨界私聊在回我。", result)
        self.assertNotIn(">>>", result)
        self.assertEqual(ctx.shared_dict["astrmai_space_jumps"], {})

    def _prepare_tool_mixin(self):
        mixin = self.side_inputs_mod.PlannerSideInputMixin()
        mixin.gateway = SimpleNamespace(config=SimpleNamespace(persona=SimpleNamespace(persona_id="persona-1")))
        mixin.memory_engine = SimpleNamespace()
        mixin.context_engine = SimpleNamespace(db=SimpleNamespace())
        mixin.reply_engine = SimpleNamespace(config=SimpleNamespace(reply=SimpleNamespace(emotion_mapping=[])))
        mixin.state_engine = None
        mixin.action_modifier = _IdentityActionModifier()
        mixin.sys3_router = _FakeSys3Router()

        def _set_disable_rag_injection(ctx, disabled):
            ctx.shared_dict["disable_rag_injection"] = disabled

        mixin._set_disable_rag_injection = _set_disable_rag_injection
        return mixin

    def test_action_modifier_energy_scale_and_query_fallbacks(self):
        modifier = self.expression_mod.ActionModifier()
        tools = [
            _Tool("wait_and_listen"),
            _Tool("omni_perception_query"),
            _Tool("self_lore_query"),
            _Tool("proactive_poke"),
        ]

        normal = modifier.modify_tools(tools, state=SimpleNamespace(energy=0.8))
        self.assertEqual([tool.name for tool in normal], [tool.name for tool in tools])

        exhausted = modifier.modify_tools(tools, state=SimpleNamespace(energy=0.05))
        self.assertEqual(
            [tool.name for tool in exhausted],
            ["wait_and_listen", "omni_perception_query", "self_lore_query"],
        )

        hostile = modifier.modify_tools(tools, profile=SimpleNamespace(social_score=-30))
        self.assertEqual(
            [tool.name for tool in hostile],
            ["wait_and_listen", "omni_perception_query", "self_lore_query"],
        )

        chat_tools = [
            _Tool("proactive_meme"),
            _Tool("message_reaction_action"),
            _Tool("proactive_like_action"),
        ]
        chat_exhausted = modifier.modify_tools(
            chat_tools,
            state=SimpleNamespace(energy=0.05),
            tool_tier="chat",
        )
        self.assertEqual(
            [tool.name for tool in chat_exhausted],
            ["message_reaction_action", "proactive_like_action"],
        )

        chat_hostile = modifier.modify_tools(
            chat_tools,
            profile=SimpleNamespace(social_score=-30),
            tool_tier="chat",
        )
        self.assertEqual([tool.name for tool in chat_hostile], ["message_reaction_action"])

        cautious = modifier.modify_tools(
            [
                _Tool("proactive_poke"),
                _Tool("construct_at_event"),
                _Tool("space_transition_action"),
                _Tool("message_reaction_action"),
            ],
            state=SimpleNamespace(energy=0.8, mood=0.0, caution=0.9),
            tool_tier="chat",
        )
        self.assertEqual([tool.name for tool in cautious], ["message_reaction_action"])

        cooldown = modifier.modify_tools(
            chat_tools,
            state=SimpleNamespace(energy=0.8, mood=0.0),
            tool_tier="chat",
            cooldown_tags=["meme", "like"],
        )
        self.assertEqual([tool.name for tool in cooldown], ["message_reaction_action"])

        sharp_cooldown = modifier.modify_tools(
            chat_tools,
            state=SimpleNamespace(energy=0.8, mood=0.0),
            tool_tier="chat",
            cooldown_tags=["sharp_reply"],
        )
        self.assertEqual([tool.name for tool in sharp_cooldown], ["message_reaction_action"])

        long_reply_cooldown = modifier.modify_tools(
            [
                _Tool("topic_hijack_action"),
                _Tool("space_transition_action"),
                _Tool("meme_resonance_action"),
                _Tool("omni_perception_query"),
            ],
            state=SimpleNamespace(energy=0.8, mood=0.0),
            tool_tier="full",
            cooldown_tags=["long_reply"],
        )
        self.assertEqual([tool.name for tool in long_reply_cooldown], ["omni_perception_query"])

        trace = self.side_inputs_mod.ToolDecisionTrace() if hasattr(self.side_inputs_mod, "ToolDecisionTrace") else None
        if trace is None:
            from astrmai.conversation.contracts.turn_context import ToolDecisionTrace

            trace = ToolDecisionTrace()
        traced = modifier.modify_tools(
            chat_tools,
            state=SimpleNamespace(energy=0.05),
            tool_tier="chat",
            trace=trace,
        )
        self.assertEqual([tool.name for tool in traced], ["message_reaction_action", "proactive_like_action"])
        self.assertIn("energy_exhausted(0.05)", trace.filter_reasons)
        self.assertEqual(trace.filter_steps[0]["stage"], "action_modifier.energy")
        self.assertEqual(trace.filter_steps[0]["category"], "energy")
        self.assertEqual(trace.removed_by_energy, ["proactive_meme"])

        mood_trace = self.side_inputs_mod.ToolDecisionTrace() if hasattr(self.side_inputs_mod, "ToolDecisionTrace") else trace.__class__()
        modifier.modify_tools(
            chat_tools,
            state=SimpleNamespace(energy=0.8, mood=-0.8),
            tool_tier="chat",
            trace=mood_trace,
        )
        self.assertEqual(mood_trace.removed_by_mood, ["proactive_meme"])

        hostile_trace = self.side_inputs_mod.ToolDecisionTrace() if hasattr(self.side_inputs_mod, "ToolDecisionTrace") else trace.__class__()
        modifier.modify_tools(
            chat_tools,
            profile=SimpleNamespace(social_score=-30),
            tool_tier="chat",
            trace=hostile_trace,
        )
        self.assertIn("proactive_meme", hostile_trace.removed_by_hostility)

        caution_trace = self.side_inputs_mod.ToolDecisionTrace() if hasattr(self.side_inputs_mod, "ToolDecisionTrace") else trace.__class__()
        modifier.modify_tools(
            [_Tool("proactive_poke"), _Tool("construct_at_event"), _Tool("message_reaction_action")],
            state=SimpleNamespace(energy=0.8, mood=0.0, caution=0.9),
            tool_tier="chat",
            trace=caution_trace,
        )
        self.assertEqual(caution_trace.removed_by_caution, ["proactive_poke", "construct_at_event"])

        cooldown_trace = self.side_inputs_mod.ToolDecisionTrace() if hasattr(self.side_inputs_mod, "ToolDecisionTrace") else trace.__class__()
        returned_tools, returned_trace = modifier.modify_tools(
            chat_tools,
            state=SimpleNamespace(energy=0.8, mood=0.0),
            tool_tier="chat",
            cooldown_tags=["meme", "like"],
            return_trace=True,
            trace=cooldown_trace,
        )
        self.assertEqual([tool.name for tool in returned_tools], ["message_reaction_action"])
        self.assertIs(returned_trace, cooldown_trace)
        self.assertEqual(set(cooldown_trace.removed_by_cooldown), {"proactive_meme", "proactive_like_action"})

    def test_all_mode_plain_chat_loads_chat_tier_and_tool_intent_loads_full_pfc_tools(self):
        mixin = self._prepare_tool_mixin()
        ctx = SimpleNamespace(shared_dict={})
        plain_event = _FakeEvent(message="你好呀")

        plain_tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                plain_event,
                "user-1",
                "Alice",
                ctx,
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )
        self.assertEqual(
            _normalized_tool_names(plain_tools),
            {"proactive_meme", "message_reaction_action", "proactive_like_action"},
        )
        self.assertEqual(plain_event.get_extra("astrmai_tool_tier"), "chat")
        plain_turn = plain_event.get_extra("astrmai_turn_context")
        self.assertEqual(plain_turn.tools.final_tier, "chat")
        self.assertEqual(plain_turn.tools.requested_tier, "")
        self.assertFalse(plain_turn.tools.explicit_tool_intent)
        self.assertEqual(
            set(plain_turn.tools.available_tools),
            {"proactive_meme", "message_reaction_action", "proactive_like_action"},
        )
        self.assertEqual(
            set(plain_turn.tools.filtered_tools),
            {"proactive_meme", "message_reaction_action", "proactive_like_action"},
        )
        self.assertTrue(ctx.shared_dict["disable_rag_injection"])

        tool_ctx = SimpleNamespace(shared_dict={})
        query_keyword = next(iter(mixin.TOOL_INTENT_KEYWORDS))
        intent_event = _FakeEvent(message=f"{query_keyword} target")
        intent_tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                intent_event,
                "user-1",
                "Alice",
                tool_ctx,
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )
        self.assertIsNotNone(intent_tools)
        intent_names = _normalized_tool_names(intent_tools)
        self.assertIn("omni_perception_query", intent_names)
        self.assertIn("self_lore_query", intent_names)
        self.assertEqual(intent_event.get_extra("astrmai_tool_tier"), "full")
        self.assertEqual(intent_event.get_extra("astrmai_turn_context").tools.final_tier, "full")
        self.assertTrue(intent_event.get_extra("astrmai_turn_context").tools.explicit_tool_intent)
        self.assertTrue(tool_ctx.shared_dict["disable_rag_injection"])

        english_ctx = SimpleNamespace(shared_dict={})
        english_event = _FakeEvent(message="Please look up whether you should withdraw the last reply.")
        english_tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                english_event,
                "user-1",
                "Alice",
                english_ctx,
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )
        english_names = _normalized_tool_names(english_tools)
        self.assertIn("omni_perception_query", english_names)
        self.assertIn("regret_and_withdraw_action", english_names)
        self.assertEqual(english_event.get_extra("astrmai_tool_tier"), "full")
        self.assertTrue(english_event.get_extra("astrmai_turn_context").tools.explicit_tool_intent)

    def test_low_energy_downgrades_requested_full_tier_without_explicit_tool_intent(self):
        mixin = self._prepare_tool_mixin()
        mixin.state_engine = _FakeStateEngine(energy=0.2)
        ctx = SimpleNamespace(shared_dict={})
        event = _FakeEvent(message="plain small talk")
        event.set_extra("astrmai_action_tier", "full")

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                event,
                "user-1",
                "Alice",
                ctx,
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        self.assertEqual(event.get_extra("astrmai_tool_tier"), "chat")
        self.assertEqual(
            _normalized_tool_names(tools),
            {"proactive_meme", "message_reaction_action", "proactive_like_action"},
        )
        trace = event.get_extra("astrmai_turn_context").tools
        self.assertEqual(trace.requested_tier, "full")
        self.assertEqual(trace.final_tier, "chat")
        self.assertIn("full_tier", trace.removed_by_energy)
        self.assertTrue(any(step["stage"] == "planner.tier_state_guard" for step in trace.filter_steps))

    def test_explicit_tool_intent_bypasses_low_energy_full_tier_downgrade(self):
        mixin = self._prepare_tool_mixin()
        mixin.state_engine = _FakeStateEngine(energy=0.2)
        ctx = SimpleNamespace(shared_dict={})
        query_keyword = next(iter(mixin.TOOL_INTENT_KEYWORDS))
        event = _FakeEvent(message=f"{query_keyword} target")
        event.set_extra("astrmai_action_tier", "full")

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                event,
                "user-1",
                "Alice",
                ctx,
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        self.assertEqual(event.get_extra("astrmai_tool_tier"), "full")
        self.assertIn("omni_perception_query", _normalized_tool_names(tools))
        trace = event.get_extra("astrmai_turn_context").tools
        self.assertEqual(trace.final_tier, "full")
        self.assertNotIn("full_tier", trace.removed_by_energy)

    def test_guarded_chat_intent_adds_guarded_chat_tools_without_full_pfc(self):
        mixin = self._prepare_tool_mixin()
        ctx = SimpleNamespace(shared_dict={})
        poke_keyword = next(iter(mixin.POKE_INTENT_KEYWORDS))
        event = _FakeEvent(message=f"{poke_keyword} target")

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                event,
                "user-1",
                "Alice",
                ctx,
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        tool_names = _normalized_tool_names(tools)
        self.assertIn("proactive_poke", tool_names)
        self.assertIn("construct_at_event", tool_names)
        self.assertNotIn("omni_perception_query", tool_names)
        self.assertNotIn("wait_and_listen", tool_names)
        self.assertEqual(event.get_extra("astrmai_tool_tier"), "chat")

        at_ctx = SimpleNamespace(shared_dict={})
        at_event = _FakeEvent(message="@target")
        at_tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                at_event,
                "user-1",
                "Alice",
                at_ctx,
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )
        at_tool_names = _normalized_tool_names(at_tools)
        self.assertIn("proactive_poke", at_tool_names)
        self.assertIn("construct_at_event", at_tool_names)

    def test_guarded_chat_intent_does_not_match_unrelated_poke_words(self):
        mixin = self._prepare_tool_mixin()
        for message in ["ordinary metaphor", "soft disagreement", "mail user at example dot com"]:
            with self.subTest(message=message):
                ctx = SimpleNamespace(shared_dict={})
                event = _FakeEvent(message=message)

                tools = asyncio.run(
                    mixin._build_execution_tools(
                        "default:GroupMessage:group-1",
                        event,
                        "user-1",
                        "Alice",
                        ctx,
                        is_all_mode=True,
                        is_fast_mode=False,
                        is_tool_call_mode=False,
                    )
                )

                tool_names = _normalized_tool_names(tools)
                self.assertNotIn("proactive_poke", tool_names)
                self.assertNotIn("construct_at_event", tool_names)
                self.assertEqual(
                    tool_names,
                    {"proactive_meme", "message_reaction_action", "proactive_like_action"},
                )

    def test_core_only_plain_chat_uses_chat_tier_instead_of_full_pfc(self):
        mixin = self._prepare_tool_mixin()
        ctx = SimpleNamespace(shared_dict={})
        event = _FakeEvent(message="你好")

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                event,
                "user-1",
                "Alice",
                ctx,
                is_all_mode=False,
                is_fast_mode=True,
                is_tool_call_mode=False,
            )
        )

        self.assertEqual(
            _normalized_tool_names(tools),
            {"proactive_meme", "message_reaction_action", "proactive_like_action"},
        )
        self.assertEqual(event.get_extra("astrmai_tool_tier"), "chat")
        self.assertTrue(ctx.shared_dict["disable_rag_injection"])

    def test_agency_tier_none_and_social_intent_constrain_tools(self):
        mixin = self._prepare_tool_mixin()
        none_ctx = SimpleNamespace(shared_dict={})
        none_event = _FakeEvent(message="我先不接这个")
        none_event.set_extra("astrmai_action_tier", "none")

        none_tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                none_event,
                "user-1",
                "Alice",
                none_ctx,
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        self.assertEqual(none_tools, [])
        self.assertEqual(none_event.get_extra("astrmai_tool_tier"), "none")

        comfort_ctx = SimpleNamespace(shared_dict={})
        comfort_event = _FakeEvent(message="rough day")
        comfort_event.set_extra("astrmai_social_intent", "comfort")

        comfort_tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                comfort_event,
                "user-1",
                "Alice",
                comfort_ctx,
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        self.assertEqual(
            _normalized_tool_names(comfort_tools),
            {"message_reaction_action", "proactive_like_action"},
        )

        recall_ctx = SimpleNamespace(shared_dict={})
        recall_event = _FakeEvent(message="你还记得我之前说的吗")
        recall_event.set_extra("astrmai_social_intent", "recall")

        recall_tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                recall_event,
                "user-1",
                "Alice",
                recall_ctx,
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        self.assertEqual(
            _normalized_tool_names(recall_tools),
            {"omni_perception_query", "self_lore_query"},
        )
        recall_turn = recall_event.get_extra("astrmai_turn_context")
        self.assertEqual(recall_turn.tools.allowed_families, ["query"])
        self.assertIn("allowed_families(query)", recall_turn.tools.filter_reasons)
        self.assertIn("proactive_meme", recall_turn.tools.removed_by_social_intent)

    def test_pushback_intent_does_not_expose_chat_tools_by_default(self):
        mixin = self._prepare_tool_mixin()
        ctx = SimpleNamespace(shared_dict={})
        event = _FakeEvent(message="你就是个废物机器人，闭嘴")
        event.set_extra("astrmai_social_intent", "pushback")

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                event,
                "user-1",
                "Alice",
                ctx,
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        self.assertEqual(tools, [])
        self.assertEqual(event.get_extra("astrmai_tool_tier"), "none")
        pushback_trace = event.get_extra("astrmai_turn_context").tools
        self.assertIn("social_intent(pushback)_forces_none", pushback_trace.filter_reasons)
        self.assertIn("requested_tier_none", pushback_trace.filter_reasons)

    def test_sys3_tool_call_mode_bypasses_all_mode_keyword_gate(self):
        mixin = self._prepare_tool_mixin()
        ctx = SimpleNamespace(shared_dict={})

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                _FakeEvent(message="你好呀"),
                "user-1",
                "Alice",
                ctx,
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=True,
            )
        )

        self.assertIsNotNone(tools)
        self.assertIn("sys3_light_tool", [tool.name for tool in tools])
        self.assertTrue(ctx.shared_dict["disable_rag_injection"])

    def test_sys3_tool_call_mode_sets_sys3_tier(self):
        mixin = self._prepare_tool_mixin()
        ctx = SimpleNamespace(shared_dict={})
        event = _FakeEvent(message="你好呀")

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                event,
                "user-1",
                "Alice",
                ctx,
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=True,
            )
        )

        self.assertIsNotNone(tools)
        self.assertIn("sys3_light_tool", [tool.name for tool in tools])
        self.assertEqual(event.get_extra("astrmai_tool_tier"), "sys3")
        self.assertTrue(ctx.shared_dict["disable_rag_injection"])


if __name__ == "__main__":
    unittest.main()
