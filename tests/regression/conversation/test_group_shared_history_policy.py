from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from astrmai.conversation.attention.group_dialogue_store import GroupDialogueStore
from astrmai.conversation.contracts.dialog_history_policy import DialogHistoryPolicy
from astrmai.conversation.contracts.focus_context import FocusThreadContext
from astrmai.conversation.execution.executor import ConcurrentExecutor
from astrmai.conversation.execution.reply_post_send import ReplyPostSendMixin
from astrmai.conversation.planning.conversation_continuity import ConversationContinuityStore
from astrmai.conversation.planning.planner import Planner
from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin


class _GroupEvent:
    def __init__(self, *, sender_id: str = "10001", group_id: str = "552752264"):
        self.unified_msg_origin = f"ff:GroupMessage:{group_id}"
        self._sender_id = sender_id
        self._group_id = group_id
        self._extras: dict[str, object] = {}

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._group_id

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


def test_group_topic_history_is_shared_across_senders_but_rotates_when_stale():
    store = ConversationContinuityStore()
    chat_id = "ff:GroupMessage:552752264"

    first = store.evaluate_group_message(chat_id, "我们一起包饺子", sender_id="10001", now=1000)
    assert first.history_mode == "none"
    assert first.topic_epoch == 1

    store.record(
        chat_id=chat_id,
        focus_preview="我们一起包饺子",
        goal_summary="讨论包饺子",
        social_intent="chat",
        action_tier="none",
        action_taken="reply",
        reply_preview="好呀",
        sender_id="10001",
        source_event_id="event-a",
        topic_epoch=first.topic_epoch,
        now=1000,
    )

    followup = store.evaluate_group_message(
        chat_id,
        "那加蛋呢",
        sender_id="10002",
        has_reply_reference=True,
        now=1010,
    )
    assert followup.history_mode == "current_topic"
    assert followup.topic_epoch == first.topic_epoch
    assert followup.current_sender_id == "10002"
    assert "reply_reference" in followup.continuity_evidence

    stale = store.evaluate_group_message(
        chat_id,
        "今天天气怎么样",
        sender_id="10002",
        now=1000 + 1801,
    )
    assert stale.history_mode == "none"
    assert stale.topic_epoch == first.topic_epoch + 1
    assert stale.rotation_reason == "topic_stale"


def test_planner_reuses_gate_bound_history_policy_without_reevaluation():
    event = _GroupEvent(sender_id="10001")
    policy = DialogHistoryPolicy(
        history_mode="current_topic",
        group_id="552752264",
        thread_key="group:552752264",
        topic_epoch=7,
        current_sender_id="10001",
        allow_provider_session=True,
    )
    policy.bind(event)
    focus_context = FocusThreadContext(focus_event=event, history_policy=policy)

    class _Continuity:
        def evaluate_group_message(self, *_args, **_kwargs):
            raise AssertionError("Gate-bound policy must not be evaluated twice")

    planner_prompt = object.__new__(PlannerPromptContextMixin)
    planner_prompt.conversation_continuity = _Continuity()

    resolved = planner_prompt._resolve_group_history_policy(
        chat_id=event.unified_msg_origin,
        focus_event=event,
        focus_context=focus_context,
        focus_message_text="周末天气",
    )

    assert resolved is policy


def test_planner_legacy_focus_context_still_evaluates_history_policy():
    event = _GroupEvent(sender_id="10001")
    focus_context = FocusThreadContext(focus_event=event)
    expected = DialogHistoryPolicy(
        history_mode="none",
        group_id="552752264",
        thread_key="group:552752264",
        topic_epoch=3,
        current_sender_id="10001",
    )

    class _Continuity:
        def __init__(self):
            self.calls = 0

        def evaluate_group_message(self, *_args, **_kwargs):
            self.calls += 1
            return expected

    continuity = _Continuity()
    planner_prompt = object.__new__(PlannerPromptContextMixin)
    planner_prompt.conversation_continuity = continuity

    resolved = planner_prompt._resolve_group_history_policy(
        chat_id=event.unified_msg_origin,
        focus_event=event,
        focus_context=focus_context,
        focus_message_text="新话题",
    )

    assert resolved is expected
    assert continuity.calls == 1


def test_explicit_group_history_recall_does_not_reuse_hidden_provider_session():
    store = ConversationContinuityStore()
    chat_id = "ff:GroupMessage:552752264"
    initial = store.evaluate_group_message(chat_id, "聊聊焦糖布丁", sender_id="10001", now=1000)
    store.record(
        chat_id=chat_id,
        focus_preview="聊聊焦糖布丁",
        goal_summary="讨论布丁",
        social_intent="chat",
        action_tier="none",
        action_taken="reply",
        reply_preview="好呀",
        sender_id="10001",
        source_event_id="event-a",
        topic_epoch=initial.topic_epoch,
        now=1000,
    )

    recalled = store.evaluate_group_message(
        chat_id,
        "还记得前几天的布丁称号吗",
        sender_id="10002",
        now=1000 + 7200,
    )

    assert recalled.history_mode == "explicit_recall"
    assert recalled.allow_provider_session is False
    assert recalled.topic_epoch == initial.topic_epoch + 1
    assert recalled.rotation_reason == "explicit_history_recall"


