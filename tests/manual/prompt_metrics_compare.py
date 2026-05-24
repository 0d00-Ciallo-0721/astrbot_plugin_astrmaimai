from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import textwrap
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


DEFAULT_BASELINE_ROOT = REPO_ROOT.parent / f"{REPO_ROOT.name}__baseline_6d5ecde"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "artifacts" / "prompt_metrics_compare"
HIGH_DYNAMIC_CASE_IDS = {
    "tool_intent",
    "pushback_strict",
    "zh_tool_intent",
    "zh_boundary_mild",
    "deep_memory",
    "zh_memory_intent",
}
SUPPORTED_PREFIX_CHANGED_REASONS = {
    "first_seen",
    "cold_summary_changed",
    "frozen_rules_or_persona_changed",
    "disabled",
    "unsupported_in_baseline",
    "unavailable_in_trace",
}
BASELINE_BLOCK_SENTINEL = "unavailable_in_baseline_analysis"


def _fallback_parse_baseline_blocks(system_prompt: str, *, baseline_mode: bool) -> tuple[dict[str, Any], str]:
    text = str(system_prompt or "")
    if not baseline_mode or not text:
        return {}, "native_trace"
    if "Only output the visible chat reply." in text:
        return {"system_rules": len(text)}, "fallback_parsed"
    return {
        "persona_core": BASELINE_BLOCK_SENTINEL,
        "style_block": BASELINE_BLOCK_SENTINEL,
        "system_rules": BASELINE_BLOCK_SENTINEL,
        "cold_summary": BASELINE_BLOCK_SENTINEL,
        "stable_behavior_rules": BASELINE_BLOCK_SENTINEL,
    }, "not_comparable"

