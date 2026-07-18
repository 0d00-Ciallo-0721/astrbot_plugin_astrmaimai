from astrmai.conversation.planning.member_action_intent import detect_member_action_candidate


def test_detects_natural_call_member_requests_without_losing_target():
    cases = {
        "再帮我叫一下亚托莉": "亚托莉",
        "帮我叫一下6": "6",
        "帮我把之前和你聊天的萤叫出来": "萤",
        "让萤出来冒个泡": "萤",
        "请艾特3650815443": "3650815443",
    }
    for message, target in cases.items():
        candidate = detect_member_action_candidate(message)
        assert candidate is not None
        assert candidate.proposed_purpose == "mention_member"
        assert candidate.target_name == target


def test_short_entity_query_is_lookup_not_mention():
    candidate = detect_member_action_candidate("萤现在在当前群里吗？")
    assert candidate is not None
    assert candidate.proposed_purpose == "lookup_member"
    assert candidate.target_name == "萤"


def test_discussion_and_identity_phrases_do_not_become_call_actions():
    for message in ("为什么要叫萤出来", "别叫萤", "我叫小明", "群里真的有人叫6"):
        candidate = detect_member_action_candidate(message)
        assert candidate is None or candidate.proposed_purpose != "mention_member"
