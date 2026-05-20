from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from astrmai.conversation.loop.chat_loop_kernel import ChatLoopKernel


FIXTURE_ROOT = Path(
    os.getenv(
        "ASTRMAI_SCHEDULER_FIXTURE_ROOT",
        str(Path(__file__).resolve().parents[2] / "artifacts" / "scheduler_fixture"),
    )
)
FIXTURE_DB_PATH = FIXTURE_ROOT / "astrmai.db"
FIXTURE_CONFIG_PATH = FIXTURE_ROOT / "config.json"


class _FixtureCoordinator:
    def __init__(self, snapshots: dict[str, dict[str, Any]]):
        self._snapshots = snapshots

    async def list_active_chats(self, max_age_seconds: float = 1800) -> list[str]:
        return list(self._snapshots.keys())

    async def get_activity_snapshot(self, chat_id: str) -> dict[str, Any]:
        return dict(self._snapshots.get(chat_id, {"chat_id": chat_id, "wait_targets": [], "executor_pending": 0}))


class _FixtureProactiveTask:
    def __init__(self, kernel: ChatLoopKernel):
        self.chat_loop_kernel = kernel
        self._last_poll_mode = "FAST"

    def describe_status(self) -> dict[str, Any]:
        report = self.chat_loop_kernel.describe_last_due_selection_sync()
        policy = self.chat_loop_kernel.scheduler_policy_sync()
        poll_mode = str(report.get("poll_mode", "") or "NORMAL")
        previous = self._last_poll_mode
        self._last_poll_mode = poll_mode
        return {
            "running": True,
            "heartbeat_mode": "kernel_mediated",
            "scheduler_poll_mode": poll_mode,
            "scheduler_poll_interval": 5.0 if poll_mode == "FAST" else (10.0 if poll_mode == "NORMAL" else 15.0),
            "global_maintenance_interval": 60.0,
            "due_chat_count": len(list(report.get("selected", []) or [])),
            "skipped_not_due_count": len(list(report.get("skipped_not_due", []) or [])),
            "due_phase_mix": dict(report.get("due_phase_mix", {}) or {}),
            "maintenance_budget_total": int(report.get("maintenance_budget_total", 0) or 0),
            "maintenance_budget_used": int(report.get("maintenance_budget_used", 0) or 0),
            "maintenance_budget_remaining": int(report.get("maintenance_budget_remaining", 0) or 0),
            "scheduler_batch_limit": int(dict(report.get("batch_plan", {}) or {}).get("total_limit", 32) or 32),
            "scheduler_batch_plan": dict(report.get("batch_plan", {}) or {}),
            "batch_fill_rate": float(report.get("batch_fill_rate", 0.0) or 0.0),
            "forced_promotion_count": len(list(report.get("forced_promotions_selected", []) or [])),
            "quota_skip_counts": dict(report.get("quota_skip_counts", {}) or {}),
            "busy_backpressure_active": bool(report.get("busy_backpressure_active", False)),
            "maintenance_backpressure_active": bool(report.get("maintenance_backpressure_active", False)),
            "poll_mode_transition": {
                "previous": previous,
                "current": poll_mode,
                "reason": str(report.get("poll_mode_reason", "") or "fixture_snapshot"),
            },
            "scheduler_policy": policy,
            "kernel_due_selection_summary": self.chat_loop_kernel.describe_status_sync().get("last_due_selection_summary", {}),
            "last_selection_summary": {
                "selected_count": len(list(report.get("selected", []) or [])),
                "dialogue_selected_count": len(list(report.get("dialogue_selected", []) or [])),
                "maintenance_selected_count": len(list(report.get("maintenance_selected", []) or [])),
                "forced_promotion_count": len(list(report.get("forced_promotions_selected", []) or [])),
            },
        }