_HARNESS_CODE = textwrap.dedent(
    r"""
    import asyncio
    from hashlib import sha256
    import json
    from pathlib import Path
    from types import MethodType
    import sys

    output_path = Path(sys.argv[1]).resolve()
    repo_root = Path.cwd().resolve()

    from tests.manual import kimi_replay_acceptance as mod


    async def fake_chat_completion(self, *, messages, request_label, allow_thinking_fallback=True):
        last = messages[-1]["content"] if messages else ""
        if "Return only one valid JSON object" in last:
            text = "{}"
        else:
            text = "Brief offline reply."
        return {"choices": [{"message": {"content": text}}]}


    def stable_hash(text: str) -> str:
        normalized = str(text or "")
        if not normalized:
            return ""
        return sha256(normalized.encode("utf-8")).hexdigest()[:16]


    def normalize_prefix_changed_reason(raw_reason, *, repo_root: Path) -> str:
        reason = str(raw_reason or "").strip()
        if reason in {"", "unknown"}:
            if str(repo_root).endswith("__baseline_6d5ecde"):
                return "unsupported_in_baseline"
            return "unavailable_in_trace"
        return reason


    def fallback_parse_baseline_blocks(system_prompt: str, *, baseline_mode: bool):
        text = str(system_prompt or "")
        if not baseline_mode or not text:
            return {}, "native_trace"
        if "Only output the visible chat reply." in text:
            return {"system_rules": len(text)}, "fallback_parsed"
        return {
            "persona_core": "unavailable_in_baseline_analysis",
            "style_block": "unavailable_in_baseline_analysis",
            "system_rules": "unavailable_in_baseline_analysis",
            "cold_summary": "unavailable_in_baseline_analysis",
            "stable_behavior_rules": "unavailable_in_baseline_analysis",
        }, "not_comparable"


    async def main() -> None:
        baseline_mode = str(repo_root).endswith("__baseline_6d5ecde")
        client = mod.KimiClient(
            api_key="fake",
            model="fake-model",
            timeout=5,
            max_calls=1000,
            max_tokens=80,
            temperature=0.0,
            rpm_limit=20,
            rpm_safety_margin=1,
            concurrency_limit=1,
        )
        client.chat_completion = MethodType(fake_chat_completion, client)
        planner = mod.build_planner(client)

        original_refine_prompt = planner.prompt_refiner.refine_prompt

        async def wrapped_refine_prompt(*args, **kwargs):
            event = kwargs.get("event") if "event" in kwargs else (args[0] if args else None)
            prompt_envelope = kwargs.get("prompt_envelope")
            result = await original_refine_prompt(*args, **kwargs)
            if event is not None and hasattr(event, "set_extra"):
                event.set_extra("_metric_final_system_prompt", result[0])
                event.set_extra("_metric_final_prompt", result[1])
                if prompt_envelope is not None:
                    dynamic_length = (
                        len(getattr(prompt_envelope, "cognitive_drive_block", "") or "")
                        + len(getattr(prompt_envelope, "soft_background_block", "") or "")
                        + len(getattr(prompt_envelope, "situational_context_block", "") or "")
                        + len(getattr(prompt_envelope, "planner_runtime_instruction_block", "") or "")
                    )
                    event.set_extra("_metric_dynamic_prompt_length", dynamic_length)
            return result

        planner.prompt_refiner.refine_prompt = wrapped_refine_prompt

        rows = []
        for case in mod.REPLAY_CASES:
            event = mod.build_event(case)
            reply_text = await planner.plan_and_execute(event, [event])
            trace = planner.turn_trace_history[-1] if planner.turn_trace_history else {}
            continuity = dict((trace.get("continuity") or {}))
            validation_errors = mod.validate_case_trace(case, trace)

            final_system_prompt = str(event.get_extra("_metric_final_system_prompt", "") or "")
            final_prompt = str(event.get_extra("_metric_final_prompt", "") or "")
            prompt_envelope = event.get_extra("astrmai_prompt_envelope", None)
            dynamic_prompt_length = int(continuity.get("dynamic_prompt_length", 0) or 0)
            if not dynamic_prompt_length and prompt_envelope is not None:
                dynamic_prompt_length = (
                    len(getattr(prompt_envelope, "cognitive_drive_block", "") or "")
                    + len(getattr(prompt_envelope, "soft_background_block", "") or "")
                    + len(getattr(prompt_envelope, "situational_context_block", "") or "")
                    + len(getattr(prompt_envelope, "planner_runtime_instruction_block", "") or "")
                )

            prefix_hash = str(continuity.get("prefix_hash", "") or event.get_extra("astrmai_prefix_hash", "") or "")
            stable_prefix_hash = stable_hash(final_system_prompt)
            frozen_prefix_blocks = dict(continuity.get("frozen_prefix_blocks", {}) or {})
            semi_stable_blocks = dict(continuity.get("semi_stable_blocks", {}) or {})
            dynamic_prompt_blocks = dict(continuity.get("dynamic_prompt_blocks", {}) or {})
            block_analysis_mode = "native_trace"
            if not frozen_prefix_blocks:
                frozen_prefix_blocks, block_analysis_mode = fallback_parse_baseline_blocks(final_system_prompt, baseline_mode=baseline_mode)
            if not dynamic_prompt_blocks and prompt_envelope is not None:
                dynamic_prompt_blocks = {
                    "cognitive_drive": len(getattr(prompt_envelope, "cognitive_drive_block", "") or ""),
                    "soft_background": len(getattr(prompt_envelope, "soft_background_block", "") or ""),
                    "situational_context": len(getattr(prompt_envelope, "situational_context_block", "") or ""),
                    "planner_runtime_instruction": len(getattr(prompt_envelope, "planner_runtime_instruction_block", "") or ""),
                }
            row = {
                "case_id": case.case_id,
                "status": "failed" if validation_errors else "ok",
                "reply_preview": " ".join(str(reply_text or "").split())[:180],
                "validation_errors": list(validation_errors),
                "continuity": {
                    "prefix_hash": prefix_hash,
                    "semantic_system_hash": str(continuity.get("semantic_system_hash", "") or ""),
                    "semantic_system_length": int(continuity.get("semantic_system_length", 0) or 0),
                    "provider_visible_system_hash": str(continuity.get("provider_visible_system_hash", "") or ""),
                    "post_hook_system_hash": str(continuity.get("post_hook_system_hash", "") or ""),
                    "prefix_stable": continuity.get("prefix_stable", None),
                    "prefix_changed_reason": normalize_prefix_changed_reason(
                        continuity.get("prefix_changed_reason", ""),
                        repo_root=repo_root,
                    ),
                    "system_prompt_length": int(continuity.get("system_prompt_length", 0) or len(final_system_prompt)),
                    "prompt_length": int(continuity.get("prompt_length", 0) or len(final_prompt)),
                    "frozen_prefix_length": int(continuity.get("frozen_prefix_length", 0) or 0),
                    "semi_stable_length": int(continuity.get("semi_stable_length", 0) or 0),
                    "dynamic_prompt_length": int(dynamic_prompt_length or 0),
                    "stable_prefix_hash": stable_prefix_hash,
                    "frozen_prefix_blocks": frozen_prefix_blocks,
                    "semi_stable_blocks": semi_stable_blocks,
                    "dynamic_prompt_blocks": dynamic_prompt_blocks,
                    "block_analysis_mode": block_analysis_mode,
                    "system_rules_items": list(continuity.get("system_rules_items", []) or []),
                    "system_rules_candidate_items": list(continuity.get("system_rules_candidate_items", []) or []),
                },
            }
            rows.append(row)

        output_path.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")


    asyncio.run(main())
    """
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare before/after prompt metrics across two worktrees.")
    parser.add_argument(
        "--baseline-root",
        default=str(DEFAULT_BASELINE_ROOT),
        help="Baseline repo root, usually the 6d5ecde worktree.",
    )
    parser.add_argument(
        "--current-root",
        default=str(REPO_ROOT),
        help="Current repo root.",
    )
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help="Directory receiving comparison artifacts.",
    )
    parser.add_argument(
        "--label",
        default="post_sys_cleanup_round3",
        help="Label appended to the output directory name.",
    )
    return parser


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _coerce_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _short_hash(text: str) -> str:
    normalized = str(text or "")
    if not normalized:
        return ""
    return sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _median(values: list[int]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _mean(values: list[int]) -> float:
    return round(float(statistics.mean(values)), 2) if values else 0.0


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = int((len(ordered) - 1) * 0.95)
    return int(ordered[index])


def _pairwise_stability_rate(values: list[str]) -> float:
    filtered = [str(value or "") for value in values if str(value or "")]
    if len(filtered) <= 1:
        return 0.0
    same_count = 0
    total = 0
    previous = filtered[0]
    for current in filtered[1:]:
        total += 1
        if current == previous:
            same_count += 1
        previous = current
    return round(same_count / total, 4) if total else 0.0


def _normalize_prefix_reason(raw_reason: Any, *, baseline_mode: bool = False) -> str:
    reason = str(raw_reason or "").strip()
    if reason in SUPPORTED_PREFIX_CHANGED_REASONS:
        return reason
    if reason in {"", "unknown"}:
        return "unsupported_in_baseline" if baseline_mode else "unavailable_in_trace"
    return "unavailable_in_trace"


def _normalize_block_map(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(key or ""): _coerce_int(value)
        for key, value in raw.items()
        if str(key or "") and value != BASELINE_BLOCK_SENTINEL
    }


def _persona_identity_total(blocks: dict[str, int]) -> int:
    return (
        _coerce_int(blocks.get("persona_core"))
        + _coerce_int(blocks.get("style_block"))
        + _coerce_int(blocks.get("system_rules"))
    )


def _block_available(raw: Any, key: str) -> bool:
    if not isinstance(raw, dict):
        return False
    value = raw.get(key)
    return value not in (None, "", BASELINE_BLOCK_SENTINEL)


def _sorted_block_lengths(blocks: dict[str, int]) -> list[dict[str, int | str]]:
    return [
        {"block": key, "length": value}
        for key, value in sorted(blocks.items(), key=lambda item: (-int(item[1] or 0), str(item[0])))
        if int(value or 0) > 0
    ]


def _recommended_migration_target(block_name: str) -> str:
    mapping = {
        "stable_behavior_rules": "planner_runtime_instruction",
        "stable_private_chat": "soft_background",
        "stable_state": "soft_background",
        "cold_summary": "soft_background",
        "persona_core": "keep_in_system",
        "style_block": "keep_in_system",
        "system_rules": "keep_in_system",
        "persona_or_identity": "keep_in_system",
        "stable_expression": "soft_background",
        "stable_slang": "soft_background",
        "stable_jargon": "soft_background",
    }
    return mapping.get(str(block_name or ""), "review_manually")


def _system_rules_item_frequency(rows: list[dict[str, Any]], *, candidate_only: bool) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        continuity = row.get("continuity") or {}
        if candidate_only:
            for key in list(continuity.get("system_rules_candidate_items", []) or []):
                if str(key or "").strip():
                    counter[str(key).strip()] += 1
        else:
            for item in list(continuity.get("system_rules_items", []) or []):
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key", "") or "").strip()
                target = str(item.get("default_target", "") or "").strip()
                if key and target == "keep_in_system":
                    counter[key] += 1
    return dict(counter)


def _system_rules_item_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        for item in list((row.get("continuity") or {}).get("system_rules_items", []) or []):
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "") or "").strip()
            if not key:
                continue
            entry = totals.setdefault(
                key,
                {
                    "key": key,
                    "total_length": 0,
                    "count": 0,
                    "default_target": str(item.get("default_target", "") or ""),
                },
            )
            entry["total_length"] += _coerce_int(item.get("length"))
            entry["count"] += 1
    return sorted(totals.values(), key=lambda item: (-_coerce_int(item["total_length"]), str(item["key"])))


