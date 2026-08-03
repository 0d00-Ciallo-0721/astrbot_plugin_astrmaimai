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
                    {
                        "stage": "planner.final_prompt_sources",
                        "total_chars": 5000,
                        "duplicate_block_count": 2,
                        "metadata": {"scope": "source"},
                    },
                    {
                        "stage": "planner.final_prompt_transmitted",
                        "total_chars": 3200,
                        "duplicate_block_count": 0,
                        "metadata": {"scope": "transmitted"},
                    },
                ],
                "decision_observation": {
                    "wait_reason": "group_ambient_short_wait",
                    "stale_category": "newer_activity_same_thread",
                    "judge_outcome": "reply",
                },
                "budget": {"remaining_ms": 120000, "exhausted": False},
                "memory_funnel": {
                    "status": "injected",
                    "candidate_count": 10,
                    "selected_count": 3,
                    "rendered_chars": 500,
                    "query_rewrite_trace": {"status": "success"},
                },
            }
        ]
    )

    assert report["reply"]["chars_max"] == 321
    assert report["llm"]["attempts_per_call_p95"] == 2
    assert report["llm"]["judge_calls_per_turn_p95"] == 1
    assert report["llm"]["path_counts"] == {"critical": 1}
    assert report["context"]["duplicate_block_count"] == 2
    assert report["context"]["by_scope"]["source"]["duplicate_block_count"] == 2
    assert report["context"]["by_scope"]["transmitted"]["chars_p50"] == 3200
    assert report["decision"]["wait_reasons"] == {"group_ambient_short_wait": 1}
    assert report["decision"]["stale_categories"] == {"newer_activity_same_thread": 1}
    assert report["decision"]["judge_outcomes"] == {"reply": 1}
    assert report["budget"]["exhausted_count"] == 0
    assert report["query_rewrite"]["status_counts"] == {"success": 1}
    assert report["memory"]["selection_rate"] == 0.3


def test_analyze_traces_counts_abandoned_calls_budget_and_attempt_failures():
    report = analyze_traces(
        [
            {
                "turn_id": "turn-2",
                "chat_id": "chat-2",
                "status": "skipped_wait",
                "llm_call_ledger": [
                    {
                        "stage": "memory.query_rewrite",
                        "status": "abandoned",
                        "model_attempts": [
                            {"status": "error", "error_kind": "provider_failure"},
                            {"status": "cooldown_skipped", "error_kind": "provider_cooldown"},
                        ],
                    }
                ],
                "decision_observation": {
                    "wait_reason": "cognitive_wait",
                    "judge_timeout": True,
                },
                "budget": {"remaining_ms": 0, "exhausted": True},
                "memory_funnel": {
                    "query_rewrite_trace": {"fallback_reason": "timeout"},
                },
            }
        ]
    )

    assert report["llm"]["status_counts"] == {"abandoned": 1}
    assert report["llm"]["model_attempt_status_counts"] == {
        "error": 1,
        "cooldown_skipped": 1,
    }
    assert report["llm"]["model_attempt_error_counts"] == {
        "provider_failure": 1,
        "provider_cooldown": 1,
    }
    assert report["decision"]["judge_outcomes"] == {"timeout": 1}
    assert report["budget"]["exhausted_count"] == 1
    assert report["query_rewrite"]["status_counts"] == {"timeout": 1}


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


def test_analyzer_reads_nested_tool_disclosure_and_execution_fields():
    report = analyze_traces(
        [
            {
                "turn_id": "tool-turn",
                "chat_id": "chat-1",
                "status": "executed",
                "tools": {
                    "requested_tier": "chat",
                    "final_tier": "task",
                    "disclosure_enabled": True,
                    "disclosure_tier": "task",
                    "disclosure_packages": ["core", "identity"],
                    "disclosure_second_pass_packages": ["persona_lore"],
                    "disclosure_expanded_packages": ["identity"],
                    "explicit_tool_intent": True,
                    "intent_contracts": [{"tool_name": "qq_friend_lookup"}],
                    "contract_outcomes": [{"outcome": "satisfied"}],
                    "contract_unsatisfied": [],
                    "correction_pass_used": True,
                    "correction_packages": ["identity"],
                    "second_pass_resolution": "satisfied",
                    "second_pass_selected_tools": ["qq_friend_lookup"],
                },
                "tool_ledger_summary": {
                    "tool_disclosure_tier": "task",
                    "tool_call_count": 1,
                },
                "tool_execution_trace": [
                    {
                        "tool_name": "qq_friend_lookup",
                        "family": "friend_fact",
                        "status": "success",
                        "source_domain": "platform_friend",
                        "operation": "list",
                    }
                ],
            }
        ]
    )

    tools = report["tools"]
    assert tools["trace_present_count"] == 1
    assert tools["ledger_summary_present_count"] == 1
    assert tools["field_presence_counts"]["intent_contracts"] == 1
    assert tools["second_pass_resolution_counts"] == {"satisfied": 1}
    assert tools["execution_name_counts"] == {"qq_friend_lookup": 1}
    assert tools["execution_missing_structure"] == {}
    assert "## Tool Disclosure" in render_markdown(report)
