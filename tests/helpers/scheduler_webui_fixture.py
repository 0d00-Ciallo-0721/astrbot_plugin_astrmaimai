from __future__ import annotations

import asyncio
import copy
import json
import os
import sqlite3
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
FIXTURE_PERSONA_CACHE_PATH = FIXTURE_ROOT / "persona_cache.json"
FIXTURE_DIRECT_OPEN_HARNESS_PATH = FIXTURE_ROOT / "direct_open_plugin_page.html"
FIXTURE_ACCEPTANCE_BASELINE_DIR = (
    Path(__file__).resolve().parents[2]
    / "artifacts"
    / "pages_acceptance"
    / "20260519T221500Z-astrbot-plugin-host-check"
)

DEFAULT_FIXTURE_PROFILE = "admin_full"
DEFAULT_SCHEDULER_PROFILE = "balanced"
SEED_PROFILE_NAMES = ("scheduler_only", "admin_full", "acceptance_host")
SCHEDULER_PROFILE_NAMES = ("dialogue_first", "balanced", "maintenance_friendly")
FIXTURE_PERSONA_ID = "fixture-persona"
FIXTURE_SERVER_BASE_URL = "http://127.0.0.1:8765"
FIXTURE_STATIC_BASE_URL = "http://127.0.0.1:8766"


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
                "persona_summary@v1": {
                    "workload_families": {"persona_summary": 9},
                    "call_count": 9,
                    "lane_rotate_count": 1,
                    "fallback_count": 0,
                    "primary_hit_rate": 0.94,
                    "provider_session_usage_rate": 0.88,
                    "provider_session_reuse_rate": 0.84,
                    "cache_affinity_ready_rate": 0.86,
                    "avg_stable_prefix_length": 244.0,
                    "avg_dynamic_payload_length": 62.0,
                    "actual_models": {"kimi-k2.6": 9},
                    "rotate_reasons": {"persona_changed": 1},
                },
            },
        }

    def get_context_economy_stats(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._context_economy))