def summarize_trace_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    system_prompt_lengths = [_coerce_int((row.get("continuity") or {}).get("system_prompt_length")) for row in rows]
    prompt_lengths = [_coerce_int((row.get("continuity") or {}).get("prompt_length")) for row in rows]
    frozen_prefix_lengths = [_coerce_int((row.get("continuity") or {}).get("frozen_prefix_length")) for row in rows]
    semi_stable_lengths = [_coerce_int((row.get("continuity") or {}).get("semi_stable_length")) for row in rows]
    dynamic_prompt_lengths = [_coerce_int((row.get("continuity") or {}).get("dynamic_prompt_length")) for row in rows]
    semantic_system_hashes = [str((row.get("continuity") or {}).get("semantic_system_hash", "") or "") for row in rows]
    stable_prefix_hashes = [str((row.get("continuity") or {}).get("stable_prefix_hash", "") or "") for row in rows]
    prefix_hashes = [str((row.get("continuity") or {}).get("prefix_hash", "") or "") for row in rows]

    native_prefix_stable_values = [
        (row.get("continuity") or {}).get("prefix_stable")
        for row in rows
        if (row.get("continuity") or {}).get("prefix_stable") is not None
    ]
    native_prefix_stable_rate = (
        round(sum(1 for value in native_prefix_stable_values if bool(value)) / len(native_prefix_stable_values), 4)
        if native_prefix_stable_values
        else None
    )
    cache_ready_values = [bool((row.get("continuity") or {}).get("cache_ready", False)) for row in rows]
    cache_hit_values = [bool((row.get("continuity") or {}).get("cache_hit", False)) for row in rows]
    cache_ready_reason_frequency: Counter[str] = Counter()
    for row in rows:
        for reason in list((row.get("continuity") or {}).get("cache_ready_reasons", []) or []):
            normalized_reason = str(reason or "").strip()
            if normalized_reason:
                cache_ready_reason_frequency[normalized_reason] += 1

    reason_counts = Counter(
        _normalize_prefix_reason((row.get("continuity") or {}).get("prefix_changed_reason", ""))
        for row in rows
    )
    stable_prefix_counts = Counter(value for value in stable_prefix_hashes if value)
    dominant_hash_rate = (
        round(max(stable_prefix_counts.values()) / len([value for value in stable_prefix_hashes if value]), 4)
        if stable_prefix_counts
        else 0.0
    )
    frozen_block_totals: Counter[str] = Counter()
    semi_stable_block_totals: Counter[str] = Counter()
    dynamic_block_totals: Counter[str] = Counter()
    block_analysis_modes = Counter(str((row.get("continuity") or {}).get("block_analysis_mode", "") or "unknown") for row in rows)
    for row in rows:
        continuity = row.get("continuity") or {}
        current_frozen = _normalize_block_map(continuity.get("frozen_prefix_blocks"))
        if current_frozen:
            current_frozen["persona_or_identity"] = _persona_identity_total(current_frozen)
        for key, value in current_frozen.items():
            frozen_block_totals[key] += value
        for key, value in _normalize_block_map(continuity.get("semi_stable_blocks")).items():
            semi_stable_block_totals[key] += value
        for key, value in _normalize_block_map(continuity.get("dynamic_prompt_blocks")).items():
            dynamic_block_totals[key] += value

    return {
        "sample_count": len(rows),
        "case_ids": [str(row.get("case_id", "") or "") for row in rows],
        "status_counts": dict(Counter(str(row.get("status", "") or "") for row in rows)),
        "validation_failure_count": sum(1 for row in rows if row.get("status") != "ok"),
        "system_prompt_length": {
            "mean": _mean(system_prompt_lengths),
            "median": _median(system_prompt_lengths),
            "p95": _p95(system_prompt_lengths),
        },
        "prompt_length": {
            "mean": _mean(prompt_lengths),
            "median": _median(prompt_lengths),
            "p95": _p95(prompt_lengths),
        },
        "dynamic_prompt_length": {
            "mean": _mean(dynamic_prompt_lengths),
            "median": _median(dynamic_prompt_lengths),
            "p95": _p95(dynamic_prompt_lengths),
        },
        "frozen_prefix_length": {
            "mean": _mean(frozen_prefix_lengths),
            "median": _median(frozen_prefix_lengths),
            "p95": _p95(frozen_prefix_lengths),
        },
        "semi_stable_length": {
            "mean": _mean(semi_stable_lengths),
            "median": _median(semi_stable_lengths),
            "p95": _p95(semi_stable_lengths),
        },
        "stable_prefix_hash": {
            "unique_count": len(stable_prefix_counts),
            "dominant_hash_rate": dominant_hash_rate,
            "pairwise_stability_rate": _pairwise_stability_rate(stable_prefix_hashes),
            "counts": dict(stable_prefix_counts),
        },
        "semantic_system_hash": {
            "unique_count": len({value for value in semantic_system_hashes if value}),
            "dominant_hash_rate": round(
                max(Counter(value for value in semantic_system_hashes if value).values()) / len([value for value in semantic_system_hashes if value]),
                4,
            ) if [value for value in semantic_system_hashes if value] else 0.0,
            "pairwise_stability_rate": _pairwise_stability_rate(semantic_system_hashes),
            "counts": dict(Counter(value for value in semantic_system_hashes if value)),
        },
        "prefix_hash": {
            "unique_count": len({value for value in prefix_hashes if value}),
            "counts": dict(Counter(value for value in prefix_hashes if value)),
        },
        "native_prefix_stable_rate": native_prefix_stable_rate,
        "cache_ready_rate": round(sum(1 for value in cache_ready_values if value) / max(1, len(cache_ready_values)), 4),
        "cache_hit_rate": round(sum(1 for value in cache_hit_values if value) / max(1, len(cache_hit_values)), 4),
        "cache_ready_reason_frequency": dict(cache_ready_reason_frequency),
        "prefix_changed_reasons": dict(reason_counts),
        "remaining_system_composition": {
            "frozen_prefix_blocks": dict(frozen_block_totals),
            "dynamic_prompt_blocks": dict(dynamic_block_totals),
        },
        "remaining_prompt_background_composition": {
            "soft_background_blocks": dict(semi_stable_block_totals),
        },
        "block_analysis_modes": dict(block_analysis_modes),
        "system_rules_breakdown": _system_rules_item_breakdown(rows),
        "system_rules_candidate_frequency": _system_rules_item_frequency(rows, candidate_only=True),
        "system_rules_keep_frequency": _system_rules_item_frequency(rows, candidate_only=False),
    }


