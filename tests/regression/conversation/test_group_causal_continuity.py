from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from astrmai.conversation.attention.group_context_snapshot import (
    GroupContextSnapshotBuilder,
    is_group_direct_correction,
)
from astrmai.conversation.attention.group_dialogue_store import GroupDialogueStore
from astrmai.conversation.contracts.committed_reply import (
    CommittedBotTurn,
    ReplyCommitStatus,
    ReplyPlan,
    ReplySendReceipt,
)
from astrmai.conversation.contracts.dialog_history_policy import DialogHistoryPolicy
from astrmai.conversation.contracts.turn_target import TargetKind, TurnTarget
from astrmai.conversation.planning.planner import Planner
from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin
from astrmai.infrastructure.runtime.chat_runtime_coordinator import ChatRuntimeCoordinator
from astrmai.infrastructure.runtime.runtime_contracts import FreshnessState


class _Event:
    def __init__(self, sender_id="xin", sender_name="小欣", message_id="u1"):
        self._extras = {}
        self.message_id = message_id
        self.message_obj = SimpleNamespace(message_id=message_id)
        self._sender_id = sender_id
        self._sender_name = sender_name

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name


def test_group_snapshot_keeps_actor_bot_stance_and_filters_echoes():
    async def run():
        chat_id = "ff:GroupMessage:552752264"
        store = GroupDialogueStore()
        direct = await store.append_segment(
            chat_id,
            event_id="u-offense",
            speaker_id="xin",
            speaker_name="小欣",
            content="吃我的肉棒好不好",
            role="user",
            is_at_bot=True,
            topic_epoch=4,
            create_pending_direct=True,
            timestamp=1000,
        )
        await store.observe_social_incident(
            chat_id,
            kind="boundary_violation",
            actor_id="xin",
            actor_name="小欣",
            target_id="bot",
            target_name="妃爱",
            evidence_event_id=direct.event_id,
            topic_epoch=4,
            now=1000,
        )
        await store.append_segment(
            chat_id,
            event_id="bot-reject",
            speaker_id="bot",
            speaker_name="妃爱",
            content="好恶心，请你自重。妃爱不是那种人。",
            role="assistant",
            is_bot=True,
            reply_target_sender_id="xin",
            reply_target_sender_name="小欣",
            source_event_ids=["u-offense"],
            stance="reject",
            social_event="boundary_violation",
            topic_epoch=4,
            timestamp=1001,
        )
        first_echo = await store.append_segment(
            chat_id,
            event_id="echo-a",
            speaker_id="other-a",
            speaker_name="飞飞宝",
            content="好恶心，请你自重。妃爱不是那种人。",
            role="user",
            topic_epoch=4,
            timestamp=1002,
        )
        second_echo = await store.append_segment(
            chat_id,
            event_id="echo-b",
            speaker_id="other-b",
            speaker_name="Murmure",
            content="好恶心，请你自重。妃爱不是那种人。",
            role="user",
            topic_epoch=4,
            timestamp=1003,
        )
        await store.append_segment(
            chat_id,
            event_id="u-followup",
            speaker_id="xin",
            speaker_name="小欣",
            content="你怎么了",
            role="user",
            is_at_bot=True,
            topic_epoch=5,
            create_pending_direct=True,
            timestamp=1010,
        )
        builder = GroupContextSnapshotBuilder(
            store,
            actor_tail_ttl_sec=1200,
            actor_tail_max_segments=8,
            pending_direct_ttl_sec=1200,
            social_incident_ttl_sec=1800,
            max_chars=5000,
        )
        snapshot = await builder.build(
            chat_id,
            current_sender_id="xin",
            current_sender_name="小欣",
            topic_epoch=5,
            now=1011,
        )
        return first_echo, second_echo, snapshot

    first_echo, second_echo, snapshot = asyncio.run(run())

    assert first_echo.provenance == "bot_echo"
    assert second_echo.provenance == "bot_echo"
    assert snapshot.echo_filtered_count == 2
    assert "小欣" in snapshot.text
    assert "吃我的肉棒好不好" in snapshot.text
    assert "Bot 对 小欣 的上一轮回应" in snapshot.text
    assert "拒绝" in snapshot.text
    assert "边界冒犯" in snapshot.text
    assert "飞飞宝帮忙说话" not in snapshot.text
    assert "Murmure帮忙说话" not in snapshot.text
    assert snapshot.topic_bridge is True
    assert snapshot.topic_epoch == 5
    assert snapshot.participant_actor_ids == ["xin"]
    assert snapshot.source_event_ids == ["u-offense", "u-followup", "bot-reject"]
    assert snapshot.last_committed_target_actor_id == "xin"
    trace = snapshot.trace_payload()
    assert trace["topic_epoch"] == 5
    assert trace["topic_participant_ids"] == ["xin"]
    assert trace["summary_source_event_ids"] == [
        "u-offense",
        "u-followup",
        "bot-reject",
    ]
    assert trace["last_committed_target_actor_id"] == "xin"


