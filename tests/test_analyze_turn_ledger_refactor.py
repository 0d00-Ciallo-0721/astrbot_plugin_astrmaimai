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


def test_load_traces_merges_later_jsonl_snapshots_instead_of_dropping_them(tmp_path):
    path = tmp_path / "turn_trace_samples.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"turn_id": "turn-1", "created_at": 1, "status": "skipped_wait"}),
                json.dumps(
                    {
                        "turn_id": "turn-1",
                        "created_at": 2,
                        "status": "executed",
                        "vision_observation": {
                            "vision_path": "direct",
                            "vision_call_status": "success",
                            "image_count": 1,
                            "analyzed_count": 1,
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    traces = load_traces(path)

    assert len(traces) == 1
    assert traces[0]["status"] == "executed"
    assert traces[0]["vision_observation"]["vision_path"] == "direct"


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
                "timing_coverage": {
                    "coverage_ratio": 0.8,
                    "instrumented_ms": 800,
                    "unattributed_ms": 200,
                    "first_observed_delay_ms": 20,
                    "post_last_observed_delay_ms": 80,
                    "max_unattributed_gap_ms": 100,
                },
                "topic_observation": {
                    "valid": True,
                    "kind": "message",
                    "reason": "semantic_topic_text",
                    "effective_user_response": True,
                },
                "proactive_observation": {
                    "dispatch_status": "sent",
                    "blocked_reason": "",
                    "stage_ledger": [
                        {"stage": "proactive.reply_commit", "status": "success"},
                    ],
                },
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
                "judge_decision": {
                    "cache_hit": True,
                    "cache_action": "IGNORE",
                    "cache_scope": "ambient_topic",
                    "avoided": True,
                    "prefilter_judge_agreement": False,
                },
                "budget": {"remaining_ms": 120000, "exhausted": False},
                "memory_funnel": {
                    "status": "injected",
                    "candidate_count": 10,
                    "selected_count": 3,
                    "rendered_chars": 500,
                    "hybrid_observations": [
                        {
                            "vector": {
                                "status": "timeout",
                                "timeout_origin": "faiss_index",
                                "query_queue_wait_ms": 12.5,
                                "runtime_metrics": {"degraded_ratio": 0.25},
                            }
                        }
                    ],
                    "vector_fallback": {"source": "bm25", "used": True},
                    "query_rewrite_trace": {"status": "success"},
                },
                "vision_observation": {
                    "vision_path": "direct",
                    "vision_call_status": "success",
                    "vision_wait_ms": 25,
                    "image_count": 1,
                    "raw_image_count": 2,
                    "candidate_ref_count": 3,
                    "resolved_count": 1,
                    "vision_model_attempt_count": 1,
                    "analyzed_count": 1,
                    "failure_disposition": "continue_text_only",
                    "reply_guard_action": "allowed",
                    "resolve_failure_reasons": ["get_image_failed"],
                },
            }
        ]
    )

    assert report["reply"]["chars_max"] == 321
    assert report["llm"]["attempts_per_call_p95"] == 2
    assert report["llm"]["judge_calls_per_turn_p95"] == 1
    assert report["llm"]["judge_observation_count"] == 1
    assert report["llm"]["judge_cache_hit_count"] == 1
    assert report["llm"]["judge_avoided_count"] == 1
    assert report["llm"]["judge_cache_scope_counts"] == {"ambient_topic": 1}
    assert report["llm"]["prefilter_judge_agreement_counts"] == {"false": 1}
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
    assert report["faiss"]["status_counts"] == {"timeout": 1}
    assert report["faiss"]["timeout_origin_counts"] == {"faiss_index": 1}
    assert report["faiss"]["fallback_source_counts"] == {"bm25": 1}
    assert report["faiss"]["query_queue_wait_ms_p95"] == 12.5
    assert report["faiss"]["degraded_ratio_p50"] == 0.25
    assert "## Faiss Retrieval" in render_markdown(report)
    assert report["vision"]["trace_count"] == 1
    assert report["vision"]["path_counts"] == {"direct": 1}
    assert report["vision"]["call_status_counts"] == {"success": 1}
    assert report["vision"]["raw_image_count"] == 2
    assert report["vision"]["candidate_ref_count"] == 3
    assert report["vision"]["resolved_count"] == 1
    assert report["vision"]["model_attempt_count"] == 1
    assert report["vision"]["failure_disposition_counts"] == {"continue_text_only": 1}
    assert report["vision"]["reply_guard_action_counts"] == {"allowed": 1}
    assert report["vision"]["resolve_failure_reason_counts"] == {"get_image_failed": 1}
    assert report["timing_coverage"]["coverage_ratio_p50"] == 0.8
    assert report["timing_coverage"]["unattributed_ms_p95"] == 200.0
    assert report["timing_coverage"]["total_ms_p95"] == 1000.0
    assert report["timing_coverage"]["post_last_observed_delay_ms_p95"] == 80.0
    assert report["topic_activity"]["valid_count"] == 1
    assert report["topic_activity"]["effective_user_response_count"] == 1
    assert report["proactive"]["dispatch_status_counts"] == {"sent": 1}
    assert report["proactive"]["stage_status_counts"] == {
        "proactive.reply_commit:success": 1,
    }
    assert "## Vision" in render_markdown(report)
    assert "Judge observed/cache-hit/avoided" in render_markdown(report)
    assert "## Topic Activity" in render_markdown(report)
    assert "## Proactive Lifecycle" in render_markdown(report)


def test_analyze_traces_reports_relationship_and_expression_decisions():
    report = analyze_traces(
        [
            {
                "turn_id": "turn-social-1",
                "relationship_observation": {
                    "event_type": "compliment",
                    "source": "deterministic_rule",
                    "disposition": "applied",
                },
                "expression_observation": {
                    "bot_expression_tag": "shy",
                    "source": "explicit_tool",
                    "disposition": "eligible",
                },
            }
        ]
    )

    assert report["relationship"]["event_counts"] == {"compliment": 1}
    assert report["relationship"]["disposition_counts"] == {"applied": 1}
    assert report["expression"]["tag_counts"] == {"shy": 1}
    assert "## Relationship Events" in render_markdown(report)


def test_analyze_traces_reads_memory_retrieval_from_architecture_contract():
    report = analyze_traces(
        [
            {
                "architecture_contract": {
                    "memory_retrieval_observation": {
                        "vector_fallback": {"source": "canonical_fts"},
                        "hybrid_observations": [
                            {"vector": {"status": "circuit_open"}}
                        ],
                    }
                }
            }
        ]
    )

    assert report["faiss"]["status_counts"] == {"circuit_open": 1}
    assert report["faiss"]["fallback_source_counts"] == {"canonical_fts": 1}


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
                    "disclosure_request_source": "natural_language_need",
                    "disclosure_requested_tools": ["qq_friend_lookup"],
                    "disclosure_rejected_requests": [],
                    "second_pass_added_tools": ["qq_friend_lookup"],
                    "second_pass_tool_executed": True,
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
    assert tools["request_source_counts"] == {"natural_language_need": 1}
    assert tools["second_pass_added_tool_count"] == 1
    assert tools["second_pass_executed_count"] == 1
    assert tools["execution_name_counts"] == {"qq_friend_lookup": 1}
    assert tools["execution_missing_structure"] == {}
    assert "## Tool Disclosure" in render_markdown(report)
