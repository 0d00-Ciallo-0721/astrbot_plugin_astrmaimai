from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from .chat_loop_kernel import ChatLoopKernel


DEFAULT_BENCHMARK_PROFILES = (
    "dialogue_first",
    "balanced",
    "maintenance_friendly",
)


def scheduler_benchmark_scenarios() -> dict[str, dict[str, Any]]:
    return {
        "hot_dialogue_pressure": {
            "description": "High WAITING/BUSY/ACTIVE pressure with small maintenance backlog.",
            "max_batch": 20,
            "horizon_seconds": 2.0,
            "now": 100.0,
            "chats": [
                *[
                    {
                        "chat_id": f"hot-busy-{index}",
                        "phase": "BUSY" if index < 8 else "ACTIVE",
                        "next_tick_at": 92.0 + (index * 0.1),
                        "latest_activity_ts": 100.0 - index,
                        "executor_pending": 1 if index < 8 else 0,
                        "consecutive_selected_count": 2 if index < 4 else 0,
                    }
                    for index in range(12)
                ],
                *[
                    {
                        "chat_id": f"hot-maint-{index}",
                        "phase": "MAINTENANCE",
                        "next_tick_at": 90.0 + (index * 0.2),
                        "latest_activity_ts": 80.0 - index,
                        "maintenance_candidates_summary": {
                            "memory": {"candidate_present": True, "reason": "eligible"},
                        },
                    }
                    for index in range(6)
                ],
            ],
        },
        "maintenance_backlog": {
            "description": "Maintenance-heavy due set without dialogue pressure.",
            "max_batch": 20,
            "horizon_seconds": 2.0,
            "now": 100.0,
            "chats": [
                *[
                    {
                        "chat_id": f"maint-backlog-{index}",
                        "phase": "MAINTENANCE",
                        "next_tick_at": 88.0 + (index * 0.15),
                        "latest_activity_ts": 85.0 - index,
                        "last_maintenance_selected_at": 10.0,
                        "maintenance_candidates_summary": {
                            "compaction": {"eligible": index < 4},
                            "memory": {"candidate_present": True, "reason": "eligible"},
                            "dream": {"eligible": index >= 8},
                        },
                    }
                    for index in range(10)
                ],
                *[
                    {
                        "chat_id": f"maint-idle-{index}",
                        "phase": "IDLE",
                        "next_tick_at": 91.0 + (index * 0.2),
                        "latest_activity_ts": 60.0 - index,
                    }
                    for index in range(4)
                ],
            ],
        },
        "busy_executor_pressure": {
            "description": "Executor backlog is high enough to trigger busy backpressure.",
            "max_batch": 20,
            "horizon_seconds": 2.0,
            "now": 100.0,
            "chats": [
                *[
                    {
                        "chat_id": f"busy-exec-{index}",
                        "phase": "BUSY",
                        "next_tick_at": 89.0 + (index * 0.05),
                        "latest_activity_ts": 100.0 - index,
                        "executor_pending": 1,
                    }
                    for index in range(16)
                ],
                *[
                    {
                        "chat_id": f"busy-maint-{index}",
                        "phase": "MAINTENANCE",
                        "next_tick_at": 90.0 + (index * 0.1),
                        "latest_activity_ts": 72.0 - index,
                        "maintenance_candidates_summary": {
                            "memory": {"candidate_present": True, "reason": "eligible"},
                        },
                    }
                    for index in range(6)
                ],
            ],
        },
        "retry_pressure_mix": {
            "description": "Due set mixes retry pressure, light dialogue work, and maintenance backlog.",
            "max_batch": 18,
            "horizon_seconds": 2.0,
            "now": 100.0,
            "chats": [
                *[
                    {
                        "chat_id": f"retry-active-{index}",
                        "phase": "ACTIVE",
                        "next_tick_at": 90.0 + (index * 0.2),
                        "latest_activity_ts": 95.0 - index,
                        "retry_backoff_until": 120.0 if index < 3 else 0.0,
                    }
                    for index in range(6)
                ],
                *[
                    {
                        "chat_id": f"retry-maint-{index}",
                        "phase": "MAINTENANCE",
                        "next_tick_at": 89.5 + (index * 0.2),
                        "latest_activity_ts": 70.0 - index,
                        "maintenance_candidates_summary": {
                            "memory": {"candidate_present": True, "reason": "eligible"},
                        },
                        "retry_backoff_until": 130.0 if index == 0 else 0.0,
                    }
                    for index in range(4)
                ],
                *[
                    {
                        "chat_id": f"retry-idle-{index}",
                        "phase": "IDLE",
                        "next_tick_at": 90.0 + (index * 0.25),
                        "latest_activity_ts": 55.0 - index,
                    }
                    for index in range(3)
                ],
            ],
        },
        "forced_promotion_pressure": {
            "description": "Several chats are due for starvation protection and should surface differently by profile.",
            "max_batch": 8,
            "horizon_seconds": 2.0,
            "now": 100.0,
            "chats": [
                *[
                    {
                        "chat_id": f"forced-maint-{index}",
                        "phase": "MAINTENANCE",
                        "next_tick_at": 90.0 + (index * 0.1),
                        "latest_activity_ts": 65.0 - index,
                        "missed_due_passes": 4,
                        "maintenance_candidates_summary": {
                            "memory": {"candidate_present": True, "reason": "eligible"},
                        },
                    }
                    for index in range(3)
                ],
                *[
                    {
                        "chat_id": f"forced-hot-{index}",
                        "phase": "ACTIVE",
                        "next_tick_at": 92.0 + (index * 0.1),
                        "latest_activity_ts": 100.0 - index,
                        "consecutive_selected_count": 5,
                        "last_selected_at": 99.0,
                    }
                    for index in range(4)
                ],
            ],
        },
    }