def test_apology_resolves_only_the_same_actors_incident():
    async def run():
        chat_id = "ff:GroupMessage:552752264"
        store = GroupDialogueStore()
        own = await store.observe_social_incident(
            chat_id,
            kind="boundary_violation",
            actor_id="xin",
            actor_name="小欣",
            target_id="bot",
            target_name="妃爱",
            evidence_event_id="offense-xin",
            now=1000,
        )
        other = await store.observe_social_incident(
            chat_id,
            kind="insult",
            actor_id="other",
            actor_name="其他人",
            target_id="bot",
            target_name="妃爱",
            evidence_event_id="offense-other",
            now=1001,
        )
        resolved = await store.resolve_social_incidents(
            chat_id,
            actor_id="xin",
            resolution_event_id="apology-xin",
            resolution_kind="apology",
            now=1010,
        )
        open_for_xin = await store.get_social_incidents(
            chat_id,
            current_sender_id="xin",
            include_resolved=False,
            now=1011,
        )
        open_for_other = await store.get_social_incidents(
            chat_id,
            current_sender_id="other",
            include_resolved=False,
            now=1011,
        )
        return own, other, resolved, open_for_xin, open_for_other

    own, other, resolved, open_for_xin, open_for_other = asyncio.run(run())

    assert own is not None and other is not None
    assert resolved == 1
    assert open_for_xin == []
    assert [item.incident_id for item in open_for_other] == [other.incident_id]


def test_causal_state_snapshot_roundtrip_is_backward_compatible(tmp_path):
    async def run():
        chat_id = "ff:GroupMessage:552752264"
        source = GroupDialogueStore(snapshot_dir=tmp_path, warm_zone_ttl_seconds=3000)
        await source.append_segment(
            chat_id,
            event_id="u1",
            speaker_id="xin",
            speaker_name="小欣",
            content="你怎么了",
            is_at_bot=True,
            create_pending_direct=True,
            topic_epoch=3,
            timestamp=1000,
        )
        await source.observe_social_incident(
            chat_id,
            kind="conflict",
            actor_id="xin",
            actor_name="小欣",
            target_id="bot",
            target_name="妃爱",
            evidence_event_id="u1",
            topic_epoch=3,
            now=1000,
        )
        await source.append_segment(
            chat_id,
            event_id="a1",
            speaker_id="bot",
            speaker_name="妃爱",
            content="我在回应你。",
            role="assistant",
            is_bot=True,
            reply_target_sender_id="xin",
            source_event_ids=["u1"],
            topic_epoch=3,
            timestamp=1001,
        )
        assert await source.persist_snapshot()

        restored = GroupDialogueStore(snapshot_dir=tmp_path, warm_zone_ttl_seconds=3000)
        count = await restored.restore_snapshot()
        bot_turns = await restored.get_recent_bot_turns(
            chat_id,
            target_sender_id="xin",
            now=1002,
        )
        incidents = await restored.get_social_incidents(
            chat_id,
            current_sender_id="xin",
            now=1002,
        )
        return count, bot_turns, incidents

    count, bot_turns, incidents = asyncio.run(run())

    assert count == 1
    assert bot_turns and bot_turns[-1].source_event_ids == ["u1"]
    assert incidents and incidents[-1].actor_id == "xin"


