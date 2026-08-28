from astrmai.conversation.planning.tool_contracts import (
    TOOL_CAPABILITIES,
    TOOL_DISPLAY_NAMES,
    ToolCapabilitySpec,
    is_model_disclosure_requestable,
    is_autonomous_interaction,
    requires_explicit_authorization,
)
from astrmai.conversation.planning.tool_disclosure import (
    DEFAULT_VISIBLE_TOOL_NAMES,
    ToolDisclosurePlanner,
)
from astrmai.conversation.planning.tool_contracts import requires_explicit_authorization


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
    "message_emoji_reaction_action",
    "proactive_meme",
    "meme_resonance_action",
    "proactive_like_action",
    "topic_hijack_action",
}


def test_tool_catalog_has_core_defaults_and_on_demand_tools():
    registered = set(TOOL_CAPABILITIES)
    defaults = set(DEFAULT_VISIBLE_TOOL_NAMES)

    assert len(registered) == 32
    assert defaults == CORE_TOOLS | DEFAULT_ACTION_TOOLS
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


def test_pfc_policy_gate_reads_current_capability_registry():
    expected = {
        name
        for name, spec in TOOL_CAPABILITIES.items()
        if spec.requires_explicit_authorization
    }

    assert {name for name in TOOL_CAPABILITIES if requires_explicit_authorization(name)} == expected


def test_autonomous_interaction_capabilities_are_live_and_canonical():
    assert is_autonomous_interaction("message_emoji_like_action")
    assert is_autonomous_interaction("message_emoji_reaction_action")
    assert is_autonomous_interaction("proactive_like_action")
    assert is_autonomous_interaction("topic_hijack_action")


def test_authorization_helper_observes_runtime_registry_changes():
    name = "_test_dynamic_authorized_tool"
    original = TOOL_CAPABILITIES.get(name)
    try:
        TOOL_CAPABILITIES[name] = ToolCapabilitySpec(
            name,
            "test",
            "message",
            requires_explicit_authorization=True,
        )
        assert requires_explicit_authorization(name)
        TOOL_CAPABILITIES[name] = ToolCapabilitySpec(name, "test", "message")
        assert not requires_explicit_authorization(name)
    finally:
        if original is None:
            TOOL_CAPABILITIES.pop(name, None)
        else:
            TOOL_CAPABILITIES[name] = original


def test_plain_chat_discloses_core_package_only():
    plan = ToolDisclosurePlanner().plan(
        message="你好呀",
        requested_tier="",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
    )

    assert plan.packages == ("core", "default_actions")
    assert CORE_TOOLS | DEFAULT_ACTION_TOOLS <= set(plan.tool_names)
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


def test_image_context_adds_read_only_vision_package():
    plan = ToolDisclosurePlanner().plan(
        message="看这个",
        requested_tier="",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
        has_image=True,
    )

    assert plan.packages == ("core", "default_actions", "default_vision", "artifact")
    assert "vision_message_analyze_tool" in plan.tool_names
    assert CORE_TOOLS | DEFAULT_ACTION_TOOLS <= set(plan.tool_names)


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


def test_non_explicit_package_signal_stays_relationship_only():
    plan = ToolDisclosurePlanner().plan(
        message="我刚才发消息给朋友了",
        requested_tier="",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
    )

    assert "cross_session" not in plan.packages
    assert "relationship" in plan.packages
    assert "space_transition_action" not in plan.tool_names
    assert "qq_recent_contact_lookup" in plan.tool_names


def test_negated_relay_does_not_open_cross_session_package():
    plan = ToolDisclosurePlanner().plan(
        message="不要帮我给他发消息",
        requested_tier="",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
    )

    assert "cross_session" not in plan.packages
    assert "space_transition_action" not in plan.tool_names


def test_explicit_relay_family_opens_cross_session_package():
    plan = ToolDisclosurePlanner().plan(
        message="帮我给他发消息",
        requested_tier="",
        explicit_tool_intent=True,
        explicit_tool_families={"private"},
    )

    assert "cross_session" in plan.packages
    assert "space_transition_action" in plan.tool_names


def test_raw_fun_and_control_words_do_not_open_side_effect_packages():
    meme_plan = ToolDisclosurePlanner().plan(
        message="来个表情包",
        requested_tier="",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
    )
    control_plan = ToolDisclosurePlanner().plan(
        message="换个话题",
        requested_tier="",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
    )

    assert "fun" not in meme_plan.packages
    assert "conversation_control" not in control_plan.packages


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
    assert "proactive_like_action" in plan.tool_names
    assert "proactive_poke" in plan.tool_names
