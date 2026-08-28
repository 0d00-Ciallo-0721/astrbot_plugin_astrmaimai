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
    "QQFriendLookupTool": "qq_friend_lookup",
    "QQGroupMemberLookupTool": "qq_group_member_lookup",
    "QQUserIdentityLookupTool": "qq_user_identity_lookup",
    "QQForwardMessageLookupTool": "qq_forward_message_lookup",
    "QQGroupPresenceLookupTool": "qq_group_presence_lookup",
    "QQRecentContactLookupTool": "qq_recent_contact_lookup",
    "QQMessageArtifactLookupTool": "qq_message_artifact_lookup",
    "VisionMessageAnalyzeTool": "vision_message_analyze_tool",
    "CrossSessionReplyLookupTool": "cross_session_reply_lookup",
    "QuoteReplyActionTool": "quote_reply_action",
    "QQMessageRecallLookupTool": "qq_message_recall_lookup",
    "TopicThreadLookupTool": "topic_thread_lookup",
    "BotCapabilityLookupTool": "bot_capability_lookup",
    "LearnedLanguageLookupTool": "learned_language_lookup",
    "MemoryWriteCorrectionTool": "memory_write_correction_tool",
    "UnverifiedReportRecordTool": "unverified_report_record_tool",
    "PersonaFactCheckTool": "persona_fact_check_tool",
    "GroupActivitySnapshotTool": "group_activity_snapshot_tool",
    "ContactRouteSuggestTool": "contact_route_suggest_tool",
    "CrossChatMemoryQueryTool": "cross_chat_memory_query",
    "ConstructAtEventTool": "construct_at_event",
    "ProactivePokeTool": "proactive_poke",
    "ProactiveMemeTool": "proactive_meme",
    "MemeResonanceTool": "meme_resonance_action",
    "TopicHijackTool": "topic_hijack_action",
    "SpaceTransitionTool": "space_transition_action",
    "RegretAndWithdrawTool": "regret_and_withdraw_action",
    "MessageEmojiLikeTool": "message_emoji_like_action",
    "MessageReactionTool": "message_reaction_action",
    "ProactiveLikeTool": "proactive_like_action",
}


def _normalized_tool_names(tools):
    return {_STUB_TOOL_NAME_ALIASES.get(tool.name, tool.name) for tool in tools}


FACT_TOOL_NAMES = {
    "qq_friend_lookup",
    "qq_user_identity_lookup",
    "qq_forward_message_lookup",
    "qq_group_presence_lookup",
    "qq_recent_contact_lookup",
    "qq_message_artifact_lookup",
    "vision_message_analyze_tool",
    "cross_session_reply_lookup",
    "qq_message_recall_lookup",
    "topic_thread_lookup",
    "bot_capability_lookup",
    "learned_language_lookup",
    "memory_write_correction_tool",
    "unverified_report_record_tool",
    "persona_fact_check_tool",
    "contact_route_suggest_tool",
    "cross_chat_memory_query",
}

CORE_TOOL_NAMES = {
    "wait_and_listen",
    "omni_perception_query",
    "cross_chat_memory_query",
    "bot_capability_lookup",
    "learned_language_lookup",
}

DEFAULT_ACTION_TOOL_NAMES = {
    "regret_and_withdraw_action",
    "proactive_poke",
    "construct_at_event",
    "quote_reply_action",
    "message_emoji_like_action",
    "vision_message_analyze_tool",
    "proactive_meme",
    "meme_resonance_action",
}

PRIVATE_DEFAULT_ACTION_TOOL_NAMES = DEFAULT_ACTION_TOOL_NAMES - {"construct_at_event", "meme_resonance_action"}

LEGACY_CHAT_EXPECTED_TOOL_NAMES = FACT_TOOL_NAMES | {
    "proactive_meme",
    "message_reaction_action",
    "message_emoji_like_action",
    "proactive_like_action",
    "proactive_poke",
    "space_transition_action",
    "quote_reply_action",
    "regret_and_withdraw_action",
}

# Unqualified chat turns expose only read-only/context tools and textual
# reaction shaping; external side-effect tools require an explicit family.
CHAT_EXPECTED_TOOL_NAMES = CORE_TOOL_NAMES | {"vision_message_analyze_tool"}

CROSS_SESSION_DISCLOSURE_TOOL_NAMES = CHAT_EXPECTED_TOOL_NAMES | {
    "qq_friend_lookup",
    "qq_user_identity_lookup",
    "qq_recent_contact_lookup",
    "contact_route_suggest_tool",
    "space_transition_action",
} | PRIVATE_DEFAULT_ACTION_TOOL_NAMES


class _IdentityActionModifier:
    def modify_tools(self, tools, **kwargs):
        return tools


class _FakeSys3Router:
    async def get_light_tools_for_planner(self):
        return SimpleNamespace(tools=[_Tool("sys3_light_tool")])


