from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _percentile(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(max(0.0, _number(value)) for value in values)
    if not ordered:
        return 0.0
    rank = (len(ordered) - 1) * max(0.0, min(1.0, percentile))
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return round(ordered[low], 1)
    weighted = ordered[low] * (high - rank) + ordered[high] * (rank - low)
    return round(weighted, 1)


def _safe_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _safe_list(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in list(value or []) if isinstance(item, dict)]


def _is_nonempty(value: Any) -> bool:
    if value in (None, "", False):
        return False
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return True


def _has_sent_reply(trace: dict[str, Any]) -> bool:
    status = str(trace.get("status", "") or "")
    if status in {"executed", "executed_topic_confirmation", "replied", "completed"}:
        return True
    reply_stats = _safe_dict(trace.get("reply_stats"))
    return any(
        int(_number(reply_stats.get(key))) > 0
        for key in ("actual_send_count", "message_count", "sent_segments", "sent_segment_count")
    )


def _merge_trace_snapshots(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged = {**previous, **current}
    for key in (
        "vision_observation",
        "memory_funnel",
        "tool_ledger_summary",
        "reply_stats",
        "context_block_stats",
        "stage_ledger",
        "llm_call_ledger",
    ):
        if _is_nonempty(previous.get(key)) and not _is_nonempty(current.get(key)):
            merged[key] = previous[key]
    if _has_sent_reply(previous) and not _has_sent_reply(current):
        merged["latest_snapshot_status"] = str(current.get("status", "") or "")
        merged["status"] = str(previous.get("status") or "executed")
        merged["turn_final_status"] = merged["status"]
    else:
        merged["turn_final_status"] = str(merged.get("status", "") or "")
    return merged


def load_traces(
    path: str | Path,
    *,
    instrumentation_version: str = "",
    since: float = 0.0,
) -> list[dict[str, Any]]:
    # G8/WU-06: 同时支持 append-only JSONL（新格式）与整文件 JSON（历史快照，
    # 例如 .agent/runtime-observability-* 里已归档的样本）
    text = Path(path).read_text(encoding="utf-8")
    if str(path).endswith(".jsonl"):
        payload = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except Exception:
                continue  # 崩溃截断的半行
            if isinstance(item, dict):
                payload.append(item)
    else:
        payload = json.loads(text)
    if isinstance(payload, list):
        source = _safe_list(payload)
    elif isinstance(payload, dict):
        recent = _safe_list(payload.get("recent"))
        if recent:
            source = recent
        else:
            source = []
            for items in _safe_dict(payload.get("by_chat")).values():
                source.extend(_safe_list(items))
    else:
        source = []

    filtered: list[dict[str, Any]] = []
    index_by_key: dict[str, int] = {}
    for index, trace in enumerate(source):
        if instrumentation_version and str(trace.get("instrumentation_version", "")) != instrumentation_version:
            continue
        created_at = _number(trace.get("created_at") or trace.get("started_at"))
        if since and created_at < since:
            continue
        stable_key = str(trace.get("turn_id") or trace.get("trace_id") or "").strip()
        if not stable_key:
            stable_key = "|".join(
                [
                    str(trace.get("chat_id", "")),
                    str(trace.get("created_at", "")),
                    str(trace.get("status", "")),
                    str(index),
                ]
            )
        if stable_key in index_by_key:
            existing_index = index_by_key[stable_key]
            filtered[existing_index] = _merge_trace_snapshots(filtered[existing_index], trace)
            continue
        index_by_key[stable_key] = len(filtered)
        filtered.append(trace)
    filtered.sort(key=lambda item: _number(item.get("created_at") or item.get("started_at")))
    return filtered


def analyze_traces(traces: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = [dict(trace) for trace in traces if isinstance(trace, dict)]
    status_counts: Counter[str] = Counter()
    chat_counts: Counter[str] = Counter()
    instrumentation_counts: Counter[str] = Counter()
    skip_reason_counts: Counter[str] = Counter()
    wait_reason_counts: Counter[str] = Counter()
    stale_category_counts: Counter[str] = Counter()
    judge_outcome_counts: Counter[str] = Counter()
    llm_stage_counts: Counter[str] = Counter()
    llm_status_counts: Counter[str] = Counter()
    llm_path_counts: Counter[str] = Counter()
    model_attempt_status_counts: Counter[str] = Counter()
    model_attempt_error_counts: Counter[str] = Counter()
    llm_stage_latencies: defaultdict[str, list[float]] = defaultdict(list)
    stage_status_counts: Counter[str] = Counter()
    stage_latencies: defaultdict[str, list[float]] = defaultdict(list)
    memory_status_counts: Counter[str] = Counter()
    reply_lengths: list[float] = []
    judge_calls_per_turn: list[float] = []
    llm_calls_per_turn: list[float] = []
    attempt_counts: list[float] = []
    context_totals: list[float] = []
    context_scope_totals: defaultdict[str, list[float]] = defaultdict(list)
    context_scope_duplicates: Counter[str] = Counter()
    budget_exhausted_count = 0
    budget_remaining_ms: list[float] = []
    query_rewrite_status_counts: Counter[str] = Counter()
    memory_candidates = 0
    memory_selected = 0
    memory_rendered_chars = 0
    tool_trace_present_count = 0
    tool_summary_present_count = 0
    tool_field_presence_counts: Counter[str] = Counter()
    tool_field_nonempty_counts: Counter[str] = Counter()
    tool_tier_counts: Counter[str] = Counter()
    tool_package_counts: Counter[str] = Counter()
    tool_resolution_counts: Counter[str] = Counter()
    tool_request_source_counts: Counter[str] = Counter()
    tool_execution_name_counts: Counter[str] = Counter()
    tool_execution_status_counts: Counter[str] = Counter()
    tool_execution_family_counts: Counter[str] = Counter()
    tool_execution_domain_counts: Counter[str] = Counter()
    tool_execution_operation_counts: Counter[str] = Counter()
    tool_execution_missing_structure: Counter[str] = Counter()
    tool_contract_count = 0
    tool_contract_unsatisfied_count = 0
    tool_correction_pass_count = 0
    tool_second_pass_added_count = 0
    tool_second_pass_executed_count = 0
    tool_disclosure_rejected_count = 0
    missing = Counter()
    vision_trace_count = 0
    vision_status_counts: Counter[str] = Counter()
    vision_path_counts: Counter[str] = Counter()
    vision_call_status_counts: Counter[str] = Counter()
    vision_fallback_counts: Counter[str] = Counter()
    vision_image_count = 0
    vision_raw_image_count = 0
    vision_candidate_ref_count = 0
    vision_resolved_count = 0
    vision_model_attempt_count = 0
    vision_analyzed_count = 0
    vision_failed_count = 0
    vision_timeout_count = 0
    vision_wait_ms: list[float] = []
    vision_failure_disposition_counts: Counter[str] = Counter()
    vision_reply_guard_action_counts: Counter[str] = Counter()
    vision_resolve_failure_reason_counts: Counter[str] = Counter()
    relationship_event_counts: Counter[str] = Counter()
    relationship_source_counts: Counter[str] = Counter()
    relationship_disposition_counts: Counter[str] = Counter()
    expression_tag_counts: Counter[str] = Counter()
    expression_source_counts: Counter[str] = Counter()
    expression_disposition_counts: Counter[str] = Counter()

    for trace in items:
        status = str(trace.get("status", "unknown") or "unknown")
        status_counts[status] += 1
        chat_counts[str(trace.get("chat_id", "") or "unknown")] += 1
        instrumentation_counts[str(trace.get("instrumentation_version", "") or "legacy")] += 1

        decision = _safe_dict(trace.get("decision_observation"))
        skip_reason = str(decision.get("skip_reason", "") or "")
        if not skip_reason and status.startswith("skipped"):
            skip_reason = status
        if skip_reason:
            skip_reason_counts[skip_reason] += 1
        wait_reason = str(decision.get("wait_reason", "") or "")
        if wait_reason:
            wait_reason_counts[wait_reason] += 1
        stale_category = str(decision.get("stale_category", "") or "")
        if stale_category:
            stale_category_counts[stale_category] += 1
        judge_outcome = str(decision.get("judge_outcome", "") or "")
        if judge_outcome:
            judge_outcome_counts[judge_outcome] += 1
        if decision.get("judge_timeout") and judge_outcome != "timeout":
            judge_outcome_counts["timeout"] += 1

        calls = _safe_list(trace.get("llm_call_ledger"))
        llm_calls_per_turn.append(float(len(calls)))
        judge_count = 0
        for call in calls:
            stage = str(call.get("stage", "") or "unknown")
            call_status = str(call.get("status", "") or "unknown")
            llm_stage_counts[stage] += 1
            llm_status_counts[call_status] += 1
            llm_path_counts["critical" if call.get("critical_path", True) else "background"] += 1
            llm_stage_latencies[stage].append(_number(call.get("elapsed_ms")))
            attempts = _safe_list(call.get("model_attempts"))
            attempt_counts.append(float(len(attempts) or int(_number(call.get("attempts")))))
            for attempt in attempts:
                attempt_status = str(attempt.get("status", "") or "unknown")
                model_attempt_status_counts[attempt_status] += 1
                error_kind = str(attempt.get("error_kind", "") or "")
                if error_kind:
                    model_attempt_error_counts[error_kind] += 1
            # OPT-11/RT-02: judge 记账在 pool 字段（stage 是 gateway.chat），旧口径
            # 按 stage 匹配恒为 0，制造了"judge 重复调用已修复"的假象（真实 max=10）
            pool = str(call.get("pool", "") or "")
            if pool == "judge" or stage == "attention.judge" or "judge" in stage:
                judge_count += 1
        judge_calls_per_turn.append(float(judge_count))
        if not calls:
            missing["llm_call_ledger"] += 1

        stages = _safe_list(trace.get("stage_ledger"))
        for stage in stages:
            name = str(stage.get("stage", "") or "unknown")
            stage_status_counts[f"{name}:{stage.get('status', 'unknown')}"] += 1
            stage_latencies[name].append(_number(stage.get("elapsed_ms")))
        if not stages:
            missing["stage_ledger"] += 1

        contexts = _safe_list(trace.get("context_block_stats"))
        for context in contexts:
            total_chars = _number(context.get("total_chars"))
            context_totals.append(total_chars)
            metadata = _safe_dict(context.get("metadata"))
            scope = str(metadata.get("scope", "") or "")
            if not scope:
                stage_name = str(context.get("stage", "") or "")
                scope = "transmitted" if stage_name.endswith("_transmitted") else "source"
            context_scope_totals[scope].append(total_chars)
            context_scope_duplicates[scope] += int(_number(context.get("duplicate_block_count")))
        if not contexts:
            missing["context_block_stats"] += 1

        budget = _safe_dict(trace.get("budget"))
        if budget:
            budget_remaining_ms.append(_number(budget.get("remaining_ms")))
            if bool(budget.get("exhausted")):
                budget_exhausted_count += 1
        else:
            missing["budget"] += 1

        memory = _safe_dict(trace.get("memory_funnel"))
        rewrite_trace = _safe_dict(
            memory.get("query_rewrite_trace")
            or memory.get("rewrite_trace")
            or trace.get("query_rewrite_trace")
        )
        if rewrite_trace:
            rewrite_status = str(
                rewrite_trace.get("status")
                or rewrite_trace.get("fallback_reason")
                or "observed"
            )
            query_rewrite_status_counts[rewrite_status] += 1

        reply = _safe_dict(trace.get("reply_stats"))
        actual_reply_chars = reply.get("actual_reply_chars", reply.get("total_chars"))
        if actual_reply_chars is not None:
            reply_lengths.append(_number(actual_reply_chars))
        elif trace.get("reply_sent"):
            missing["reply_stats"] += 1

        if memory:
            memory_status_counts[str(memory.get("status", "") or "unknown")] += 1
            memory_candidates += int(_number(memory.get("candidate_count")))
            memory_selected += int(_number(memory.get("selected_count")))
            memory_rendered_chars += int(_number(memory.get("rendered_chars")))
        else:
            missing["memory_funnel"] += 1

        vision = _safe_dict(trace.get("vision_observation"))
        if vision:
            vision_trace_count += 1
            path = str(vision.get("vision_path") or trace.get("vision_path") or "").strip()
            status_name = str(
                vision.get("final_status")
                or vision.get("vision_call_status")
                or vision.get("vision_barrier_status")
                or "observed"
            ).strip()
            call_status = str(vision.get("vision_call_status") or "").strip()
            fallback = str(vision.get("vision_fallback") or vision.get("fallback_reason") or "").strip()
            if path:
                vision_path_counts[path] += 1
            if status_name:
                vision_status_counts[status_name] += 1
            if call_status:
                vision_call_status_counts[call_status] += 1
            if fallback:
                vision_fallback_counts[fallback] += 1
            vision_image_count += int(_number(vision.get("image_count")))
            vision_raw_image_count += int(
                _number(vision.get("raw_image_count", vision.get("image_raw_component_count")))
            )
            vision_candidate_ref_count += int(_number(vision.get("candidate_ref_count")))
            vision_resolved_count += int(
                _number(vision.get("resolved_count", vision.get("image_resolved_count")))
            )
            vision_model_attempt_count += int(
                _number(vision.get("vision_model_attempt_count", vision.get("attempt_count")))
            )
            vision_analyzed_count += int(_number(vision.get("analyzed_count")))
            vision_failed_count += int(_number(vision.get("failed_count")))
            vision_timeout_count += int(_number(vision.get("timeout_count")))
            wait = vision.get("vision_wait_ms")
            if wait is not None:
                vision_wait_ms.append(_number(wait))
            failure_disposition = str(vision.get("failure_disposition") or "").strip()
            if failure_disposition:
                vision_failure_disposition_counts[failure_disposition] += 1
            reply_guard_action = str(vision.get("reply_guard_action") or "").strip()
            if reply_guard_action:
                vision_reply_guard_action_counts[reply_guard_action] += 1
            for reason in list(vision.get("resolve_failure_reasons") or []):
                normalized_reason = str(reason or "").strip()
                if normalized_reason:
                    vision_resolve_failure_reason_counts[normalized_reason] += 1
        else:
            missing["vision_observation"] += 1

        relationship = _safe_dict(trace.get("relationship_observation"))
        if relationship:
            event_type = str(relationship.get("event_type") or "").strip()
            source = str(relationship.get("source") or "").strip()
            disposition = str(relationship.get("disposition") or "").strip()
            if event_type:
                relationship_event_counts[event_type] += 1
            if source:
                relationship_source_counts[source] += 1
            if disposition:
                relationship_disposition_counts[disposition] += 1
        expression = _safe_dict(trace.get("expression_observation"))
        if expression:
            tag = str(expression.get("bot_expression_tag") or "").strip()
            source = str(expression.get("source") or "").strip()
            disposition = str(expression.get("disposition") or "").strip()
            if tag:
                expression_tag_counts[tag] += 1
            if source:
                expression_source_counts[source] += 1
            if disposition:
                expression_disposition_counts[disposition] += 1

        tools = trace.get("tools")
        if isinstance(tools, dict):
            tool_trace_present_count += 1
            for field in (
                "requested_tier",
                "final_tier",
                "disclosure_enabled",
                "disclosure_tier",
                "disclosure_packages",
                "disclosure_second_pass_packages",
                "disclosure_expanded_packages",
                "disclosure_decisions",
                "preselected_tools",
                "hidden_requestable_tools",
                "disclosure_request_source",
                "disclosure_requested_tools",
                "disclosure_rejected_requests",
                "explicit_tool_intent",
                "intent_contracts",
                "contract_outcomes",
                "contract_unsatisfied",
                "correction_pass_used",
                "correction_packages",
                "second_pass_resolution",
                "second_pass_selected_tools",
                "second_pass_added_tools",
                "second_pass_tool_executed",
            ):
                if field in tools:
                    tool_field_presence_counts[field] += 1
                    if tools.get(field) not in (None, "", False, [], {}):
                        tool_field_nonempty_counts[field] += 1
            tier = str(tools.get("disclosure_tier") or tools.get("final_tier") or "").strip()
            if tier:
                tool_tier_counts[tier] += 1
            for package in list(tools.get("disclosure_packages") or []):
                tool_package_counts[str(package or "unknown")] += 1
            resolution = str(tools.get("second_pass_resolution") or "").strip()
            if resolution:
                tool_resolution_counts[resolution] += 1
            request_source = str(tools.get("disclosure_request_source") or "").strip()
            if request_source:
                tool_request_source_counts[request_source] += 1
            tool_second_pass_added_count += len(list(tools.get("second_pass_added_tools") or []))
            tool_disclosure_rejected_count += len(list(tools.get("disclosure_rejected_requests") or []))
            if bool(tools.get("second_pass_tool_executed")):
                tool_second_pass_executed_count += 1
            tool_contract_count += len(list(tools.get("intent_contracts") or []))
            tool_contract_unsatisfied_count += len(list(tools.get("contract_unsatisfied") or []))
            if bool(tools.get("correction_pass_used")):
                tool_correction_pass_count += 1
        else:
            missing["tools"] += 1

        tool_summary = trace.get("tool_ledger_summary")
        if isinstance(tool_summary, dict):
            tool_summary_present_count += 1
        else:
            missing["tool_ledger_summary"] += 1

        for execution in _safe_list(trace.get("tool_execution_trace")):
            name = str(execution.get("tool_name") or "unknown").strip() or "unknown"
            status_name = str(execution.get("status") or "unknown").strip() or "unknown"
            family = str(execution.get("family") or "").strip()
            domain = str(execution.get("source_domain") or "").strip()
            operation = str(execution.get("operation") or "").strip()
            tool_execution_name_counts[name] += 1
            tool_execution_status_counts[status_name] += 1
            if family:
                tool_execution_family_counts[family] += 1
            else:
                tool_execution_missing_structure["family"] += 1
            if domain:
                tool_execution_domain_counts[domain] += 1
            else:
                tool_execution_missing_structure["source_domain"] += 1
            if operation:
                tool_execution_operation_counts[operation] += 1
            else:
                tool_execution_missing_structure["operation"] += 1

    trace_count = len(items)
    reply_count = sum(status_counts[status] for status in status_counts if "replied" in status or status == "completed")
    if not reply_count:
        reply_count = sum(1 for trace in items if trace.get("reply_sent"))

    return {
        "trace_count": trace_count,
        "reply_count": reply_count,
        "status_counts": dict(status_counts.most_common()),
        "top_chats": dict(chat_counts.most_common(20)),
        "instrumentation_versions": dict(instrumentation_counts.most_common()),
        "skip_reasons": dict(skip_reason_counts.most_common(20)),
        "llm": {
            "total_calls": sum(llm_stage_counts.values()),
            "stage_counts": dict(llm_stage_counts.most_common()),
            "status_counts": dict(llm_status_counts.most_common()),
            "path_counts": dict(llm_path_counts.most_common()),
            "model_attempt_status_counts": dict(model_attempt_status_counts.most_common()),
            "model_attempt_error_counts": dict(model_attempt_error_counts.most_common()),
            "calls_per_turn_p50": _percentile(llm_calls_per_turn, 0.50),
            "calls_per_turn_p95": _percentile(llm_calls_per_turn, 0.95),
            "judge_calls_per_turn_p50": _percentile(judge_calls_per_turn, 0.50),
            "judge_calls_per_turn_p95": _percentile(judge_calls_per_turn, 0.95),
            "attempts_per_call_p95": _percentile(attempt_counts, 0.95),
            "latency_by_stage": {
                stage: {
                    "count": len(values),
                    "p50_ms": _percentile(values, 0.50),
                    "p95_ms": _percentile(values, 0.95),
                    "max_ms": round(max(values), 1) if values else 0.0,
                }
                for stage, values in sorted(llm_stage_latencies.items())
            },
        },
        "stages": {
            "status_counts": dict(stage_status_counts.most_common()),
            "latency_by_stage": {
                stage: {
                    "count": len(values),
                    "p50_ms": _percentile(values, 0.50),
                    "p95_ms": _percentile(values, 0.95),
                    "max_ms": round(max(values), 1) if values else 0.0,
                }
                for stage, values in sorted(stage_latencies.items())
            },
        },
        "reply": {
            "count_with_stats": len(reply_lengths),
            "chars_p50": _percentile(reply_lengths, 0.50),
            "chars_p95": _percentile(reply_lengths, 0.95),
            "chars_max": round(max(reply_lengths), 1) if reply_lengths else 0.0,
        },
        "relationship": {
            "event_counts": dict(relationship_event_counts.most_common()),
            "source_counts": dict(relationship_source_counts.most_common()),
            "disposition_counts": dict(relationship_disposition_counts.most_common()),
        },
        "expression": {
            "tag_counts": dict(expression_tag_counts.most_common()),
            "source_counts": dict(expression_source_counts.most_common()),
            "disposition_counts": dict(expression_disposition_counts.most_common()),
        },
        "context": {
            "samples": len(context_totals),
            "chars_p50": _percentile(context_totals, 0.50),
            "chars_p95": _percentile(context_totals, 0.95),
            "by_scope": {
                scope: {
                    "samples": len(values),
                    "chars_p50": _percentile(values, 0.50),
                    "chars_p95": _percentile(values, 0.95),
                    "duplicate_block_count": int(context_scope_duplicates[scope]),
                }
                for scope, values in sorted(context_scope_totals.items())
            },
            "duplicate_block_count": sum(context_scope_duplicates.values()),
        },
        "decision": {
            "wait_reasons": dict(wait_reason_counts.most_common(20)),
            "stale_categories": dict(stale_category_counts.most_common(20)),
            "judge_outcomes": dict(judge_outcome_counts.most_common(20)),
        },
        "budget": {
            "samples": len(budget_remaining_ms),
            "exhausted_count": budget_exhausted_count,
            "remaining_ms_p50": _percentile(budget_remaining_ms, 0.50),
            "remaining_ms_p05": _percentile(budget_remaining_ms, 0.05),
        },
        "query_rewrite": {
            "status_counts": dict(query_rewrite_status_counts.most_common()),
        },
        "memory": {
            "status_counts": dict(memory_status_counts.most_common()),
            "candidate_count": memory_candidates,
            "selected_count": memory_selected,
            "rendered_chars": memory_rendered_chars,
            "selection_rate": round(memory_selected / memory_candidates, 4) if memory_candidates else 0.0,
        },
        "vision": {
            "trace_count": vision_trace_count,
            "status_counts": dict(vision_status_counts.most_common()),
            "path_counts": dict(vision_path_counts.most_common()),
            "call_status_counts": dict(vision_call_status_counts.most_common()),
            "fallback_counts": dict(vision_fallback_counts.most_common()),
            "image_count": vision_image_count,
            "raw_image_count": vision_raw_image_count,
            "candidate_ref_count": vision_candidate_ref_count,
            "resolved_count": vision_resolved_count,
            "model_attempt_count": vision_model_attempt_count,
            "analyzed_count": vision_analyzed_count,
            "failed_count": vision_failed_count,
            "timeout_count": vision_timeout_count,
            "wait_ms_p50": _percentile(vision_wait_ms, 0.50),
            "wait_ms_p95": _percentile(vision_wait_ms, 0.95),
            "failure_disposition_counts": dict(vision_failure_disposition_counts.most_common()),
            "reply_guard_action_counts": dict(vision_reply_guard_action_counts.most_common()),
            "resolve_failure_reason_counts": dict(vision_resolve_failure_reason_counts.most_common()),
        },
        "tools": {
            "trace_present_count": tool_trace_present_count,
            "ledger_summary_present_count": tool_summary_present_count,
            "field_presence_counts": dict(tool_field_presence_counts.most_common()),
            "field_nonempty_counts": dict(tool_field_nonempty_counts.most_common()),
            "tier_counts": dict(tool_tier_counts.most_common()),
            "package_counts": dict(tool_package_counts.most_common()),
            "second_pass_resolution_counts": dict(tool_resolution_counts.most_common()),
            "request_source_counts": dict(tool_request_source_counts.most_common()),
            "second_pass_added_tool_count": tool_second_pass_added_count,
            "second_pass_executed_count": tool_second_pass_executed_count,
            "disclosure_rejected_count": tool_disclosure_rejected_count,
            "contract_count": tool_contract_count,
            "contract_unsatisfied_count": tool_contract_unsatisfied_count,
            "correction_pass_count": tool_correction_pass_count,
            "execution_name_counts": dict(tool_execution_name_counts.most_common()),
            "execution_status_counts": dict(tool_execution_status_counts.most_common()),
            "execution_family_counts": dict(tool_execution_family_counts.most_common()),
            "execution_domain_counts": dict(tool_execution_domain_counts.most_common()),
            "execution_operation_counts": dict(tool_execution_operation_counts.most_common()),
            "execution_missing_structure": dict(tool_execution_missing_structure.most_common()),
        },
        "missing_fields": dict(missing.most_common()),
    }


def render_markdown(report: dict[str, Any]) -> str:
    llm = _safe_dict(report.get("llm"))
    reply = _safe_dict(report.get("reply"))
    context = _safe_dict(report.get("context"))
    memory = _safe_dict(report.get("memory"))
    decision = _safe_dict(report.get("decision"))
    budget = _safe_dict(report.get("budget"))
    rewrite = _safe_dict(report.get("query_rewrite"))
    tools = _safe_dict(report.get("tools"))
    vision = _safe_dict(report.get("vision"))
    relationship = _safe_dict(report.get("relationship"))
    expression = _safe_dict(report.get("expression"))
    lines = [
        "# AstrMai Turn Ledger Analysis",
        "",
        f"- Traces: {int(report.get('trace_count', 0) or 0)}",
        f"- Replies: {int(report.get('reply_count', 0) or 0)}",
        f"- LLM calls: {int(llm.get('total_calls', 0) or 0)}",
        f"- Reply chars P50/P95/max: {reply.get('chars_p50', 0)} / {reply.get('chars_p95', 0)} / {reply.get('chars_max', 0)}",
        f"- Context chars P50/P95: {context.get('chars_p50', 0)} / {context.get('chars_p95', 0)}",
        f"- Duplicate context blocks: {int(context.get('duplicate_block_count', 0) or 0)}",
        f"- Turn budget exhausted: {int(budget.get('exhausted_count', 0) or 0)}",
        f"- Memory candidates/selected: {int(memory.get('candidate_count', 0) or 0)} / {int(memory.get('selected_count', 0) or 0)}",
        "",
        "## Status",
    ]
    for key, value in _safe_dict(report.get("status_counts")).items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## LLM Stages"])
    for key, value in _safe_dict(llm.get("stage_counts")).items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Skip Reasons"])
    for key, value in _safe_dict(report.get("skip_reasons")).items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Decision Diagnostics"])
    for label, values in (
        ("wait", _safe_dict(decision.get("wait_reasons"))),
        ("stale", _safe_dict(decision.get("stale_categories"))),
        ("judge", _safe_dict(decision.get("judge_outcomes"))),
    ):
        for key, value in values.items():
            lines.append(f"- `{label}:{key}`: {value}")
    lines.extend(["", "## Query Rewrite"])
    rewrite_counts = _safe_dict(rewrite.get("status_counts"))
    if rewrite_counts:
        for key, value in rewrite_counts.items():
            lines.append(f"- `{key}`: {value}")
    else:
        lines.append("- No observed rewrite traces")
    lines.extend(["", "## Vision"])
    lines.append(f"- Vision traces: {int(vision.get('trace_count', 0) or 0)}")
    lines.append(
        f"- Images analyzed/failed/timeout: {int(vision.get('analyzed_count', 0) or 0)} / "
        f"{int(vision.get('failed_count', 0) or 0)} / {int(vision.get('timeout_count', 0) or 0)}"
    )
    lines.append(f"- Vision wait P50/P95 ms: {vision.get('wait_ms_p50', 0)} / {vision.get('wait_ms_p95', 0)}")
    for key, value in _safe_dict(vision.get("path_counts")).items():
        lines.append(f"- `path:{key}`: {value}")
    for key, value in _safe_dict(vision.get("call_status_counts")).items():
        lines.append(f"- `call:{key}`: {value}")
    for key, value in _safe_dict(vision.get("fallback_counts")).items():
        lines.append(f"- `fallback:{key}`: {value}")
    lines.extend(["", "## Relationship Events"])
    for key, value in _safe_dict(relationship.get("event_counts")).items():
        lines.append(f"- `event:{key}`: {value}")
    for key, value in _safe_dict(relationship.get("disposition_counts")).items():
        lines.append(f"- `disposition:{key}`: {value}")
    lines.extend(["", "## Expression Decisions"])
    for key, value in _safe_dict(expression.get("tag_counts")).items():
        lines.append(f"- `tag:{key}`: {value}")
    for key, value in _safe_dict(expression.get("disposition_counts")).items():
        lines.append(f"- `disposition:{key}`: {value}")
    lines.extend(["", "## Tool Disclosure"])
    lines.append(f"- Tool trace present: {int(tools.get('trace_present_count', 0) or 0)}")
    lines.append(f"- Tool ledger summary present: {int(tools.get('ledger_summary_present_count', 0) or 0)}")
    lines.append(f"- Contracts/unsatisfied: {int(tools.get('contract_count', 0) or 0)} / {int(tools.get('contract_unsatisfied_count', 0) or 0)}")
    lines.append(f"- Correction passes: {int(tools.get('correction_pass_count', 0) or 0)}")
    lines.append(f"- Second-pass tools added: {int(tools.get('second_pass_added_tool_count', 0) or 0)}")
    lines.append(f"- Second-pass tools executed: {int(tools.get('second_pass_executed_count', 0) or 0)}")
    lines.append(f"- Rejected disclosure requests: {int(tools.get('disclosure_rejected_count', 0) or 0)}")
    for key, value in _safe_dict(tools.get("second_pass_resolution_counts")).items():
        lines.append(f"- `resolution:{key}`: {value}")
    for key, value in _safe_dict(tools.get("request_source_counts")).items():
        lines.append(f"- `request_source:{key}`: {value}")
    for key, value in _safe_dict(tools.get("execution_status_counts")).items():
        lines.append(f"- `execution:{key}`: {value}")
    for key, value in _safe_dict(tools.get("execution_missing_structure")).items():
        lines.append(f"- `execution_missing:{key}`: {value}")
    lines.extend(["", "## Coverage Gaps"])
    missing = _safe_dict(report.get("missing_fields"))
    if missing:
        for key, value in missing.items():
            lines.append(f"- `{key}` missing: {value}")
    else:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze privacy-safe AstrMai turn ledgers.")
    parser.add_argument("input", help="turn_trace_samples.json path")
    parser.add_argument("--output", help="write the report to this path")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--instrumentation-version", default="")
    parser.add_argument("--since", type=float, default=0.0, help="minimum Unix timestamp")
    args = parser.parse_args()

    traces = load_traces(
        args.input,
        instrumentation_version=args.instrumentation_version,
        since=args.since,
    )
    report = analyze_traces(traces)
    rendered = (
        json.dumps(report, ensure_ascii=False, indent=2)
        if args.format == "json"
        else render_markdown(report)
    )
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