class _ScenarioCoordinator:
    def __init__(self, snapshots: dict[str, dict[str, Any]]):
        self._snapshots = snapshots

    async def get_activity_snapshot(self, chat_id: str):
        return dict(self._snapshots.get(chat_id, {"chat_id": chat_id, "wait_targets": [], "executor_pending": 0}))


async def _build_kernel_for_scenario(
    scenario_name: str,
    *,
    profile_name: str,
    scenario: dict[str, Any],
) -> ChatLoopKernel:
    snapshots: dict[str, dict[str, Any]] = {}
    for item in list(scenario.get("chats", []) or []):
        snapshots[str(item["chat_id"])] = {
            "chat_id": str(item["chat_id"]),
            "latest_activity_ts": float(item.get("latest_activity_ts", 0.0) or 0.0),
            "executor_pending": int(item.get("executor_pending", 0) or 0),
            "wait_targets": list(item.get("wait_targets", []) or []),
        }

    kernel = ChatLoopKernel(runtime_coordinator=_ScenarioCoordinator(snapshots))
    kernel.set_scheduler_profile_for_testing(profile_name)

    for item in list(scenario.get("chats", []) or []):
        state = await kernel.get_loop_state(str(item["chat_id"]))
        state.phase = str(item.get("phase", "IDLE") or "IDLE")
        state.next_tick_at = float(item.get("next_tick_at", 0.0) or 0.0)
        state.last_selected_at = float(item.get("last_selected_at", 0.0) or 0.0)
        state.consecutive_selected_count = int(item.get("consecutive_selected_count", 0) or 0)
        state.last_maintenance_selected_at = float(item.get("last_maintenance_selected_at", 0.0) or 0.0)
        state.retry_backoff_until = float(item.get("retry_backoff_until", 0.0) or 0.0)
        state.missed_due_passes = int(item.get("missed_due_passes", 0) or 0)
        state.pending_signals["maintenance_candidates_summary"] = dict(
            item.get("maintenance_candidates_summary", {}) or {}
        )
        await kernel._state_store.save(state)

    return kernel