class _FakeStateEngine:
    def __init__(self, *, energy=0.8, mood=0.0, caution=0.4, social_score=0, trust=0.0):
        self._state = SimpleNamespace(energy=energy, mood=mood, caution=caution)
        self._profile = SimpleNamespace(social_score=social_score)
        self.relationship_engine = SimpleNamespace(
            get_or_create=lambda user_id: SimpleNamespace(social_score=social_score, trust=trust)
        )

    async def get_state(self, chat_id):
        return self._state

    async def get_user_profile(self, user_id):
        return self._profile

    async def settle_no_send_affection(self, **kwargs):
        return False


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
        self.mixin._event_has_component_hint = lambda event, hints: False

    def test_image_only_policy_removes_unauthorized_side_effect_tools(self):
        event = _FakeEvent(message="")
        event.set_extra("direct_image_refs", ["fake://image"])
        tools = [_Tool("proactive_meme"), _Tool("proactive_poke"), _Tool("vision_message_analyze_tool")]

        filtered = self.mixin._apply_event_tool_call_policy(event, tools)

        self.assertEqual(_normalized_tool_names(filtered), {"vision_message_analyze_tool"})
        policy = event.get_extra("astrmai_tool_call_policy")
        self.assertEqual(policy["mode"], "image_only")
        self.assertFalse(policy["action_authorized"])
        self.assertEqual(policy["families"], [])
        self.assertEqual(policy["confidence"], "none")

    def test_cognitive_allowed_family_does_not_authorize_image_meme(self):
        event = _FakeEvent(message="")
        event.set_extra("direct_image_refs", ["fake://image"])
        event.set_extra("astrmai_allowed_action_families", ["meme"])
        tools = [_Tool("proactive_meme"), _Tool("vision_message_analyze_tool")]

        filtered = self.mixin._apply_event_tool_call_policy(event, tools)

        self.assertEqual(_normalized_tool_names(filtered), {"vision_message_analyze_tool"})
        self.assertFalse(event.get_extra("astrmai_tool_call_policy")["action_authorized"])

    def test_image_explicit_meme_request_authorizes_only_meme_family(self):
        event = _FakeEvent(message="发一个表情包")
        event.set_extra("direct_image_refs", ["fake://image"])
        tools = [_Tool("proactive_meme"), _Tool("proactive_poke"), _Tool("vision_message_analyze_tool")]

        filtered = self.mixin._apply_event_tool_call_policy(event, tools)

        self.assertEqual(
            _normalized_tool_names(filtered),
            {"proactive_meme", "vision_message_analyze_tool"},
        )
        self.assertEqual(
            event.get_extra("astrmai_explicit_user_intent_families"),
            ["meme"],
        )

    def test_negated_meme_request_does_not_authorize_or_expose_meme(self):
        event = _FakeEvent(message="不要发个表情包")
        tools = [_Tool("proactive_meme"), _Tool("proactive_poke"), _Tool("vision_message_analyze_tool")]

        filtered = self.mixin._apply_event_tool_call_policy(event, tools)

        self.assertEqual(_normalized_tool_names(filtered), {"vision_message_analyze_tool"})
        self.assertNotIn("meme", event.get_extra("astrmai_explicit_user_intent_families", []))
        self.assertFalse(event.get_extra("astrmai_tool_call_policy")["action_authorized"])

    def test_object_inserted_negation_does_not_authorize_meme(self):
        for message in ("不要给我发个表情包", "我不想让你发个表情包", "不需要你发个表情包"):
            event = _FakeEvent(message=message)
            filtered = self.mixin._apply_event_tool_call_policy(
                event,
                [_Tool("proactive_meme"), _Tool("vision_message_analyze_tool")],
            )

            self.assertEqual(_normalized_tool_names(filtered), {"vision_message_analyze_tool"}, message)
            self.assertFalse(event.get_extra("astrmai_tool_call_policy")["action_authorized"], message)
            self.assertFalse(self.mixin._has_tool_intent(event), message)

    def test_explicit_poke_exposes_only_poke_family_and_read_only_tools(self):
        event = _FakeEvent(message="戳我一下")
        tools = [
            _Tool("proactive_poke"),
            _Tool("proactive_meme"),
            _Tool("proactive_like_action"),
            _Tool("wait_and_listen"),
            _Tool("omni_perception_query"),
        ]

        filtered = self.mixin._apply_event_tool_call_policy(event, tools)

        self.assertEqual(
            _normalized_tool_names(filtered),
            {"proactive_poke", "wait_and_listen", "omni_perception_query"},
        )
        self.assertEqual(event.get_extra("astrmai_explicit_user_intent_families"), ["poke"])
        self.assertEqual(event.get_extra("astrmai_tool_call_policy")["families"], ["poke"])

    def test_external_non_action_families_do_not_mark_side_effects_authorized(self):
        event = _FakeEvent(message="今天天气怎么样")
        event.set_extra("astrmai_explicit_user_intent_families", ["query", "unknown"])

        filtered = self.mixin._apply_event_tool_call_policy(
            event,
            [_Tool("proactive_meme"), _Tool("vision_message_analyze_tool")],
        )

        self.assertEqual(_normalized_tool_names(filtered), {"vision_message_analyze_tool"})
        self.assertEqual(event.get_extra("astrmai_explicit_user_intent_families"), [])
        self.assertFalse(event.get_extra("astrmai_tool_call_policy")["action_authorized"])

    def test_plain_text_without_action_has_no_side_effect_permission(self):
        event = _FakeEvent(message="今天天气怎么样")

        self.mixin._apply_event_tool_call_policy(event, [_Tool("vision_message_analyze_tool")])

        policy = event.get_extra("astrmai_tool_call_policy")
        self.assertFalse(policy["action_authorized"])
        self.assertFalse(policy["side_effects_allowed"])

    def test_plain_text_hides_all_unassigned_side_effect_tools(self):
        event = _FakeEvent(message="今天天气怎么样")
        tools = [
            _Tool("proactive_meme"),
            _Tool("proactive_poke"),
            _Tool("construct_at_event"),
            _Tool("quote_reply_action"),
            _Tool("space_transition_action"),
            _Tool("vision_message_analyze_tool"),
        ]

        filtered = self.mixin._apply_event_tool_call_policy(event, tools)

        self.assertEqual(_normalized_tool_names(filtered), {"vision_message_analyze_tool"})

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_mode_instructions_use_first_person_without_legacy_markers(self):
        envelope = self.side_inputs_mod.PromptEnvelope()
        result = self.mixin._append_mode_instructions(
            _FakeEvent(message="帮我查一下天气"),
            prompt_envelope=envelope,
            is_tool_call_mode=True,
            is_all_mode=True,
            is_fast_mode=True,
        )

        self.assertIsNone(result)
        self.assertIn("对方这次是在让我帮忙办事。", envelope.planner_runtime_instruction_block)
        self.assertIn("对方刚才说的是：“帮我查一下天气”。我这轮就先接住这一条来回。", envelope.planner_runtime_instruction_block)
        self.assertIn("有人在喊我，我得马上用简短直接的话接住这次呼唤，不绕远路。", envelope.planner_runtime_instruction_block)
        self.assertNotIn(">>>", envelope.planner_runtime_instruction_block)
        self.assertNotIn("你现在的首要任务是", envelope.planner_runtime_instruction_block)

    def test_mode_instructions_write_into_runtime_instruction_block(self):
        envelope = self.side_inputs_mod.PromptEnvelope()
        self.mixin._append_mode_instructions(
            _FakeEvent(message="请直接说重点"),
            prompt_envelope=envelope,
            is_tool_call_mode=True,
            is_all_mode=True,
            is_fast_mode=True,
        )

        self.assertIn("对方这次是在让我帮忙办事。", envelope.planner_runtime_instruction_block)
        self.assertIn("对方刚才说的是：“请直接说重点”。我这轮就先接住这一条来回。", envelope.planner_runtime_instruction_block)
        self.assertIn("有人在喊我，我得马上用简短直接的话接住这次呼唤，不绕远路。", envelope.planner_runtime_instruction_block)
        self.assertLessEqual(
            len(envelope.planner_runtime_instruction_block),
            self.side_inputs_mod.PlannerSideInputMixin.PLANNER_RUNTIME_INSTRUCTION_MAX_CHARS,
        )

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

        event = _FakeEvent(group_id=None)
        envelope = self.side_inputs_mod.PromptEnvelope()
        event.set_extra("astrmai_prompt_envelope", envelope)
        result = asyncio.run(
            self.mixin._apply_private_jump_context(
                ctx,
                event,
                "user-1",
                prompt_envelope=envelope,
            )
        )

        self.assertIsNone(result)
        self.assertIn("刚才我还在群聊里和大家说话", envelope.planner_runtime_instruction_block)
        self.assertIn("【我刚才的悄悄话】：晚上再接着聊呀", envelope.planner_runtime_instruction_block)
        self.assertIn("对方现在这句，多半就是接着我刚才那次跨界私聊在回我。", envelope.planner_runtime_instruction_block)
        self.assertEqual(ctx.shared_dict["astrmai_space_jumps"], {})

    def test_mode_instructions_truncate_long_user_message(self):
        envelope = self.side_inputs_mod.PromptEnvelope()
        long_message = "请你先把这段很长很长的原话完整复述一遍然后再解释" * 10
        self.mixin._append_mode_instructions(
            _FakeEvent(message=long_message),
            prompt_envelope=envelope,
            is_tool_call_mode=True,
            is_all_mode=True,
            is_fast_mode=True,
        )

        self.assertLessEqual(
            len(envelope.planner_runtime_instruction_block),
            self.side_inputs_mod.PlannerSideInputMixin.PLANNER_RUNTIME_INSTRUCTION_MAX_CHARS,
        )
        self.assertNotIn(long_message, envelope.planner_runtime_instruction_block)

    def test_private_jump_context_clamps_long_history_and_message(self):
        long_private_message = "晚上再接着聊呀" * 30
        long_group_history = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "群聊内容" * 30}],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "回复内容" * 30}],
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "更早的群聊内容" * 30}],
            },
        ]

        class _ConversationManager:
            async def get_curr_conversation_id(self, uid):
                return "conv-1"

            async def get_conversation(self, uid, curr_cid):
                return SimpleNamespace(history=json.dumps(long_group_history, ensure_ascii=False))

        ctx = type(
            "Ctx",
            (),
            {
                "shared_dict": {
                    "astrmai_space_jumps": {
                        "user-1": {
                            "timestamp": time.time(),
                            "private_message": long_private_message,
                            "group_id": "group-1",
                        }
                    }
                },
                "conversation_manager": _ConversationManager(),
            },
        )()

        event = _FakeEvent(group_id=None)
        envelope = self.side_inputs_mod.PromptEnvelope()
        event.set_extra("astrmai_prompt_envelope", envelope)
        asyncio.run(
            self.mixin._apply_private_jump_context(
                ctx,
                event,
                "user-1",
                prompt_envelope=envelope,
            )
        )

        self.assertLessEqual(
            len(envelope.planner_runtime_instruction_block),
            self.side_inputs_mod.PlannerSideInputMixin.PLANNER_RUNTIME_INSTRUCTION_MAX_CHARS,
        )
        self.assertLessEqual(
            envelope.planner_runtime_instruction_block.count("[群友]:") + envelope.planner_runtime_instruction_block.count("[你]:"),
            self.side_inputs_mod.PlannerSideInputMixin.PRIVATE_JUMP_MAX_HISTORY_MESSAGES,
        )

    def test_private_jump_context_bridges_relay_without_mixing_people(self):
        ctx = SimpleNamespace(
            shared_dict={
                "astrmai_space_jumps": {
                    "recipient-1": {
                        "timestamp": time.time(),
                        "source_umo": "default:FriendMessage:origin-1",
                        "source_sender_name": "Alice",
                        "private_message": "Alice让我转告你：明天十点见",
                        "context_summary": "Alice 在另一个私聊中委托我通知 Bob 见面时间。",
                        "delivery_mode": "relay",
                    }
                }
            }
        )
        envelope = self.side_inputs_mod.PromptEnvelope()

        asyncio.run(
            self.mixin._apply_private_jump_context(
                ctx,
                _FakeEvent(group_id=None),
                "recipient-1",
                prompt_envelope=envelope,
            )
        )

        block = envelope.planner_runtime_instruction_block
        self.assertIn("另一个私聊", block)
        self.assertIn("【发起人】：Alice", block)
        self.assertIn("【跨会话摘要】", block)
        self.assertIn("当前发消息给我的人是收件人", block)
        self.assertIn("不要把三者混为一人", block)
        self.assertEqual(ctx.shared_dict["astrmai_space_jumps"], {})

    def test_private_jump_context_uses_runtime_store_and_keeps_short_continuation(self):
        store_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.cross_session_handoff_store"
        )
        store = store_mod.CrossSessionHandoffStore()
        self.mixin.cross_session_handoff_store = store
        event = _FakeEvent(group_id=None)
        event.unified_msg_origin = "default:FriendMessage:1481314186"

        async def _run():
            await store.put(
                store_mod.CrossSessionHandoff(
                    platform_id="default",
                    source_umo="default:FriendMessage:516779421",
                    source_sender_id="516779421",
                    source_sender_name="恸",
                    target_umo=event.unified_msg_origin,
                    target_id="1481314186",
                    target_name="萤",
                    outbound_message="恸让我转告你：和妃爱聊天开不开心？",
                    context_summary="恸委托我询问萤的聊天感受。",
                    delivery_mode="relay",
                )
            )
            blocks = []
            for _ in range(3):
                envelope = self.side_inputs_mod.PromptEnvelope()
                await self.mixin._apply_private_jump_context(
                    SimpleNamespace(),
                    event,
                    "1481314186",
                    prompt_envelope=envelope,
                )
                blocks.append(envelope.planner_runtime_instruction_block)
            remaining = await store.peek_for_recipient("default", "1481314186")
            return blocks, remaining

        blocks, remaining = asyncio.run(_run())

        self.assertIn("【发起人】：恸（QQ：516779421）", blocks[0])
        self.assertIn("当前发消息给我的人是收件人", blocks[1])
        self.assertIn("已经发给当前对方的消息", blocks[2])
        self.assertIsNone(remaining)

    def test_private_jump_context_falls_back_when_runtime_store_is_unavailable(self):
        class _BrokenStore:
            async def peek_for_recipient(self, platform_id, target_id):
                raise RuntimeError("store unavailable")

        self.mixin.cross_session_handoff_store = _BrokenStore()
        ctx = SimpleNamespace(
            shared_dict={
                "astrmai_space_jumps": {
                    "recipient-1": {
                        "timestamp": time.time(),
                        "private_message": "旧兼容桥仍可续接",
                    }
                }
            }
        )
        event = _FakeEvent(group_id=None)
        event.unified_msg_origin = "default:FriendMessage:recipient-1"
        envelope = self.side_inputs_mod.PromptEnvelope()

        asyncio.run(
            self.mixin._apply_private_jump_context(
                ctx,
                event,
                "recipient-1",
                prompt_envelope=envelope,
            )
        )

        self.assertIn("旧兼容桥仍可续接", envelope.planner_runtime_instruction_block)
        self.assertEqual(ctx.shared_dict["astrmai_space_jumps"], {})

    def test_cross_session_relay_intent_covers_natural_commands_without_false_positives(self):
        positives = [
            "帮我给1481314186发个消息，问他吃饭了吗",
            "去问问你好友516779421吃饭了没有",
            "替我问一下小明明天来不来",
            "跟萤说一声我晚点到",
            "请联系我的朋友",
        ]
        negatives = [
            "我朋友给我发消息了",
            "你觉得发消息好吗",
            "我去问问他",
            "请你告诉我天气",
            "不要帮我给他发消息",
            "帮我给他不要发消息",
            "给他别发消息",
            "请不要告诉他",
            "我不想让你联系他",
            "不要替我问他",
        ]

        for message in positives:
            with self.subTest(message=message):
                self.assertTrue(
                    self.mixin._looks_like_cross_session_relay_request(message)
                )
        for message in negatives:
            with self.subTest(message=message):
                self.assertFalse(
                    self.mixin._looks_like_cross_session_relay_request(message)
                )

    def _prepare_tool_mixin(self):
        mixin = self.side_inputs_mod.PlannerSideInputMixin()
        mixin.gateway = SimpleNamespace(
            config=SimpleNamespace(
                persona=SimpleNamespace(persona_id="persona-1"),
                reply=SimpleNamespace(follow_up_probability=0.20),
            )
        )
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
            _Tool("message_emoji_like_action"),
            _Tool("proactive_like_action"),
        ]
        chat_exhausted = modifier.modify_tools(
            chat_tools,
            state=SimpleNamespace(energy=0.05),
            tool_tier="chat",
        )
        self.assertEqual(
            [tool.name for tool in chat_exhausted],
            ["message_reaction_action", "message_emoji_like_action", "proactive_like_action"],
        )

        chat_hostile = modifier.modify_tools(
            chat_tools,
            profile=SimpleNamespace(social_score=-30),
            tool_tier="chat",
        )
        self.assertEqual([tool.name for tool in chat_hostile], ["message_reaction_action", "message_emoji_like_action"])

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
        self.assertEqual([tool.name for tool in cooldown], ["message_reaction_action", "message_emoji_like_action"])

        sharp_cooldown = modifier.modify_tools(
            chat_tools,
            state=SimpleNamespace(energy=0.8, mood=0.0),
            tool_tier="chat",
            cooldown_tags=["sharp_reply"],
        )
        self.assertEqual([tool.name for tool in sharp_cooldown], ["message_reaction_action", "message_emoji_like_action"])

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
        self.assertEqual([tool.name for tool in traced], ["message_reaction_action", "message_emoji_like_action", "proactive_like_action"])
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
            [_Tool("proactive_poke"), _Tool("construct_at_event"), _Tool("message_reaction_action"), _Tool("message_emoji_like_action")],
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
        self.assertEqual([tool.name for tool in returned_tools], ["message_reaction_action", "message_emoji_like_action"])
        self.assertIs(returned_trace, cooldown_trace)
        self.assertEqual(set(cooldown_trace.removed_by_cooldown), {"proactive_meme", "proactive_like_action"})

    def test_guarded_stance_filters_proactive_social_tools_and_records_trace(self):
        modifier = self.expression_mod.ActionModifier()
        tools = [
            _Tool("proactive_meme"),
            _Tool("proactive_poke"),
            _Tool("construct_at_event"),
            _Tool("message_reaction_action"),
            _Tool("omni_perception_query"),
        ]
        trace = self.side_inputs_mod.ToolDecisionTrace() if hasattr(self.side_inputs_mod, "ToolDecisionTrace") else None
        if trace is None:
            from astrmai.conversation.contracts.turn_context import ToolDecisionTrace

            trace = ToolDecisionTrace()

        filtered = modifier.modify_tools(
            tools,
            state=SimpleNamespace(energy=0.8, mood=0.0, caution=0.0),
            tool_tier="chat",
            stance="guarded",
            trace=trace,
        )

        self.assertEqual(
            [tool.name for tool in filtered],
            ["message_reaction_action", "omni_perception_query"],
        )
        self.assertEqual(
            trace.removed_by_stance,
            ["proactive_meme", "proactive_poke", "construct_at_event"],
        )
        self.assertTrue(any(step["stage"] == "action_modifier.stance" for step in trace.filter_steps))
        self.assertIn("stance_guarded_guard", trace.filter_reasons)

    def test_guarded_stance_records_removed_tools_in_dict_trace_fallback(self):
        modifier = self.expression_mod.ActionModifier()
        tools = [
            _Tool("proactive_meme"),
            _Tool("proactive_poke"),
            _Tool("message_reaction_action"),
        ]
        trace = {}

        filtered = modifier.modify_tools(
            tools,
            state=SimpleNamespace(energy=0.8, mood=0.0, caution=0.0),
            tool_tier="chat",
            stance="guarded",
            trace=trace,
        )

        self.assertEqual([tool.name for tool in filtered], ["message_reaction_action"])
        self.assertEqual(trace["removed_by_stance"], ["proactive_meme", "proactive_poke"])
        self.assertEqual(trace["filter_steps"][0]["category"], "stance")

    def test_low_trust_filters_real_intrusive_tools(self):
        modifier = self.expression_mod.ActionModifier()
        relationship_vec = SimpleNamespace(social_score=50, trust=-20.0)
        tools = [
            _Tool("proactive_poke"),
            _Tool("construct_at_event"),
            _Tool("space_transition_action"),
            _Tool("topic_hijack_action"),
            _Tool("proactive_like_action"),
            _Tool("message_reaction_action"),
            _Tool("message_emoji_like_action"),
        ]
        trace = {}

        filtered = modifier.modify_tools(
            tools,
            relationship_vec=relationship_vec,
            tool_tier="chat",
            trace=trace,
        )

        self.assertEqual(
            [tool.name for tool in filtered],
            ["message_reaction_action", "message_emoji_like_action"],
        )
        self.assertIn("low_trust(-20)", trace["filter_reasons"])
        self.assertIn("proactive_poke", trace["removed_by_hostility"])

    def test_guarded_stance_scales_follow_up_probability(self):
        mixin = self._prepare_tool_mixin()
        mixin.gateway.config.reply.follow_up_probability = 1.0
        mixin.state_engine = _FakeStateEngine(energy=0.8)
        event = _FakeEvent(message="能再帮我看一下吗")
        event.set_extra("astrmai_think_level", 1)
        event.set_extra("astrmai_focus_reason", "direct_reply")
        event.set_extra("astrmai_reply_need", "reply")
        event.set_extra("astrmai_action_tier", "chat")
        event.set_extra("astrmai_social_intent", "answer")
        event.set_extra("astrmai_stance", "guarded")

        calls = {}

        async def _llm(*args, **kwargs):
            calls["called"] = True
            return {"follow": True, "reason": "extra_detail"}

        mixin.gateway.call_data_process_task = _llm
        original_random = self.side_inputs_mod.random.random
        self.side_inputs_mod.random.random = lambda: 0.30
        try:
            result = asyncio.run(
                mixin._should_follow_up(
                    "default:GroupMessage:group-1",
                    "我先把关键点拎出来再一起看",
                    event=event,
                    tools=None,
                    decision=None,
                )
            )
        finally:
            self.side_inputs_mod.random.random = original_random

        self.assertIsNone(result)
        self.assertNotIn("called", calls)
        trace = event.get_extra("astrmai_turn_context").follow_up
        self.assertIn("stance_guarded", trace.signals)
        self.assertIn("follow_up_probability_scaled:0.35", trace.signals)
        self.assertEqual(trace.skipped_reason, "probability_gate")

    def test_post_reply_feedback_event_cancels_followup_delay(self):
        event = _FakeEvent()
        feedback_event = asyncio.Event()
        feedback_event.set()
        event.set_extra("astrmai_post_reply_feedback_event", feedback_event)

        cancelled = asyncio.run(
            self.mixin._wait_for_post_reply_feedback(event, 60.0)
        )

        self.assertTrue(cancelled)

    def test_followup_delay_runs_normally_without_feedback_event(self):
        event = _FakeEvent()

        cancelled = asyncio.run(
            self.mixin._wait_for_post_reply_feedback(event, 0.0)
        )

        self.assertFalse(cancelled)

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
            CHAT_EXPECTED_TOOL_NAMES,
        )
        self.assertEqual(plain_event.get_extra("astrmai_tool_tier"), "chat")
        plain_turn = plain_event.get_extra("astrmai_turn_context")
        self.assertEqual(plain_turn.tools.final_tier, "chat")
        self.assertEqual(plain_turn.tools.requested_tier, "")
        self.assertFalse(plain_turn.tools.explicit_tool_intent)
        self.assertTrue(plain_turn.tools.disclosure_enabled)
        self.assertEqual(plain_turn.tools.disclosure_packages, ["core", "default_actions"])
        self.assertEqual(
            set(plain_turn.tools.available_tools),
            CHAT_EXPECTED_TOOL_NAMES,
        )
        self.assertEqual(
            set(plain_turn.tools.filtered_tools),
            CHAT_EXPECTED_TOOL_NAMES,
        )
        self.assertEqual(plain_event.get_extra("astrmai_required_tools"), [])
        self.assertIn("qq_friend_lookup", plain_turn.tools.hidden_requestable_tools)
        self.assertNotIn("space_transition_action", plain_turn.tools.hidden_requestable_tools)
        self.assertTrue(ctx.shared_dict["disable_rag_injection"])

        legacy_mixin = self._prepare_tool_mixin()
        legacy_mixin.gateway.config.conversation = SimpleNamespace(
            qq_native_tools_enabled=True,
            qq_deferred_action_commit_enabled=True,
            qq_explicit_intent_override_enabled=True,
            explicit_tool_execution_enabled=True,
            autonomous_chat_tools_enabled=True,
            tool_progressive_disclosure_enabled=False,
        )
        legacy_tools = asyncio.run(
            legacy_mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                _FakeEvent(message="你好呀"),
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )
        legacy_names = _normalized_tool_names(legacy_tools)
        self.assertTrue(legacy_names)
        self.assertNotIn("proactive_meme", legacy_names)
        self.assertNotIn("proactive_poke", legacy_names)
        self.assertNotIn("space_transition_action", legacy_names)

        tool_ctx = SimpleNamespace(shared_dict={})
        query_keyword = "查一下"
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
        self.assertEqual(intent_names, CHAT_EXPECTED_TOOL_NAMES)
        self.assertEqual(intent_event.get_extra("astrmai_required_tools"), ["omni_perception_query"])
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

    def test_semantic_identity_query_preselects_and_requires_exact_tool(self):
        mixin = self._prepare_tool_mixin()
        event = _FakeEvent(message="我叫什么名字来着", group_id=None)

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:FriendMessage:user-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        names = _normalized_tool_names(tools)
        turn_tools = event.get_extra("astrmai_turn_context").tools
        self.assertIn("qq_user_identity_lookup", names)
        self.assertNotIn("qq_friend_lookup", names)
        self.assertEqual(event.get_extra("astrmai_required_tools"), ["qq_user_identity_lookup"])
        self.assertEqual(turn_tools.preselected_tools, ["qq_user_identity_lookup"])
        self.assertEqual(turn_tools.disclosure_decisions[-1]["source"], "semantic_query")

    def test_friend_list_request_builds_platform_friend_contract(self):
        mixin = self._prepare_tool_mixin()
        event = _FakeEvent(message="看看你的好友列表", group_id=None)

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:FriendMessage:user-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        tool_names = _normalized_tool_names(tools)
        self.assertIn("qq_friend_lookup", tool_names)
        self.assertIn("bot_capability_lookup", tool_names)
        self.assertNotIn("self_lore_query", tool_names)
        self.assertEqual(event.get_extra("astrmai_required_tools"), ["qq_friend_lookup"])
        plans = event.get_extra("astrmai_tool_invocation_plans")
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["entity_domain"], "platform_friend")
        self.assertEqual(plans[0]["operation"], "list")
        self.assertEqual(plans[0]["target"], "")
        self.assertEqual(plans[0]["prepared_arguments"], {"mode": "list", "target": ""})

    def test_explicit_persona_character_request_uses_self_lore_contract(self):
        mixin = self._prepare_tool_mixin()
        event = _FakeEvent(message="人设中的亚托莉是谁", group_id=None)

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:FriendMessage:user-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        tool_names = _normalized_tool_names(tools)
        self.assertIn("self_lore_query", tool_names)
        self.assertIn("bot_capability_lookup", tool_names)
        self.assertNotIn("qq_friend_lookup", tool_names)
        self.assertEqual(event.get_extra("astrmai_required_tools"), ["self_lore_query"])
        plans = event.get_extra("astrmai_tool_invocation_plans")
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["entity_domain"], "persona_lore")
        self.assertEqual(plans[0]["operation"], "describe")
        self.assertEqual(plans[0]["target"], "亚托莉")

    def test_known_persona_name_disambiguates_natural_acquaintance_question(self):
        mixin = self._prepare_tool_mixin()
        mixin.context_engine.summarizer = SimpleNamespace(
            cache={
                "persona-1": {
                    "summary": "我是和泉妃爱。",
                    "shards": {"relations": "亚托莉是机器人朋友。"},
                }
            }
        )
        event = _FakeEvent(message="你认识亚托莉吗", group_id=None)

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:FriendMessage:user-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        tool_names = _normalized_tool_names(tools)
        self.assertIn("self_lore_query", tool_names)
        self.assertNotIn("qq_friend_lookup", tool_names)
        self.assertEqual(event.get_extra("astrmai_required_tools"), ["self_lore_query"])
        plans = event.get_extra("astrmai_tool_invocation_plans")
        self.assertEqual(plans[0]["entity_domain"], "persona_lore")
        self.assertEqual(plans[0]["target"], "亚托莉")

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
            CHAT_EXPECTED_TOOL_NAMES,
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
        query_keyword = "查一下"
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

    def test_explicit_qq_intent_injects_only_requested_action_family(self):
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
        self.assertNotIn("construct_at_event", tool_names)
        self.assertTrue(CORE_TOOL_NAMES.issubset(tool_names))
        self.assertEqual(event.get_extra("astrmai_tool_tier"), "full")

        at_ctx = SimpleNamespace(shared_dict={})
        at_event = _FakeEvent(message="请艾特 target", group_id="group-1")
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
        self.assertTrue(
            (CORE_TOOL_NAMES | {"vision_message_analyze_tool", "construct_at_event"}).issubset(at_tool_names)
        )
        self.assertNotIn("proactive_meme", at_tool_names)
        self.assertNotIn("proactive_poke", at_tool_names)
        self.assertIn("construct_at_event", at_tool_names)

    def test_natural_call_member_request_requires_native_at_tool(self):
        mixin = self._prepare_tool_mixin()
        event = _FakeEvent(message="帮我把之前和你聊天的萤叫出来", group_id="group-1")
        event.set_extra("astrmai_member_action_purpose", "mention_member")
        event.set_extra("astrmai_member_action_target", "萤")

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        self.assertIn("construct_at_event", _normalized_tool_names(tools))
        self.assertEqual(event.get_extra("astrmai_required_tools"), ["construct_at_event"])
        self.assertEqual(event.get_extra("astrmai_member_action_effective_target"), "萤")
        self.assertEqual(event.get_extra("astrmai_member_action_resolution_source"), "cognitive_confirmation")

    def test_member_action_discussion_does_not_expose_native_at_tool(self):
        mixin = self._prepare_tool_mixin()
        event = _FakeEvent(message="为什么要艾特萤出来", group_id="group-1")
        event.set_extra("astrmai_member_action_purpose", "discuss_member")

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        self.assertNotIn("construct_at_event", _normalized_tool_names(tools))
        self.assertNotIn("construct_at_event", event.get_extra("astrmai_required_tools", []))

    def test_explicit_relay_request_only_exposes_cross_session_tool(self):
        mixin = self._prepare_tool_mixin()
        event = _FakeEvent(message="帮我给小明发消息告诉他明天十点见", group_id=None)

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:FriendMessage:user-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        self.assertEqual(
            _normalized_tool_names(tools),
            CORE_TOOL_NAMES
            | {
                "vision_message_analyze_tool",
                "qq_friend_lookup",
                "qq_user_identity_lookup",
                "qq_recent_contact_lookup",
                "contact_route_suggest_tool",
                "space_transition_action",
            },
        )
        self.assertEqual(event.get_extra("astrmai_required_tools"), ["space_transition_action"])
        self.assertEqual(event.get_extra("astrmai_tool_tier"), "full")

    def test_cross_session_request_missing_message_clarifies_instead_of_required_tool(self):
        mixin = self._prepare_tool_mixin()
        event = _FakeEvent(message="帮我给1481314186发个消息", group_id=None)

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:FriendMessage:user-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        self.assertEqual(tools, [])
        self.assertEqual(event.get_extra("astrmai_required_tools"), [])
        self.assertTrue(event.get_extra("astrmai_tool_clarification_needed"))
        self.assertIn("转达什么内容", event.get_extra("astrmai_tool_clarification_prompt"))
        self.assertIn("private.message", event.get_extra("astrmai_tool_clarification_missing_slots"))
        self.assertEqual(event.get_extra("astrmai_turn_context").tools.invocation_mode, "clarify")

    def test_cross_session_request_missing_target_clarifies_instead_of_guessing_pronoun(self):
        mixin = self._prepare_tool_mixin()
        event = _FakeEvent(message="帮我问问他吃饭了没有", group_id=None)

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:FriendMessage:user-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        self.assertEqual(tools, [])
        self.assertEqual(event.get_extra("astrmai_required_tools"), [])
        self.assertTrue(event.get_extra("astrmai_tool_clarification_needed"))
        self.assertIn("发给谁", event.get_extra("astrmai_tool_clarification_prompt"))
        self.assertIn("private.target_name", event.get_extra("astrmai_tool_clarification_missing_slots"))

    def test_natural_reverse_relay_request_is_required_after_behavior_filter(self):
        mixin = self._prepare_tool_mixin()
        mixin.action_modifier = SimpleNamespace(modify_tools=lambda tools, **kwargs: [])
        event = _FakeEvent(
            message="去问问你好友516779421吃饭了没有",
            group_id=None,
        )

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:FriendMessage:1481314186",
                event,
                "1481314186",
                "萤",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        self.assertEqual(
            _normalized_tool_names(tools),
            CORE_TOOL_NAMES | {"vision_message_analyze_tool", "qq_friend_lookup", "space_transition_action"},
        )
        self.assertEqual(
            event.get_extra("astrmai_required_tools"),
            ["qq_friend_lookup", "space_transition_action"],
        )

    def test_explicit_poke_is_restored_after_action_modifier_filter(self):
        mixin = self._prepare_tool_mixin()
        mixin.action_modifier = SimpleNamespace(modify_tools=lambda tools, **kwargs: [])
        event = _FakeEvent(message="请戳一戳我", group_id="group-1")

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        self.assertEqual(
            _normalized_tool_names(tools),
            CORE_TOOL_NAMES | {"vision_message_analyze_tool", "proactive_poke"},
        )
        trace = event.get_extra("astrmai_turn_context").tools
        self.assertTrue(any(step["stage"] == "planner.default_actions_restore" for step in trace.filter_steps))
        self.assertEqual(trace.invocation_mode, "required")
        self.assertEqual(trace.required_tools, ["proactive_poke"])
        self.assertEqual(event.get_extra("astrmai_required_tools"), ["proactive_poke"])

    def test_disabling_autonomous_chat_tools_keeps_explicit_tools_available(self):
        mixin = self._prepare_tool_mixin()
        mixin.gateway.config.conversation = SimpleNamespace(
            qq_native_tools_enabled=True,
            qq_deferred_action_commit_enabled=True,
            qq_explicit_intent_override_enabled=True,
            explicit_tool_execution_enabled=True,
            autonomous_chat_tools_enabled=False,
        )

        plain_tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                _FakeEvent(message="你好", group_id="group-1"),
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )
        explicit_event = _FakeEvent(message="请戳一戳我", group_id="group-1")
        explicit_tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                explicit_event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        expected = CORE_TOOL_NAMES | {"vision_message_analyze_tool", "proactive_poke"}
        self.assertEqual(_normalized_tool_names(plain_tools), CHAT_EXPECTED_TOOL_NAMES)
        self.assertEqual(_normalized_tool_names(explicit_tools), expected)
        self.assertEqual(explicit_event.get_extra("astrmai_required_tools"), ["proactive_poke"])

    def test_disabling_reliable_explicit_execution_restores_optional_legacy_behavior(self):
        mixin = self._prepare_tool_mixin()
        mixin.gateway.config.conversation = SimpleNamespace(
            qq_native_tools_enabled=True,
            qq_deferred_action_commit_enabled=True,
            qq_explicit_intent_override_enabled=True,
            explicit_tool_execution_enabled=False,
            autonomous_chat_tools_enabled=True,
        )
        event = _FakeEvent(message="请戳一戳我", group_id="group-1")

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        self.assertEqual(
            _normalized_tool_names(tools),
            CORE_TOOL_NAMES | {"vision_message_analyze_tool", "proactive_poke"},
        )
        self.assertEqual(event.get_extra("astrmai_required_tools"), [])
        self.assertEqual(event.get_extra("astrmai_turn_context").tools.invocation_mode, "auto")

    def test_explicit_meme_request_gets_full_tier_and_forced_fallback(self):
        mixin = self._prepare_tool_mixin()
        event = _FakeEvent(message="发一个开学表情包给我", group_id="group-1")

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        self.assertEqual(
            _normalized_tool_names(tools),
            CORE_TOOL_NAMES | {"vision_message_analyze_tool", "proactive_meme"},
        )
        self.assertEqual(event.get_extra("astrmai_tool_tier"), "full")
        self.assertEqual(event.get_extra("astrmai_required_tools"), ["proactive_meme"])

    def test_meme_semantics_cover_topic_image_request(self):
        mixin = self._prepare_tool_mixin()

        self.assertTrue(mixin._looks_like_meme_request("发一个开学表情包给我"))
        self.assertTrue(mixin._looks_like_meme_request("给我发张开心的表情包"))
        self.assertTrue(mixin._looks_like_meme_request("发个能表示开学的图"))
        self.assertFalse(mixin._looks_like_meme_request("帮我看看这张图片"))

    def test_pending_meme_intent_inherits_same_sender_emotion_once(self):
        mixin = self._prepare_tool_mixin()
        first = _FakeEvent(message="发一个开学表情包给我", group_id="group-1")
        second = _FakeEvent(message="开心的", group_id="group-1")

        first_text = mixin._prepare_pending_meme_intent("chat-1", "user-1", first)
        second_text = mixin._prepare_pending_meme_intent("chat-1", "user-1", second)

        self.assertEqual(first_text, "发一个开学表情包给我")
        self.assertIn("发一个开学表情包给我", second_text)
        self.assertIn("开心的", second_text)
        self.assertTrue(second.get_extra("astrmai_pending_meme_intent_inherited"))
        self.assertEqual(second.get_extra("astrmai_meme_intent")["topic"], "开学")
        self.assertEqual(second.get_extra("astrmai_meme_intent")["emotion"], "happy")

        mixin._consume_pending_meme_intent("chat-1", "user-1", second)
        third = _FakeEvent(message="生气的", group_id="group-1")
        self.assertEqual(
            mixin._prepare_pending_meme_intent("chat-1", "user-1", third),
            "生气的",
        )
        self.assertFalse(third.get_extra("astrmai_pending_meme_intent_inherited", False))

    def test_pending_meme_intent_does_not_cross_sender(self):
        mixin = self._prepare_tool_mixin()
        first = _FakeEvent(message="发一个开学表情包给我", group_id="group-1")
        other = _FakeEvent(message="开心的", group_id="group-1")

        mixin._prepare_pending_meme_intent("chat-1", "user-1", first)

        self.assertEqual(
            mixin._prepare_pending_meme_intent("chat-1", "user-2", other),
            "开心的",
        )
        self.assertFalse(other.get_extra("astrmai_pending_meme_intent_inherited", False))

    def test_pending_meme_intent_expires_after_ttl(self):
        mixin = self._prepare_tool_mixin()
        first = _FakeEvent(message="发一个开学表情包给我", group_id="group-1")
        supplement = _FakeEvent(message="开心的", group_id="group-1")

        mixin._prepare_pending_meme_intent("chat-1", "user-1", first)
        mixin._pending_explicit_meme_intents[("chat-1", "user-1")]["expires_at"] = time.monotonic() - 1

        self.assertEqual(
            mixin._prepare_pending_meme_intent("chat-1", "user-1", supplement),
            "开心的",
        )
        self.assertFalse(supplement.get_extra("astrmai_pending_meme_intent_inherited", False))

    def test_unsupported_adapter_hides_qq_native_default_actions(self):
        mixin = self._prepare_tool_mixin()
        event = _FakeEvent(message="你好", group_id="group-1")
        event.bot = SimpleNamespace(api=object())

        tools = asyncio.run(
            mixin._build_execution_tools(
                "other:GroupMessage:group-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        names = _normalized_tool_names(tools)
        self.assertTrue(names.isdisjoint(mixin.QQ_NATIVE_TOOL_NAMES))
        self.assertNotIn("proactive_meme", names)
        self.assertIn("vision_message_analyze_tool", names)

    def test_explicit_native_action_on_unsupported_adapter_degrades_before_model_tool_choice(self):
        mixin = self._prepare_tool_mixin()
        event = _FakeEvent(message="戳我一下", group_id="group-1")
        event.bot = SimpleNamespace(api=object())

        tools = asyncio.run(
            mixin._build_execution_tools(
                "other:GroupMessage:group-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        self.assertEqual(tools, [])
        self.assertTrue(event.get_extra("astrmai_tool_clarification_needed"))
        self.assertIn("当前会话或平台不支持", event.get_extra("astrmai_tool_clarification_prompt"))
        self.assertIn("poke.tool_unavailable", event.get_extra("astrmai_tool_clarification_missing_slots"))

    def test_plain_bot_mention_does_not_become_explicit_at_command(self):
        mixin = self._prepare_tool_mixin()
        event = _FakeEvent(message="@机器人 你好", group_id="group-1")

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        names = _normalized_tool_names(tools)
        self.assertEqual(names, CHAT_EXPECTED_TOOL_NAMES)
        self.assertEqual(event.get_extra("astrmai_tool_tier"), "chat")
        self.assertEqual(event.get_extra("astrmai_required_tools"), [])

    def test_qq_flags_restore_safe_no_native_tool_path(self):
        mixin = self._prepare_tool_mixin()
        mixin.gateway.config.conversation = SimpleNamespace(
            qq_native_tools_enabled=True,
            qq_deferred_action_commit_enabled=False,
            qq_explicit_intent_override_enabled=True,
        )
        event = _FakeEvent(message="你好", group_id="group-1")

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        names = _normalized_tool_names(tools)
        self.assertTrue(names.isdisjoint({
            "proactive_poke",
            "construct_at_event",
            "quote_reply_action",
            "message_emoji_like_action",
            "regret_and_withdraw_action",
        }))
        self.assertNotIn("proactive_meme", names)
        self.assertIn("vision_message_analyze_tool", names)

    def test_explicit_message_reaction_only_injects_native_qq_tool(self):
        mixin = self._prepare_tool_mixin()
        event = _FakeEvent(message="给这条消息加个表情", group_id="group-1")

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=True,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        self.assertEqual(
            _normalized_tool_names(tools),
            CORE_TOOL_NAMES | {"vision_message_analyze_tool", "message_emoji_like_action"},
        )

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
                self.assertEqual(
                    tool_names,
                    CHAT_EXPECTED_TOOL_NAMES,
                )
                self.assertFalse(event.get_extra("astrmai_turn_context").tools.explicit_tool_intent)
                self.assertEqual(event.get_extra("astrmai_required_tools"), [])

    def test_plain_bot_mention_does_not_open_at_or_poke_tools(self):
        mixin = self._prepare_tool_mixin()
        event = _FakeEvent(message="@2715245266 你好", group_id="group-1")

        tools = asyncio.run(
            mixin._build_execution_tools(
                "default:GroupMessage:group-1",
                event,
                "user-1",
                "Alice",
                SimpleNamespace(shared_dict={}),
                is_all_mode=False,
                is_fast_mode=False,
                is_tool_call_mode=False,
            )
        )

        names = _normalized_tool_names(tools)
        self.assertFalse(event.get_extra("astrmai_turn_context").tools.explicit_tool_intent)
        self.assertIn("qq_user_identity_lookup", names)
        self.assertIn("qq_group_member_lookup", names)
        self.assertFalse(event.get_extra("astrmai_turn_context").tools.explicit_tool_intent)
        self.assertEqual(event.get_extra("astrmai_required_tools"), [])

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
            CHAT_EXPECTED_TOOL_NAMES,
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

        # OPT-12/TL-02: wait/capability 是披露层保护家族，comfort/tease 等 intent
        # 白名单不得剥除（旧断言锁定的正是"连等待与自检能力一并清空"的缺陷）
        self.assertEqual(
            _normalized_tool_names(comfort_tools),
            CHAT_EXPECTED_TOOL_NAMES | {"message_reaction_action"},
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
            CHAT_EXPECTED_TOOL_NAMES,
        )
        recall_turn = recall_event.get_extra("astrmai_turn_context")
        self.assertTrue(recall_turn.tools.disclosure_enabled)
        self.assertIn("core", recall_turn.tools.disclosure_packages)

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

    def test_load_planning_side_inputs_uses_async_evolution_manager_api(self):
        mixin = self.side_inputs_mod.PlannerSideInputMixin()
        calls = []

        class _EvolutionManager:
            async def get_active_patterns_canonical_async(self, chat_id, limit=5):
                calls.append(("async", chat_id, limit))
                return "async slang context"

            def get_active_patterns(self, chat_id, limit=5):
                raise AssertionError("legacy sync API should not be used")

        class _GoalManager:
            async def analyze_and_update(self, chat_id, recent_messages):
                return "goal context"

        class _ExpressionSelector:
            async def select(self, **kwargs):
                return "expression habits"

        mixin.evolution_manager = _EvolutionManager()
        mixin.goal_manager = _GoalManager()
        mixin.expression_selector = _ExpressionSelector()

        result = asyncio.run(
            mixin._load_planning_side_inputs(
                "chat-1",
                self.side_inputs_mod.PromptEnvelope(),
                ["hello", "this context is long enough to trigger expression selection"],
                is_fast_mode=False,
            )
        )

        self.assertEqual(result["slang_context"], "async slang context")
        self.assertEqual(result["goal_text"], "goal context")
        self.assertEqual(result["expression_habits"], "expression habits")
        self.assertEqual(result["situational_style_cues"], "async slang context")
        self.assertEqual(calls, [("async", "chat-1", 5)])


if __name__ == "__main__":
    unittest.main()
