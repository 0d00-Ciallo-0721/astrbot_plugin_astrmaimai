from astrmai.conversation.planning.tool_disclosure import ToolDisclosurePlanner


CORE_TOOLS = {
    "wait_and_listen",
    "omni_perception_query",
    "cross_chat_memory_query",
    "bot_capability_lookup",
    "learned_language_lookup",
}


def test_plain_chat_discloses_core_package_only():
    plan = ToolDisclosurePlanner().plan(
        message="你好呀",
        requested_tier="",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
    )

    assert plan.packages == ("core",)
    assert set(plan.tool_names) == CORE_TOOLS
    assert plan.tier == "chat"


def test_plain_chat_keeps_learned_language_lookup_as_core_fallback():
    plan = ToolDisclosurePlanner().plan(
        message="这个词在群里是什么意思",
        requested_tier="",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
    )

    assert plan.packages == ("core",)
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


def test_explicit_high_side_effect_action_does_not_open_whole_native_package():
    plan = ToolDisclosurePlanner().plan(
        message="请帮当前群签到",
        requested_tier="",
        explicit_tool_intent=True,
        explicit_tool_families={"sign"},
    )

    assert "group_sign_action" in plan.tool_names
    assert "proactive_poke" not in plan.tool_names
    assert "construct_at_event" not in plan.tool_names