def _profile_metrics(
    *,
    profile_name: str,
    report: dict[str, Any],
) -> dict[str, Any]:
    return {
        "profile": profile_name,
        "selected": list(report.get("selected", []) or []),
        "selected_count": len(list(report.get("selected", []) or [])),
        "dialogue_selected_count": len(list(report.get("dialogue_selected", []) or [])),
        "maintenance_selected_count": len(list(report.get("maintenance_selected", []) or [])),
        "forced_promotion_count": len(list(report.get("forced_promotions_selected", []) or [])),
        "batch_fill_rate": float(report.get("batch_fill_rate", 0.0) or 0.0),
        "quota_skip_counts": dict(report.get("quota_skip_counts", {}) or {}),
        "busy_backpressure_active": bool(report.get("busy_backpressure_active", False)),
        "maintenance_backpressure_active": bool(report.get("maintenance_backpressure_active", False)),
        "poll_mode": str(report.get("poll_mode", "") or ""),
        "maintenance_budget_total": int(report.get("maintenance_budget_total", 0) or 0),
        "maintenance_budget_used": int(report.get("maintenance_budget_used", 0) or 0),
        "maintenance_budget_remaining": int(report.get("maintenance_budget_remaining", 0) or 0),
        "scheduler_batch_plan": dict(report.get("batch_plan", {}) or {}),
        "forced_promotions_selected": list(report.get("forced_promotions_selected", []) or []),
        "score_breakdown": dict(report.get("score_breakdown", {}) or {}),
    }


