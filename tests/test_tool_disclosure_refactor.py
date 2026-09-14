from astrmai.conversation.planning.tool_contracts import (
    TOOL_CAPABILITIES,
    TOOL_DISPLAY_NAMES,
    ToolCapabilitySpec,
    is_model_disclosure_requestable,
    is_planner_disclosure_requestable,
    is_autonomous_interaction,
    requires_explicit_authorization,
)
from astrmai.conversation.planning.tool_disclosure import (
    DEFAULT_VISIBLE_TOOL_NAMES,
    ToolDisclosurePlanner,
)
from astrmai.conversation.planning.tool_contracts import requires_explicit_authorization
from astrmai.conversation.planning.tool_contracts import TOOL_NAME_ALIASES
from astrmai.conversation.planning.tool_semantics import (
    TOOL_PLANNER_SEMANTICS,
    build_planner_capability_catalog,
    validate_planner_semantics,
)
from astrmai.conversation.planning.tool_intent_resolution import resolve_capability_need


CORE_TOOLS = {
    "wait_and_listen",
    "omni_perception_query",
    "cross_chat_memory_query",
    "bot_capability_lookup",
    "learned_language_lookup",
}


def test_planner_semantics_cover_canonical_registry_without_aliases():
    missing, extra = validate_planner_semantics()
    assert not missing
    assert not extra
    assert not set(TOOL_NAME_ALIASES) & set(TOOL_PLANNER_SEMANTICS)
    assert set(TOOL_PLANNER_SEMANTICS) == set(TOOL_CAPABILITIES)


def test_capability_catalog_is_context_pruned_and_schema_free():
    private_catalog = build_planner_capability_catalog(is_group=False, has_image=False)
    group_catalog = build_planner_capability_catalog(is_group=True, has_image=True)
    assert "construct_at_event" not in private_catalog
    assert "vision_message_analyze_tool" not in private_catalog
    assert "construct_at_event" in group_catalog
    assert "vision_message_analyze_tool" in group_catalog
    assert '"properties"' not in group_catalog
    assert "target=" in group_catalog
    assert "context=" in group_catalog


def test_unverified_report_can_be_planned_but_is_not_default_disclosed():
    default_plan = ToolDisclosurePlanner().plan(
        message="听说他换工作了", requested_tier="", explicit_tool_intent=False, explicit_tool_families=set()
    )
    planned = ToolDisclosurePlanner().plan(
        message="听说他换工作了",
        requested_tier="",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
        planned_tool_families=["unverified_report"],
    )
    assert "unverified_report_record_tool" not in default_plan.tool_names
    assert "unverified_report_record_tool" in planned.tool_names

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


def test_autonomous_planner_private_plan_opens_cross_session_without_explicit_words():
    plan = ToolDisclosurePlanner().plan(
        message="闲聊",
        requested_tier="chat",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
        planned_tool_families=["private"],
    )
    assert "cross_session" in plan.packages
    assert "space_transition_action" in plan.tool_names
    assert any(item.source == "autonomous_planner" for item in plan.decisions)


def test_negated_autonomous_planner_private_plan_is_suppressed():
    plan = ToolDisclosurePlanner().plan(
        message="闲聊",
        requested_tier="chat",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
        planned_tool_families=["private"],
        negated_tool_families=["private"],
    )
    assert "space_transition_action" not in plan.tool_names


def test_suppressed_autonomous_plan_is_not_disclosed():
    plan = ToolDisclosurePlanner().plan(
        message="闲聊",
        requested_tier="chat",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
        planned_tool_families=["private"],
        suppressed_tool_families=["private"],
    )
    assert "space_transition_action" not in plan.tool_names
    assert "cross_session" not in plan.packages


def test_planner_disclosure_requestable_includes_autonomous_hidden_tools():
    assert is_planner_disclosure_requestable("space_transition_action")


def test_negated_family_vetoes_default_action_disclosure():
    plan = ToolDisclosurePlanner().plan(
        message="不要发任何表情包",
        requested_tier="",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
        negated_tool_families=["meme"],
    )
    assert "proactive_meme" not in plan.tool_names
    assert "proactive_poke" in plan.tool_names


def test_production_relay_phrase_resolves_private_capability_before_friend_lookup():
    phrase = "对的对的 你和她发一个消息，说我想看妃爱的cos，你看看能不能发出去"
    resolution = resolve_capability_need(
        phrase,
        available_tool_names=["qq_friend_lookup", "contact_route_suggest_tool", "space_transition_action"],
    )
    assert resolution is not None
    assert resolution.family == "private"
    assert resolution.tool_name == "space_transition_action"
    friend_word_phrase = "给好友发私聊消息，说我想看妃爱的cos"
    friend_word_resolution = resolve_capability_need(
        friend_word_phrase,
        available_tool_names=["qq_friend_lookup", "space_transition_action"],
    )
    assert friend_word_resolution is not None
    assert friend_word_resolution.family == "private"
    assert friend_word_resolution.tool_name == "space_transition_action"


def test_capability_need_does_not_treat_ordinary_questions_as_cross_session_send():
    ordinary = (
        "告诉我这个词什么意思",
        "问一下群里发生了什么",
        "联系上下文回答",
        "询问一下你的设定",
        "帮我问问这是什么意思",
        "查一下好友关系",
    )
    for message in ordinary:
        resolution = resolve_capability_need(
            message,
            available_tool_names=["space_transition_action"],
        )
        assert resolution is None, message


def test_capability_need_requires_explicit_send_language_for_cross_session_action():
    explicit = (
        "给空酱发消息说晚安",
        "私聊空酱说我到了",
        "转告空酱明天见",
        "把这句话发出去",
        "带话给空酱说晚安",
    )
    for message in explicit:
        resolution = resolve_capability_need(
            message,
            available_tool_names=["space_transition_action"],
        )
        assert resolution is not None, message
        assert resolution.tool_name == "space_transition_action"


def test_disclosure_caps_dynamic_tools_without_removing_default_tools():
    plan = ToolDisclosurePlanner().plan(
        message="闲聊",
        requested_tier="chat",
        explicit_tool_intent=False,
        explicit_tool_families=set(),
        planned_tool_families=["private", "friend_fact", "group_fact", "artifact"],
        max_chat_tools=2,
        max_task_tools=2,
    )
    extras = set(plan.tool_names) - set(DEFAULT_VISIBLE_TOOL_NAMES)
    assert len(extras) <= 2
    assert set(DEFAULT_VISIBLE_TOOL_NAMES) <= set(plan.tool_names)
