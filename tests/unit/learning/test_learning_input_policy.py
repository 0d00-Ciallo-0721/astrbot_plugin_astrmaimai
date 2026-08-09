from types import SimpleNamespace

from astrmai.learning.mining.candidate_router import LearningCandidateRouter
from astrmai.learning.mining.learning_input_policy import LearningInputPolicy


def _message(content: str, **kwargs):
    return SimpleNamespace(content=content, **kwargs)


def test_learning_input_policy_keeps_only_human_group_text():
    policy = LearningInputPolicy()
    messages = [
        _message("真的好耶的说", sender_id="1", role="user", provenance="original", chat_kind="group"),
        _message("机器人回执", sender_id="bot", is_bot=True),
        _message("插件结果", sender_id="2", provenance="external_plugin"),
        _message("私聊内容", sender_id="3", chat_kind="private"),
    ]

    result = policy.normalize(messages)

    assert [item.content for item in result] == ["真的好耶的说"]
    assert policy.last_report["accepted_messages"] == 1
    assert policy.last_report["rejected_by_reason"] == {
        "bot_message": 1,
        "non_group_scope": 1,
        "non_human_provenance": 1,
    }


def test_learning_input_policy_rejects_legacy_cards_and_does_not_learn_quotes():
    policy = LearningInputPolicy()
    messages = [
        _message("[](%7B%22version%22%3A2%7D) [点击](mqqapi://markdown/mention?id=1)"),
        _message("0.53 复制打开抖音 https://v.douyin.com/abc"),
        _message("> 唉嘿嘿～\n我只是在引用上面那句"),
    ]

    result = policy.normalize(messages)

    assert [item.content for item in result] == ["我只是在引用上面那句"]
    assert policy.last_report["rejected_by_reason"]["transport_payload"] == 1
    assert policy.last_report["rejected_by_reason"]["markdown_card"] == 1


def test_candidate_router_keeps_expression_markers_out_of_jargon():
    assert LearningCandidateRouter.classify("的说").target == "expression"
    assert LearningCandidateRouter.classify("摸摸").target == "reject"
    assert LearningCandidateRouter.classify("特定群内缩写").target == "jargon"
