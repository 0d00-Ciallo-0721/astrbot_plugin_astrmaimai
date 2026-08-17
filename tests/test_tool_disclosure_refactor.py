from astrmai.conversation.planning.tool_contracts import (
    TOOL_CAPABILITIES,
    TOOL_DISPLAY_NAMES,
    is_model_disclosure_requestable,
)
from astrmai.conversation.planning.tool_disclosure import (
    DEFAULT_VISIBLE_TOOL_NAMES,
    ToolDisclosurePlanner,
)


CORE_TOOLS = {
    "wait_and_listen",
    "omni_perception_query",
    "cross_chat_memory_query",
    "bot_capability_lookup",
    "learned_language_lookup",
}

DEFAULT_ACTION_TOOLS = {
    "regret_and_withdraw_action",
    "proactive_poke",
    "construct_at_event",
    "quote_reply_action",
    "message_emoji_like_action",
    "vision_message_analyze_tool",
    "proactive_meme",
}


def test_tool_catalog_has_12_default_and_21_on_demand_tools():
    registered = set(TOOL_CAPABILITIES)
    defaults = set(DEFAULT_VISIBLE_TOOL_NAMES)

    assert len(registered) == 33
    assert len(defaults) == 12
    assert len(registered - defaults) == 21
    assert set(TOOL_DISPLAY_NAMES) == registered
    assert not {
        "group_sign_action",
        "custom_face_catalog_query",
        "qq_custom_face_send_tool",
    } & registered


def test_model_disclosure_only_allows_hidden_readonly_tools():
    requestable = {
        name for name in TOOL_CAPABILITIES if is_model_disclosure_requestable(name)
    }

    assert "qq_friend_lookup" in requestable
    assert "space_transition_action" not in requestable
    assert "memory_write_correction_tool" not in requestable


def test_plain_chat_discloses_core_and_default_actions_packages():
    plan = ToolDisclosurePlanner().plan(
        message="你好呀",
        requested_tier="",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
    )

    assert plan.packages == ("core", "default_actions")
    assert set(plan.tool_names) == CORE_TOOLS | DEFAULT_ACTION_TOOLS
    assert plan.tier == "chat"


def test_plain_chat_keeps_learned_language_lookup_as_core_fallback():
    plan = ToolDisclosurePlanner().plan(
        message="这个词在群里是什么意思",
        requested_tier="",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
    )

    assert plan.packages == ("core", "default_actions")
    assert "learned_language_lookup" in plan.tool_names


def test_plain_recommendation_does_not_disclose_persona_lore_tools():
    plan = ToolDisclosurePlanner().plan(
        message="推荐几个好玩的景点和游乐园",
        requested_tier="",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
    )

    assert "self_lore_query" not in plan.tool_names
    assert "persona_fact_check_tool" not in plan.tool_names
    assert "bot_capability_lookup" in plan.tool_names


def test_explicit_persona_lore_intent_discloses_only_persona_package():
    plan = ToolDisclosurePlanner().plan(
        message="人设中的亚托莉是谁",
        requested_tier="",
        explicit_tool_intent=True,
        explicit_tool_families={"self_lore"},
    )

    assert "persona_lore" in plan.packages
    assert "self_lore_query" in plan.tool_names
    assert "persona_fact_check_tool" in plan.tool_names
    assert "qq_friend_lookup" not in plan.tool_names


def test_cross_session_request_gets_auxiliary_package_without_losing_exact_tool():
    plan = ToolDisclosurePlanner().plan(
        message="帮我给1481314186发消息，问他吃饭没",
        requested_tier="",
        explicit_tool_intent=True,
        explicit_tool_families={"private"},
    )

    assert "core" in plan.packages
    assert "cross_session" in plan.packages
    assert "space_transition_action" in plan.tool_names
    assert "qq_friend_lookup" in plan.tool_names
    assert "contact_route_suggest_tool" in plan.tool_names


def test_non_explicit_package_signal_does_not_expose_cross_session_side_effect():
    plan = ToolDisclosurePlanner().plan(
        message="我刚才发消息给朋友了",
        requested_tier="",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
    )

    assert "cross_session" in plan.packages
    assert "space_transition_action" not in plan.tool_names
    assert "qq_recent_contact_lookup" in plan.tool_names


def test_non_explicit_hearsay_does_not_expose_memory_write_tools():
    plan = ToolDisclosurePlanner().plan(
        message="听说他最近换工作了",
        requested_tier="",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
    )

    assert "memory_governance" in plan.packages
    assert "memory_write_correction_tool" not in plan.tool_names
    assert "unverified_report_record_tool" not in plan.tool_names


def test_explicit_meme_request_keeps_default_actions_without_opening_fun_package():
    plan = ToolDisclosurePlanner().plan(
        message="给我发张开心的表情包",
        requested_tier="",
        explicit_tool_intent=True,
        explicit_tool_families={"meme"},
    )

    assert "proactive_meme" in plan.tool_names
    assert "fun" not in plan.packages
    assert "message_reaction_action" not in plan.tool_names
    assert "proactive_like_action" not in plan.tool_names