def compare_trace_runs(baseline_rows: list[dict[str, Any]], current_rows: list[dict[str, Any]]) -> dict[str, Any]:
    baseline_by_case = {str(row.get("case_id", "") or ""): row for row in baseline_rows}
    current_by_case = {str(row.get("case_id", "") or ""): row for row in current_rows}
    all_case_ids = sorted(set(baseline_by_case) | set(current_by_case))
    aligned_rows: list[dict[str, Any]] = []
    for case_id in all_case_ids:
        baseline_row = baseline_by_case.get(case_id, {})
        current_row = current_by_case.get(case_id, {})
        baseline_continuity = baseline_row.get("continuity") or {}
        current_continuity = current_row.get("continuity") or {}
        current_frozen = _normalize_block_map(current_continuity.get("frozen_prefix_blocks"))
        if current_frozen:
            current_frozen["persona_or_identity"] = _persona_identity_total(current_frozen)
        baseline_raw_frozen = baseline_continuity.get("frozen_prefix_blocks")
        current_raw_frozen = current_continuity.get("frozen_prefix_blocks")
        current_system_blocks = {**current_frozen, **_normalize_block_map(current_continuity.get("semi_stable_blocks"))}
        current_ranked_blocks = _sorted_block_lengths(current_system_blocks)
        largest_remaining_system_block = current_ranked_blocks[0]["block"] if current_ranked_blocks else ""
        largest_migratable_block = ""
        for block in current_ranked_blocks:
            if _recommended_migration_target(str(block["block"])) != "keep_in_system":
                largest_migratable_block = str(block["block"])
                break
        aligned_rows.append(
            {
                "case_id": case_id,
                "baseline_status": str(baseline_row.get("status", "missing") or "missing"),
                "current_status": str(current_row.get("status", "missing") or "missing"),
                "baseline_system_prompt_length": _coerce_int(baseline_continuity.get("system_prompt_length")),
                "current_system_prompt_length": _coerce_int(current_continuity.get("system_prompt_length")),
                "delta_system_prompt_length": _coerce_int(current_continuity.get("system_prompt_length")) - _coerce_int(baseline_continuity.get("system_prompt_length")),
                "baseline_dynamic_prompt_length": _coerce_int(baseline_continuity.get("dynamic_prompt_length")),
                "current_dynamic_prompt_length": _coerce_int(current_continuity.get("dynamic_prompt_length")),
                "delta_dynamic_prompt_length": _coerce_int(current_continuity.get("dynamic_prompt_length")) - _coerce_int(baseline_continuity.get("dynamic_prompt_length")),
                "baseline_stable_prefix_hash": str(baseline_continuity.get("stable_prefix_hash", "") or ""),
                "current_stable_prefix_hash": str(current_continuity.get("stable_prefix_hash", "") or ""),
                "current_frozen_prefix_length": _coerce_int(current_continuity.get("frozen_prefix_length")),
                "current_semi_stable_length": _coerce_int(current_continuity.get("semi_stable_length")),
                "current_remaining_system_blocks": current_ranked_blocks,
                "largest_remaining_system_block": str(largest_remaining_system_block or ""),
                "largest_migratable_block": largest_migratable_block,
                "recommended_migration_target": _recommended_migration_target(str(largest_migratable_block or largest_remaining_system_block or "")),
                "baseline_block_analysis_mode": str(baseline_continuity.get("block_analysis_mode", "") or "unknown"),
                "current_block_analysis_mode": str(current_continuity.get("block_analysis_mode", "") or "unknown"),
                "system_rules_comparable": _block_available(baseline_raw_frozen, "system_rules") and _block_available(current_raw_frozen, "system_rules"),
                "persona_or_identity_comparable": (
                    _block_available(baseline_raw_frozen, "persona_core")
                    and _block_available(baseline_raw_frozen, "style_block")
                    and _block_available(baseline_raw_frozen, "system_rules")
                    and _block_available(current_raw_frozen, "persona_core")
                    and _block_available(current_raw_frozen, "style_block")
                    and _block_available(current_raw_frozen, "system_rules")
                ),
            }
        )
    migration_priority_rows = [
        row
        for row in aligned_rows
        if row["case_id"] in HIGH_DYNAMIC_CASE_IDS
        and row["delta_dynamic_prompt_length"] > 0
        and row["largest_remaining_system_block"]
        and row["largest_migratable_block"]
    ]
    migration_priority_rows.sort(
        key=lambda row: (
            -_coerce_int(row["delta_dynamic_prompt_length"]),
            -len(str(row["largest_migratable_block"] or "")),
            str(row["case_id"]),
        )
    )
    migration_candidate_frequency = dict(
        Counter(
            str(row["largest_migratable_block"] or "")
            for row in migration_priority_rows
            if str(row["largest_migratable_block"] or "")
        )
    )
    return {
        "aligned_case_count": len(aligned_rows),
        "status_mismatches": [
            {
                "case_id": row["case_id"],
                "baseline_status": row["baseline_status"],
                "current_status": row["current_status"],
            }
            for row in aligned_rows
            if row["baseline_status"] != row["current_status"]
        ],
        "rows": aligned_rows,
        "migration_priority_rows": migration_priority_rows,
        "migration_candidate_frequency": migration_candidate_frequency,
    }