class _FixtureFacade:
    def __init__(self, runtime: Any, review_items: list[dict[str, Any]], seed_summary: dict[str, Any]):
        self.runtime = runtime
        self._review_items = [dict(item) for item in review_items]
        self.seed_summary = dict(seed_summary)

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
                "profiles": list(SCHEDULER_PROFILE_NAMES),
                "default_profile": DEFAULT_SCHEDULER_PROFILE,
                "fixture_profile": self.seed_summary.get("profile", DEFAULT_FIXTURE_PROFILE),
            },
            "memory": {"enabled": True},
            "persona": {"cache_ready": True},
        }

    async def get_capability_overview(self) -> dict[str, Any]:
        return self.get_capability_overview_sync()

    async def list_pending_expression_reviews(self, group_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        items = [
            item
            for item in self._review_items
            if str(item.get("review_status") or item.get("status") or "").strip().lower() in {"pending", "review_pending"}
        ]
        if group_id:
            items = [item for item in items if str(item.get("group_id", "")) == str(group_id)]
        return [dict(item) for item in items[: max(1, limit)]]

    async def list_recent_expression_reviews(self, group_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        items = list(self._review_items)
        if group_id:
            items = [item for item in items if str(item.get("group_id", "")) == str(group_id)]
        return [dict(item) for item in items[: max(1, limit)]]

    async def get_expression_review_detail(self, pattern_id: str) -> dict[str, Any] | None:
        for item in self._review_items:
            if str(item.get("id", "")) == str(pattern_id):
                return dict(item)
        return None

    async def submit_expression_review(self, **kwargs: Any) -> dict[str, Any]:
        pattern_id = str(kwargs.get("pattern_id", ""))
        decision = str(kwargs.get("decision", "")).strip().lower()
        mapped = "approved" if decision in {"approved", "approve"} else ("rejected" if decision else "pending")
        for item in self._review_items:
            if str(item.get("id", "")) != pattern_id:
                continue
            item["review_status"] = mapped
            item["status"] = "active" if mapped == "approved" else mapped
            item["checked"] = mapped == "approved"
            item["rejected"] = mapped == "rejected"
            item["review_reason"] = str(kwargs.get("reason") or item.get("review_reason") or "")
            replacement = str(kwargs.get("replacement_expression") or "").strip()
            if replacement:
                item["expression"] = replacement
            return dict(item)
        return {"status": "missing", "id": pattern_id}


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _scheduler_policy_profiles() -> list[str]:
    return list(SCHEDULER_PROFILE_NAMES)


def _validate_profile_name(profile: str) -> str:
    selected = str(profile or DEFAULT_FIXTURE_PROFILE).strip()
    if selected not in SEED_PROFILE_NAMES:
        raise ValueError(f"Unknown fixture profile: {selected}")
    return selected


def _scheduler_chat_snapshots(profile: str) -> dict[str, dict[str, Any]]:
    base = {
        "chat-wait-1": {"chat_id": "chat-wait-1", "latest_activity_ts": 99.0, "executor_pending": 0, "wait_targets": ["user-1"]},
        "chat-busy-1": {"chat_id": "chat-busy-1", "latest_activity_ts": 98.0, "executor_pending": 1, "wait_targets": []},
        "chat-active-1": {"chat_id": "chat-active-1", "latest_activity_ts": 97.0, "executor_pending": 0, "wait_targets": []},
        "chat-maint-1": {"chat_id": "chat-maint-1", "latest_activity_ts": 91.0, "executor_pending": 0, "wait_targets": []},
        "chat-maint-2": {"chat_id": "chat-maint-2", "latest_activity_ts": 89.0, "executor_pending": 0, "wait_targets": []},
        "chat-idle-1": {"chat_id": "chat-idle-1", "latest_activity_ts": 80.0, "executor_pending": 0, "wait_targets": []},
    }
    if profile == "acceptance_host":
        base["chat-host-focus"] = {
            "chat_id": "chat-host-focus",
            "latest_activity_ts": 100.0,
            "executor_pending": 0,
            "wait_targets": [],
        }
    return base


def _scheduler_seed_states(profile: str) -> list[dict[str, Any]]:
    states = [
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
    if profile == "acceptance_host":
        states.append(
            {
                "chat_id": "chat-host-focus",
                "phase": "ACTIVE",
                "next_tick_at": 95.0,
                "missed_due_passes": 0,
                "maintenance_candidates_summary": {},
            }
        )
    return states


def _review_fixture_items() -> list[dict[str, Any]]:
    return [
        {
            "id": "expr-1",
            "group_id": "group-fixture",
            "situation": "群聊闲聊",
            "expression": "稳住节奏先看一眼。",
            "style": "supportive",
            "count": 3,
            "checked": False,
            "rejected": False,
            "review_status": "pending",
            "review_reason": "需要确认是否过于口语化",
            "review_suggestion": "增加更正式的备选表达",
            "shared_scope": "group",
            "think_level": 1,
            "weight": 1.0,
            "modified_by": "",
            "source": "learning_expression_pattern",
            "content_list": _json_dump(["稳住节奏先看一眼。"]),
            "last_review_time": 0.0,
            "last_active_time": 1716100000.0,
            "create_time": 1716000000.0,
            "status": "review_pending",
            "legacy": False,
        },
        {
            "id": "expr-2",
            "group_id": "group-fixture",
            "situation": "私聊安抚",
            "expression": "我们先把问题拆小一点。",
            "style": "structured",
            "count": 8,
            "checked": True,
            "rejected": False,
            "review_status": "approved",
            "review_reason": "",
            "review_suggestion": "",
            "shared_scope": "global",
            "think_level": 2,
            "weight": 1.2,
            "modified_by": "reviewer-a",
            "source": "learning_expression_pattern",
            "content_list": _json_dump(["我们先把问题拆小一点。"]),
            "last_review_time": 1716200000.0,
            "last_active_time": 1716200100.0,
            "create_time": 1716000300.0,
            "status": "active",
            "legacy": False,
        },
    ]


def _planner_history(profile: str) -> SimpleNamespace:
    turn_trace_history = [
        {
            "chat_id": "chat-wait-1",
            "turn_id": "turn-100",
            "think_level": "medium",
            "think_reason": "wait_target_ready",
            "follow_up": {"required": True, "reason": "wait_expired"},
        },
        {
            "chat_id": "chat-maint-1",
            "turn_id": "turn-099",
            "think_level": "low",
            "think_reason": "maintenance_window",
            "follow_up": {"required": False, "reason": ""},
        },
    ]
    tool_trace_history = [
        {
            "chat_id": "chat-wait-1",
            "tool_name": "memory_lookup",
            "family": "memory",
            "status": "ok",
            "latency_ms": 42,
        },
        {
            "chat_id": "chat-active-1",
            "tool_name": "calendar_lookup",
            "family": "readonly",
            "status": "ok",
            "latency_ms": 31,
        },
    ]
    if profile == "acceptance_host":
        turn_trace_history.insert(
            0,
            {
                "chat_id": "chat-host-focus",
                "turn_id": "turn-host-001",
                "think_level": "high",
                "think_reason": "host_acceptance_seed",
                "follow_up": {"required": False, "reason": ""},
            },
        )
    return SimpleNamespace(
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
        ],
        turn_trace_history=turn_trace_history,
        tool_trace_history=tool_trace_history,
    )


def _config_payload(profile: str) -> dict[str, Any]:
    return {
        "global_settings": {
            "webui_password": "astrmai_admin",
            "fixture_profile": profile,
        },
        "persona": {
            "persona_id": FIXTURE_PERSONA_ID,
        },
    }


def _persona_cache_payload(profile: str) -> dict[str, Any]:
    return {
        FIXTURE_PERSONA_ID: {
            "summary": "这是一个用于 AstrMai 管理台页面验收的角色摘要，覆盖 scheduler、记忆、审核与画像演示场景。",
            "first_person_rewrite": "我是一个偏冷静、结构化、愿意先拆解问题再陪你推进的人。",
            "style": "克制、支持式、偏工程搭档口吻。",
            "raw": f"fixture profile: {profile}",
            "shards": {
                "logic_style": "先分层看问题，再进入最小闭环。",
                "speech_style": "语气平和，避免无意义夸张。",
                "world_view": "系统感和长期可维护性优先。",
                "timeline": "长期维护 AstrMai/AstrBot 相关管理能力。",
                "relations": "偏向协作型伙伴关系。",
                "skills": "调度、记忆、页面诊断、验收。",
                "values": "真实验证、最小改动、清晰解释。",
                "secrets": "对 iframe 验收边界格外敏感。",
            },
            "is_full_ready": True,
            "timestamp": 1716110000.0,
        },
        "global": {
            "summary": "全局默认 persona cache。",
            "first_person_rewrite": "我是全局回退人格。",
            "style": "neutral",
            "raw": "global persona cache",
            "shards": {},
            "is_full_ready": False,
            "timestamp": 1716109000.0,
        },
    }


def _canonical_seed_rows(profile: str) -> list[tuple[Any, ...]]:
    rows = [
        (
            "expr-1",
            "group-fixture",
            "",
            "learning_expression_pattern",
            "expression_pattern",
            "稳住节奏先看一眼。",
            "稳住节奏先看一眼。",
            _json_dump(["supportive", "pending"]),
            0.72,
            0.81,
            "review_pending",
            1.0,
            1716000000.0,
            1716100000.0,
            1716100500.0,
            3,
            "",
            "",
            _json_dump(
                {
                    "review_status": "pending",
                    "review_reason": "需要确认是否过于口语化",
                    "review_suggestion": "增加更正式的备选表达",
                    "situation": "群聊闲聊",
                    "style": "supportive",
                    "count": 3,
                    "shared_scope": "group",
                    "think_level": 1,
                }
            ),
            "expr:fixture:1",
            "fixture:expr:1",
            "maintenance_only",
        ),
        (
            "expr-2",
            "group-fixture",
            "",
            "learning_expression_pattern",
            "expression_pattern",
            "我们先把问题拆小一点。",
            "我们先把问题拆小一点。",
            _json_dump(["approved"]),
            0.81,
            0.92,
            "active",
            1.0,
            1716000300.0,
            1716200000.0,
            1716200100.0,
            8,
            "",
            "",
            _json_dump(
                {
                    "review_status": "approved",
                    "situation": "私聊安抚",
                    "style": "structured",
                    "count": 8,
                    "shared_scope": "global",
                    "think_level": 2,
                }
            ),
            "expr:fixture:2",
            "fixture:expr:2",
            "auto_and_tool",
        ),
        (
            "mem-1",
            "chat-wait-1",
            FIXTURE_PERSONA_ID,
            "summary",
            "memory",
            "用户偏好在做风险较高操作前先看证据。",
            "用户偏好在做风险较高操作前先看证据。",
            _json_dump(["user_pref", "verification"]),
            0.88,
            0.93,
            "active",
            1.0,
            1716000400.0,
            1716200200.0,
            1716200400.0,
            5,
            "",
            "",
            _json_dump({"source_layer": "summary"}),
            "mem:fixture:1",
            "fixture:mem:1",
            "auto_and_tool",
        ),
        (
            "jargon-1",
            "group-fixture",
            "",
            "learning_jargon",
            "jargon",
            "大蓝鸟",
            "群里对某个常驻机器人目标的黑话称呼",
            _json_dump(["pending_jargon"]),
            0.63,
            0.74,
            "review_pending",
            1.0,
            1716000500.0,
            1716200500.0,
            1716200600.0,
            1,
            "",
            "",
            _json_dump(
                {
                    "meaning": "群里对某个常驻机器人目标的黑话称呼",
                    "scene": "raid call",
                    "examples": ["大蓝鸟来了"],
                    "review_status": "review_pending",
                    "review_reason": "需要补充分群上下文",
                    "review_suggestion": "确认是否仅限特定群",
                    "legacy_jargon_id": 12,
                }
            ),
            "jargon:fixture:1",
            "fixture:jargon:1",
            "maintenance_only",
        ),
    ]
    if profile != "scheduler_only":
        rows.extend(
            [
                (
                    "mem-2",
                    "chat-active-1",
                    FIXTURE_PERSONA_ID,
                    "summary",
                    "memory",
                    "用户习惯在前端改动后做真实页面验收。",
                    "用户习惯在前端改动后做真实页面验收。",
                    _json_dump(["frontend", "acceptance"]),
                    0.77,
                    0.89,
                    "active",
                    1.0,
                    1716000600.0,
                    1716200700.0,
                    1716200710.0,
                    3,
                    "",
                    "",
                    _json_dump({"source_layer": "summary"}),
                    "mem:fixture:2",
                    "fixture:mem:2",
                    "auto_and_tool",
                ),
                (
                    "jargon-2",
                    "group-fixture",
                    "",
                    "learning_jargon",
                    "jargon",
                    "收口",
                    "指把一轮修复做成可验证闭环。",
                    _json_dump(["active_jargon"]),
                    0.7,
                    0.82,
                    "active",
                    1.0,
                    1716000800.0,
                    1716200800.0,
                    1716200900.0,
                    2,
                    "",
                    "",
                    _json_dump(
                        {
                            "meaning": "指把一轮修复做成可验证闭环。",
                            "scene": "开发协作",
                            "examples": ["这一轮先把 P8 收口。"],
                            "review_status": "approved",
                            "legacy_jargon_id": 15,
                        }
                    ),
                    "jargon:fixture:2",
                    "fixture:jargon:2",
                    "auto_and_tool",
                ),
            ]
        )
    return rows


def _sqlite_seed_rows(profile: str) -> dict[str, list[tuple[Any, ...]]]:
    profile = _validate_profile_name(profile)
    user_profiles = [
        ("user-a", "Alice", "证据派", "喜欢先看验证", 0.82, "工程协作者", _json_dump(["qa", "frontend"]), "会主动要求真实验收", _json_dump(["先看页面", "再看日志"]), _json_dump(["偏冷静"]), _json_dump(["关注实际可见结果"]), _json_dump(["偏合作"]), _json_dump(["描述具体阻塞"]), _json_dump({"group-fixture": {"last_seen": 1716200000.0}}), _json_dump({"manual_locked_fields": ["nickname"]})),
        ("user-b", "Bob", "调度控", "喜欢对比 profile", 0.74, "调参协作者", _json_dump(["scheduler", "benchmark"]), "偏爱参数矩阵和压测", _json_dump(["会对比 profile"]), _json_dump(["喜欢表格化"]), _json_dump(["关注 maintenance"]), _json_dump(["愿意做压测"]), _json_dump(["说话直接"]), _json_dump({"group-fixture": {"last_seen": 1716200500.0}}), _json_dump({"manual_locked_fields": []})),
    ]
    if profile != "scheduler_only":
        user_profiles.append(
            ("user-c", "Carol", "画像流", "常看 persona slices", 0.66, "画像协作者", _json_dump(["persona", "memory"]), "偏好 persona 切片", _json_dump(["会追问 persona cache"]), _json_dump(["重视风格一致性"]), _json_dump(["喜欢看 summary"]), _json_dump(["偏观察"]), _json_dump(["表达温和"]), _json_dump({"group-fixture": {"last_seen": 1716200600.0}}), _json_dump({"manual_locked_fields": ["memory_points"]}))
        )
    memory_events = [
        ("evt-1", "chat-wait-1", "2099-01-09", "用户要求先跑真实页面验收。", "focused", 0.91, 0.44, "后续要配合截图留档。", "event", "summary", _json_dump(["acceptance", "frontend"]), 1716200001.0),
        ("evt-2", "chat-maint-1", "2099-01-09", "调度面板需要补空 state 观察。", "curious", 0.73, 0.31, "属于 scheduler diagnostics。", "event", "summary", _json_dump(["scheduler", "ui"]), 1716200002.0),
    ]
    if profile != "scheduler_only":
        memory_events.append(
            ("evt-3", "chat-active-1", "2099-01-09", "用户画像页需要展示长期记忆点。", "steady", 0.69, 0.28, "适合 admin_full 验收。", "event", "summary", _json_dump(["users", "persona"]), 1716200003.0)
        )
    daily_reflections = [
        ("2099-01-08", "昨天的 scheduler 迭代已经把 quota 和 starvation 主链闭合。", 1716111000.0),
    ]
    if profile != "scheduler_only":
        daily_reflections.append(("2099-01-09", "今天补全了全管理台 seed fixture 和页面验收基线。", 1716201000.0))
    memory_nodes = [
        ("Scheduler Diagnostics", "topic", "围绕 due selection、poll mode、batch plan 的管理台诊断节点。", 1716202000.0),
    ]
    if profile != "scheduler_only":
        memory_nodes.append(("Persona Cache", "topic", "用于 persona slices 面板验收的缓存节点。", 1716202100.0))
    user_profile_count_rows = [("user-a",), ("user-b",)]
    if profile != "scheduler_only":
        user_profile_count_rows.append(("user-c",))
    return {
        "UserProfile": user_profile_count_rows,
        "user_profiles": user_profiles,
        "MemoryEvent": memory_events,
        "canonical_memories": _canonical_seed_rows(profile),
        "DailyReflection": daily_reflections,
        "MemoryNode": memory_nodes,
    }


def _table_count(conn: sqlite3.Connection, table_name: str) -> int:
    cursor = conn.execute(f"SELECT COUNT(*) FROM {table_name}")
    row = cursor.fetchone()
    return int((row or [0])[0] or 0)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,))
    return cursor.fetchone() is not None


