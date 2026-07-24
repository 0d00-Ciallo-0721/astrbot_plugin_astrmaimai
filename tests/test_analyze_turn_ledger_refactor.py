import json

from scripts.analyze_turn_ledger import analyze_traces, load_traces, render_markdown


def test_load_traces_prefers_v2_recent_and_filters_instrumentation(tmp_path):
    payload = {
        "version": 2,
        "recent": [
            {"turn_id": "old", "created_at": 1, "instrumentation_version": "v1"},
            {"turn_id": "new", "created_at": 2, "instrumentation_version": "v2"},
        ],
        "by_chat": {"chat": [{"turn_id": "duplicate", "created_at": 3}]},
    }
    path = tmp_path / "turn_trace_samples.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    traces = load_traces(path, instrumentation_version="v2")

    assert [trace["turn_id"] for trace in traces] == ["new"]


def test_analyze_traces_uses_real_reply_length_and_exact_attempts():
    report = analyze_traces(
        [
            {
                "turn_id": "turn-1",
                "chat_id": "chat-1",
                "status": "replied",
                "reply_sent": True,
                "instrumentation_version": "v2",
                "reply_stats": {"actual_reply_chars": 321},
                "llm_call_ledger": [
                    {
                        "stage": "attention.judge",
                        "critical_path": True,
                        "status": "success",
                        "elapsed_ms": 1200,
                        "model_attempts": [
                            {"model": "a", "status": "error"},
                            {"model": "b", "status": "success"},
                        ],
                    }
                ],
                "stage_ledger": [
                    {"stage": "reply.send", "status": "success", "elapsed_ms": 30},
                ],
                "context_block_stats": [
                    {"total_chars": 5000, "duplicate_block_count": 2},
                ],
                "memory_funnel": {
                    "status": "injected",
                    "candidate_count": 10,
                    "selected_count": 3,
                    "rendered_chars": 500,
                },
            }
        ]
    )

    assert report["reply"]["chars_max"] == 321
    assert report["llm"]["attempts_per_call_p95"] == 2
    assert report["llm"]["judge_calls_per_turn_p95"] == 1
    assert report["llm"]["path_counts"] == {"critical": 1}
    assert report["context"]["duplicate_block_count"] == 2
    assert report["memory"]["selection_rate"] == 0.3


def test_analyzer_never_renders_trace_content():
    report = analyze_traces(
        [
            {
                "turn_id": "turn-1",
                "chat_id": "chat-1",
                "status": "skipped_wait",
                "reply_preview": "PRIVATE_REPLY",
                "perception": {"text_preview": "PRIVATE_QUERY"},
                "decision_observation": {"skip_reason": "wait_for_more_context"},
            }
        ]
    )

    rendered = render_markdown(report)

    assert "PRIVATE_REPLY" not in rendered
    assert "PRIVATE_QUERY" not in rendered
    assert "wait_for_more_context" in rendered
