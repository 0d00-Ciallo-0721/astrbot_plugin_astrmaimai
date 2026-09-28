import asyncio
import time
import tempfile
from pathlib import Path
from types import SimpleNamespace

from astrmai.conversation.attention.group_dialogue_store import GroupDialogueStore
from astrmai.conversation.attention.group_social_feedback_observer import GroupSocialFeedbackObserver
from astrmai.conversation.attention.participation_policy import ParticipationPolicy
from astrmai.proactive.dispatcher import ProactiveDispatcher, ProactiveMessageIntent
from astrmai.conversation.contracts.turn_target import TurnTarget


def _event(sender_id, text, *, direct=False, timestamp=100.0):
    extras = {"astrmai_timestamp": timestamp}
    if direct:
        extras["astrmai_group_direct_wakeup"] = True
    return SimpleNamespace(
        message_str=text,
        timestamp=timestamp,
        get_sender_id=lambda: sender_id,
        get_extra=lambda key, default=None: extras.get(key, default),
    )


def test_feedback_aggregate_is_idempotent_bounded_and_redacted():
    async def run():
        store = GroupDialogueStore()
        kwargs = dict(
            chat_id="chat-g",
            observation_id="observation-1",
            bot_turn_id="turn-1",
            feedback_kind="followup",
            status="impacted",
            actor_id="user-a",
            evidence_event_id="event-1",
            confidence=0.9,
            feedback_at=100.0,
        )
        await store.record_bot_turn_feedback(**kwargs)
        await store.record_bot_turn_feedback(**kwargs)
        summary = await store.get_feedback_summary("chat-g", actor_id="user-a", now=101.0)
        return summary

    summary = asyncio.run(run())
    assert summary["proactive_response_rate"] == 1.0
    assert summary["recent_followup_strength"] > 0
    assert "text" not in summary
    assert "prompt" not in summary


def test_observation_closed_is_neutral_after_reaction_or_followup():
    async def run():
        store = GroupDialogueStore()
        for kind, event_id in (("reaction", "reaction-1"), ("observation_closed", "close-1"), ("followup", "followup-1"), ("observation_closed", "close-2")):
            await store.record_bot_turn_feedback(
                "chat-g",
                observation_id="observation-1",
                bot_turn_id="turn-1",
                feedback_kind=kind,
                status="impacted",
                actor_id="user-a",
                evidence_event_id=event_id,
                feedback_at=100.0,
            )
        return await store.get_feedback_summary("chat-g", actor_id="user-a", now=101.0)

    summary = asyncio.run(run())
    assert summary.get("consecutive_unanswered_count", 0) == 0
    assert summary["recent_reaction_strength"] > 0
    assert summary["recent_followup_strength"] > 0


def test_explicit_negative_is_local_and_expires():
    async def run():
        store = GroupDialogueStore()
        await store.record_bot_turn_feedback(
            "chat-g",
            observation_id="observation-1",
            bot_turn_id="turn-1",
            feedback_kind="explicit_negative",
            status="impacted",
            actor_id="user-a",
            evidence_event_id="event-1",
            feedback_at=100.0,
        )
        still_suppressed = await store.get_feedback_summary("chat-g", actor_id="user-a", now=2000.0)
        return (
            await store.get_feedback_summary("chat-g", actor_id="user-a", now=101.0),
            await store.get_feedback_summary("chat-g", actor_id="user-b", now=101.0),
            still_suppressed,
            await store.get_feedback_summary("chat-g", actor_id="user-a", now=4001.0),
        )

    local, other, still_suppressed, expired = asyncio.run(run())
    assert local["explicit_negative_suppression"] is True
    assert other.get("explicit_negative_suppression", False) is False
    assert still_suppressed["explicit_negative_suppression"] is True
    assert expired["explicit_negative_suppression"] is False


def test_participation_reads_feedback_without_overriding_direct_wakeup():
    policy = ParticipationPolicy()
    focus = _event("user-a", "别总插话")
    result, _ = policy.evaluate(
        focus_event=focus,
        batch_events=[focus],
        feedback_summary={
            "explicit_negative_suppression": True,
            "consecutive_unanswered_count": 2,
        },
        now=101.0,
    )
    assert result.feedback_effect == "local_suppression"
    assert "feedback_explicit_negative" in result.signals
    assert result.social_admission == "wait"

    direct = _event("user-a", "请回答这个问题", direct=True)
    direct_result, _ = policy.evaluate(
        focus_event=direct,
        batch_events=[direct],
        feedback_summary={"explicit_negative_suppression": True},
        now=101.0,
    )
    assert direct_result.social_admission == "allow"