def _write_direct_open_harness(profile: str) -> str:
    style_uri = f"{FIXTURE_STATIC_BASE_URL}/pages/admin/style.css"
    app_uri = f"{FIXTURE_STATIC_BASE_URL}/pages/admin/app.js"
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AstrMai Direct Fixture Harness</title>
  <link rel="stylesheet" href="{style_uri}">
</head>
<body>
  <div id="app" class="admin-shell">
    <header class="topbar">
      <div>
        <p class="eyebrow">Fixture Bridge Stub</p>
        <h1>AstrMai 管理台直开验收页</h1>
      </div>
      <div class="topbar-actions">
        <span id="bridge-status" class="status-pill">Bridge 初始化中</span>
        <button id="refresh-button" class="ghost-button" type="button">刷新当前页</button>
      </div>
    </header>

    <nav id="tabs" class="tabs" aria-label="AstrMai 管理导航"></nav>

    <main id="content" class="content">
      <section class="empty-state">
        <h2>正在连接 Fixture Bridge</h2>
        <p>该页面通过开发态 bridge stub 直连 fixture server，用于绕开 AstrBot 宿主页 iframe 边界。</p>
      </section>
    </main>

    <div id="toast" class="toast" role="status" aria-live="polite"></div>
    <div id="modal-root" class="modal-root" hidden></div>
  </div>

  <script>
  (function () {{
    const fixtureBase = "{FIXTURE_SERVER_BASE_URL}";
    const activeProfile = "{profile}";
    let authTokenPromise = null;
    function normalizeEndpoint(endpoint) {{
      const clean = String(endpoint || "").replace(/^\\/+/, "");
      return clean.startsWith("admin/") ? clean.slice("admin/".length) : clean;
    }}
    async function authToken() {{
      if (!authTokenPromise) {{
        authTokenPromise = fetch(new URL("/api/auth/login", fixtureBase).toString(), {{
          method: "POST",
          headers: {{"content-type": "application/json"}},
          body: JSON.stringify({{ password: "astrmai_admin" }}),
        }})
          .then((response) => response.json())
          .then((payload) => payload.token || "");
      }}
      return authTokenPromise;
    }}
    async function request(method, endpoint, payload) {{
      const mapped = normalizeEndpoint(endpoint);
      const url = new URL(`/api/${{mapped}}`, fixtureBase);
      if (method === "GET" && payload && typeof payload === "object") {{
        Object.entries(payload).forEach(([key, value]) => {{
          if (value === undefined || value === null || value === "") return;
          url.searchParams.set(key, String(value));
        }});
      }}
      const token = await authToken();
      const options = {{
        method,
        headers: {{
          "content-type": "application/json",
          "Authorization": token ? `Bearer ${{token}}` : "",
        }},
      }};
      if (method !== "GET") {{
        options.body = JSON.stringify(payload || {{}});
      }}
      const response = await fetch(url.toString(), options);
      return response.json();
    }}
    window.AstrBotPluginPage = {{
      ready: async () => ({{ status: "ok", mode: "fixture_stub", profile: activeProfile }}),
      apiGet: async (endpoint, params) => request("GET", endpoint, params || {{}}),
      apiPost: async (endpoint, body) => request("POST", endpoint, body || {{}}),
    }};
  }})();
  </script>
  <script defer src="{app_uri}"></script>
