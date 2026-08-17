import asyncio
import importlib
import sys
import tempfile
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _Event:
    def __init__(self, message, *, group_id=None, sender_id="123", self_id="999", message_id="456"):
        self.message_str = message
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self._extra = {}
        self.message_obj = SimpleNamespace(message_id=message_id)
        self.bot = SimpleNamespace(api=None)

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id

    def get_sender_name(self):
        return "Alice"

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


def _load_modules():
    temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    install_astrbot_stubs(temp_dir.name)
    for name in (
        "astrmai.conversation.planning.tool_contracts",
        "astrmai.conversation.planning.tools.pfc_tools",
    ):
        sys.modules.pop(name, None)
    contracts = importlib.import_module("astrmai.conversation.planning.tool_contracts")
    tools = importlib.import_module("astrmai.conversation.planning.tools.pfc_tools")
    return temp_dir, contracts, tools


def test_all_chat_tools_have_registered_strict_schemas():
    temp_dir, contracts, tools_mod = _load_modules()
    try:
        tools = [
            tools_mod.WaitTool(),
            tools_mod.OmniPerceptionTool(),
            tools_mod.SelfLoreQueryTool(),
            tools_mod.QQFriendLookupTool(),
            tools_mod.QQGroupMemberLookupTool(),
            tools_mod.QQUserIdentityLookupTool(),
            tools_mod.QQForwardMessageLookupTool(),
            tools_mod.QQGroupPresenceLookupTool(),
            tools_mod.QQRecentContactLookupTool(),
            tools_mod.QQMessageArtifactLookupTool(),
            tools_mod.VisionMessageAnalyzeTool(),
            tools_mod.CrossSessionReplyLookupTool(),
            tools_mod.QuoteReplyActionTool(),
            tools_mod.QQMessageRecallLookupTool(),
            tools_mod.TopicThreadLookupTool(),
            tools_mod.BotCapabilityLookupTool(),
            tools_mod.LearnedLanguageLookupTool(),
            tools_mod.MemoryWriteCorrectionTool(),
            tools_mod.UnverifiedReportRecordTool(),
            tools_mod.PersonaFactCheckTool(),
            tools_mod.GroupActivitySnapshotTool(),
            tools_mod.ContactRouteSuggestTool(),
            tools_mod.CrossChatMemoryQueryTool(),
            tools_mod.ConstructAtEventTool(),
            tools_mod.ProactivePokeTool(),
            tools_mod.ProactiveMemeTool(emotion_mapping=["happy: 开心"]),
            tools_mod.MemeResonanceTool(),
            tools_mod.TopicHijackTool(),
            tools_mod.SpaceTransitionTool(),
            tools_mod.RegretAndWithdrawTool(),
            tools_mod.MessageReactionTool(),
            tools_mod.MessageEmojiLikeTool(),
            tools_mod.ProactiveLikeTool(),
        ]
        normalized = contracts.normalize_tool_schemas(tools)

        assert {tool.name for tool in normalized} == set(contracts.TOOL_CAPABILITIES)
        assert all(tool.parameters["type"] == "object" for tool in normalized)
        assert all(tool.parameters["additionalProperties"] is False for tool in normalized)
        assert all(isinstance(tool.parameters["properties"], dict) for tool in normalized)
    finally:
        temp_dir.cleanup()


def test_explicit_private_poke_is_prepared_once_without_fake_success():
    temp_dir, _, tools_mod = _load_modules()
    try:
        event = _Event("妃妃你戳一下我")

        first = asyncio.run(tools_mod.prepare_explicit_tool_fallbacks(event, ["proactive_poke"]))
        second = asyncio.run(tools_mod.prepare_explicit_tool_fallbacks(event, ["proactive_poke"]))

        assert first == ["proactive_poke"]
        assert second == []
        assert event.get_extra("astrmai_pending_actions") == [
            {
                "action_type": "poke",
                "target_id": "123",
                "target_name": "Alice",
                "group_id": "",
                "message_id": "",
                "payload": {},
                "requested_at": event.get_extra("astrmai_pending_actions")[0]["requested_at"],
                "action": "poke",
            }
        ]
        assert event.get_extra("astrmai_tool_execution_trace", []) == []
        assert any(
            item["tool"] == "proactive_poke" and item["phase"] == "explicit_fallback_prepared"
            for item in event.get_extra("astrmai_tool_lifecycle_trace")
        )
    finally:
        temp_dir.cleanup()


def test_group_poke_for_named_target_is_not_guessed_as_current_sender():
    temp_dir, _, tools_mod = _load_modules()
    try:
        event = _Event("请戳一下小明", group_id="777")

        queued = asyncio.run(tools_mod.prepare_explicit_tool_fallbacks(event, ["proactive_poke"]))

        assert queued == []
        assert event.get_extra("astrmai_pending_actions", []) == []
    finally:
        temp_dir.cleanup()


def test_private_poke_for_named_target_is_not_guessed_as_current_sender():
    temp_dir, _, tools_mod = _load_modules()
    try:
        event = _Event("请戳一下小明")

        queued = asyncio.run(tools_mod.prepare_explicit_tool_fallbacks(event, ["proactive_poke"]))

        assert queued == []
        assert event.get_extra("astrmai_pending_actions", []) == []
    finally:
        temp_dir.cleanup()


def test_explicit_plan_registry_covers_every_tool_family():
    temp_dir, contracts, _ = _load_modules()
    try:
        plans = contracts.build_explicit_invocation_plans(
            contracts.FAMILY_TO_TOOL,
            contracts.TOOL_CAPABILITIES,
        )

        assert {plan.tool_name for plan in plans} == set(contracts.FAMILY_TO_TOOL.values())
        assert all(plan.required for plan in plans)
    finally:
        temp_dir.cleanup()


def test_context_specific_tools_keep_cross_session_send_in_private_chat():
    temp_dir, contracts, tools_mod = _load_modules()
    try:
        tools = [
            tools_mod.ConstructAtEventTool(),
            tools_mod.SpaceTransitionTool(),
            tools_mod.ProactivePokeTool(),
        ]

        private_tools = contracts.filter_tools_for_context(tools, is_group=False)
        group_tools = contracts.filter_tools_for_context(tools, is_group=True)

        assert {tool.name for tool in private_tools} == {"proactive_poke", "space_transition_action"}
        assert {tool.name for tool in group_tools} == {tool.name for tool in tools}
    finally:
        temp_dir.cleanup()


def test_explicit_meme_fallback_uses_configured_emotion_hint():
    temp_dir, _, tools_mod = _load_modules()
    try:
        event = _Event("给我来个开心的表情包")

        queued = asyncio.run(
            tools_mod.prepare_explicit_tool_fallbacks(
                event,
                ["proactive_meme"],
                emotion_mapping=["happy: 积极、开心、感谢", "neutral: 平静"],
            )
        )

        assert queued == ["proactive_meme"]
        assert event.get_extra("astrmai_bypass_mood_analysis") == "happy"
        assert event.get_extra("astrmai_force_meme") is True
        assert event.get_extra("astrmai_pending_actions")[0]["tag"] == "happy"
    finally:
        temp_dir.cleanup()