def test_pending_direct_lifecycle_preserves_wait_and_closes_explicitly():
    async def run():
        chat_id = "ff:GroupMessage:552752264"
        store = GroupDialogueStore()
        await store.append_segment(
            chat_id,
            event_id="waited",
            speaker_id="xin",
            speaker_name="小欣",
            content="你怎么了",
            is_at_bot=True,
            create_pending_direct=True,
            timestamp=1000,
        )
        still_pending = await store.get_pending_direct_items(
            chat_id,
            current_sender_id="xin",
            ttl_seconds=1200,
            now=1010,
        )
        still_pending_statuses = [item.status for item in still_pending]
        superseded_count = await store.supersede_pending_direct_for_actor(
            chat_id,
            actor_id="xin",
            superseded_by_event_id="correction",
            now=1020,
        )
        after_superseded = await store.get_pending_direct_items(
            chat_id,
            current_sender_id="xin",
            ttl_seconds=1200,
            include_answered=True,
            now=1021,
        )
        after_superseded_statuses = [item.status for item in after_superseded]
        await store.append_segment(
            chat_id,
            event_id="withdrawn",
            speaker_id="xin",
            speaker_name="小欣",
            content="算了",
            is_at_bot=True,
            create_pending_direct=True,
            timestamp=1030,
        )
        assert await store.mark_recalled(chat_id, "withdrawn")
        await store.append_segment(
            chat_id,
            event_id="expired",
            speaker_id="xin",
            speaker_name="小欣",
            content="还在吗",
            is_at_bot=True,
            create_pending_direct=True,
            timestamp=1040,
        )
        await store.get_pending_direct_items(
            chat_id,
            current_sender_id="xin",
            ttl_seconds=60,
            include_answered=True,
            now=1110,
        )
        all_items = await store.get_pending_direct_items(
            chat_id,
            current_sender_id="xin",
            ttl_seconds=1200,
            include_answered=True,
            now=1111,
        )
        return superseded_count, still_pending_statuses, after_superseded_statuses, all_items

    superseded_count, still_pending_statuses, after_superseded_statuses, all_items = asyncio.run(run())

    assert superseded_count == 1
    assert still_pending_statuses == ["pending"]
    assert after_superseded_statuses == ["superseded"]
    assert {item.event_id: item.status for item in all_items} == {
        "waited": "superseded",
        "withdrawn": "withdrawn",
        "expired": "expired",
    }


def test_group_direct_correction_detection_is_conservative():
    assert is_group_direct_correction("不是，我是问刚才那句")
    assert is_group_direct_correction("我的意思是你有没有看到前文")
    assert is_group_direct_correction("说错了，应该是小欣")
    assert not is_group_direct_correction("不对")
    assert not is_group_direct_correction("再补充一句")


def test_group_freshness_watermark_distinguishes_actor_update_from_ambient_noise():
    async def run():
        coordinator = ChatRuntimeCoordinator()
        chat_id = "ff:GroupMessage:552752264"
        watermark = await coordinator.mark_activity(
            chat_id,
            1000,
            "xin",
            "小欣",
            "你怎么了",
            "thread-main",
            event_id="focus",
            is_direct=True,
        )
        await coordinator.mark_activity(
            chat_id,
            1003,
            "ambient",
            "路人",
            "哈哈",
            "thread-other",
            event_id="ambient",
            is_direct=False,
        )
        ambient_state = await coordinator.evaluate_reply_freshness(
            chat_id,
            1000,
            max_age_seconds=90,
            thread_signature="thread-main",
            allow_parallel_threads=True,
            focus_sender_id="xin",
            focus_watermark=watermark,
        )
        await coordinator.mark_activity(
            chat_id,
            1005,
            "xin",
            "小欣",
            "不是，我是问刚才那句",
            "thread-main",
            event_id="correction",
            is_direct=True,
        )
        actor_state = await coordinator.evaluate_reply_freshness(
            chat_id,
            1000,
            max_age_seconds=90,
            thread_signature="thread-main",
            allow_parallel_threads=True,
            focus_sender_id="xin",
            focus_watermark=watermark,
        )
        return ambient_state, actor_state

    ambient_state, actor_state = asyncio.run(run())

    assert ambient_state[0] == FreshnessState.FRESH
    assert actor_state[0] == FreshnessState.STALE_BUT_SALVAGEABLE
    assert "same_actor_direct_update" in actor_state[1]


