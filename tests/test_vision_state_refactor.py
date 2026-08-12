from astrmai.conversation.vision_state import (
    classify_autonomous_vision_need,
    guard_unresolved_image_reply,
    select_autonomous_vision_candidate,
    user_asked_about_image,
)


def test_user_asked_about_image_distinguishes_text_topic_from_media_question():
    assert user_asked_about_image("这张图里是什么？") is True
    assert user_asked_about_image("帮我看看刚才那个表情包") is True
    assert user_asked_about_image("中文区是什么猎奇区吗？") is False


def test_autonomous_vision_need_separates_required_optional_and_irrelevant_context():
    recent = [{"message_id": "m-1", "relation": "same_sender_recent", "age_seconds": 8}]

    assert classify_autonomous_vision_need("这是什么意思？", recent) == (
        "required",
        "implicit_visual_reference",
    )
    assert classify_autonomous_vision_need("中文区是什么猎奇区吗？", recent) == (
        "optional",
        "same_sender_recent_question",
    )
    assert classify_autonomous_vision_need("我先去吃饭", recent) == (
        "irrelevant",
        "text_independent",
    )


def test_autonomous_vision_need_forces_explicit_request_and_prioritizes_bound_candidate():
    candidates = [
        {"message_id": "recent", "relation": "recent", "age_seconds": 2},
        {"message_id": "reply", "relation": "reply_target", "age_seconds": 20},
    ]

    assert classify_autonomous_vision_need(
        "帮我看图",
        candidates,
        explicit_request=True,
    ) == ("required", "explicit_image_request")
    assert select_autonomous_vision_candidate(candidates)["message_id"] == "reply"


def test_unresolved_image_reply_drops_loading_clause_when_text_does_not_ask_about_image():
    reply, action, reason = guard_unresolved_image_reply(
        "诶？中文区怎么了嘛？图片我看不到啦，能说说发生了什么吗？",
        user_text="中文区是什么猎奇区吗？",
        has_valid_image_context=False,
        enabled=True,
    )

    assert "看不到" not in reply
    assert "中文区" in reply
    assert action == "repaired"
    assert reason == "unrequested_unresolved_image_claim"


def test_unresolved_image_reply_keeps_concise_failure_when_user_really_asks_about_image():
    original = "这张图我暂时看不到，没法确认里面的人是谁。"

    reply, action, reason = guard_unresolved_image_reply(
        original,
        user_text="这张图里是谁？",
        has_valid_image_context=False,
        enabled=True,
    )

    assert reply == original
    assert action == "allowed"
    assert reason == "explicit_image_question"