async def run_scheduler_profile_matrix(
    *,
    profiles: tuple[str, ...] = DEFAULT_BENCHMARK_PROFILES,
    scenario_names: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    scenarios = scheduler_benchmark_scenarios()
    target_names = tuple(scenario_names or tuple(scenarios.keys()))
    results: dict[str, Any] = {
        "generated_at": time.time(),
        "profiles": list(profiles),
        "scenario_order": list(target_names),
        "scenarios": {},
    }

    for scenario_name in target_names:
        scenario = deepcopy(scenarios[scenario_name])
        scenario_results: dict[str, Any] = {}
        for profile_name in profiles:
            kernel = await _build_kernel_for_scenario(
                scenario_name,
                profile_name=profile_name,
                scenario=scenario,
            )
            report = await kernel.describe_due_selection(
                [str(item["chat_id"]) for item in list(scenario.get("chats", []) or [])],
                now=float(scenario.get("now", 100.0) or 100.0),
                horizon_seconds=float(scenario.get("horizon_seconds", 2.0) or 2.0),
                max_batch=int(scenario.get("max_batch", kernel.HEARTBEAT_MAX_BATCH) or kernel.HEARTBEAT_MAX_BATCH),
            )
            scenario_results[profile_name] = _profile_metrics(profile_name=profile_name, report=report)
        results["scenarios"][scenario_name] = {
            "description": str(scenario.get("description", "") or ""),
            "max_batch": int(scenario.get("max_batch", 0) or 0),
            "profiles": scenario_results,
        }
    return results


def build_scheduler_benchmark_meta(
    *,
    scenario_names: list[str],
    profile_names: list[str],
    label: str = "scheduler-matrix",
) -> dict[str, Any]:
    return {
        "label": label,
        "generated_at": time.time(),
        "scenario_names": list(scenario_names),
        "profile_names": list(profile_names),
    }


def recommend_balanced_tuning(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    scenarios = dict(matrix.get("scenarios", {}) or {})
    balanced_hot = dict(scenarios.get("hot_dialogue_pressure", {}).get("profiles", {}).get("balanced", {}) or {})
    if int(balanced_hot.get("maintenance_selected_count", 0) or 0) > 4:
        recommendations.append(
            {
                "scenario": "hot_dialogue_pressure",
                "severity": "high",
                "suggestion": "Lower maintenance_batch_slots or raise busy_backpressure_dialogue_cap guard.",
            }
        )

    balanced_maintenance = dict(scenarios.get("maintenance_backlog", {}).get("profiles", {}).get("balanced", {}) or {})
    if int(balanced_maintenance.get("maintenance_selected_count", 0) or 0) < 4:
        recommendations.append(
            {
                "scenario": "maintenance_backlog",
                "severity": "medium",
                "suggestion": "Raise maintenance_heavy_batch_slots or lower maintenance_boost_divisor_seconds.",
            }
        )

    dialogue_first = dict(scenarios.get("hot_dialogue_pressure", {}).get("profiles", {}).get("dialogue_first", {}) or {})
    if balanced_hot and dialogue_first and int(dialogue_first.get("maintenance_selected_count", 0) or 0) == int(balanced_hot.get("maintenance_selected_count", 0) or 0):
        recommendations.append(
            {
                "scenario": "hot_dialogue_pressure",
                "severity": "low",
                "suggestion": "Increase dialogue_first fairness_penalty_multiplier or shrink maintenance slots for stronger contrast.",
            }
        )

    maintenance_friendly = dict(scenarios.get("hot_dialogue_pressure", {}).get("profiles", {}).get("maintenance_friendly", {}) or {})
    if maintenance_friendly and int(maintenance_friendly.get("maintenance_selected_count", 0) or 0) > int(dict(maintenance_friendly.get("scheduler_batch_plan", {}) or {}).get("maintenance_slots", 0) or 0):
        recommendations.append(
            {
                "scenario": "hot_dialogue_pressure",
                "severity": "medium",
                "suggestion": "Maintenance-friendly profile is still too aggressive; reclaim maintenance_batch_slots.",
            }
        )

    return recommendations


def render_scheduler_benchmark_summary(matrix: dict[str, Any], meta: dict[str, Any], recommendations: list[dict[str, Any]]) -> str:
    lines = [
        "# Scheduler Profile Matrix Benchmark",
        "",
        f"- Label: `{meta.get('label', 'scheduler-matrix')}`",
        f"- Profiles: `{', '.join(list(meta.get('profile_names', []) or []))}`",
        f"- Scenarios: `{', '.join(list(meta.get('scenario_names', []) or []))}`",
        "",
    ]
    for scenario_name in list(meta.get("scenario_names", []) or []):
        scenario = dict(dict(matrix.get("scenarios", {}) or {}).get(scenario_name, {}) or {})
        lines.extend(
            [
                f"## {scenario_name}",
                "",
                f"- Description: {scenario.get('description', '')}",
                f"- Batch Limit: `{scenario.get('max_batch', 0)}`",
                "",
                "| Profile | Selected | Dialogue | Maintenance | Forced Promotion | Fill Rate | Poll Mode | Busy Backpressure | Maintenance Backpressure |",
                "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
            ]
        )
        for profile_name, metrics in dict(scenario.get("profiles", {}) or {}).items():
            lines.append(
                "| "
                + " | ".join(
                    [
                        profile_name,
                        str(metrics.get("selected_count", 0)),
                        str(metrics.get("dialogue_selected_count", 0)),
                        str(metrics.get("maintenance_selected_count", 0)),
                        str(metrics.get("forced_promotion_count", 0)),
                        f"{float(metrics.get('batch_fill_rate', 0.0) or 0.0):.2f}",
                        str(metrics.get("poll_mode", "")),
                        "true" if metrics.get("busy_backpressure_active", False) else "false",
                        "true" if metrics.get("maintenance_backpressure_active", False) else "false",
                    ]
                )
                + " |"
            )
        lines.append("")
    lines.extend(["## Recommendations", ""])
    if not recommendations:
        lines.append("- No immediate default-constant changes are recommended from this matrix.")
    else:
        for item in recommendations:
            lines.append(
                f"- `{item.get('scenario', '')}` [{item.get('severity', 'info')}] {item.get('suggestion', '')}"
            )
    lines.append("")
    return "\n".join(lines)


def write_scheduler_benchmark_artifacts(
    *,
    output_root: Path,
    matrix: dict[str, Any],
    meta: dict[str, Any],
    label: str,
    repo_root: Path,
) -> Path:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_id = f"{timestamp}-{label}"
    run_dir = Path(output_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    recommendations = recommend_balanced_tuning(matrix)
    summary = {
        "meta": meta,
        "matrix": matrix,
        "recommendations": recommendations,
    }
    (run_dir / "samples_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "matrix_results.json").write_text(
        json.dumps(matrix, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "benchmark_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "summary.md").write_text(
        render_scheduler_benchmark_summary(matrix, meta, recommendations),
        encoding="utf-8",
    )
    (run_dir / "repo_root.txt").write_text(str(Path(repo_root)), encoding="utf-8")
    return run_dir


def run_scheduler_profile_matrix_sync(
    *,
    profiles: tuple[str, ...] = DEFAULT_BENCHMARK_PROFILES,
    scenario_names: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        run_scheduler_profile_matrix(
            profiles=profiles,
            scenario_names=scenario_names,
        )
    )


__all__ = [
    "DEFAULT_BENCHMARK_PROFILES",
    "build_scheduler_benchmark_meta",
    "recommend_balanced_tuning",
    "render_scheduler_benchmark_summary",
    "run_scheduler_profile_matrix",
    "run_scheduler_profile_matrix_sync",
    "scheduler_benchmark_scenarios",
    "write_scheduler_benchmark_artifacts",
]
