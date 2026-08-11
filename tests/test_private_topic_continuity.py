from types import SimpleNamespace

from astrmai.conversation.planning.conversation_continuity import ConversationContinuityStore


def _store():
    store = ConversationContinuityStore()
    store.refresh_config(
        SimpleNamespace(
            private_chat=SimpleNamespace(
                topic_continuity_enabled=True,
                topic_active_ttl_sec=900,
                topic_confirm_after_sec=1800,
                topic_confirmation_wait_sec=120,
                topic_summary_max_chars=300,
            )
        )
    )
    return store


def _record_topic(store, *, chat_id="private-1", now=1000.0, topic="一起去泡温泉的计划"):
    store.record(
        chat_id=chat_id,
        focus_preview=topic,
        goal_summary="确认温泉行程",
        social_intent="answer",
        action_taken="reply",
        now=now,
    )


def test_private_topic_stays_active_for_followup_within_15_minutes():
    store = _store()
    _record_topic(store)

    decision = store.evaluate_private_message("private-1", "然后呢", now=1000.0 + 899)

    assert decision["action"] == "continue"
    assert decision["status"] == "active"
    assert decision["inherited"] is True
    assert "一起去泡温泉的计划" in decision["prompt_summary"]


def test_private_short_name_ping_inherits_but_unrelated_short_question_starts_new_topic():
    store = _store()
    _record_topic(store)

    assert store.evaluate_private_message("private-1", "妃爱？", now=1100)["action"] == "continue"
    new_decision = store.evaluate_private_message("private-1", "你吃饭了吗", now=1200)

    assert new_decision["action"] == "new"
    assert new_decision["inherited"] is False


def test_private_topic_after_30_minutes_requires_confirmation_before_reply():
    store = _store()
    _record_topic(store)

    decision = store.evaluate_private_message("private-1", "那温泉后来怎么样了", now=1000.0 + 1801)

    assert decision["action"] == "confirm"
    assert decision["requires_confirmation"] is True
    assert "继续这个话题" in decision["confirmation_text"] or "接着聊" in decision["confirmation_text"]


def test_private_stale_topic_ignores_punctuation_and_emoji_only_inputs():
    store = _store()
    _record_topic(store)

    for text in (".", "。", "？", "...", "🥺"):
        decision = store.evaluate_private_message("private-1", text, now=1000.0 + 1801)
        assert decision["action"] == "new"
        assert decision["requires_confirmation"] is False


def test_private_stale_topic_still_confirms_explicit_old_topic_reference():
    store = _store()
    _record_topic(store)

    decision = store.evaluate_private_message("private-1", "刚才那个温泉计划呢", now=1000.0 + 1801)

    assert decision["action"] == "confirm"
    assert decision["requires_confirmation"] is True


def test_internal_event_envelope_is_never_committed_as_private_topic():
    store = _store()
    internal = (
        "[事件=1727617753 | 发言人=恸（ID:516779421） | 角色=成员 | "
        "类型=image | 来源=original | 媒体=图片:1]\n"
        "内容：[表情包转述：一个金发双马尾女孩]"
    )

    store.record(
        chat_id="private-1",
        focus_preview=internal,
        goal_summary="回应图片",
        social_intent="answer",
        action_taken="reply",
        now=1000.0,
    )

    snapshot = store.snapshot("private-1", now=1001.0)
    assert snapshot["current_topic"] == ""
    assert snapshot["last_goal_update_ts"] == 0.0


def test_private_confirmation_yes_resumes_old_topic_and_no_starts_new_topic():
    store = _store()
    _record_topic(store)
    store.evaluate_private_message("private-1", "然后呢", now=2801.0)

    yes = store.evaluate_private_message("private-1", "继续", now=2850.0)
    assert yes["action"] == "continue"
    assert yes["status"] == "confirmed"
    assert yes["inherited"] is True

    store.evaluate_private_message("private-1", "然后呢", now=5000.0)
    no = store.evaluate_private_message("private-1", "换个话题", now=5010.0)
    assert no["action"] == "new"
    assert no["inherited"] is False


def test_private_confirmation_does_not_leak_between_chats():
    store = _store()
    _record_topic(store, chat_id="private-1")
    store.evaluate_private_message("private-1", "然后呢", now=2801.0)

    other = store.evaluate_private_message("private-2", "然后呢", now=2802.0)

    assert other["action"] == "new"
    assert other["requires_confirmation"] is False


def test_private_topic_confirmation_state_expires_and_requires_a_fresh_confirmation():
    store = _store()
    _record_topic(store)
    store.evaluate_private_message("private-1", "然后呢", now=2801.0)

    after_wait = store.evaluate_private_message("private-1", "继续", now=2922.0)

    assert after_wait["action"] == "confirm"
    assert after_wait["requires_confirmation"] is True


def test_private_topic_can_be_disabled_without_changing_legacy_path():
    store = _store()
    _record_topic(store)
    store.refresh_config(
        SimpleNamespace(
            private_chat=SimpleNamespace(
                topic_continuity_enabled=False,
                topic_active_ttl_sec=900,
                topic_confirm_after_sec=1800,
                topic_confirmation_wait_sec=120,
                topic_summary_max_chars=300,
            )
        )
    )

    decision = store.evaluate_private_message("private-1", "然后呢", now=2801.0)

    assert decision["action"] == "new"
    assert decision["requires_confirmation"] is False


def test_default_config_exposes_private_topic_controls():
    from config import AstrMaiConfig

    config = AstrMaiConfig()

    assert config.private_chat.topic_continuity_enabled is True
    assert config.private_chat.topic_active_ttl_sec == 900
    assert config.private_chat.topic_confirm_after_sec == 1800
    assert config.private_chat.topic_confirmation_wait_sec == 120
    assert config.private_chat.topic_summary_max_chars == 300
