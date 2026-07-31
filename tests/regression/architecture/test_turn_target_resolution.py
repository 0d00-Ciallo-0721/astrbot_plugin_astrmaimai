from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from astrmai.conversation.attention.event_normalizer import NormalizedEvent
from astrmai.conversation.attention.thread_builder import build_focus_thread
from astrmai.conversation.attention.turn_target_resolver import resolve_turn_target
from astrmai.conversation.contracts.conversation_event import ConversationEvent
from astrmai.conversation.contracts.turn_target import TargetKind


class StubEvent:
    def __init__(self, *, sender_id: str, sender_name: str, text: str, extras=None):
        self.unified_msg_origin = "ff:GroupMessage:100"
        self.message_str = text
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._extras = dict(extras or {})

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


def _normalized(
    *,
    sender_id: str,
    sender_name: str,
    text: str,
    event_id: str,
    index: int,
    at_actor_ids=(),
    reply_target_actor_id: str = "",
    reply_target_event_id: str = "",
    is_at_bot: bool = False,
    is_reply_to_bot: bool = False,
    is_direct_wakeup: bool = False,
    topic_epoch: int = 1,
    interaction_kind: str = "",
):
    event = StubEvent(
        sender_id=sender_id,
        sender_name=sender_name,
        text=text,
        extras={"astrmai_interaction_kind": interaction_kind},
    )
    canonical = ConversationEvent(
        event_id=event_id,
        chat_id=event.unified_msg_origin,
        chat_kind="group",
        timestamp=100.0 + index,
        actor_id=sender_id,
        actor_name=sender_name,
        visible_text=text,
        rich_text=text,
        message_kind="interaction" if interaction_kind else "text",
        role="interaction" if interaction_kind else "user",
        reply_target_event_id=reply_target_event_id,
        reply_target_actor_id=reply_target_actor_id,
        at_actor_ids=tuple(at_actor_ids),
        topic_epoch=topic_epoch,
        source_event_ids=(event_id,),
        interaction_kind=interaction_kind,
        is_at_bot=is_at_bot,
        is_reply_to_bot=is_reply_to_bot,
        is_direct_wakeup=is_direct_wakeup,
    )
    event.set_extra("astrmai_conversation_event", canonical)
    return NormalizedEvent(
        event=event,
        sender_id=sender_id,
        sender_name=sender_name,
        text=text,
        rich_text=text,
        timestamp=canonical.timestamp,
        is_self=False,
        is_reply_to_bot=is_reply_to_bot,
        is_at_bot=is_at_bot,
        is_direct_wakeup=is_direct_wakeup,
        is_near_context_query=False,
        reply_target_sender_id=reply_target_actor_id,
        index=index,
        canonical_event=canonical,
    )


def test_direct_wakeup_after_high_frequency_actor_targets_current_actor():
    frequent = _normalized(
        sender_id="ying",
        sender_name="萤",
        text="前面的长对话",
        event_id="msg-1",
        index=0,
    )
    current = _normalized(
        sender_id="6",
        sender_name="6",
        text="@妃妃 妃妃",
        event_id="msg-2",
        index=1,
        at_actor_ids=("bot",),
        is_at_bot=True,
        is_direct_wakeup=True,
    )

    target, actor_set = resolve_turn_target(
        current,
        current,
        [frequent, current],
        bot_id="bot",
    )

    assert target.target_kind is TargetKind.ACTOR
    assert target.target_actor_id == "6"
    assert target.evidence == "direct_at"
    assert actor_set.current_actor_id == "6"
    assert "ying" in actor_set.recent_topic_actor_ids


def test_reply_to_third_party_message_keeps_reply_actor_primary_and_at_actor_explicit():
    current = _normalized(
        sender_id="b",
        sender_name="B",
        text="@机器人 @C 回复A",
        event_id="msg-current",
        index=2,
        at_actor_ids=("bot", "c"),
        reply_target_actor_id="a",
        reply_target_event_id="msg-a",
        is_at_bot=True,
    )

    target, actor_set = resolve_turn_target(current, current, [current], bot_id="bot")

    assert target.target_kind is TargetKind.MESSAGE
    assert target.target_actor_id == "a"
    assert target.target_event_id == "msg-a"
    assert target.evidence == "reply"
    assert actor_set.current_actor_id == "b"
    assert actor_set.explicit_target_actor_ids == ("a", "c")


def test_reply_to_bot_targets_current_actor_not_bot():
    current = _normalized(
        sender_id="xiaoxin",
        sender_name="小欣",
        text="对不起",
        event_id="msg-apology",
        index=1,
        reply_target_actor_id="bot",
        reply_target_event_id="bot-reply",
        is_reply_to_bot=True,
    )

    target, actor_set = resolve_turn_target(current, current, [current], bot_id="bot")

    assert target.target_actor_id == "xiaoxin"
    assert target.evidence == "reply_to_bot"
    assert "bot" not in actor_set.explicit_target_actor_ids


def test_same_nickname_never_merges_actor_ids():
    first = _normalized(
        sender_id="1001",
        sender_name="同名",
        text="前文",
        event_id="msg-first",
        index=0,
    )
    second = _normalized(
        sender_id="1002",
        sender_name="同名",
        text="现在是我",
        event_id="msg-second",
        index=1,
        is_direct_wakeup=True,
    )

    target, actor_set = resolve_turn_target(second, second, [first, second], bot_id="bot")

    assert target.target_actor_id == "1002"
    assert actor_set.current_actor_id == "1002"
    assert actor_set.recent_topic_actor_ids == ("1001",)


def test_focus_thread_carries_frozen_target_and_actor_set():
    focus = _normalized(
        sender_id="current",
        sender_name="当前用户",
        text="普通焦点",
        event_id="msg-focus",
        index=0,
    )
    gate = SimpleNamespace(
        config=SimpleNamespace(
            attention=SimpleNamespace(
                focus_thread_enabled=False,
                focus_thread_core_max_messages=4,
                focus_thread_related_max_messages=3,
                ambient_background_max_messages=2,
            )
        ),
        self_id="bot",
    )

    context = build_focus_thread(gate, focus, focus, [focus])

    assert context.turn_target.target_actor_id == "current"
    assert context.actor_set.current_actor_id == "current"
    with pytest.raises(FrozenInstanceError):
        context.turn_target.target_actor_id = "someone-else"