def test_planner_prompt_context_injects_privacy_safe_group_causal_snapshot():
    async def run():
        chat_id = "ff:GroupMessage:552752264"
        store = GroupDialogueStore()
        now = time.time()
        await store.append_segment(
            chat_id,
            event_id="u1",
            speaker_id="xin",
            speaker_name="小欣",
            content="你怎么了",
            is_at_bot=True,
            create_pending_direct=True,
            topic_epoch=2,
            timestamp=now,
        )
        event = _Event()
        event.set_extra("astrmai_group_activity_watermark", 7)
        mixin = PlannerPromptContextMixin()
        mixin.dialogue_store = store
        mixin.gateway = SimpleNamespace(
            config=SimpleNamespace(
                conversation=SimpleNamespace(
                    group_actor_tail_ttl_sec=1200,
                    group_actor_tail_max_segments=8,
                    group_pending_direct_ttl_sec=1200,
                    group_social_incident_ttl_sec=1800,
                    group_context_snapshot_max_chars=5500,
                )
            )
        )
        history_policy = DialogHistoryPolicy(
            history_mode="current_topic",
            group_id="552752264",
            topic_epoch=2,
            current_sender_id="xin",
        )
        text = await mixin._get_group_causal_context(chat_id, history_policy, event)
        return text, event.get_extra("astrmai_group_context_snapshot")

    text, trace = asyncio.run(run())

    assert "当前发言人：小欣" in text
    assert "你怎么了" in text
    assert trace["watermark"] == 7
    assert trace["pending_direct_count"] == 1
    assert "你怎么了" not in str(trace)


def test_committed_reply_records_bot_target_source_and_stance():
    async def run():
        chat_id = "ff:GroupMessage:552752264"
        store = GroupDialogueStore()
        await store.append_segment(
            chat_id,
            event_id="u-offense",
            speaker_id="xin",
            speaker_name="小欣",
            content="吃我的肉棒好不好",
            is_at_bot=True,
            create_pending_direct=True,
            topic_epoch=4,
            timestamp=1000,
        )
        target = TurnTarget(
            target_kind=TargetKind.ACTOR,
            target_actor_id="xin",
            target_actor_name="小欣",
            target_event_id="u-offense",
            topic_epoch=4,
            source_event_ids=("u-offense",),
        )
        plan = ReplyPlan.create(
            turn_id="turn-offense",
            chat_id=chat_id,
            chat_kind="group",
            target=target,
            planned_text="好恶心，请你自重。",
            planned_segments=("好恶心，请你自重。",),
            created_at=1001,
        )
        committed = CommittedBotTurn.from_plan(
            plan,
            ReplySendReceipt(
                status=ReplyCommitStatus.SENT,
                sent_segments=("好恶心，请你自重。",),
                sent_at=1001,
            ),
        )
        await store.append_committed_bot_turn(
            committed,
            bot_id="bot",
            bot_name="妃爱",
            stance="reject",
            social_event="boundary_violation",
        )
        await store.observe_social_incident(
            chat_id,
            kind="boundary_violation",
            actor_id="xin",
            actor_name="小欣",
            target_id="bot",
            target_name="妃爱",
            evidence_event_id="u-offense",
            topic_epoch=4,
            stance="reject",
        )
        turns = await store.get_recent_bot_turns(
            chat_id,
            target_sender_id="xin",
            now=2000,
            ttl_seconds=2000,
        )
        incidents = await store.get_social_incidents(
            chat_id,
            current_sender_id="xin",
            now=2000,
            ttl_seconds=2000,
        )
        pending = await store.get_pending_direct_items(
            chat_id,
            current_sender_id="xin",
            include_answered=True,
            now=2000,
            ttl_seconds=2000,
        )
        return turns, incidents, pending

    turns, incidents, pending = asyncio.run(run())

    assert turns[-1].target_sender_id == "xin"
    assert turns[-1].source_event_ids == ["u-offense"]
    assert turns[-1].stance == "reject"
    assert incidents[-1].stance == "reject"
    assert pending[-1].status == "answered"