def build_report(
    *,
    baseline_label: str,
    current_label: str,
    baseline_rows: list[dict[str, Any]],
    current_rows: list[dict[str, Any]],
    baseline_benchmark: dict[str, Any],
    current_benchmark: dict[str, Any],
    baseline_root: Path,
    current_root: Path,
) -> dict[str, Any]:
    baseline_summary = summarize_trace_rows(baseline_rows)
    current_summary = summarize_trace_rows(current_rows)
    comparison = compare_trace_runs(baseline_rows, current_rows)
    current_frozen = dict(current_summary.get("remaining_system_composition", {}).get("frozen_prefix_blocks", {}) or {})
    baseline_frozen = dict(baseline_summary.get("remaining_system_composition", {}).get("frozen_prefix_blocks", {}) or {})
    current_cold_count = int(current_summary.get("prefix_changed_reasons", {}).get("cold_summary_changed", 0) or 0)
    current_sample_count = max(1, int(current_summary.get("sample_count", 0) or 0))
    current_soft_background = dict(current_summary.get("remaining_prompt_background_composition", {}).get("soft_background_blocks", {}) or {})
    baseline_soft_background = dict(baseline_summary.get("remaining_prompt_background_composition", {}).get("soft_background_blocks", {}) or {})
    limitations = [
        "旧基线版本没有原生落盘 continuity prompt 指标，因此 before 侧部分字段由同轮离线 replay 的最终 prompt 实测补采，而不是读取历史 artifact 字段。",
        "native_prefix_stable_rate 只对原生提供 prefix_stable 的版本有解释力；跨版本稳定性以 stable_prefix_hash 的派生统计为准。",
        "benchmark 汇总沿用 replay seed builder，只作为辅助参考，不作为主回复链 prompt 迁移收益的主证据。",
    ]
    limitations.append("cache_ready != cache_hit: `cache_ready` 只代表稳定 hash / request_cache_control / cache affinity 等准备条件存在；只有 provider usage 明确返回 cached input 才算真实命中。")
    persona_blocks_comparable = any(bool(row.get("persona_or_identity_comparable")) for row in comparison.get("rows", []) or [])
    system_rules_comparable = any(bool(row.get("system_rules_comparable")) for row in comparison.get("rows", []) or [])
    if persona_blocks_comparable:
        if _coerce_int(current_frozen.get("persona_or_identity")) - _coerce_int(baseline_frozen.get("persona_or_identity")) > 25:
            limitations.append("runtime_inflation_suspected: current persona_or_identity block is materially larger than baseline.")
    else:
        limitations.append("runtime_inflation_unresolved_due_to_baseline_gap")
    if current_cold_count / current_sample_count > 0.5:
        limitations.append("cold_summary churn may still be replay-driven")
    baseline_vs_current_block_delta = {}
    for block_name in ("persona_or_identity", "cold_summary", "stable_behavior_rules", "system_rules"):
        comparable = False
        if block_name == "system_rules":
            comparable = system_rules_comparable
        elif block_name == "persona_or_identity":
            comparable = persona_blocks_comparable
        else:
            baseline_source = baseline_soft_background if block_name == "cold_summary" else baseline_frozen
            current_source = current_soft_background if block_name == "cold_summary" else current_frozen
            comparable = _coerce_int(baseline_source.get(block_name)) > 0 and _coerce_int(current_source.get(block_name)) > 0
        if comparable:
            baseline_source = baseline_soft_background if block_name == "cold_summary" else baseline_frozen
            current_source = current_soft_background if block_name == "cold_summary" else current_frozen
            baseline_value = _coerce_int(baseline_source.get(block_name))
            current_value = _coerce_int(current_source.get(block_name))
            baseline_vs_current_block_delta[block_name] = {
                "baseline_mean_proxy": baseline_value,
                "current_mean_proxy": current_value,
                "delta_mean_proxy": current_value - baseline_value,
                "baseline_median_proxy": baseline_value,
                "current_median_proxy": current_value,
                "delta_median_proxy": current_value - baseline_value,
                "delta_mode": "comparable",
            }
        else:
            baseline_vs_current_block_delta[block_name] = {
                "delta_mode": "not_comparable",
            }
    comparable_deltas = {
        key: value.get("delta_mean_proxy", -10**9)
        for key, value in baseline_vs_current_block_delta.items()
        if value.get("delta_mode") == "comparable"
    }
    if comparable_deltas and max(comparable_deltas.items(), key=lambda item: item[1])[0] == "system_rules":
        limitations.append("system_rules inflation dominates replay delta")

    live_summary_root = REPO_ROOT / "artifacts" / "main_reply_cache_live"
    live_summary_path = live_summary_root / "summary.json"
    for provider_family in ("anthropic", "gemini", "native_chat"):
        provider_dir = live_summary_root / provider_family
        if (provider_dir / "summary.json").exists():
            live_summary_path = provider_dir / "summary.json"
            break
    live_summary = {}
    if live_summary_path.exists():
        try:
            live_summary = json.loads(live_summary_path.read_text(encoding="utf-8"))
        except Exception:
            live_summary = {}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "baseline": {
            "label": baseline_label,
            "root": str(baseline_root),
            "trace_summary": baseline_summary,
            "benchmark_summary": baseline_benchmark,
        },
        "current": {
            "label": current_label,
            "root": str(current_root),
            "trace_summary": current_summary,
            "benchmark_summary": current_benchmark,
        },
        "delta": {
            "system_prompt_length_mean": round(
                _coerce_float(current_summary["system_prompt_length"]["mean"]) - _coerce_float(baseline_summary["system_prompt_length"]["mean"]),
                2,
            ),
            "system_prompt_length_median": round(
                _coerce_float(current_summary["system_prompt_length"]["median"]) - _coerce_float(baseline_summary["system_prompt_length"]["median"]),
                2,
            ),
            "dynamic_prompt_length_mean": round(
                _coerce_float(current_summary["dynamic_prompt_length"]["mean"]) - _coerce_float(baseline_summary["dynamic_prompt_length"]["mean"]),
                2,
            ),
            "dynamic_prompt_length_median": round(
                _coerce_float(current_summary["dynamic_prompt_length"]["median"]) - _coerce_float(baseline_summary["dynamic_prompt_length"]["median"]),
                2,
            ),
            "stable_prefix_hash_pairwise_rate": round(
                _coerce_float(current_summary["stable_prefix_hash"]["pairwise_stability_rate"])
                - _coerce_float(baseline_summary["stable_prefix_hash"]["pairwise_stability_rate"]),
                4,
            ),
            "semantic_system_hash_pairwise_rate": round(
                _coerce_float(current_summary["semantic_system_hash"]["pairwise_stability_rate"])
                - _coerce_float(baseline_summary["semantic_system_hash"]["pairwise_stability_rate"]),
                4,
            ),
            "stable_prefix_hash_dominant_rate": round(
                _coerce_float(current_summary["stable_prefix_hash"]["dominant_hash_rate"])
                - _coerce_float(baseline_summary["stable_prefix_hash"]["dominant_hash_rate"]),
                4,
            ),
        },
        "comparison": comparison,
        "baseline_vs_current_block_delta": baseline_vs_current_block_delta,
        "diagnostics": {
            "legacy_cold_summary_changed_case_ids": [
                str(row.get("case_id", "") or "")
                for row in current_rows
                if str((row.get("continuity") or {}).get("prefix_changed_reason", "") or "") == "cold_summary_changed"
            ],
            "hook_changed_system_case_ids": [
                str(row.get("case_id", "") or "")
                for row in current_rows
                if str((row.get("continuity") or {}).get("provider_visible_system_hash", "") or "")
                and str((row.get("continuity") or {}).get("post_hook_system_hash", "") or "")
                and str((row.get("continuity") or {}).get("provider_visible_system_hash", "") or "")
                != str((row.get("continuity") or {}).get("post_hook_system_hash", "") or "")
            ],
            "first_seen_case_ids": [
                str(row.get("case_id", "") or "")
                for row in current_rows
                if str((row.get("continuity") or {}).get("prefix_changed_reason", "") or "") == "first_seen"
            ],
            "system_rules_comparable": system_rules_comparable,
            "persona_or_identity_comparable": persona_blocks_comparable,
        },
        "current_system_rules_breakdown": current_summary.get("system_rules_breakdown", []),
        "system_rules_migration_candidates": current_summary.get("system_rules_candidate_frequency", {}),
        "system_rules_keep_items": current_summary.get("system_rules_keep_frequency", {}),
        "next_real_migration_candidate": (
            max(current_summary.get("system_rules_candidate_frequency", {}).items(), key=lambda item: item[1])[0]
            if current_summary.get("system_rules_candidate_frequency")
            else (
                "stable_behavior_rules"
                if comparison.get("migration_candidate_frequency", {}).get("stable_behavior_rules", 0) > 0
                else ""
            )
        ),
        "limitations": limitations,
        "live_replay": live_summary,
    }