</body>
</html>
"""
    FIXTURE_DIRECT_OPEN_HARNESS_PATH.write_text(html, encoding="utf-8")
    return str(FIXTURE_DIRECT_OPEN_HARNESS_PATH)


def _write_fixture_config(profile: str) -> None:
    FIXTURE_CONFIG_PATH.write_text(json.dumps(_config_payload(profile), ensure_ascii=False, indent=2), encoding="utf-8")
    FIXTURE_PERSONA_CACHE_PATH.write_text(
        json.dumps(_persona_cache_payload(profile), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS UserProfile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT
        );
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id TEXT PRIMARY KEY,
            name TEXT,
            nickname TEXT,
            nickname_reason TEXT,
            social_score REAL,
            identity TEXT,
            tags TEXT,
            persona_analysis TEXT,
            memory_points TEXT,
            identity_points TEXT,
            preference_points TEXT,
            relationship_points TEXT,
            speech_style_points TEXT,
            group_footprints TEXT,
            profile_metadata TEXT
        );
        CREATE TABLE IF NOT EXISTS MemoryEvent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT,
            session_id TEXT,
            date TEXT,
            narrative TEXT,
            emotion TEXT,
            importance REAL,
            emotional_intensity REAL,
            reflection TEXT,
            memory_kind TEXT,
            source_layer TEXT,
            tags TEXT,
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS canonical_memories (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            persona_id TEXT,
            source TEXT,
            kind TEXT,
            content TEXT,
            summary TEXT,
            tags TEXT,
            importance REAL,
            confidence REAL,
            status TEXT,
            decay_score REAL,
            create_time REAL,
            update_time REAL,
            last_access_time REAL,
            access_count INTEGER,
            superseded_by TEXT,
            deleted_reason TEXT,
            metadata TEXT,
            dedup_key TEXT,
            source_ref TEXT,
            visibility TEXT
        );
        CREATE TABLE IF NOT EXISTS DailyReflection (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE,
            reflection TEXT,
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS MemoryNode (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            type TEXT,
            description TEXT,
            last_updated REAL
        );
        """
    )
    conn.commit()


