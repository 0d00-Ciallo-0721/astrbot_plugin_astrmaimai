import asyncio
from types import SimpleNamespace

from astrmai.conversation.attention.group_social_feedback_observer import (
    GroupSocialFeedbackObserver,
)
from astrmai.conversation.execution.post_reply_feedback_coordinator import (
    PostReplyFeedbackCoordinator,
)
from astrmai.conversation.execution.reply_post_send import ReplyPostSendMixin


class Reply:
    def __init__(self, message_id):
        self.message_id = message_id


class At:
    def __init__(self, qq):
        self.qq = qq


class Face:
    pass


class _Event:
    def __init__(
        self,
        *,
        chat_id="default:GroupMessage:group-1",
        group_id="group-1",
        sender_id="user-1",
        self_id="bot-1",
        message_id="incoming-1",
        text="继续说说",
        thread_id="thread-1",
        components=None,
    ):
        self.unified_msg_origin = chat_id
        self.message_str = text
        self.message_obj = SimpleNamespace(
            message_id=message_id,
            message=list(components or []),
        )
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self._extra = {"astrmai_turn_thread_id": thread_id}

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


def _turn(*, turn_id="turn-1", outbound=("bot-message-1",), target="user-1"):
    return SimpleNamespace(
        commit_id=f"commit-{turn_id}",
        turn_id=turn_id,
        chat_id="default:GroupMessage:group-1",
        chat_kind="group",
        target=SimpleNamespace(target_actor_id=target, target_actor_name=target),
        topic_epoch=2,
        visible_text="我们可以从这个思路继续分析",
        persistable_text="我们可以从这个思路继续分析",
        outbound_message_ids=tuple(outbound),
        sent_at=100.0,
        send_status=SimpleNamespace(value="sent"),
    )


def _config():
    return SimpleNamespace(
        conversation=SimpleNamespace(
            social_feedback_observation_enabled=True,
            social_feedback_window_sec=45.0,
            social_feedback_max_active_per_chat=5,
            group_thread_wait_enabled=True,
        )
    )


def test_group_quote_is_strong_feedback_and_signals_followup_wait():
    async def _run():
        observer = GroupSocialFeedbackObserver(config=_config())
        outgoing = _Event(message_id="source-1")
        observation = await observer.arm(
            _turn(),
            event=outgoing,
            context={"thread_id": "thread-1", "generation": 7, "bot_id": "bot-1"},
        )
        incoming = _Event(
            sender_id="user-2",
            components=[Reply("bot-message-1")],
            thread_id="other-thread",
        )
        decision = await observer.observe(incoming)
        return observation, incoming, decision

    observation, incoming, decision = asyncio.run(_run())

    assert decision.kind == "direct_quote"
    assert decision.action == "force_engage"
    assert decision.observation_id == observation.observation_id
    assert incoming.get_extra("astrmai_force_engage") is True
    assert observation.feedback_event.is_set()


def test_group_unrelated_message_neither_signals_nor_consumes_observation():
    async def _run():
        observer = GroupSocialFeedbackObserver(config=_config())
        observation = await observer.arm(
            _turn(),
            event=_Event(),
            context={"thread_id": "thread-1", "generation": 7, "bot_id": "bot-1"},
        )
        unrelated = _Event(
            sender_id="user-9",
            text="今晚吃什么",
            thread_id="thread-9",
        )
        decision = await observer.observe(unrelated)
        active = observer.get_active_observations(unrelated.unified_msg_origin)
        return observation, decision, active

    observation, decision, active = asyncio.run(_run())

    assert decision.kind == "unrelated"
    assert decision.action == "none"
    assert observation.feedback_event.is_set() is False
    assert [item.observation_id for item in active] == [observation.observation_id]


def test_group_same_turn_followup_merges_outbound_ids_and_replaces_signal_on_event():
    async def _run():
        observer = GroupSocialFeedbackObserver(config=_config())
        first_event = _Event()
        first = await observer.arm(
            _turn(outbound=("bot-message-1",)),
            event=first_event,
            context={"thread_id": "thread-1", "generation": 7, "bot_id": "bot-1"},
        )
        second_event = _Event(message_id="source-2")
        second = await observer.arm(
            _turn(outbound=("bot-message-2",)),
            event=second_event,
            context={"thread_id": "thread-1", "generation": 7, "bot_id": "bot-1"},
        )
        return first, second, first_event, second_event, observer

    first, second, first_event, second_event, observer = asyncio.run(_run())

    assert first is second
    assert second.outbound_message_ids == ["bot-message-1", "bot-message-2"]
    assert first_event.get_extra("astrmai_post_reply_feedback_event") is second.feedback_event
    assert second_event.get_extra("astrmai_post_reply_feedback_event") is second.feedback_event
    assert len(observer.get_active_observations(second.chat_id)) == 1


