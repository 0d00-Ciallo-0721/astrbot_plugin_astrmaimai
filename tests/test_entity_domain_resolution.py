from astrmai.conversation.planning.entity_domain_resolution import (
    EntityDomain,
    ToolOperation,
    build_tool_intent_contracts,
    resolve_entity_domain,
)


AVAILABLE_TOOLS = {
    "qq_friend_lookup",
    "self_lore_query",
    "omni_perception_query",
}


def test_friend_list_request_builds_platform_list_contract():
    domain, operation, target, reason = resolve_entity_domain(
        "看看你的好友列表",
        explicit_families={"friend_fact"},
    )
    contracts = build_tool_intent_contracts(
        {"friend_fact"},
        message="看看你的好友列表",
        available_tool_names=AVAILABLE_TOOLS,
    )

    assert domain is EntityDomain.PLATFORM_FRIEND
    assert operation is ToolOperation.LIST
    assert target == ""
    assert reason == "platform_friend_marker"
    assert len(contracts) == 1
    assert contracts[0].required_tool == "qq_friend_lookup"
    assert contracts[0].entity_domain == "platform_friend"
    assert contracts[0].operation == "list"
    assert contracts[0].prepared_arguments == {"mode": "list", "target": ""}
    assert contracts[0].acceptable_statuses == ("success",)


def test_friend_count_request_builds_count_contract():
    contracts = build_tool_intent_contracts(
        {"friend_fact"},
        message="你的好友有多少",
        available_tool_names=AVAILABLE_TOOLS,
    )

    assert contracts[0].operation == "count"
    assert contracts[0].target == ""
    assert contracts[0].prepared_arguments == {"mode": "count", "target": ""}


def test_explicit_persona_marker_wins_over_friend_family():
    contracts = build_tool_intent_contracts(
        {"friend_fact", "query"},
        message="人设中的亚托莉是谁",
        available_tool_names=AVAILABLE_TOOLS,
        persona_text="亚托莉是妃爱的机器人朋友。",
    )

    by_family = {contract.family: contract for contract in contracts}
    assert "friend_fact" not in by_family
    assert by_family["self_lore"].required_tool == "self_lore_query"
    assert by_family["self_lore"].entity_domain == "persona_lore"
    assert by_family["self_lore"].target == "亚托莉"


def test_known_persona_name_uses_self_lore_instead_of_real_memory():
    domain, operation, target, reason = resolve_entity_domain(
        "你认识亚托莉吗",
        persona_text="朋友：亚托莉、小锦、乃乃花。",
    )

    assert domain is EntityDomain.PERSONA_LORE
    assert operation is ToolOperation.DESCRIBE
    assert target == "亚托莉"
    assert reason == "persona_catalog_match"


def test_unknown_known_person_stays_conversation_person():
    domain, operation, target, reason = resolve_entity_domain(
        "你认识小明吗",
        persona_text="朋友：亚托莉、小锦。",
    )

    assert domain is EntityDomain.CONVERSATION_PERSON
    assert operation is ToolOperation.DESCRIBE
    assert target == "小明"
    assert reason == "conversation_person_reference"


def test_named_friend_match_extracts_clean_target():
    contracts = build_tool_intent_contracts(
        {"friend_fact"},
        message="查看好友萤是不是你的好友",
        available_tool_names=AVAILABLE_TOOLS,
    )

    assert contracts[0].operation == "match"
    assert contracts[0].target == "萤"
    assert contracts[0].prepared_arguments == {"mode": "match", "target": "萤"}
    assert contracts[0].acceptable_statuses == ("success", "not_found")