def test_observer_classifies_explicit_negative_without_storing_original_text():
    async def run():
        store = GroupDialogueStore()
        observer = GroupSocialFeedbackObserver(
            config=SimpleNamespace(
                conversation=SimpleNamespace(
                    social_feedback_observation_enabled=True,
                    social_feedback_window_sec=45.0,
                    social_feedback_max_active_per_chat=5,
                )
            ),
            dialogue_store=store,
        )
        turn = SimpleNamespace(
            commit_id="commit-1",
            turn_id="turn-1",
            chat_id="chat-g",
            chat_kind="group",
            target=SimpleNamespace(target_actor_id="user-a"),
            topic_epoch=1,
            visible_text="hello",
            persistable_text="hello",
            outbound_message_ids=("bot-message-1",),
            sent_at=100.0,
            send_status=SimpleNamespace(value="sent"),
        )
        await observer.arm(turn, context={"thread_id": "thread-1", "bot_id": "bot-1"})
        incoming = SimpleNamespace(
            unified_msg_origin="chat-g",
            message_str="别总插话",
            message_obj=SimpleNamespace(message_id="event-1", message=[]),
            get_group_id=lambda: "group-g",
            get_sender_id=lambda: "user-a",
            get_extra=lambda key, default=None: {"astrmai_turn_thread_id": "thread-1"}.get(key, default),
            set_extra=lambda *_args: None,
        )
        decision = await observer.observe(incoming)
        summary = await store.get_feedback_summary("chat-g", actor_id="user-a", now=101.0)
        records = await store.get_feedback_records("chat-g")
        return decision, summary, records

    decision, summary, records = asyncio.run(run())
    assert decision.kind == "explicit_negative"
    assert summary["explicit_negative_suppression"] is True
    assert records and all(not hasattr(item, "reply_text") for item in records)


def test_observer_superseded_after_reaction_does_not_create_unanswered_suppression():
    async def run():
        store = GroupDialogueStore()
        observer = GroupSocialFeedbackObserver(
            config=SimpleNamespace(
                conversation=SimpleNamespace(
                    social_feedback_observation_enabled=True,
                    social_feedback_window_sec=45.0,
                    social_feedback_max_active_per_chat=5,
                )
            ),
            dialogue_store=store,
        )
        turn_base = dict(
            chat_id="chat-g",
            chat_kind="group",
            target=SimpleNamespace(target_actor_id="user-a"),
            topic_epoch=1,
            visible_text="hello",
            persistable_text="hello",
            outbound_message_ids=("bot-message-1",),
            sent_at=100.0,
            send_status=SimpleNamespace(value="sent"),
        )
        first = SimpleNamespace(commit_id="commit-1", turn_id="turn-1", **turn_base)
        await observer.arm(first, context={"thread_id": "thread-1", "bot_id": "bot-1"})
        reaction = SimpleNamespace(
            unified_msg_origin="chat-g",
            message_str="",
            message_obj=SimpleNamespace(message_id="reaction-1", message=[SimpleNamespace(type="face")]),
            get_group_id=lambda: "group-g",
            get_sender_id=lambda: "user-a",
            get_extra=lambda key, default=None: {"astrmai_turn_thread_id": "thread-1"}.get(key, default),
            set_extra=lambda *_args: None,
        )
        await observer.observe(reaction)
        second = SimpleNamespace(commit_id="commit-2", turn_id="turn-2", **{**turn_base, "outbound_message_ids": ("bot-message-2",)})
        await observer.arm(second, context={"thread_id": "thread-1", "bot_id": "bot-1"})
        return await store.get_feedback_summary("chat-g", actor_id="user-a", now=101.0)

    summary = asyncio.run(run())
    assert summary.get("consecutive_unanswered_count", 0) == 0
    assert summary["recent_reaction_strength"] > 0