class _FixtureGateway:
    def __init__(self):
        self._context_economy = {
            "memory_global_summary": {
                "call_count": 22,
                "lane_rotate_count": 4,
                "fallback_count": 0,
                "primary_hit_rate": 0.82,
                "provider_session_usage_rate": 0.91,
                "provider_session_reuse_rate": 0.77,
            },
            "chat_dialog": {
                "call_count": 18,
                "lane_rotate_count": 2,
                "fallback_count": 1,
                "primary_hit_rate": 0.88,
                "provider_session_usage_rate": 0.95,
                "provider_session_reuse_rate": 0.72,
            },
            "_templates": {
                "memory_global_summary@v1": {
                    "workload_families": {"memory_global_summary": 22},
                    "call_count": 22,
                    "lane_rotate_count": 4,
                    "fallback_count": 0,
                    "primary_hit_rate": 0.82,
                    "provider_session_usage_rate": 0.91,
                    "provider_session_reuse_rate": 0.77,
                    "cache_affinity_ready_rate": 0.75,
                    "avg_stable_prefix_length": 212.0,
                    "avg_dynamic_payload_length": 51.0,
                    "actual_models": {"kimi-k2.6": 22},
                    "rotate_reasons": {"template_version_changed": 3, "schema_changed": 1},
                },
                "chat_dialog@v2": {
                    "workload_families": {"chat_dialog": 18},
                    "call_count": 18,
                    "lane_rotate_count": 2,
                    "fallback_count": 1,
                    "primary_hit_rate": 0.88,
                    "provider_session_usage_rate": 0.95,
                    "provider_session_reuse_rate": 0.72,
                    "cache_affinity_ready_rate": 0.81,
                    "avg_stable_prefix_length": 156.0,
                    "avg_dynamic_payload_length": 73.0,
                    "actual_models": {"kimi-k2.6": 17, "fallback-1": 1},
                    "rotate_reasons": {"provider_session_changed": 2},
                },
            },
        }

    def get_context_economy_stats(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._context_economy))


class _FixtureFacade:
    def __init__(self, runtime: Any):
        self.runtime = runtime

    def get_runtime_diagnostics(self) -> dict[str, Any]:
        return {
            "status": {
                "lifecycle_started": True,
                "boot_phase": "running",
                "degraded_components": {},
                "memory_initialized": True,
                "proactive_started": True,
            },
            "models": {
                "chat": {"provider": "kimi-k2.6", "configured": True, "healthy": True},
                "vision": {"provider": "doubao", "configured": True, "healthy": True},
            },
            "capabilities": self.get_capability_overview_sync(),
        }

    def get_capability_overview_sync(self) -> dict[str, Any]:
        return {
            "scheduler": {
                "profiles": ["dialogue_first", "balanced", "maintenance_friendly"],
                "default_profile": "balanced",
            },
            "memory": {"enabled": True},
        }

    async def get_capability_overview(self) -> dict[str, Any]:
        return self.get_capability_overview_sync()