def render_report_markdown(report: dict[str, Any]) -> str:
    baseline = report["baseline"]
    current = report["current"]
    delta = report["delta"]
    comparison = report["comparison"]
    baseline_trace = baseline["trace_summary"]
    current_trace = current["trace_summary"]
    live_replay = dict(report.get("live_replay", {}) or {})

    lines = [
        "# Prompt Metrics Before/After Report",
        "",
        f"- Generated At: `{report.get('generated_at', '')}`",
        f"- Baseline: `{baseline.get('label', '')}`",
        f"- Current: `{current.get('label', '')}`",
        f"- Baseline Root: `{baseline.get('root', '')}`",
        f"- Current Root: `{current.get('root', '')}`",
        "",
        "## Trace Overview",
        "",
        f"- Baseline Sample Count: `{baseline_trace.get('sample_count', 0)}`",
        f"- Current Sample Count: `{current_trace.get('sample_count', 0)}`",
        f"- Status Mismatches: `{len(comparison.get('status_mismatches', []) or [])}`",
        "",
        "## Core Metrics",
        "",
        f"- System Prompt Length Mean: baseline `{baseline_trace['system_prompt_length']['mean']}` -> current `{current_trace['system_prompt_length']['mean']}` (delta `{delta['system_prompt_length_mean']}`)",
        f"- System Prompt Length Median: baseline `{baseline_trace['system_prompt_length']['median']}` -> current `{current_trace['system_prompt_length']['median']}` (delta `{delta['system_prompt_length_median']}`)",
        f"- Dynamic Prompt Length Mean: baseline `{baseline_trace['dynamic_prompt_length']['mean']}` -> current `{current_trace['dynamic_prompt_length']['mean']}` (delta `{delta['dynamic_prompt_length_mean']}`)",
        f"- Dynamic Prompt Length Median: baseline `{baseline_trace['dynamic_prompt_length']['median']}` -> current `{current_trace['dynamic_prompt_length']['median']}` (delta `{delta['dynamic_prompt_length_median']}`)",
        f"- Stable Prefix Hash Pairwise Rate: baseline `{baseline_trace['stable_prefix_hash']['pairwise_stability_rate']}` -> current `{current_trace['stable_prefix_hash']['pairwise_stability_rate']}` (delta `{delta['stable_prefix_hash_pairwise_rate']}`)",
        f"- Stable Prefix Hash Dominant Rate: baseline `{baseline_trace['stable_prefix_hash']['dominant_hash_rate']}` -> current `{current_trace['stable_prefix_hash']['dominant_hash_rate']}` (delta `{delta['stable_prefix_hash_dominant_rate']}`)",
        f"- Semantic System Hash Pairwise Rate: baseline `{baseline_trace['semantic_system_hash']['pairwise_stability_rate']}` -> current `{current_trace['semantic_system_hash']['pairwise_stability_rate']}` (delta `{delta['semantic_system_hash_pairwise_rate']}`)",
        "",
        "## Prefix Diagnostics",
        "",
        f"- Baseline Native Prefix Stable Rate: `{baseline_trace.get('native_prefix_stable_rate')}`",
        f"- Current Native Prefix Stable Rate: `{current_trace.get('native_prefix_stable_rate')}`",
        f"- Baseline Cache Ready Rate: `{baseline_trace.get('cache_ready_rate')}`",
        f"- Current Cache Ready Rate: `{current_trace.get('cache_ready_rate')}`",
        f"- Baseline Cache Hit Rate: `{baseline_trace.get('cache_hit_rate')}`",
        f"- Current Cache Hit Rate: `{current_trace.get('cache_hit_rate')}`",
        f"- Current Cache Ready Reasons: `{current_trace.get('cache_ready_reason_frequency', {})}`",
        f"- Baseline Prefix Changed Reasons: `{baseline_trace.get('prefix_changed_reasons', {})}`",
        f"- Current Prefix Changed Reasons: `{current_trace.get('prefix_changed_reasons', {})}`",
        f"- Block Analysis Modes: baseline `{baseline_trace.get('block_analysis_modes', {})}` | current `{current_trace.get('block_analysis_modes', {})}`",
        f"- Hook Changed System Case Ids: `{report.get('diagnostics', {}).get('hook_changed_system_case_ids', [])}`",
        "- `prefix_hash` should be read as a continuity/native prefix compatibility signal, not as the canonical semantic-system identity.",
        "",
        "## Semantic System Diagnostics",
        "",
        f"- Baseline Semantic System Hash Stats: `{baseline_trace.get('semantic_system_hash', {})}`",
        f"- Current Semantic System Hash Stats: `{current_trace.get('semantic_system_hash', {})}`",
        "- `semantic_system_hash` tracks semantic-layer system stability, while `provider_visible_system_hash` should be interpreted as final provider-visible stability after hook/provider processing.",
        "",
        "## Live Replay Evidence",
        "",
        f"- Validation Verdict: `{live_replay.get('validation_verdict', '')}`",
        f"- Provider Supports Usage Reporting: `{live_replay.get('provider_supports_usage_reporting', '')}`",
        f"- Provider Supports Session ID: `{live_replay.get('provider_supports_session_id', '')}`",
        f"- Session Reuse Validation Deferred: `{live_replay.get('session_reuse_validation_deferred', '')}`",
        f"- Cache Ready Rate: `{live_replay.get('cache_ready_rate', '')}`",
        f"- Cache Hit Rate: `{live_replay.get('cache_hit_rate', '')}`",
        f"- Cache Ready But Hit Miss Case Ids: `{live_replay.get('cache_ready_but_hit_miss_case_ids', [])}`",
        f"- Usage Reporting Supported: `{live_replay.get('usage_reporting_supported', '')}`",
        f"- Hash Stable But Cache Miss Case Ids: `{live_replay.get('hash_stable_but_cache_miss_case_ids', [])}`",
        "",
        "## Remaining System Composition",
        "",
        f"- Baseline Frozen Prefix Blocks: `{baseline_trace.get('remaining_system_composition', {}).get('frozen_prefix_blocks', {})}`",
        f"- Current Frozen Prefix Blocks: `{current_trace.get('remaining_system_composition', {}).get('frozen_prefix_blocks', {})}`",
        f"- Baseline Soft Background Blocks: `{baseline_trace.get('remaining_prompt_background_composition', {}).get('soft_background_blocks', {})}`",
        f"- Current Soft Background Blocks: `{current_trace.get('remaining_prompt_background_composition', {}).get('soft_background_blocks', {})}`",
        f"- Block Delta: `{report.get('baseline_vs_current_block_delta', {})}`",
        f"- Largest Block In Current Trace: `{(comparison.get('rows') or [{}])[0].get('largest_remaining_system_block', '') if (comparison.get('rows') or []) else ''}`",
        "",
        "## System Rules Breakdown",
        "",
        f"- Current System Rules Breakdown: `{report.get('current_system_rules_breakdown', [])}`",
        f"- System Rules Migration Candidates: `{report.get('system_rules_migration_candidates', {})}`",
        f"- System Rules Keep Items: `{report.get('system_rules_keep_items', {})}`",
        f"- Next Real Migration Candidate: `{report.get('next_real_migration_candidate', '')}`",
        f"- Runtime Prompt Layer: `planner_runtime_instruction` is treated as dynamic prompt control text, not remaining system content.",
        "",
        "## High-Dynamic Case Priority",
        "",
    ]
    priority_rows = list(comparison.get("migration_priority_rows", []) or [])
    if priority_rows:
        for row in priority_rows:
            lines.append(
                f"- `{row.get('case_id', '')}` | dynamic `{row.get('delta_dynamic_prompt_length', 0)}` | system `{row.get('delta_system_prompt_length', 0)}` | largest `{row.get('largest_remaining_system_block', '')}` | migratable `{row.get('largest_migratable_block', '')}` -> target `{row.get('recommended_migration_target', '')}`"
            )
    else:
        lines.append("_No high-dynamic migration targets identified._")
    freq = dict(comparison.get("migration_candidate_frequency", {}) or {})
    if freq:
        top_block = max(freq.items(), key=lambda item: item[1])[0]
        top_count = max(freq.items(), key=lambda item: item[1])[1]
        if int(top_count or 0) >= 3:
            lines.append(f"- Stable Global Candidate: `{top_block}`")
        else:
            lines.append("- Stable Global Candidate: `no stable global migration candidate yet`")
    else:
        lines.append("- Stable Global Candidate: `no stable global migration candidate yet`")
    if not report.get("diagnostics", {}).get("system_rules_comparable", False):
        lines.append("- system_rules not comparable across baseline/current")
    lines.extend(
        [
            "",
        "## Benchmark Overview",
        "",
        f"- Baseline Avg Stable Prefix Length: `{((baseline.get('benchmark_summary') or {}).get('overview') or {}).get('avg_stable_prefix_length', 0)}`",
        f"- Current Avg Stable Prefix Length: `{((current.get('benchmark_summary') or {}).get('overview') or {}).get('avg_stable_prefix_length', 0)}`",
        f"- Baseline Avg Dynamic Payload Length: `{((baseline.get('benchmark_summary') or {}).get('overview') or {}).get('avg_dynamic_payload_length', 0)}`",
        f"- Current Avg Dynamic Payload Length: `{((current.get('benchmark_summary') or {}).get('overview') or {}).get('avg_dynamic_payload_length', 0)}`",
        "- Benchmark Signal: `辅助无新增信号`" if ((baseline.get('benchmark_summary') or {}).get('overview') or {}) == ((current.get('benchmark_summary') or {}).get('overview') or {}) else "- Benchmark Signal: `存在辅助变化，需结合主回复链 trace 解释`",
        "",
        "## Status Mismatches",
        "",
        ]
    )
    mismatches = list(comparison.get("status_mismatches", []) or [])
    if mismatches:
        for item in mismatches:
            lines.append(
                f"- `{item.get('case_id', '')}` | baseline `{item.get('baseline_status', '')}` | current `{item.get('current_status', '')}`"
            )
    else:
        lines.append("_None._")
    lines.extend(
        [
            "",
            "## Limitations",
            "",
        ]
    )
    for item in report.get("limitations", []) or []:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def _run_harness(repo_root: Path, output_path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-c", _HARNESS_CODE, str(output_path)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Harness failed for {repo_root}:\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )
    return json.loads(output_path.read_text(encoding="utf-8"))


def _build_benchmark_from_rows(repo_root: Path, rows: list[dict[str, Any]], output_root: Path, label: str) -> dict[str, Any]:
    replay_root = output_root / "_tmp_replay" / label / "run-1"
    replay_root.mkdir(parents=True, exist_ok=True)
    report_path = replay_root / "report.jsonl"
    with report_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    {
                        "kind": "case",
                        "case_id": row.get("case_id", ""),
                        "reply_preview": row.get("reply_preview", ""),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    seed_output = output_root / f"{label}_context_economy_samples.jsonl"
    env = dict(**__import__("os").environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + (__import__("os").pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    builder = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tests" / "manual" / "context_economy_replay_seed_builder.py"),
            "--replay-root",
            str(replay_root.parent),
            "--output",
            str(seed_output),
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if builder.returncode != 0:
        raise RuntimeError(
            f"Seed builder failed for {repo_root}:\nstdout:\n{builder.stdout}\n\nstderr:\n{builder.stderr}"
        )

    benchmark_root = output_root / "benchmarks"
    benchmark = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tests" / "manual" / "context_economy_benchmark.py"),
            "--samples",
            str(seed_output),
            "--output-root",
            str(benchmark_root),
            "--label",
            label,
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if benchmark.returncode != 0:
        raise RuntimeError(
            f"Benchmark runner failed for {repo_root}:\nstdout:\n{benchmark.stdout}\n\nstderr:\n{benchmark.stderr}"
        )
    run_dir = Path(benchmark.stdout.strip().splitlines()[-1].strip())
    return json.loads((run_dir / "benchmark_summary.json").read_text(encoding="utf-8"))


def _resolve_run_dir(output_root: Path, label: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_label = str(label or "compare").strip().replace(" ", "_")
    run_dir = output_root / f"{timestamp}-{safe_label}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def main() -> int:
    args = build_parser().parse_args()
    baseline_root = Path(args.baseline_root).resolve()
    current_root = Path(args.current_root).resolve()
    output_root = Path(args.output_root).resolve()
    run_dir = _resolve_run_dir(output_root, args.label)

    with tempfile.TemporaryDirectory(prefix="prompt-metrics-compare-") as temp_dir:
        temp_root = Path(temp_dir)
        baseline_trace_raw = _run_harness(baseline_root, temp_root / "baseline_trace.json")
        current_trace_raw = _run_harness(current_root, temp_root / "current_trace.json")

    baseline_rows = list(baseline_trace_raw.get("rows", []) or [])
    current_rows = list(current_trace_raw.get("rows", []) or [])

    baseline_benchmark = _build_benchmark_from_rows(baseline_root, baseline_rows, run_dir, "baseline_6d5ecde")
    current_benchmark = _build_benchmark_from_rows(current_root, current_rows, run_dir, "current_round3")

    report = build_report(
        baseline_label="6d5ecde",
        current_label=args.label,
        baseline_rows=baseline_rows,
        current_rows=current_rows,
        baseline_benchmark=baseline_benchmark,
        current_benchmark=current_benchmark,
        baseline_root=baseline_root,
        current_root=current_root,
    )

    (run_dir / "baseline_trace_rows.json").write_text(json.dumps({"rows": baseline_rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "current_trace_rows.json").write_text(json.dumps({"rows": current_rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "comparison_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "comparison_report.md").write_text(render_report_markdown(report), encoding="utf-8")

    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