def test_new_group_turn_in_same_thread_supersedes_old_observation():
    async def _run():
        observer = GroupSocialFeedbackObserver(config=_config())
        first = await observer.arm(
            _turn(turn_id="turn-1"),
            event=_Event(),
            context={"thread_id": "thread-1", "generation": 7, "bot_id": "bot-1"},
        )
        second = await observer.arm(
            _turn(turn_id="turn-2"),
            event=_Event(message_id="source-2"),
            context={"thread_id": "thread-1", "generation": 8, "bot_id": "bot-1"},
        )
        return first, second, observer.get_active_observations(second.chat_id)

    first, second, active = asyncio.run(_run())

    assert first.status == "superseded"
    assert first.terminal_reason == "new_bot_turn_same_thread"
    assert [item.observation_id for item in active] == [second.observation_id]


def test_group_reaction_records_feedback_without_forcing_reply():
    async def _run():
        observer = GroupSocialFeedbackObserver(config=_config())
        observation = await observer.arm(
            _turn(),
            event=_Event(),
            context={"thread_id": "thread-1", "generation": 7, "bot_id": "bot-1"},
        )
        incoming = _Event(text="", thread_id="thread-1", components=[Face()])
        decision = await observer.observe(incoming)
        return observation, incoming, decision

    observation, incoming, decision = asyncio.run(_run())

    assert decision.kind == "reaction"
    assert decision.action == "record_only"
    assert incoming.get_extra("astrmai_force_engage", False) is False
    assert observation.feedback_event.is_set()


def test_private_coordinator_signals_cycle_without_consuming_message():
    class _PrivateManager:
        def __init__(self):
            self.calls = []

        async def signal_new_message(self, user_id, message_str="", chat_id="", **kwargs):
            self.calls.append((user_id, message_str, chat_id, kwargs))
            return True

    async def _run():
        private = _PrivateManager()
        coordinator = PostReplyFeedbackCoordinator(
            private_chat_manager=private,
            group_social_feedback_observer=None,
        )
        event = _Event(
            chat_id="default:FriendMessage:user-1",
            group_id="",
            sender_id="user-1",
            text="我再补充一句",
        )
        decision = await coordinator.observe_incoming(event)
        return private, event, decision

    private, event, decision = asyncio.run(_run())

    assert decision.kind == "private_continuation"
    assert decision.action == "attention_boost"
    assert private.calls[0][0:3] == (
        "user-1",
        "我再补充一句",
        "default:FriendMessage:user-1",
    )
    assert event.message_str == "我再补充一句"


def test_private_interaction_is_recorded_without_signaling_reply_cycle():
    class _PrivateManager:
        async def signal_new_message(self, *args, **kwargs):
            raise AssertionError("interaction must not signal private continuation")

    async def _run():
        coordinator = PostReplyFeedbackCoordinator(
            private_chat_manager=_PrivateManager(),
            group_social_feedback_observer=None,
        )
        event = _Event(
            chat_id="default:FriendMessage:user-1",
            group_id="",
            sender_id="user-1",
            text="",
            components=[Face()],
        )
        return event, await coordinator.observe_incoming(event)

    event, decision = asyncio.run(_run())

    assert decision.kind == "interaction_feedback"
    assert decision.action == "record_only"
    assert event.get_extra("astrmai_private_reply_cycle_checked") is True


def test_reply_commit_registers_social_feedback_before_other_side_effects():
    class _Coordinator:
        def __init__(self):
            self.calls = []

        async def register_committed_reply(self, event, committed_turn, *, context):
            self.calls.append((event, committed_turn, context))
            return "committed"

    class _Service(ReplyPostSendMixin):
        def __init__(self):
            self.post_reply_feedback_coordinator = _Coordinator()

    async def _run():
        service = _Service()
        event = _Event()
        consumers = service._build_reply_commit_consumers(
            _turn(),
            {"thread_id": "thread-1", "generation": 7},
            event,
        )
        outcome = await consumers["social_feedback"](_turn())
        return service, consumers, outcome

    service, consumers, outcome = asyncio.run(_run())

    assert next(iter(consumers)) == "social_feedback"
    assert outcome == "committed"
    assert service.post_reply_feedback_coordinator.calls[0][0] is not None
