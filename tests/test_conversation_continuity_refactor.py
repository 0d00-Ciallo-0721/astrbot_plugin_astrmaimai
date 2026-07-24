import astrmai.conversation.planning.conversation_continuity as continuity_module

from astrmai.conversation.planning.conversation_continuity import ConversationContinuityStore


def test_conversation_continuity_records_new_goal_state():
    store = ConversationContinuityStore()

    store.record(
        chat_id="chat-1",
        focus_preview="Alice: talk about homework plan",
        goal_summary="help Alice sort the homework plan",
        social_intent="answer",
        action_taken="reply",
        reply_preview="ok",
        now=1000.0,
    )

    snapshot = store.snapshot("chat-1", now=1001.0)
    assert snapshot["current_topic"] == "Alice: talk about homework plan"
    assert snapshot["current_goal"] == "help Alice sort the homework plan"
    assert snapshot["goal_status"] == "new"
    assert snapshot["turn_count"] == 1
    assert "current_topic=" in store.summary("chat-1", now=1001.0)
    assert "goal_status=new" in store.summary("chat-1", now=1001.0)


def test_conversation_continuity_continues_similar_topic():
    store = ConversationContinuityStore()
    store.record(
        chat_id="chat-1",
        focus_preview="Alice: talk about homework plan",
        goal_summary="help Alice sort the homework plan",
        social_intent="answer",
        action_taken="reply",
        now=1000.0,
    )

    store.record(
        chat_id="chat-1",
        focus_preview="Alice: homework plan details",
        goal_summary="help Alice sort the homework plan",
        social_intent="inquire",
        action_taken="reply",
        now=1010.0,
    )

    snapshot = store.snapshot("chat-1", now=1011.0)
    assert snapshot["goal_status"] == "continuing"
    assert snapshot["turn_count"] == 2
    assert snapshot["current_topic"] == "Alice: talk about homework plan"
    assert snapshot["continuity_weight"] == "strong"


def test_conversation_continuity_starts_new_topic_when_focus_changes():
    store = ConversationContinuityStore()
    store.record(
        chat_id="chat-1",
        focus_preview="Alice: talk about homework plan",
        goal_summary="help Alice sort the homework plan",
        social_intent="answer",
        action_taken="reply",
        now=1000.0,
    )

    store.record(
        chat_id="chat-1",
        focus_preview="Bob: discuss tomorrow's weather",
        goal_summary="chat about the weather",
        social_intent="answer",
        action_taken="reply",
        now=1010.0,
    )

    snapshot = store.snapshot("chat-1", now=1011.0)
    assert snapshot["goal_status"] == "new"
    assert snapshot["turn_count"] == 1
    assert snapshot["current_topic"] == "Bob: discuss tomorrow's weather"
    assert snapshot["current_goal"] == "chat about the weather"


def test_conversation_continuity_marks_redirect_boundary_and_observe():
    store = ConversationContinuityStore()
    store.record(
        chat_id="chat-1",
        focus_preview="Alice: original topic",
        goal_summary="stay with original topic",
        social_intent="answer",
        action_taken="reply",
        now=1000.0,
    )

    store.record(
        chat_id="chat-1",
        focus_preview="Alice: change the subject",
        goal_summary="move to a lighter topic",
        social_intent="redirect",
        action_taken="reply",
        now=1010.0,
    )
    assert store.snapshot("chat-1", now=1011.0)["goal_status"] == "redirected"

    store.record(
        chat_id="chat-1",
        focus_preview="Alice: harsh wording",
        goal_summary="keep a boundary",
        social_intent="boundary",
        action_taken="reply",
        now=1020.0,
    )
    assert store.snapshot("chat-1", now=1021.0)["goal_status"] == "guarded"

    store.record(
        chat_id="chat-1",
        focus_preview="Alice: background noise",
        goal_summary="observe without forcing a reply",
        social_intent="observe",
        action_taken="reply",
        now=1030.0,
    )
    assert store.snapshot("chat-1", now=1031.0)["goal_status"] == "observing"


