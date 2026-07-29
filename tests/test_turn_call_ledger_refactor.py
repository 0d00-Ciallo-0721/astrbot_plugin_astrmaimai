import asyncio
from types import SimpleNamespace

from astrmai.infrastructure.runtime.turn_call_ledger import (
    CALL_LEDGER_KEY,
    CONTEXT_BLOCK_STATS_KEY,
    BACKGROUND_TASK_LEDGER_KEY,
    REPLY_STATS_KEY,
    STAGE_LEDGER_KEY,
    begin_stage,
    clamp_timeout_to_turn_budget,
    configure_turn_budget,
    finalize_turn_telemetry,
    begin_llm_call,
    finish_llm_call,
    observe_stage,
    attach_background_task_trace,
    record_context_block_stats,
    record_reply_stats,
    record_vision_observation,
    turn_telemetry_scope,
    turn_telemetry_snapshot,
)


class _Event:
    def __init__(self):
        self._extra = {}

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


def test_call_ledger_records_lengths_without_prompt_content():
    event = _Event()

    call_id = begin_llm_call(
        event,
        stage="gateway.chat",
        family="chat_dialog",
        pool="dialog",
        system_prompt="private system prompt",
        prompt="用户的私密原话",
        contexts=["历史一", "历史二"],
        metadata={"lane_enabled": True},
    )
    finish_llm_call(
        event,
        call_id,
        model="model-a",
        provider="provider-a",
        output="回复",
        attempts=2,
    )

    entry = event.get_extra(CALL_LEDGER_KEY)[0]
    assert entry["status"] == "success"
    assert entry["system_chars"] == len("private system prompt")
    assert entry["prompt_chars"] == len("用户的私密原话")
    assert entry["context_count"] == 2
    assert entry["attempts"] == 2
    assert "用户的私密原话" not in str(entry)
    assert "历史一" not in str(entry)


def test_context_block_stats_keep_hashes_and_lengths_only():
    event = _Event()

    record_context_block_stats(
        event,
        stage="planner.final_prompt",
        blocks={"focus": "当前问题", "memory": "一段长期记忆"},
        metadata={"think_level": 2},
    )

    payload = event.get_extra(CONTEXT_BLOCK_STATS_KEY)[0]
    assert payload["blocks"]["focus"]["chars"] == 4
    assert payload["blocks"]["focus"]["nonempty"] is True
    assert payload["blocks"]["focus"]["hash"]
    assert "当前问题" not in str(payload)
    assert payload["metadata"]["think_level"] == 2


def test_context_block_stats_identify_exact_duplicate_blocks():
    event = _Event()

    record_context_block_stats(
        event,
        stage="planner.final_prompt",
        blocks={
            "focus": "同一段上下文",
            "recent": "同一段上下文",
            "memory": "另一段内容",
        },
    )

    payload = event.get_extra(CONTEXT_BLOCK_STATS_KEY)[0]
    assert payload["duplicate_block_count"] == 1
    assert payload["blocks"]["recent"]["duplicate_of"] == "focus"
    assert payload["duplicate_pairs"] == [{"block": "recent", "duplicate_of": "focus"}]


def test_vision_observation_keeps_diagnostics_without_source_or_description():
    event = _Event()

    record_vision_observation(
        event,
        {
            "vision_path": "direct",
            "vision_call_status": "failed",
            "image_count": 1,
            "cache_hit_count": 0,
            "cache_miss_count": 1,
            "singleflight_wait_count": 0,
            "asset_ids": ["opaque-asset-id"],
            "binding_count": 0,
            "failure_stage": "resolve",
            "skip_reason": "remote_fetch_disabled",
            "model_ids": ["vision-model"],
            "analysis_prompt_version": "v2",
            "asset_storage_status": "disabled",
            "final_status": "fallback",
            "source_ref": "https://private.example/image.png",
            "description": "用户图片里的私密文字",
        },
    )

    payload = event.get_extra("astrmai_vision_observation")
    assert payload["failure_stage"] == "resolve"
    assert payload["skip_reason"] == "remote_fetch_disabled"
    assert payload["asset_ids"] == ["opaque-asset-id"]
    assert payload["model_ids"] == ["vision-model"]
    assert payload["analysis_prompt_version"] == "v2"
    assert "source_ref" not in payload
    assert "description" not in payload
    assert "private.example" not in str(payload)
    assert "私密文字" not in str(payload)


