import json
import re
import time
from types import SimpleNamespace

from astrmai.infrastructure.runtime.runtime_status_schema import (
    aggregate_provider_telemetry,
    aggregate_stage_waits,
    aggregate_turn_telemetry,
    build_runtime_status_schema,
    effective_config_fingerprint,
    normalize_api_base,
    utc_now_iso,
)
from astrmai.conversation.attention.decision_router import AttentionDecisionRouter


def test_schema_envelope_version_and_utc_timestamp():
    payload = build_runtime_status_schema(runtime_status={"runtime_generation": 3})
    assert payload["runtime_status_schema_version"] == 1
    assert payload["runtime_generation"] == 3
    assert payload["generated_at"].endswith("Z")
    assert re.match(r"^\d{4}-\d{2}-\d{2}T.*Z$", utc_now_iso())


def test_missing_measurements_remain_null_and_domains_stay_separate():
    payload = build_runtime_status_schema(
        attention={"queue_wait_ms": 12.5},
        gateway={"provider_latency_p95_ms": 91.0},
    )
    assert payload["queues"]["attention_queue_wait_ms"] == 12.5
    assert payload["queues"]["gateway_semaphore_wait_ms"] is None
    assert payload["provider"]["provider_latency_p95_ms"] == 91.0
    assert payload["provider"]["retry_count"] is None


def test_aggregate_percentiles_never_fill_single_turn_total_elapsed():
    payload = build_runtime_status_schema(
        turns={"elapsed_ms_p50": 12.0, "elapsed_ms_p95": 40.0, "elapsed_ms_p99": 80.0, "sample_size": 3}
    )
    assert payload["turns"]["total_elapsed_ms"] is None
    assert payload["turns"]["total_elapsed_p50_ms"] == 12.0
    assert payload["turns"]["total_elapsed_p95_ms"] == 40.0
    assert payload["turns"]["total_elapsed_p99_ms"] == 80.0


def test_fingerprint_is_stable_changes_with_effective_config_and_excludes_secrets():
    config = SimpleNamespace(
        provider=SimpleNamespace(model="m1", source="openai", api_base="HTTPS://API.Example/v1/?key=secret"),
        infra=SimpleNamespace(api_timeout=20, llm_retries=2, backoff_factor=1.5, max_concurrent_llm_calls=3),
    )
    first = effective_config_fingerprint(config)
    second = effective_config_fingerprint(config)
    assert first == second
    assert first != effective_config_fingerprint(
        SimpleNamespace(provider=SimpleNamespace(model="m2", source="openai", api_base="https://api.example/v1"), infra=config.infra)
    )
    assert "secret" not in first
    assert normalize_api_base("HTTPS://API.Example/v1/?key=secret") == "https://api.example/v1"


def test_fingerprint_changes_when_real_model_pool_or_background_budget_changes():
    base = SimpleNamespace(
        provider=SimpleNamespace(
            fallback_models=["source/fallback"],
            agent_models=["source/agent"],
            task_models=["source/task"],
            vision_models=["source/vision"],
            embedding_models=["source/embed"],
        ),
        infra=SimpleNamespace(
            background_task_concurrency=2,
            background_task_queue_limit=64,
            background_task_wait_timeout_sec=120,
            background_task_execution_timeout_sec=300,
        ),
    )
    changed_pool = SimpleNamespace(provider=SimpleNamespace(**{**vars(base.provider), "task_models": ["source/other"]}), infra=base.infra)
    changed_budget = SimpleNamespace(provider=base.provider, infra=SimpleNamespace(**{**vars(base.infra), "background_task_queue_limit": 65}))
    assert effective_config_fingerprint(base) != effective_config_fingerprint(changed_pool)
    assert effective_config_fingerprint(base) != effective_config_fingerprint(changed_budget)


def test_unknown_degraded_and_blocked_are_not_reported_as_ready():
    for status, expected in (({}, "unknown"), ({"shutdown_final_status": "degraded"}, "degraded"), ({"boot_phase": "blocked"}, "blocked")):
        payload = build_runtime_status_schema(runtime_status=status)
        assert payload["lifecycle_status"] == expected


def test_schema_is_json_serializable_and_memory_shutdown_are_mapped():
    payload = build_runtime_status_schema(
        runtime_status={"is_running": True, "accepting_events": True, "runtime_generation": 7},
        memory={"resource_state": "active", "health_status": "healthy", "generation": 7},
        shutdown={"remaining": 2, "remaining_by_kind": {"memory.vector": 1}},
    )
    json.dumps(payload)
    assert payload["lifecycle_status"] == "ready"
    assert payload["memory"]["resource_state"] == "active"
    assert payload["shutdown"]["pending"] == 2


def test_memory_circuit_health_does_not_become_resource_lifecycle():
    payload = build_runtime_status_schema(
        memory={"circuit_state": "open", "health_status": "degraded", "resources": [{"state": "active"}]}
    )
    assert payload["memory"]["resource_state"] == "active"
    assert payload["memory"]["lifecycle_status"] == "active"
    assert payload["memory"]["health_status"] == "degraded"