def test_conversation_continuity_wait_and_ignore_do_not_refresh_goal():
    store = ConversationContinuityStore()
    store.record(
        chat_id="chat-1",
        focus_preview="Alice: talk about homework plan",
        goal_summary="help Alice sort the homework plan",
        social_intent="answer",
        action_taken="reply",
        now=1000.0,
    )

    store.record(
        chat_id="chat-1",
        focus_preview="Alice: unrelated interruption",
        goal_summary="should not replace",
        social_intent="observe",
        action_taken="none",
        reply_need="wait",
        now=1010.0,
    )

    store.record(
        chat_id="chat-1",
        focus_preview="Alice: second interruption",
        goal_summary="still should not replace",
        social_intent="observe",
        action_taken="none",
        reply_need="ignore",
        now=1020.0,
    )

    snapshot = store.snapshot("chat-1", now=1021.0)
    assert snapshot["current_topic"] == "Alice: talk about homework plan"
    assert snapshot["current_goal"] == "help Alice sort the homework plan"
    assert snapshot["turn_count"] == 1
    recent = store.recent("chat-1", now=1021.0)
    assert len(recent) == 3
    assert [item.reply_need for item in recent[-2:]] == ["wait", "ignore"]
    summary = store.summary("chat-1", now=1021.0)
    assert "unrelated interruption" in summary
    assert "second interruption" in summary


def test_conversation_continuity_weakens_after_soft_decay_and_avoids_forced_old_topic():
    store = ConversationContinuityStore()
    store.record(
        chat_id="chat-1",
        focus_preview="Alice: talk about homework plan",
        goal_summary="help Alice sort the homework plan",
        social_intent="answer",
        action_taken="reply",
        now=1000.0,
    )

    weak_now = 1000.0 + store.SOFT_DECAY_SECONDS + 1
    summary = store.summary("chat-1", now=weak_now)
    assert "continuity_weight=weak" in summary
    assert "weak_reference_only" in summary

    store.record(
        chat_id="chat-1",
        focus_preview="Alice: loosely says homework maybe",
        goal_summary="new lightweight homework mention",
        social_intent="answer",
        action_taken="reply",
        now=weak_now,
    )

    snapshot = store.snapshot("chat-1", now=weak_now + 1)
    assert snapshot["goal_status"] == "new"
    assert snapshot["turn_count"] == 1


def test_conversation_continuity_lightweight_event_does_not_refresh_goal():
    store = ConversationContinuityStore()
    store.record(
        chat_id="chat-1",
        focus_preview="Alice: talk about homework plan",
        goal_summary="help Alice sort the homework plan",
        social_intent="answer",
        action_taken="reply",
        now=1000.0,
    )

    store.record(
        chat_id="chat-1",
        focus_preview="Alice poked the bot",
        goal_summary="should not replace",
        social_intent="tease",
        action_taken="reply",
        lightweight_event=True,
        now=1010.0,
    )

    snapshot = store.snapshot("chat-1", now=1011.0)
    assert snapshot["current_topic"] == "Alice: talk about homework plan"
    assert snapshot["current_goal"] == "help Alice sort the homework plan"
    assert snapshot["turn_count"] == 1
    recent = store.recent("chat-1", now=1011.0)
    assert len(recent) == 2
    assert recent[-1].focus_preview == "Alice poked the bot"
    assert "Alice poked the bot" in store.summary("chat-1", now=1011.0)


def test_conversation_continuity_expires_after_ttl():
    store = ConversationContinuityStore()
    store.record(
        chat_id="chat-1",
        focus_preview="Alice: old topic",
        goal_summary="old goal",
        social_intent="answer",
        action_taken="reply",
        now=1000.0,
    )

    assert store.summary("chat-1", now=1000.0 + store.TURN_TTL_SECONDS + 1) == ""

    store.record(
        chat_id="chat-1",
        focus_preview="Alice: fresh topic",
        goal_summary="fresh goal",
        social_intent="answer",
        action_taken="reply",
        now=1000.0 + store.TURN_TTL_SECONDS + 2,
    )

    snapshot = store.snapshot("chat-1", now=1000.0 + store.TURN_TTL_SECONDS + 3)
    assert snapshot["goal_status"] == "new"
    assert snapshot["turn_count"] == 1
    assert snapshot["current_topic"] == "Alice: fresh topic"


def test_conversation_continuity_snapshot_uses_wall_clock_for_default_expiry(monkeypatch):
    store = ConversationContinuityStore()
    stale_now = 1000.0 + store.TURN_TTL_SECONDS + 1

    store.record(
        chat_id="chat-1",
        focus_preview="Alice: old topic",
        goal_summary="old goal",
        social_intent="answer",
        action_taken="reply",
        now=1000.0,
    )
    monkeypatch.setattr(continuity_module.time, "time", lambda: stale_now)

    snapshot = store.snapshot("chat-1")

    assert snapshot["current_topic"] == ""
    assert snapshot["current_goal"] == ""
    assert snapshot["turn_count"] == 0
    assert snapshot["continuity_weight"] == ""