def test_short_followup_bootstraps_recent_history_without_hidden_provider_session():
    store = ConversationContinuityStore()

    policy = store.evaluate_group_message(
        "ff:GroupMessage:552752264",
        "为什么不？",
        sender_id="10002",
        now=1000,
    )

    assert policy.history_mode == "current_topic"
    assert policy.topic_epoch == 1
    assert policy.rotation_reason == "recent_history_bootstrap"
    assert policy.allow_provider_session is False
    assert "short_followup" in policy.continuity_evidence


def test_group_dialogue_is_shared_while_social_state_is_owner_scoped():
    async def run():
        store = GroupDialogueStore()
        chat_id = "ff:GroupMessage:552752264"
        await store.append_segment(
            chat_id,
            event_id="event-a",
            speaker_id="10001",
            speaker_name="Murmure",
            content="布丁放冰箱了",
            role="user",
        )
        await store.append_segment(
            chat_id,
            event_id="event-b",
            speaker_id="10002",
            speaker_name="萤",
            content="我的称号呢",
            role="user",
        )
        await store.upsert_social_candidate(
            chat_id,
            kind="title",
            value="焦糖布丁骑士",
            owner_id="10001",
            owner_name="Murmure",
            topic_epoch=1,
            source_event_id="reply-a",
            now=1000,
        )
        await store.upsert_social_candidate(
            chat_id,
            kind="title",
            value="全宇宙最会戳人的欧尼",
            owner_id="10002",
            owner_name="萤",
            topic_epoch=1,
            source_event_id="reply-b",
            now=1001,
        )
        warm = await store.get_warm_context_bundle(chat_id)
        for_murmure = await store.render_social_context(
            chat_id,
            current_sender_id="10001",
            topic_epoch=1,
            now=1010,
        )
        for_ying = await store.render_social_context(
            chat_id,
            current_sender_id="10002",
            topic_epoch=1,
            now=1010,
        )
        return warm, for_murmure, for_ying

    warm, for_murmure, for_ying = asyncio.run(run())

    assert "Murmure" in warm.quote_text
    assert "萤" in warm.quote_text
    assert "焦糖布丁骑士" in for_murmure
    assert "全宇宙最会戳人的欧尼" not in for_murmure
    assert "全宇宙最会戳人的欧尼" in for_ying
    assert "焦糖布丁骑士" not in for_ying


def test_social_state_snapshot_roundtrip_keeps_owner_and_omits_rejected(tmp_path):
    async def run():
        chat_id = "ff:GroupMessage:552752264"
        source = GroupDialogueStore(snapshot_dir=tmp_path)
        kept = await source.upsert_social_candidate(
            chat_id,
            kind="title",
            value="布丁骑士",
            owner_id="10001",
            owner_name="Murmure",
            source_event_id="reply-a",
            now=1000,
        )
        rejected = await source.upsert_social_candidate(
            chat_id,
            kind="title",
            value="错误称号",
            owner_id="10002",
            owner_name="萤",
            source_event_id="reply-b",
            now=1001,
        )
        assert kept is not None and rejected is not None
        await source.set_social_state_status(chat_id, kept.state_id, "confirmed", now=1002)
        await source.set_social_state_status(chat_id, rejected.state_id, "rejected", now=1002)
        assert await source.persist_snapshot()

        restored = GroupDialogueStore(snapshot_dir=tmp_path)
        restored_count = await restored.restore_snapshot()
        for_owner = await restored.render_social_context(
            chat_id,
            current_sender_id="10001",
            now=1003,
        )
        for_other = await restored.render_social_context(
            chat_id,
            current_sender_id="10002",
            now=1003,
        )
        payload = json.loads((tmp_path / source.SNAPSHOT_FILENAME).read_text(encoding="utf-8"))
        return restored_count, for_owner, for_other, payload

    restored_count, for_owner, for_other, payload = asyncio.run(run())

    assert restored_count == 1
    assert "布丁骑士" in for_owner
    assert "已确认" in for_owner
    assert "错误称号" not in for_other
    assert payload["social_states"]