def test_tool_ledger_summary_exposes_execution_without_prompt_content():
    event = _Event()
    event.set_extra(
        "astrmai_tool_execution_trace",
        [
            {"tool_name": "qq_friend_lookup", "status": "success"},
            {"tool_name": "send_message", "status": "error", "reason": "target_not_found"},
        ],
    )
    event.set_extra(
        "astrmai_tool_lifecycle_trace",
        [{"tool": "send_message", "phase": "tool_completed", "status": "failed", "reason": "target_not_found"}],
    )
    event.set_extra("astrmai_tool_tier", "chat")
    event.set_extra("astrmai_required_tool_missing", ["send_message"])
    event.set_extra(
        "astrmai_turn_context",
        SimpleNamespace(
            tools=SimpleNamespace(
                available_tools=["qq_friend_lookup", "send_message"],
                disclosure_tier="chat",
            )
        ),
    )
    with turn_telemetry_scope(event) as telemetry:
        first = begin_llm_call(event, stage="gateway.tool", tools=["qq_friend_lookup"], max_steps=4)
        finish_llm_call(event, first, model="model-a")
        second = begin_llm_call(event, stage="gateway.tool", tools=["send_message"], max_steps=4)
        finish_llm_call(event, second, model="model-a", status="error", error_kind="target_not_found")
        record_reply_stats(
            event,
            segment_count=1,
            segment_lengths=[2],
            total_chars=2,
            reply_completed=True,
        )

    summary = turn_telemetry_snapshot(event)["tool_ledger_summary"]
    assert summary["tool_disclosure_tier"] == "chat"
    assert summary["tool_candidates"] == ["qq_friend_lookup", "send_message"]
    assert summary["selected_tool"] == ["qq_friend_lookup"]
    assert summary["tool_call_count"] == 2
    assert summary["tool_loop_steps"] == 2
    assert summary["tool_failure_reason"] == "target_not_found"
    assert summary["required_tool_missing"] == ["send_message"]
    assert summary["final_reply_after_tool"] is True
    assert "target_not_found" not in str(summary["tool_candidates"])


def test_reply_stats_support_multiple_segments_and_sent_count():
    event = _Event()

    record_reply_stats(
        event,
        segment_count=3,
        segment_lengths=[4, 5, 6],
        total_chars=15,
        strategy="smart",
        send_status="sent",
        sent_segment_count=3,
    )

    assert event.get_extra(REPLY_STATS_KEY) == {
        "segment_count": 3,
        "segment_lengths": [4, 5, 6],
        "total_chars": 15,
        "actual_reply_chars": 15,
        "strategy": "smart",
        "send_status": "sent",
        "sent_segment_count": 3,
    }


def test_eventless_call_is_attached_to_active_turn_scope():
    event = _Event()

    with turn_telemetry_scope(event):
        call_id = begin_llm_call(
            None,
            stage="attention.judge",
            family="judge",
            pool="task",
            prompt="不应进入账本的原文",
        )
        finish_llm_call(None, call_id, model="judge-a", output="reply")

    entries = event.get_extra(CALL_LEDGER_KEY)
    assert len(entries) == 1
    assert entries[0]["stage"] == "attention.judge"
    assert entries[0]["model"] == "judge-a"
    assert "不应进入账本的原文" not in str(entries[0])


def test_turn_scopes_are_isolated_across_concurrent_tasks():
    first = _Event()
    second = _Event()

    async def record(event, stage):
        with turn_telemetry_scope(event):
            await asyncio.sleep(0)
            call_id = begin_llm_call(None, stage=stage, prompt=stage)
            await asyncio.sleep(0)
            finish_llm_call(None, call_id, model=f"{stage}-model")

    async def run_both():
        await asyncio.gather(record(first, "first"), record(second, "second"))

    asyncio.run(run_both())

    assert [item["stage"] for item in first.get_extra(CALL_LEDGER_KEY)] == ["first"]
    assert [item["stage"] for item in second.get_extra(CALL_LEDGER_KEY)] == ["second"]