def _seed_sqlite(conn: sqlite3.Connection, profile: str) -> dict[str, int]:
    rows = _sqlite_seed_rows(profile)
    for table_name in ("UserProfile", "user_profiles", "MemoryEvent", "canonical_memories", "DailyReflection", "MemoryNode"):
        if _table_exists(conn, table_name):
            conn.execute(f"DELETE FROM {table_name}")
    conn.executemany("INSERT INTO UserProfile(user_id) VALUES (?)", rows["UserProfile"])
    conn.executemany(
        """
        INSERT INTO user_profiles(
            user_id, name, nickname, nickname_reason, social_score, identity, tags, persona_analysis,
            memory_points, identity_points, preference_points, relationship_points, speech_style_points,
            group_footprints, profile_metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows["user_profiles"],
    )
    conn.executemany(
        """
        INSERT INTO MemoryEvent(
            event_id, session_id, date, narrative, emotion, importance, emotional_intensity, reflection,
            memory_kind, source_layer, tags, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows["MemoryEvent"],
    )
    conn.executemany(
        """
        INSERT INTO canonical_memories(
            id, session_id, persona_id, source, kind, content, summary, tags, importance, confidence, status,
            decay_score, create_time, update_time, last_access_time, access_count, superseded_by, deleted_reason,
            metadata, dedup_key, source_ref, visibility
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows["canonical_memories"],
    )
    conn.executemany(
        "INSERT INTO DailyReflection(date, reflection, created_at) VALUES (?, ?, ?)",
        rows["DailyReflection"],
    )
    conn.executemany(
        "INSERT INTO MemoryNode(name, type, description, last_updated) VALUES (?, ?, ?, ?)",
        rows["MemoryNode"],
    )
    conn.commit()
    return {
        "UserProfile": _table_count(conn, "UserProfile"),
        "user_profiles": _table_count(conn, "user_profiles"),
        "MemoryEvent": _table_count(conn, "MemoryEvent"),
        "canonical_memories": _table_count(conn, "canonical_memories"),
        "DailyReflection": _table_count(conn, "DailyReflection"),
        "MemoryNode": _table_count(conn, "MemoryNode"),
    }


def ensure_fixture_files(profile: str = DEFAULT_FIXTURE_PROFILE) -> dict[str, Any]:
    selected_profile = _validate_profile_name(profile)
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    _write_fixture_config(selected_profile)
    harness_path = _write_direct_open_harness(selected_profile)
    conn = sqlite3.connect(FIXTURE_DB_PATH)
    try:
        _create_schema(conn)
        table_counts = _seed_sqlite(conn, selected_profile)
    finally:
        conn.close()
    summary = {
        "db_path": str(FIXTURE_DB_PATH),
        "config_path": str(FIXTURE_CONFIG_PATH),
        "persona_cache_path": str(FIXTURE_PERSONA_CACHE_PATH),
        "direct_open_harness_path": harness_path,
        "profile": selected_profile,
        "table_counts": table_counts,
        "runtime_snapshots": len(_scheduler_chat_snapshots(selected_profile)),
        "scheduler_selected_count": 0,
        "pending_review_count": 1,
        "memory_event_count": table_counts["MemoryEvent"],
        "user_count": table_counts["user_profiles"],
        "acceptance_baseline_dir": str(FIXTURE_ACCEPTANCE_BASELINE_DIR),
    }
    return summary


def _seed_summary(profile: str, report: dict[str, Any], file_summary: dict[str, Any]) -> dict[str, Any]:
    summary = dict(file_summary)
    summary.update(
        {
            "profile": profile,
            "scheduler_selected_count": len(list(report.get("selected", []) or [])),
            "pending_review_count": 1,
            "memory_event_count": int(file_summary.get("table_counts", {}).get("MemoryEvent", 0) or 0),
            "user_count": int(file_summary.get("table_counts", {}).get("user_profiles", 0) or 0),
        }
    )
    return summary


async def build_scheduler_fixture_facade(
    profile: str = DEFAULT_FIXTURE_PROFILE,
    scheduler_profile: str = DEFAULT_SCHEDULER_PROFILE,
) -> _FixtureFacade:
    selected_profile = _validate_profile_name(profile)
    file_summary = ensure_fixture_files(selected_profile)
    snapshots = _scheduler_chat_snapshots(selected_profile)
    coordinator = _FixtureCoordinator(snapshots)
    kernel = ChatLoopKernel(runtime_coordinator=coordinator)
    kernel.set_scheduler_profile_for_testing(scheduler_profile)

    for item in _scheduler_seed_states(selected_profile):
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

    planner = _planner_history(selected_profile)
    runtime = SimpleNamespace(
        chat_loop_kernel=kernel,
        proactive_task=_FixtureProactiveTask(kernel),
        system2_planner=planner,
        gateway=_FixtureGateway(),
        runtime_coordinator=coordinator,
        config=SimpleNamespace(
            global_settings=SimpleNamespace(webui_password="astrmai_admin"),
            persona=SimpleNamespace(persona_id=FIXTURE_PERSONA_ID),
        ),
        persona_summarizer=SimpleNamespace(pending_tasks={}),
        memory_engine=SimpleNamespace(__self_lore__={"persona_id": FIXTURE_PERSONA_ID}),
    )
    seed_summary = _seed_summary(selected_profile, report, file_summary)
    return _FixtureFacade(runtime, _review_fixture_items(), seed_summary)


async def build_fixture_facade(
    profile: str = DEFAULT_FIXTURE_PROFILE,
    scheduler_profile: str = DEFAULT_SCHEDULER_PROFILE,
) -> _FixtureFacade:
    return await build_scheduler_fixture_facade(profile=profile, scheduler_profile=scheduler_profile)


def build_scheduler_fixture_facade_sync(
    profile: str = DEFAULT_FIXTURE_PROFILE,
    scheduler_profile: str = DEFAULT_SCHEDULER_PROFILE,
) -> _FixtureFacade:
    return asyncio.run(build_scheduler_fixture_facade(profile=profile, scheduler_profile=scheduler_profile))


def build_fixture_facade_sync(
    profile: str = DEFAULT_FIXTURE_PROFILE,
    scheduler_profile: str = DEFAULT_SCHEDULER_PROFILE,
) -> _FixtureFacade:
    return asyncio.run(build_fixture_facade(profile=profile, scheduler_profile=scheduler_profile))


__all__ = [
    "DEFAULT_FIXTURE_PROFILE",
    "DEFAULT_SCHEDULER_PROFILE",
    "FIXTURE_ACCEPTANCE_BASELINE_DIR",
    "FIXTURE_CONFIG_PATH",
    "FIXTURE_DB_PATH",
    "FIXTURE_DIRECT_OPEN_HARNESS_PATH",
    "FIXTURE_PERSONA_CACHE_PATH",
    "FIXTURE_ROOT",
    "SEED_PROFILE_NAMES",
    "SCHEDULER_PROFILE_NAMES",
    "build_fixture_facade",
    "build_fixture_facade_sync",
    "build_scheduler_fixture_facade",
    "build_scheduler_fixture_facade_sync",
    "ensure_fixture_files",
]
