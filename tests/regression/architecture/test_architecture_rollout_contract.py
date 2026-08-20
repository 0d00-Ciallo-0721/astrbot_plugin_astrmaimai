from __future__ import annotations

from types import SimpleNamespace

from astrmai.conversation.contracts.committed_reply import (
    CommittedBotTurn,
    ReplyCommitStatus,
    ReplyPlan,
    ReplySendReceipt,
)
from astrmai.conversation.contracts.conversation_event import ConversationEvent
from astrmai.conversation.contracts.turn_context import TurnContext
from astrmai.conversation.contracts.turn_target import ActorSet, TargetKind, TurnTarget
from astrmai.conversation.runtime.architecture_rollout import rollout_state
from astrmai.conversation.runtime.architecture_trace import (
    ARCHITECTURE_TRACE_SCHEMA_VERSION,
    build_architecture_trace_contract,
)


class _Event:
    def __init__(self) -> None:
        self._extras = {}

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value) -> None:
        self._extras[key] = value


def _config(**overrides):
    defaults = {
        "shadow_enabled": True,
        "canonical_read_enabled": True,
        "turn_target_read_enabled": True,
        "committed_history_enabled": True,
        "context_renderer_enabled": True,
        "memory_actor_filter_enabled": True,
        "proactive_due_enabled": True,
    }
    defaults.update(overrides)
    return SimpleNamespace(
        architecture_rollout=SimpleNamespace(**defaults),
        attention=SimpleNamespace(
            participation_force_pass_enabled=True,
            participation_drop_enabled=True,
        ),
    )


def test_rollout_cutovers_are_independent_and_reported():
    state = rollout_state(
        _config(
            canonical_read_enabled=False,
            context_renderer_enabled=False,
            proactive_due_enabled=False,
        )
    )

    assert state["shadow_enabled"] is True
    assert state["canonical_read_enabled"] is False
    assert state["turn_target_read_enabled"] is True
    assert state["committed_history_enabled"] is True
    assert state["context_renderer_enabled"] is False
    assert state["memory_actor_filter_enabled"] is True
    assert state["proactive_due_enabled"] is False
    assert state["participation_force_pass_enabled"] is True
    assert state["participation_drop_enabled"] is True


def test_architecture_trace_contract_links_full_chain_without_raw_reply_text():
    event = _Event()
    canonical = ConversationEvent(
        event_id="evt-1",
        chat_id="ff:GroupMessage:fixture",
        chat_kind="group",
        timestamp=1000.0,
        actor_id="actor-1",
        actor_name="Alice",
        visible_text="用户原文不应进入架构契约",
        rich_text="用户原文不应进入架构契约",
        role="user",
        message_kind="text",
    )
    target = TurnTarget(
        target_kind=TargetKind.ACTOR,
        target_actor_id="actor-1",
        target_actor_name="Alice",
        target_event_id="evt-1",
        source_event_ids=("evt-1",),
        confidence=1.0,
    )
    actor_set = ActorSet(current_actor_id="actor-1", bot_id="bot-1")
    context = TurnContext()
    context.attention.window_events = [canonical]
    context.attention.turn_target = target
    context.attention.actor_set = actor_set
    context.attention.judge_action = "REPLY"
    context.memory.actor_whitelist = ["actor-1", "bot-1"]
    plan = ReplyPlan.create(
        turn_id="turn-1",
        chat_id=canonical.chat_id,
        chat_kind="group",
        target=target,
        planned_text="绝密草稿文本",
        planned_segments=("绝密草稿文本",),
        created_at=1001.0,
    )
    committed = CommittedBotTurn.from_plan(
        plan,
        ReplySendReceipt(
            status=ReplyCommitStatus.SENT,
            sent_segments=("实际可见回复文本",),
            sent_at=1002.0,
        ),
    )
    event.set_extra("astrmai_conversation_event", canonical)
    event.set_extra("astrmai_reply_plan", plan)
    event.set_extra("astrmai_committed_bot_turn", committed)
    event.set_extra("astrmai_judge_cache_hit", True)
    event.set_extra("astrmai_judge_cache_action", "IGNORE")
    event.set_extra("astrmai_judge_cache_scope", "ambient_topic")
    event.set_extra("astrmai_judge_avoided", True)
    event.set_extra("astrmai_prefilter_judge_agreement", False)

    contract = build_architecture_trace_contract(
        event=event,
        turn_context=context,
        trace_item={
            "turn_id": "turn-1",
            "turn_total_elapsed_ms": 12.5,
            "timing_coverage": {"coverage_ratio": 0.4, "unattributed_ms": 7.5},
            "stage_ledger": [{"stage": "attention.dispatch", "elapsed_ms": 2.0}],
            "context_block_stats": [{"block_type": "shared_timeline", "char_count": 120}],
            "memory_funnel": {
                "vector_fallback": {"source": "bm25", "used": True},
                "hybrid_observations": [{"vector": {"status": "timeout"}}],
            },
        },
        status="executed",
        config=_config(),
    )
    serialized = repr(contract)

    assert contract["schema_version"] == ARCHITECTURE_TRACE_SCHEMA_VERSION
    assert contract["input_event_ids"] == ["evt-1"]
    assert contract["turn_target"]["target_actor_id"] == "actor-1"
    assert contract["actor_whitelist"] == ["actor-1", "bot-1"]
    assert contract["judge_decision"]["action"] == "REPLY"
    assert contract["judge_decision"]["cache_hit"] is True
    assert contract["judge_decision"]["cache_action"] == "IGNORE"
    assert contract["judge_decision"]["cache_scope"] == "ambient_topic"
    assert contract["judge_decision"]["avoided"] is True
    assert contract["judge_decision"]["prefilter_judge_agreement"] is False
    assert contract["timing_coverage"]["unattributed_ms"] == 7.5
    assert contract["stage_ledger"][0]["stage"] == "attention.dispatch"
    assert contract["reply_plan"]["planned_char_count"] == len("绝密草稿文本")
    assert contract["reply_commit"]["commit_id"] == committed.commit_id
    assert contract["memory_actor_filter"]["actor_whitelist"] == ["actor-1", "bot-1"]
    assert contract["memory_retrieval_observation"]["vector_fallback"]["source"] == "bm25"
    assert contract["memory_retrieval_observation"]["hybrid_observations"][0]["vector"]["status"] == "timeout"
    assert contract["status"] == "executed"
    assert "绝密草稿文本" not in serialized
    assert "实际可见回复文本" not in serialized
    assert "用户原文不应进入架构契约" not in serialized