def test_stage_ledger_records_status_without_raw_error():
    event = _Event()

    with turn_telemetry_scope(event):
        with observe_stage(None, "memory.retrieve") as span:
            span["candidate_count"] = 12
            span["selected_count"] = 3

    entry = event.get_extra(STAGE_LEDGER_KEY)[0]
    assert entry["stage"] == "memory.retrieve"
    assert entry["status"] == "success"
    assert entry["elapsed_ms"] >= 0
    assert entry["metadata"]["candidate_count"] == 12
    assert entry["metadata"]["selected_count"] == 3


def test_snapshot_is_versioned_and_contains_no_prompt_text():
    event = _Event()

    with turn_telemetry_scope(event):
        call_id = begin_llm_call(None, stage="dialog", prompt="隐私问题")
        finish_llm_call(None, call_id, output="隐私回答")
        snapshot = turn_telemetry_snapshot()

    assert snapshot["trace_schema_version"] == 2
    assert snapshot["instrumentation_version"]
    assert snapshot["turn_id"] == snapshot["trace_id"]
    assert snapshot["total_elapsed_ms"] >= 0
    assert "隐私问题" not in str(snapshot)
    assert "隐私回答" not in str(snapshot)


def test_dialog_history_policy_summary_contains_ids_but_not_conversation_text():
    event = _Event()
    event.set_extra(
        "astrmai_dialog_history_policy",
        {
            "history_mode": "current_topic",
            "group_id": "552752264",
            "topic_epoch": 7,
            "current_sender_id": "10002",
            "approved_event_ids": ["event-a", "event-b"],
            "allow_provider_session": True,
            "rotation_reason": "weak_window_evidence",
            "continuity_evidence": ["reply_reference"],
            "raw_query": "用户群聊正文不应写入摘要轨迹",
            "topic_text": "焦糖布丁称号",
        },
    )

    with turn_telemetry_scope(event):
        snapshot = turn_telemetry_snapshot(event)

    policy = snapshot["dialog_history_policy"]
    assert policy["history_mode"] == "current_topic"
    assert policy["group_id"] == "552752264"
    assert policy["topic_epoch"] == 7
    assert policy["current_sender_id"] == "10002"
    assert policy["approved_event_ids"] == ["event-a", "event-b"]
    assert policy["provider_session_allowed"] is True
    assert "用户群聊正文" not in str(policy)
    assert "焦糖布丁称号" not in str(policy)


def test_dialog_history_policy_debug_fields_require_explicit_flag():
    event = _Event()
    event.set_extra(
        "astrmai_dialog_history_policy",
        {
            "history_mode": "current_topic",
            "group_id": "552752264",
            "thread_key": "group:552752264",
            "topic_epoch": 7,
            "topic_age_seconds": 12.3456,
            "continuity_evidence": ["reply_reference"],
        },
    )

    with turn_telemetry_scope(event):
        default_snapshot = turn_telemetry_snapshot(event)
    default_policy = default_snapshot["dialog_history_policy"]
    assert default_policy["debug_enabled"] is False
    assert "thread_key" not in default_policy
    assert "topic_age_seconds" not in default_policy
    assert "continuity_evidence" not in default_policy

    event.set_extra("astrmai_group_history_debug_trace_enabled", True)
    with turn_telemetry_scope(event):
        debug_snapshot = turn_telemetry_snapshot(event)
    policy = debug_snapshot["dialog_history_policy"]
    assert policy["debug_enabled"] is True
    assert policy["thread_key"] == "group:552752264"
    assert policy["topic_age_seconds"] == 12.346
    assert policy["continuity_evidence"] == ["reply_reference"]


def test_snapshot_is_detached_from_later_ledger_mutation():
    event = _Event()

    with turn_telemetry_scope(event):
        call_id = begin_llm_call(None, stage="dialog")
        snapshot = turn_telemetry_snapshot()
        finish_llm_call(None, call_id, model="late-model")

    assert snapshot["llm_call_ledger"][0]["status"] == "pending"
    assert "model" not in snapshot["llm_call_ledger"][0]
    assert event.get_extra(CALL_LEDGER_KEY)[0]["status"] == "success"
    assert event.get_extra(CALL_LEDGER_KEY)[0]["model"] == "late-model"