def test_observer_timeout_after_reaction_or_followup_is_neutral():
    async def run_case(*, reaction: bool):
        store = GroupDialogueStore()
        observer = GroupSocialFeedbackObserver(
            config=SimpleNamespace(
                conversation=SimpleNamespace(
                    social_feedback_observation_enabled=True,
                    social_feedback_window_sec=45.0,
                    social_feedback_max_active_per_chat=5,
                )
            ),
            dialogue_store=store,
        )
        turn = SimpleNamespace(
            commit_id="commit-1",
            turn_id="turn-1",
            chat_id="chat-g",
            chat_kind="group",
            target=SimpleNamespace(target_actor_id="user-a"),
            topic_epoch=1,
            visible_text="hello",
            persistable_text="hello",
            outbound_message_ids=("bot-message-1",),
            sent_at=100.0,
            send_status=SimpleNamespace(value="sent"),
        )
        observation = await observer.arm(turn, context={"thread_id": "thread-1", "bot_id": "bot-1"})
        incoming = SimpleNamespace(
            unified_msg_origin="chat-g",
            message_str="" if reaction else "请再讲讲这个细节",
            message_obj=SimpleNamespace(
                message_id="feedback-1",
                message=[SimpleNamespace(type="face")] if reaction else [],
            ),
            get_group_id=lambda: "group-g",
            get_sender_id=lambda: "user-a",
            get_extra=lambda key, default=None: {"astrmai_turn_thread_id": "thread-1"}.get(key, default),
            set_extra=lambda *_args: None,
        )
        await observer.observe(incoming)
        observation.expires_at = time.monotonic()
        observer._arm_timeout(observation)
        await asyncio.sleep(0.01)
        return await store.get_feedback_summary("chat-g", actor_id="user-a")

    reaction_summary = asyncio.run(run_case(reaction=True))
    followup_summary = asyncio.run(run_case(reaction=False))
    assert reaction_summary.get("consecutive_unanswered_count", 0) == 0
    assert followup_summary.get("consecutive_unanswered_count", 0) == 0
    assert reaction_summary["recent_reaction_strength"] > 0
    assert followup_summary["recent_followup_strength"] > 0


def test_dispatcher_reads_feedback_and_blocks_local_proactive_candidate():
    class State:
        bot_id = "bot-1"
        dialogue_store = None

        async def get_state(self, _chat_id):
            return SimpleNamespace(chat_kind="group", last_real_user_activity_at=1000.0)

        async def is_proactive_generation_current(self, _chat_id, _generation):
            return True

    class Store:
        async def get_feedback_summary(self, chat_id, *, actor_id=""):
            assert chat_id == "chat-g"
            return {
                "explicit_negative_suppression": True,
                "consecutive_unanswered_count": 0,
                "actor_id": actor_id,
            }

    state = State()
    state.dialogue_store = Store()
    dispatcher = ProactiveDispatcher(
        state_engine=state,
        attention_gate=SimpleNamespace(inject_external_event=lambda *_args: True),
    )
    dispatcher._activity_snapshot = lambda _chat_id: asyncio.sleep(0, result={"latest_activity_ts": time.time()})
    dispatcher._state_energy = lambda _chat_id: asyncio.sleep(0, result=1.0)
    intent = ProactiveMessageIntent(
        "chat-g", "heartflow", "reason", "guidance",
        target=TurnTarget(target_actor_id="user-a", confidence=1.0, target_source="test", evidence="test"),
        metadata={"chat_kind": "group", "focus_speaker_id": "user-a", "captured_generation": 0},
    )
    decision = asyncio.run(dispatcher.dispatch(intent))
    assert decision.allowed is False
    assert decision.blocked_reason == "feedback_explicit_negative"
    assert decision.safety_checks["feedback_consumer"] == "dispatcher"


def test_feedback_aggregate_rebuilds_after_snapshot_restore():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            first = GroupDialogueStore(snapshot_dir=Path(tmp))
            await first.record_bot_turn_feedback(
                "chat-g",
                observation_id="observation-1",
                bot_turn_id="turn-1",
                feedback_kind="followup",
                status="impacted",
                actor_id="user-a",
                evidence_event_id="event-1",
                feedback_at=time.time(),
            )
            assert await first.persist_snapshot() is True
            second = GroupDialogueStore(snapshot_dir=Path(tmp))
            assert await second.restore_snapshot() == 1
            return await second.get_feedback_summary("chat-g", actor_id="user-a")

    summary = asyncio.run(run())
    assert summary["recent_followup_strength"] > 0