def ensure_fixture_files() -> None:
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    FIXTURE_CONFIG_PATH.write_text(
        json.dumps({"global_settings": {"webui_password": "astrmai_admin"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    import sqlite3

    conn = sqlite3.connect(FIXTURE_DB_PATH)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS UserProfile (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT
            );
            CREATE TABLE IF NOT EXISTS MemoryEvent (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT,
                session_id TEXT
            );
            CREATE TABLE IF NOT EXISTS canonical_memories (
                id TEXT PRIMARY KEY,
                kind TEXT,
                status TEXT,
                metadata TEXT
            );
            DELETE FROM UserProfile;
            DELETE FROM MemoryEvent;
            DELETE FROM canonical_memories;
            INSERT INTO UserProfile(user_id) VALUES ('user-a'), ('user-b');
            INSERT INTO MemoryEvent(event_id, session_id) VALUES ('evt-1', 'chat-wait-1'), ('evt-2', 'chat-maint-1');
            INSERT INTO canonical_memories(id, kind, status, metadata) VALUES
                ('expr-1', 'expression_pattern', 'review_pending', '{"review_status":"pending"}'),
                ('mem-1', 'memory', 'active', '{}');
            """
        )
        conn.commit()
    finally:
        conn.close()


async def build_scheduler_fixture_facade() -> _FixtureFacade:
    ensure_fixture_files()
    snapshots = {
        "chat-wait-1": {"chat_id": "chat-wait-1", "latest_activity_ts": 99.0, "executor_pending": 0, "wait_targets": ["user-1"]},
        "chat-busy-1": {"chat_id": "chat-busy-1", "latest_activity_ts": 98.0, "executor_pending": 1, "wait_targets": []},
        "chat-active-1": {"chat_id": "chat-active-1", "latest_activity_ts": 97.0, "executor_pending": 0, "wait_targets": []},
        "chat-maint-1": {"chat_id": "chat-maint-1", "latest_activity_ts": 91.0, "executor_pending": 0, "wait_targets": []},
        "chat-maint-2": {"chat_id": "chat-maint-2", "latest_activity_ts": 89.0, "executor_pending": 0, "wait_targets": []},
        "chat-idle-1": {"chat_id": "chat-idle-1", "latest_activity_ts": 80.0, "executor_pending": 0, "wait_targets": []},
    }
    coordinator = _FixtureCoordinator(snapshots)
    kernel = ChatLoopKernel(runtime_coordinator=coordinator)
    kernel.set_scheduler_profile_for_testing("balanced")

    seeded_states = [
        {
            "chat_id": "chat-wait-1",
            "phase": "WAITING",
            "next_tick_at": 94.0,
            "missed_due_passes": 1,
            "maintenance_candidates_summary": {},
        },
        {
            "chat_id": "chat-busy-1",
            "phase": "BUSY",
            "next_tick_at": 93.0,
            "consecutive_selected_count": 1,
            "maintenance_candidates_summary": {},
        },
        {
            "chat_id": "chat-active-1",
            "phase": "ACTIVE",
            "next_tick_at": 93.5,
            "maintenance_candidates_summary": {},
        },
        {
            "chat_id": "chat-maint-1",
            "phase": "MAINTENANCE",
            "next_tick_at": 90.0,
            "missed_due_passes": 4,
            "last_maintenance_selected_at": 10.0,
            "maintenance_candidates_summary": {
                "memory": {"candidate_present": True, "reason": "eligible"},
            },
        },
        {
            "chat_id": "chat-maint-2",
            "phase": "MAINTENANCE",
            "next_tick_at": 90.2,
            "missed_due_passes": 3,
            "last_maintenance_selected_at": 8.0,
            "maintenance_candidates_summary": {
                "compaction": {"eligible": True},
                "memory": {"candidate_present": True, "reason": "eligible"},
            },
        },
        {
            "chat_id": "chat-idle-1",
            "phase": "IDLE",
            "next_tick_at": 102.0,
            "maintenance_candidates_summary": {},
        },
    ]

    for item in seeded_states:
        state = await kernel.get_loop_state(item["chat_id"])
        state.phase = item["phase"]
        state.next_tick_at = float(item["next_tick_at"])
        state.missed_due_passes = int(item.get("missed_due_passes", 0))
        state.consecutive_selected_count = int(item.get("consecutive_selected_count", 0))
        state.last_maintenance_selected_at = float(item.get("last_maintenance_selected_at", 0.0))
        state.pending_signals["maintenance_candidates_summary"] = dict(item.get("maintenance_candidates_summary", {}) or {})
        await kernel._state_store.save(state)

    chat_ids = list(snapshots.keys())
    report = await kernel.describe_due_selection(chat_ids, now=100.0, horizon_seconds=2.0, max_batch=8)
    for chat_id in chat_ids:
        state = await kernel.get_loop_state(chat_id)
        breakdown = dict(report.get("score_breakdown", {}).get(chat_id, {}) or {})
        selected = chat_id in list(report.get("selected", []) or [])
        quota_skipped = chat_id in list(report.get("quota_skipped", {}).get("skipped_by_maintenance_quota", []) or [])
        state.pending_signals.update(
            {
                "selected_reason": "selected_by_scheduler_score" if selected else "",
                "not_selected_reason": "" if selected else ("skipped_by_quota" if quota_skipped else "not_due_or_lower_rank"),
                "quota_skip_reason": "maintenance_quota" if quota_skipped else "",
                "starvation_tier": breakdown.get("starvation_tier", ""),
                "forced_promotion_eligible": bool(breakdown.get("forced_promotion_eligible", False)),
                "missed_due_passes": int(state.missed_due_passes),
                "scheduler_score": float(breakdown.get("scheduler_score", 0.0) or 0.0),
                "due_rank": int(breakdown.get("due_rank", 0) or 0),
                "batch_plan": dict(report.get("batch_plan", {}) or {}),
                "batch_fill_rate": float(report.get("batch_fill_rate", 0.0) or 0.0),
                "poll_mode": str(report.get("poll_mode", "") or ""),
                "maintenance_budget_state": {
                    "total": int(report.get("maintenance_budget_total", 0) or 0),
                    "used": int(report.get("maintenance_budget_used", 0) or 0),
                    "remaining": int(report.get("maintenance_budget_remaining", 0) or 0),
                },
            }
        )
        await kernel._state_store.save(state)

    planner = SimpleNamespace(
        cognitive_decision_history=[
            {
                "chat_id": "chat-wait-1",
                "decision": "reply_now",
                "reason": "wait_expired",
                "timestamp": 100.0,
            },
            {
                "chat_id": "chat-maint-1",
                "decision": "maintenance",
                "reason": "memory_candidate_ready",
                "timestamp": 99.0,
            },
        ]
    )
    runtime = SimpleNamespace(
        chat_loop_kernel=kernel,
        proactive_task=_FixtureProactiveTask(kernel),
        system2_planner=planner,
        gateway=_FixtureGateway(),
        runtime_coordinator=coordinator,
        config=SimpleNamespace(global_settings=SimpleNamespace(webui_password="astrmai_admin")),
    )
    return _FixtureFacade(runtime)


def build_scheduler_fixture_facade_sync() -> _FixtureFacade:
    return asyncio.run(build_scheduler_fixture_facade())