def test_turn_budget_clamps_noncritical_timeout_and_keeps_reply_reserve():
    event = _Event()

    with turn_telemetry_scope(event):
        configure_turn_budget(event, total_budget_sec=120, main_reply_reserve_sec=30)
        timeout = clamp_timeout_to_turn_budget(event, 300, reserve_for_reply=True)
        snapshot = turn_telemetry_snapshot(event)

    assert 0 < timeout <= 90
    assert snapshot["budget"]["total_budget_sec"] == 120
    assert snapshot["budget"]["main_reply_reserve_sec"] == 30
    assert snapshot["budget"]["remaining_ms"] > 0


def test_finalize_turn_telemetry_closes_pending_calls_and_stages():
    event = _Event()

    with turn_telemetry_scope(event):
        begin_llm_call(None, stage="attention.judge")
        begin_stage(None, "memory.query_rewrite")
        finalized = finalize_turn_telemetry(event, outcome="skipped_wait")
        snapshot = turn_telemetry_snapshot(event)

    assert finalized == {"calls": 1, "stages": 1}
    assert snapshot["llm_call_ledger"][0]["status"] == "abandoned"
    assert snapshot["llm_call_ledger"][0]["error_kind"] == "turn_finalized"
    assert snapshot["stage_ledger"][0]["status"] == "abandoned"


def test_reply_completion_and_trace_finalization_have_separate_timings():
    event = _Event()

    record_reply_stats(
        event,
        segment_count=1,
        segment_lengths=[4],
        total_chars=4,
        send_status="sent",
        reply_completed=True,
    )
    finalize_turn_telemetry(event, outcome="completed")
    snapshot = turn_telemetry_snapshot(event)

    assert snapshot["reply_completed_at"] is not None
    assert snapshot["trace_finalized_at"] is not None
    assert snapshot["reply_completed_elapsed_ms"] >= 0
    assert snapshot["trace_finalized_elapsed_ms"] >= snapshot["reply_completed_elapsed_ms"]
    assert snapshot["trace_finalize_lag_ms"] >= 0


def test_background_task_trace_consumes_failures_without_raw_error_text():
    event = _Event()

    async def fail():
        raise RuntimeError("用户私密正文不应进入账本")

    async def run():
        task = asyncio.create_task(fail())
        attach_background_task_trace(task, event, "memory.background")
        await task

    try:
        asyncio.run(run())
    except RuntimeError:
        pass

    entry = event.get_extra(BACKGROUND_TASK_LEDGER_KEY)[0]
    assert entry["status"] == "failed"
    assert entry["error_kind"] == "RuntimeError"
    assert entry["error_hash"]
    assert "用户私密正文" not in str(entry)


def test_reply_stats_include_privacy_safe_freshness_metrics():
    event = _Event()
    event.set_extra("astrmai_reply_freshness_state", "expired")
    event.set_extra("astrmai_reply_stale_category", "age_expired")
    event.set_extra("astrmai_reply_stale_reason", "superseded_by_newer_activity:某用户:消息正文")
    event.set_extra("astrmai_reply_age_sec", 95.4)
    event.set_extra("astrmai_reply_max_age_sec", 90.0)

    record_reply_stats(
        event,
        segment_count=1,
        segment_lengths=[8],
        total_chars=8,
    )

    payload = event.get_extra(REPLY_STATS_KEY)
    assert payload["freshness_state"] == "expired"
    assert payload["stale_category"] == "age_expired"
    assert payload["stale_reason"] == "superseded_by_newer_activity"
    assert payload["reply_age_sec"] == 95.4
    assert payload["reply_max_age_sec"] == 90.0
    assert "某用户" not in str(payload)
    assert "消息正文" not in str(payload)


def test_reply_stats_include_short_reply_policy_without_message_content():
    event = _Event()

    record_reply_stats(
        event,
        segment_count=1,
        segment_lengths=[12],
        total_chars=12,
        metadata={
            "reply_shape_mode": "micro",
            "reply_shape_reason": "known_micro_utterance",
            "humanlike_short_reply_applied": True,
            "humanlike_short_reply_constraints": "capped_sentence_count",
            "humanlike_short_reply_before_len": 42,
            "humanlike_short_reply_after_len": 12,
            "visible_text": "不应写入账本的回复正文",
        },
    )

    payload = event.get_extra(REPLY_STATS_KEY)
    assert payload["reply_shape_mode"] == "micro"
    assert payload["humanlike_short_reply_applied"] is True
    assert payload["humanlike_short_reply_before_len"] == 42
    assert "visible_text" not in payload
    assert "不应写入账本" not in str(payload)