def test_invalid_top_level_status_is_reported_fail_closed():
    payload = build_runtime_status_schema(runtime_status={"boot_phase": "not-a-runtime-state"}, attention=[])
    assert payload["lifecycle_status"] == "unknown"
    assert "runtime_status.boot_phase:invalid_state" in payload["validation_errors"]
    assert "attention:expected_mapping" in payload["validation_errors"]


def test_attention_read_only_status_does_not_prune_runtime_state():
    router = AttentionDecisionRouter(SimpleNamespace())
    router._judge_ignore_cache["expired"] = (time.time() - 10.0, "IGNORE", 1)
    before = dict(router._judge_ignore_cache)
    router.describe_status(read_only=True)
    assert router._judge_ignore_cache == before


def test_provider_telemetry_aggregates_logical_calls_and_attempt_counters():
    traces = [
        {
            "turn_id": "turn-1",
            "llm_call_ledger": [
                {
                    "call_id": "call-1",
                    "status": "success",
                    "elapsed_ms": 40.0,
                    "model_attempts": [
                        {"retry_index": 0, "status": "error", "fallback": False},
                        {"retry_index": 1, "status": "success", "fallback": True},
                    ],
                },
                {
                    "call_id": "call-2",
                    "status": "timeout",
                    "elapsed_ms": 90.0,
                    "error_kind": "timeout",
                    "model_attempts": [],
                },
            ],
            "stage_ledger": [{"stage": "gateway.retry_backoff", "status": "success", "elapsed_ms": 3.0}],
        },
        # A repeated snapshot must not double count the calls.
        {
            "turn_id": "turn-1",
            "llm_call_ledger": [
                {"call_id": "call-1", "status": "success", "elapsed_ms": 40.0}
            ],
        },
    ]
    metrics = aggregate_provider_telemetry(traces)
    assert metrics["provider_request_count"] == 2
    assert metrics["provider_success_count"] == 1
    assert metrics["provider_timeout_count"] == 1
    assert metrics["retry_count"] == 1
    assert metrics["fallback_count"] == 1
    assert metrics["retry_backoff_ms"] == 3.0
    assert metrics["provider_latency_max_ms"] == 90.0
    assert metrics["measurement_scope"] == "runtime_turn_traces"


def test_stage_wait_telemetry_maps_real_stage_names_and_preserves_nulls():
    metrics = aggregate_stage_waits(
        [
            {
                "turn_id": "turn-1",
                "stage_ledger": [
                    {"stage": "gateway.semaphore_wait", "elapsed_ms": 7.0},
                    {"stage": "system2.chat_lock_wait", "elapsed_ms": 11.0},
                    {"stage": "executor.chat_lock_wait", "elapsed_ms": 13.0},
                ],
            }
        ]
    )
    assert metrics["gateway_semaphore_wait_ms"] == 7.0
    assert metrics["sys2_lock_wait_ms"] == 11.0
    assert metrics["executor_lock_wait_ms"] == 13.0
    assert metrics["lane_lock_wait_ms"] is None
    assert metrics["attention_queue_wait_ms"] is None


def test_runtime_schema_uses_real_trace_producer_and_multiturn_total_is_null():
    traces = [
        {"turn_id": "t1", "trace_id": "trace-1", "started_at": 100.0, "turn_total_elapsed_ms": 20.0,
         "status": "completed", "llm_call_ledger": [{"call_id": "c1", "status": "success", "elapsed_ms": 5.0}]},
        {"turn_id": "t2", "trace_id": "trace-2", "started_at": 200.0, "turn_total_elapsed_ms": 80.0,
         "status": "completed", "llm_call_ledger": [{"call_id": "c2", "status": "success", "elapsed_ms": 15.0,
                                                        "model_attempts": [{"retry_index": 1, "fallback": True}]}],
         "stage_ledger": [{"stage": "gateway.tool_semaphore_wait", "elapsed_ms": 9.0}]},
    ]
    payload = build_runtime_status_schema(traces=traces)
    assert payload["turns"]["total_elapsed_ms"] is None
    assert payload["turns"]["total_elapsed_p95_ms"] == 80.0
    assert len(payload["turns"]["records"]) == 2
    assert payload["provider"]["provider_request_count"] == 2
    assert payload["provider"]["retry_count"] == 1
    assert payload["queues"]["gateway_semaphore_wait_ms"] == 9.0


def test_background_kind_metrics_keep_mapping_and_sum_counters():
    payload = build_runtime_status_schema(
        background={
            "queue_wait_ms_by_kind": {"attention.judge": {"p95_ms": 12.0}},
            "cancelled_by_kind": {"attention.judge": 2, "dream": 1},
            "timed_out_by_kind": {"dream": 3},
            "rejected_by_kind": {"dream": 4},
            "shutdown_rejected_by_kind": {"dream": 5},
        }
    )
    assert payload["queues"]["background_budget_queue_wait_ms"] is None
    assert payload["queues"]["background_budget_queue_wait_ms_by_kind"]["attention.judge"]["p95_ms"] == 12.0
    assert payload["background"]["cancelled_count"] == 3
    assert payload["background"]["queue_timeout_count"] == 3
    assert payload["background"]["queue_full_count"] == 4
    assert payload["background"]["shutdown_rejected_count"] == 5