def test_snapshot_restore_counts_a_chat_once_when_dialogue_and_social_state_coexist(tmp_path):
    async def run():
        chat_id = "ff:GroupMessage:552752264"
        source = GroupDialogueStore(snapshot_dir=tmp_path)
        await source.append_segment(
            chat_id,
            event_id="event-a",
            speaker_id="10001",
            speaker_name="Murmure",
            content="布丁放冰箱了",
            timestamp=1000,
        )
        candidate = await source.upsert_social_candidate(
            chat_id,
            kind="title",
            value="布丁骑士",
            owner_id="10001",
            owner_name="Murmure",
            source_event_id="reply-a",
            now=1000,
        )
        assert candidate is not None
        await source.set_social_state_status(chat_id, candidate.state_id, "confirmed", now=1001)
        assert await source.persist_snapshot()

        restored = GroupDialogueStore(snapshot_dir=tmp_path)
        return await restored.restore_snapshot()

    assert asyncio.run(run()) == 1


def test_social_candidate_extraction_avoids_question_tail_false_positive():
    candidates = Planner._extract_group_social_candidates(
        "「焦糖布丁骑士」——这个称号满意吗？不满意还能再改。"
    )

    assert ("title", "焦糖布丁骑士") in candidates
    assert ("title", "满意吗") not in candidates


def test_group_dialog_lane_is_shared_by_topic_epoch_not_sender():
    executor = object.__new__(ConcurrentExecutor)
    event_a = _GroupEvent(sender_id="10001")
    event_b = _GroupEvent(sender_id="10002")
    DialogHistoryPolicy(
        history_mode="current_topic",
        group_id="552752264",
        thread_key="group:552752264",
        topic_epoch=4,
        current_sender_id="10001",
        allow_provider_session=True,
    ).bind(event_a)
    DialogHistoryPolicy(
        history_mode="current_topic",
        group_id="552752264",
        thread_key="group:552752264",
        topic_epoch=4,
        current_sender_id="10002",
        allow_provider_session=True,
    ).bind(event_b)

    lane_a, origin_a = executor._resolve_dialog_lane_identity(event_a, event_a.unified_msg_origin)
    lane_b, origin_b = executor._resolve_dialog_lane_identity(event_b, event_b.unified_msg_origin)

    assert lane_a.scope_id == lane_b.scope_id
    assert origin_a == origin_b
    assert "10001" not in lane_a.scope_id
    assert "10002" not in lane_b.scope_id

    event_next = _GroupEvent(sender_id="10001")
    DialogHistoryPolicy(
        history_mode="current_topic",
        group_id="552752264",
        thread_key="group:552752264",
        topic_epoch=5,
        current_sender_id="10001",
        allow_provider_session=True,
    ).bind(event_next)
    next_lane, next_origin = executor._resolve_dialog_lane_identity(
        event_next,
        event_next.unified_msg_origin,
    )
    assert next_lane.scope_id != lane_a.scope_id
    assert next_origin != origin_a


def test_topic_lane_identity_is_shared_by_executor_planner_and_reply_history():
    event = _GroupEvent(sender_id="10001")
    policy = DialogHistoryPolicy(
        history_mode="current_topic",
        group_id="552752264",
        thread_key="group:552752264",
        topic_epoch=7,
        current_sender_id="10001",
        allow_provider_session=True,
    )
    policy.bind(event)
    calls = []

    class _LaneManager:
        async def get_recent_transcript(self, *, lane_key, base_origin, **kwargs):
            calls.append(("planner", lane_key, base_origin))
            return "recent transcript"

        async def get_lane_history(self, *, lane_key, base_origin):
            calls.append(("reply", lane_key, base_origin))
            return [{"role": "user", "content": "hello"}]

    lane_manager = _LaneManager()
    executor = object.__new__(ConcurrentExecutor)
    planner_prompt = object.__new__(PlannerPromptContextMixin)
    planner_prompt.gateway = SimpleNamespace(lane_manager=lane_manager)
    reply_post_send = object.__new__(ReplyPostSendMixin)
    reply_post_send.config = SimpleNamespace(
        attention=SimpleNamespace(bg_pool_size=20)
    )
    reply_post_send.state_engine = SimpleNamespace(
        gateway=SimpleNamespace(lane_manager=lane_manager)
    )

    async def _run():
        executor_identity = executor._resolve_dialog_lane_identity(
            event,
            event.unified_msg_origin,
        )
        transcript = await planner_prompt._get_recent_dialogue_transcript(
            event.unified_msg_origin,
            event=event,
            history_policy=policy,
        )
        history = await reply_post_send._fetch_history(
            event.unified_msg_origin,
            "hello",
            event,
        )
        return executor_identity, transcript, history

    executor_identity, transcript, history = asyncio.run(_run())

    assert transcript == "recent transcript"
    assert history == [{"role": "user", "content": "hello"}]
    assert calls[0][1:] == executor_identity
    assert calls[1][1:] == executor_identity
