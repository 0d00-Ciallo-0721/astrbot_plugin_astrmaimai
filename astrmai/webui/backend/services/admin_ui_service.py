from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Callable

from ....infrastructure.runtime.observability import RuntimeObservabilityHub
from ....shared.helpers.plugin_helpers import safe_create_task
from ..adapters.plugin_api import PluginApiAdapter


class AdminUiService:
    SCHEDULER_SIGNAL_KEYS = {
        "phase",
        "next_tick_at",
        "next_tick_delay",
        "schedule_reason",
        "scheduler_bucket",
        "scheduler_score",
        "starvation_age",
        "fairness_penalty",
        "maintenance_budget_state",
        "selected_reason",
        "not_selected_reason",
        "poll_mode",
        "due_rank",
        "quota_bucket",
        "quota_skip_reason",
        "batch_plan",
        "batch_fill_rate",
        "batch_pressure",
        "pressure_components",
        "forced_promotion_eligible",
        "starvation_tier",
        "selection_cooldown_bias",
        "missed_due_passes",
    }

    def __init__(self, plugin_api: PluginApiAdapter, db_factory: Callable | None = None):
        self.plugin_api = plugin_api
        self.db_factory = db_factory
        from .observabilityservice import ObservabilityService
        from .heartflowservice import HeartflowService
        from .schedulerservice import SchedulerService
        from .cognitionservice import CognitionService
        from .contexteconomyuiservice import ContextEconomyUiService
        from .learningservice import LearningService
        from .runtimeuiservice import RuntimeUiService
        from .toolsservice import ToolsService
        from .runtime_memory_stats import canonical_kind_review_stats, canonical_memory_stats
        self._observability = ObservabilityService(plugin_api)
        self._heartflow = HeartflowService(plugin_api)
        self._scheduler = SchedulerService(plugin_api)
        self._cognition = CognitionService(plugin_api)
        self._learning = LearningService(plugin_api)
        self._runtime = RuntimeUiService(plugin_api)
        self._context_economy_ui = ContextEconomyUiService(plugin_api)
        self._tools = ToolsService(plugin_api)
        self._canonical_kind_review_stats = canonical_kind_review_stats
        self._canonical_memory_stats = canonical_memory_stats

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return dict(value)
        if is_dataclass(value):
            return asdict(value)
        if hasattr(value, "__dict__"):
            return dict(getattr(value, "__dict__", {}) or {})
        return {}

    @classmethod
    def _scheduler_pending_signal_slice(cls, pending_signals: dict[str, Any] | None) -> dict[str, Any]:
        payload = dict(pending_signals or {})
        return {
            key: value
            for key, value in payload.items()
            if key in cls.SCHEDULER_SIGNAL_KEYS
        }

    _ALLOWED_TABLES = {"MemoryEvent", "canonical_memories"}

    async def _safe_count(self, table: str, where: str = "", params: tuple = ()) -> int:
        if table not in self._ALLOWED_TABLES:
            raise ValueError(f"Table {table!r} is not in the allowed whitelist")
        if not self.db_factory:
            return 0
        from .dashboard_repository import DashboardRepository
        if where:
            # ponytail: M16 — respect table param even when where clause is present
            if table == "MemoryEvent":
                try:
                    async with self.db_factory() as db:
                        async with db.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params) as cursor:
                            row = await cursor.fetchone()
                            return int(row[0] if row else 0)
                except Exception:
                    return 0
            from ..repositories import CanonicalMemoryRepository
            return await CanonicalMemoryRepository(self.db_factory).count(where=where, params=params)
        try:
            return await DashboardRepository(self.db_factory).count_table(table)
        except sqlite3.OperationalError:
            return 0

    async def _expression_pattern_stats(self) -> dict[str, int]:
        from .dashboard_repository import DashboardRepository
        if not self.db_factory:
            return await self._canonical_kind_review_stats(self.plugin_api, kind="expression_pattern")
        repo = DashboardRepository(self.db_factory)
        return await self._canonical_kind_review_stats(
            self.plugin_api,
            kind="expression_pattern",
            legacy_expression_repo=repo,
        )

    async def _jargon_stats(self) -> dict[str, int]:
        return await self._canonical_kind_review_stats(self.plugin_api, kind="jargon")

    async def _canonical_memory_stats_snapshot(self) -> dict[str, Any]:
        from .dashboard_repository import DashboardRepository
        repo = DashboardRepository(self.db_factory) if self.db_factory else None
        return await self._canonical_memory_stats(self.plugin_api, repo)

    async def runtime_status(self) -> dict[str, Any]:
        return await self._runtime.runtime_status()

    @staticmethod
    def _parse_filter_values(value: Any) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, (list, tuple, set)):
            raw_items = value
        else:
            raw_items = str(value or "").split(",")
        return {str(item).strip().lower() for item in raw_items if str(item).strip()}

    async def memory_observability_overview(self) -> dict[str, Any]:
        engine = self.plugin_api.get_memory_engine()
        observer = self.plugin_api.get_memory_observer()
        pipeline = self.plugin_api.get_memory_pipeline()
        instant_gate = self.plugin_api.get_instant_gate()
        session_summarizer = self.plugin_api.get_session_summarizer()
        pipeline_status = (
            pipeline.describe_runtime_status()
            if pipeline is not None and hasattr(pipeline, "describe_runtime_status")
            else {}
        )
        snapshot = (
            await observer.runtime_snapshot(
                instant_gate_ready=bool(instant_gate is not None),
                memory_pipeline_ready=bool(pipeline is not None),
                session_summarizer_ready=bool(session_summarizer is not None),
                pipeline_status=pipeline_status,
            )
            if observer is not None and hasattr(observer, "runtime_snapshot")
            else {}
        )
        recent_errors = (
            await observer.recent_errors(limit=10)
            if observer is not None and hasattr(observer, "recent_errors")
            else []
        )
        recent_errors = [self.plugin_api.format_timeline_item(item) for item in list(recent_errors or [])]
        return {
            "status": "ok",
            "data": {
                "snapshot": snapshot,
                "recent_errors": list(recent_errors or []),
            },
            "runtime_bound": engine is not None,
        }

    async def observability_overview(self) -> dict[str, Any]:
        hub = self.plugin_api.get_observability_hub()
        snapshot = await hub.global_snapshot() if hub is not None and hasattr(hub, "global_snapshot") else {}
        recent_errors = await self.observability_errors(limit=12)
        return {
            "status": "ok",
            "data": {
                "snapshot": snapshot,
                "recent_errors": list(recent_errors.get("items", []) or []),
            },
            "runtime_bound": hub is not None,
        }

    async def memory_observability_timeline(
        self,
        *,
        chat_id: str | None = None,
        component: str = "",
        level: str = "",
        limit: int = 80,
    ) -> dict[str, Any]:
        engine = self.plugin_api.get_memory_engine()
        observer = self.plugin_api.get_memory_observer()
        items: list[dict[str, Any]] = []
        if observer is not None and hasattr(observer, "recent_events"):
            items = await observer.recent_events(chat_id=chat_id, component=component, level=level, limit=limit)
        else:
            planner = self.plugin_api.get_planner()
            raw_trace_store = getattr(planner, "raw_trace_store", None) if planner else None
            if raw_trace_store is not None and hasattr(raw_trace_store, "recent"):
                try:
                    events = [dict(item or {}) for item in list(await raw_trace_store.recent(chat_id=chat_id, limit=limit))]
                    items = [
                        {
                            "event_id": str(item.get("trace_id", "") or ""),
                            "chat_id": str(item.get("chat_id", "") or ""),
                            "component": str(item.get("memory_component", "") or ""),
                            "stage": str(item.get("memory_stage", "") or str(item.get("stage", "")).replace("memory.", "")),
                            "level": str(item.get("level", "info") or "info"),
                            "reason": str(item.get("reason", "") or ""),
                            "summary": str(item.get("summary", "") or ""),
                            "timestamp": float(item.get("created_at", 0.0) or 0.0),
                            "payload": dict(item.get("payload", {}) or {}),
                        }
                        for item in events
                        if str(item.get("stage", "") or "").startswith("memory.")
                    ]
                except Exception:
                    items = []
        items = [self.plugin_api.format_timeline_item(item) for item in list(items or [])]
        return {"status": "ok", "items": list(items or []), "total": len(items), "runtime_bound": engine is not None}

    async def memory_observability_chat(self, chat_id: str) -> dict[str, Any]:
        engine = self.plugin_api.get_memory_engine()
        pipeline = self.plugin_api.get_memory_pipeline()
        observer = self.plugin_api.get_memory_observer()
        buffer_payload = (
            pipeline.describe_chat_buffer(chat_id)
            if pipeline is not None and hasattr(pipeline, "describe_chat_buffer")
            else {}
        )
        data = (
            await observer.chat_snapshot(
                chat_id=chat_id,
                pipeline_buffer=buffer_payload,
                worker_active=bool(getattr(pipeline, "is_worker_active", lambda _: False)(chat_id)),
                limit=20,
            )
            if observer is not None and hasattr(observer, "chat_snapshot")
            else {}
        )
        timeline = await self.memory_observability_timeline(chat_id=chat_id, limit=40)
        errors = await self.memory_observability_errors(chat_id=chat_id, limit=20)
        return {
            "status": "ok",
            "data": {
                "chat": data,
                "timeline": list(timeline.get("items", []) or []),
                "errors": list(errors.get("items", []) or []),
            },
            "runtime_bound": engine is not None,
        }

    async def memory_observability_errors(self, chat_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        engine = self.plugin_api.get_memory_engine()
        observer = self.plugin_api.get_memory_observer()
        items = (
            await observer.recent_errors(chat_id=chat_id, limit=limit)
            if observer is not None and hasattr(observer, "recent_errors")
            else []
        )
        items = [self.plugin_api.format_timeline_item(item) for item in list(items or [])]
        return {"status": "ok", "items": list(items or []), "total": len(items), "runtime_bound": engine is not None}

    async def observability_timeline(
        self,
        *,
        chat_id: str | None = None,
        domains: list[str] | None = None,
        levels: list[str] | None = None,
        kinds: list[str] | None = None,
        limit: int = 80,
    ) -> dict[str, Any]:
        hub = self.plugin_api.get_observability_hub()
        if hub is None or not hasattr(hub, "recent"):
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": False}
        items = await hub.recent(
            chat_id=chat_id,
            domains=self._parse_filter_values(domains),
            levels=self._parse_filter_values(levels),
            kinds=self._parse_filter_values(kinds),
            limit=max(1, min(int(limit or 80), 300)),
        )
        formatted = [hub.format_timeline_item(item) if hasattr(hub, "format_timeline_item") else dict(item or {}) for item in list(items or [])]
        return {"status": "ok", "items": formatted, "total": len(formatted), "runtime_bound": True}

    async def observability_chat(self, chat_id: str) -> dict[str, Any]:
        hub = self.plugin_api.get_observability_hub()
        if hub is None:
            return {"status": "ok", "data": {}, "runtime_bound": False}
        snapshot = await hub.chat_snapshot(chat_id) if hasattr(hub, "chat_snapshot") else {}
        timeline = await self.observability_timeline(chat_id=chat_id, limit=80)
        errors = await self.observability_errors(chat_id=chat_id, limit=30)
        scheduler = await self.scheduler_chat_view(chat_id)
        memory = await self.memory_observability_chat(chat_id)
        heartflow = await self.heartflow_chat(chat_id)
        return {
            "status": "ok",
            "data": {
                "chat": snapshot,
                "timeline": list(timeline.get("items", []) or []),
                "errors": list(errors.get("items", []) or []),
                "scheduler": scheduler.get("data", {}),
                "memory": memory.get("data", {}),
                "heartflow": heartflow.get("data", {}),
            },
            "runtime_bound": True,
        }

    async def observability_errors(self, chat_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        hub = self.plugin_api.get_observability_hub()
        if hub is None or not hasattr(hub, "recent_errors"):
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": False}
        items = await hub.recent_errors(chat_id=chat_id, limit=max(1, min(int(limit or 50), 300)))
        formatted = [hub.format_timeline_item(item) if hasattr(hub, "format_timeline_item") else dict(item or {}) for item in list(items or [])]
        return {"status": "ok", "items": formatted, "total": len(formatted), "runtime_bound": True}

    async def observability_search(
        self,
        *,
        q: str = "",
        chat_id: str = "",
        domains: list[str] | None = None,
        kinds: list[str] | None = None,
        levels: list[str] | None = None,
        tags: list[str] | None = None,
        limit: int = 80,
    ) -> dict[str, Any]:
        hub = self.plugin_api.get_observability_hub()
        if hub is None or not hasattr(hub, "search"):
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": False}
        items = await hub.search(
            q=str(q or ""),
            chat_id=str(chat_id or ""),
            domains=self._parse_filter_values(domains),
            kinds=self._parse_filter_values(kinds),
            levels=self._parse_filter_values(levels),
            tags=self._parse_filter_values(tags),
            limit=max(1, min(int(limit or 80), 300)),
        )
        formatted = [hub.format_timeline_item(item) if hasattr(hub, "format_timeline_item") else dict(item or {}) for item in list(items or [])]
        return {"status": "ok", "items": formatted, "total": len(formatted), "runtime_bound": True}

    async def runtime_capabilities(self) -> dict[str, Any]:
        return await self._runtime.runtime_capabilities()

    async def runtime_models(self) -> dict[str, Any]:
        return await self._runtime.runtime_models()

    async def runtime_health(self) -> dict[str, Any]:
        base_health = await self._runtime.runtime_health(await self._expression_pattern_stats())
        data = dict(base_health.get("data", {}) or {})
        expression_stats = await self._expression_pattern_stats()
        memory_stats = await self._canonical_memory_stats_snapshot()
        data["active_chats"] = int(data.get("active_chat_count", 0) or 0)
        data["pending_reviews"] = expression_stats["pending"]
        data["total_memory_events"] = await self._safe_count("MemoryEvent")
        data["total_canonical_memories"] = int(memory_stats.get("total", 0) or 0)
        data["canonical_memory_stats"] = memory_stats
        data.pop("active_chat_count", None)
        return {
            "status": "ok",
            "data": data,
            "runtime_bound": self.plugin_api.has_bound_facade(),
        }

    def _context_economy_snapshot(self) -> dict[str, Any]:
        gateway = self.plugin_api.get_gateway()
        if gateway and hasattr(gateway, "get_context_economy_stats"):
            try:
                snapshot = gateway.get_context_economy_stats()
                return snapshot if isinstance(snapshot, dict) else {}
            except Exception:
                return {}
        return {}

    @staticmethod
    def _template_metric_item(template_key: str, metric: dict[str, Any]) -> dict[str, Any]:
        template_id, template_version = (template_key.rsplit("@", 1) + [""])[:2]
        workload_families = dict(metric.get("workload_families", {}) or {})
        return {
            "template_key": template_key,
            "template_id": template_id,
            "template_version": template_version,
            "workload_families": workload_families,
            "call_count": int(metric.get("call_count", 0) or 0),
            "lane_rotate_count": int(metric.get("lane_rotate_count", 0) or 0),
            "fallback_count": int(metric.get("fallback_count", 0) or 0),
            "primary_hit_rate": float(metric.get("primary_hit_rate", 0.0) or 0.0),
            "provider_session_usage_rate": float(metric.get("provider_session_usage_rate", 0.0) or 0.0),
            "provider_session_reuse_rate": float(metric.get("provider_session_reuse_rate", 0.0) or 0.0),
            "cache_affinity_ready_rate": float(metric.get("cache_affinity_ready_rate", 0.0) or 0.0),
            "avg_stable_prefix_length": float(metric.get("avg_stable_prefix_length", 0.0) or 0.0),
            "avg_dynamic_payload_length": float(metric.get("avg_dynamic_payload_length", 0.0) or 0.0),
            "actual_models": dict(metric.get("actual_models", {}) or {}),
            "rotate_reasons": dict(metric.get("rotate_reasons", {}) or {}),
        }

    def _context_economy_templates(
        self,
        snapshot: dict[str, Any],
        *,
        limit: int = 50,
        template_id: str = "",
        workload_family: str = "",
        sort_by: str = "rotate",
        sort_dir: str = "desc",
    ) -> list[dict[str, Any]]:
        templates = dict(snapshot.get("_templates", {}) or {})
        items = [
            self._template_metric_item(key, value if isinstance(value, dict) else {})
            for key, value in templates.items()
        ]
        if template_id:
            needle = str(template_id or "").strip().lower()
            items = [item for item in items if needle in str(item["template_id"]).lower()]
        if workload_family:
            target_family = str(workload_family or "").strip()
            items = [
                item for item in items
                if target_family in dict(item.get("workload_families", {}) or {})
            ]
        sort_key = str(sort_by or "rotate").strip().lower()
        direction = str(sort_dir or "").strip().lower()
        if sort_key not in {"rotate", "session_reuse", "calls"}:
            sort_key = "rotate"
        if direction not in {"asc", "desc"}:
            direction = "asc" if sort_key == "session_reuse" else "desc"

        def _sort_tuple(item: dict[str, Any]) -> tuple:
            rotates = int(item.get("lane_rotate_count", 0) or 0)
            calls = int(item.get("call_count", 0) or 0)
            reuse = float(item.get("provider_session_reuse_rate", 0.0) or 0.0)
            key = str(item.get("template_key", ""))
            if sort_key == "session_reuse":
                return (reuse, -rotates, -calls, key)
            if sort_key == "calls":
                return (-calls, -rotates, reuse, key)
            return (-rotates, reuse, -calls, key)

        items.sort(key=_sort_tuple)
        default_direction = "asc" if sort_key == "session_reuse" else "desc"
        if direction != default_direction:
            items.reverse()
        return items[: max(1, min(int(limit or 50), 200))]

    @staticmethod
    def _context_economy_workload_families(snapshot: dict[str, Any]) -> list[str]:
        families: set[str] = set()
        for metric in dict(snapshot.get("_templates", {}) or {}).values():
            if not isinstance(metric, dict):
                continue
            for family in dict(metric.get("workload_families", {}) or {}).keys():
                if family:
                    families.add(str(family))
        return sorted(families)

    def _context_economy_overview(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        family_metrics = [
            value
            for key, value in snapshot.items()
            if key != "_templates" and isinstance(value, dict)
        ]
        total_calls = sum(int(item.get("call_count", 0) or 0) for item in family_metrics)
        total_rotates = sum(int(item.get("lane_rotate_count", 0) or 0) for item in family_metrics)
        total_fallbacks = sum(int(item.get("fallback_count", 0) or 0) for item in family_metrics)
        total_primary_hits = sum(
            int(round(float(item.get("primary_hit_rate", 0.0) or 0.0) * int(item.get("call_count", 0) or 0)))
            for item in family_metrics
        )
        total_provider_session_uses = sum(
            int(round(float(item.get("provider_session_usage_rate", 0.0) or 0.0) * int(item.get("call_count", 0) or 0)))
            for item in family_metrics
        )
        total_provider_session_reused = sum(
            int(round(float(item.get("provider_session_reuse_rate", 0.0) or 0.0) * int(item.get("call_count", 0) or 0)))
            for item in family_metrics
        )
        return {
            "total_calls": total_calls,
            "total_rotates": total_rotates,
            "total_fallbacks": total_fallbacks,
            "primary_hit_rate": round((total_primary_hits / total_calls), 4) if total_calls else 0.0,
            "provider_session_usage_rate": round((total_provider_session_uses / total_calls), 4) if total_calls else 0.0,
            "provider_session_reuse_rate": round((total_provider_session_reused / total_calls), 4) if total_calls else 0.0,
            "template_count": len(dict(snapshot.get("_templates", {}) or {})),
        }

    @staticmethod
    def _safe_preview(value: Any, limit: int = 240) -> str:
        text = str(value or "").replace("\r\n", "\n").strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."

    def _extract_failure_evidence(self, item: dict[str, Any]) -> dict[str, Any]:
        item = dict(item or {})
        attempted_models = list(item.get("attempted_models", []) or [])
        raw_completion = self._safe_preview(item.get("raw_completion", ""))
        failure_kind = str(item.get("failure_kind", "") or item.get("last_failure_kind", "") or "")
        failure_reason = str(item.get("failure_reason", "") or item.get("error_message", "") or "")
        protocol_passthrough = bool(item.get("protocol_passthrough", False))
        protocol_type = str(item.get("protocol_type", "") or "")
        vision_attempted_models = list(item.get("vision_direct_attempted_models", []) or [])
        vision_failure_reason = str(item.get("vision_direct_failure_reason", "") or "")
        vision_failure_kind = str(item.get("vision_direct_failure_kind", "") or "")
        return {
            "has_failure": bool(failure_kind or failure_reason or attempted_models or raw_completion),
            "failure_kind": failure_kind,
            "failure_reason": failure_reason,
            "attempted_models": attempted_models,
            "raw_completion_preview": raw_completion,
            "protocol_passthrough": protocol_passthrough,
            "protocol_type": protocol_type,
            "vision_failure_kind": vision_failure_kind,
            "vision_failure_reason": vision_failure_reason,
            "vision_attempted_models": vision_attempted_models,
        }

    def _enrich_history_item(self, item: Any) -> dict[str, Any]:
        payload = self._as_dict(item)
        payload["failure_evidence"] = self._extract_failure_evidence(payload)
        return payload

    async def context_economy_overview_view(self, limit: int = 20) -> dict[str, Any]:
        return await self._context_economy_ui.context_economy_overview_view(limit=limit)

    async def context_economy_templates_view(
        self,
        limit: int = 50,
        template_id: str | None = None,
        workload_family: str | None = None,
        sort_by: str = "rotate",
        sort_dir: str | None = None,
    ) -> dict[str, Any]:
        return await self._context_economy_ui.context_economy_templates_view(
            limit=limit,
            template_id=template_id,
            workload_family=workload_family,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )

    # ── heartflow (delegated to HeartflowService) ──

    async def heartflow_status(self) -> dict[str, Any]:
        return await self._heartflow.heartflow_status()

    async def heartflow_chats(self) -> dict[str, Any]:
        return await self._heartflow.heartflow_chats()

    async def heartflow_chat(self, chat_id: str) -> dict[str, Any]:
        return await self._heartflow.heartflow_chat(chat_id)

    async def heartflow_impulses(self, chat_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        return await self._heartflow.heartflow_impulses(chat_id=chat_id, limit=limit)

    async def heartflow_timeline(self, chat_id: str | None = None, limit: int = 80) -> dict[str, Any]:
        return await self._heartflow.heartflow_timeline(chat_id=chat_id, limit=limit)

    async def heartflow_topic_digests(self, limit: int = 50) -> dict[str, Any]:
        return await self._heartflow.heartflow_topic_digests(limit=limit)

    async def heartflow_hidden_context(self, chat_id: str) -> dict[str, Any]:
        return await self._heartflow.heartflow_hidden_context(chat_id)

    async def clear_heartflow_cooldowns(self, chat_id: str) -> dict[str, Any]:
        return await self._heartflow.clear_heartflow_cooldowns(chat_id)

    async def recent_decisions(self, chat_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        planner = self.plugin_api.get_planner()
        items = list(getattr(planner, "cognitive_decision_history", []) or [])
        if chat_id:
            items = [item for item in items if str(item.get("chat_id", "")) == chat_id]
        items = items[-max(1, min(limit, 300)) :][::-1]
        return {
            "status": "ok",
            "items": [self._enrich_history_item(item) for item in items],
            "total": len(items),
            "runtime_bound": planner is not None,
        }

    async def recent_turn_traces(self, chat_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        planner = self.plugin_api.get_planner()
        persistent_store = getattr(planner, "turn_trace_store", None) if planner else None
        items: list[dict[str, Any]] = []
        if persistent_store is not None and hasattr(persistent_store, "recent"):
            try:
                items = list(await persistent_store.recent(chat_id=chat_id, limit=limit))
            except Exception:
                items = []
        if not items:
            items = list(getattr(planner, "turn_trace_history", []) or [])
            if chat_id:
                items = [item for item in items if str(item.get("chat_id", "")) == chat_id]
            items = items[-max(1, min(limit, 300)) :][::-1]
        return {
            "status": "ok",
            "items": [self._enrich_history_item(item) for item in items],
            "total": len(items),
            "runtime_bound": planner is not None,
        }

    async def tools_status(self) -> dict[str, Any]:
        from ....conversation.planning.planner_side_inputs import PlannerSideInputMixin

        return {
            "status": "ok",
            "data": {
                "chat_tier": sorted(PlannerSideInputMixin.CHAT_TOOL_NAMES),
                "guarded_chat_tier": sorted(PlannerSideInputMixin.GUARDED_CHAT_TOOL_NAMES),
                "full_only": sorted(PlannerSideInputMixin.FULL_ONLY_TOOL_NAMES),
                "families": {key: sorted(value) for key, value in PlannerSideInputMixin.TOOL_FAMILIES.items()},
            },
        }

    async def recent_tool_traces(self, chat_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        planner = self.plugin_api.get_planner()
        items = list(getattr(planner, "tool_trace_history", []) or [])
        if chat_id:
            items = [item for item in items if str(item.get("chat_id", "")) == chat_id]
        items = items[-max(1, min(limit, 300)) :][::-1]
        return {
            "status": "ok",
            "items": [self._enrich_history_item(item) for item in items],
            "total": len(items),
            "runtime_bound": planner is not None,
        }

    @staticmethod
    def _timeline_item(
        *,
        domain: str = "",
        kind: str,
        timestamp: float,
        chat_id: str,
        title: str,
        summary: str,
        level: str = "info",
        source: str = "",
        detail: dict[str, Any] | None = None,
        raw: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "domain": str(domain or ""),
            "kind": kind,
            "timestamp": float(timestamp or 0.0),
            "chat_id": str(chat_id or ""),
            "title": str(title or ""),
            "summary": str(summary or ""),
            "level": str(level or "info").lower() or "info",
            "source": str(source or kind),
            "detail": dict(detail or {}),
            "raw": dict(raw or {}),
        }

    async def chat_trace_events(self, chat_id: str, limit: int = 80) -> dict[str, Any]:
        planner = self.plugin_api.get_planner()
        events: list[dict[str, Any]] = []
        raw_trace_store = getattr(planner, "raw_trace_store", None) if planner else None
        if raw_trace_store is not None and hasattr(raw_trace_store, "recent"):
            try:
                events = [dict(item or {}) for item in list(await raw_trace_store.recent(chat_id=chat_id, limit=limit))]
            except Exception:
                events = []
        if not events:
            turn_trace_result = await self.recent_turn_traces(chat_id=chat_id, limit=limit)
            for item in list(turn_trace_result.get("items", []) or []):
                trace_log = list(item.get("astrmai_trace_log", []) or item.get("trace_log", []) or [])
                for event in trace_log:
                    event_payload = dict(event or {})
                    event_payload["chat_id"] = chat_id
                    events.append(event_payload)
        for event_payload in events:
            event_payload["failure_evidence"] = self._extract_failure_evidence(event_payload)
        events.sort(key=lambda item: float(item.get("created_at", item.get("timestamp", 0.0)) or 0.0), reverse=True)
        events = events[: max(1, min(limit, 300))]
        return {"status": "ok", "items": events, "total": len(events), "runtime_bound": planner is not None}

    async def cognition_unified_timeline(
        self,
        *,
        chat_id: str,
        limit: int = 80,
        include: list[str] | None = None,
        level: str = "",
    ) -> dict[str, Any]:
        kind_filters = {item for item in self._parse_filter_values(include) if item in {"decision", "tool", "trace", "memory"}}
        hub = self.plugin_api.get_observability_hub()
        if hub is None:
            include_set = kind_filters or {"decision", "tool", "trace", "memory"}
            safe_limit = max(1, min(int(limit or 80), 300))
            normalized_level = str(level or "").strip().lower()
            items: list[dict[str, Any]] = []
            if "decision" in include_set:
                decisions = await self.recent_decisions(chat_id=chat_id, limit=safe_limit)
                for item in decisions.get("items", []) or []:
                    items.append(
                        self._timeline_item(
                            domain="cognition",
                            kind="decision",
                            timestamp=float(item.get("created_at", item.get("timestamp", 0.0)) or 0.0),
                            chat_id=str(item.get("chat_id", chat_id) or chat_id),
                            title=str(item.get("social_intent") or item.get("decision") or "decision"),
                            summary=self._safe_preview(item.get("failure_evidence", {}).get("failure_reason") or item.get("failure_evidence", {}).get("failure_kind") or item.get("reason") or item.get("summary") or ""),
                            level="error" if item.get("failure_evidence", {}).get("has_failure") else "info",
                            source="decision",
                            detail={"memory_policy": item.get("memory_policy"), "tool_tier": item.get("tool_tier")},
                            raw=item,
                        )
                    )
            if "tool" in include_set:
                tools = await self.recent_tool_traces(chat_id=chat_id, limit=safe_limit)
                for item in tools.get("items", []) or []:
                    evidence = item.get("failure_evidence", {}) or {}
                    items.append(
                        self._timeline_item(
                            domain="cognition",
                            kind="tool",
                            timestamp=float(item.get("created_at", item.get("timestamp", 0.0)) or 0.0),
                            chat_id=str(item.get("chat_id", chat_id) or chat_id),
                            title=str(item.get("tool_tier") or item.get("protocol_type") or "tool"),
                            summary=self._safe_preview(evidence.get("failure_reason") or evidence.get("failure_kind") or item.get("summary") or ""),
                            level="error" if evidence.get("has_failure") else "info",
                            source="tool",
                            detail={"protocol_passthrough": evidence.get("protocol_passthrough"), "attempted_models": evidence.get("attempted_models", [])},
                            raw=item,
                        )
                    )
            if "trace" in include_set:
                traces = await self.chat_trace_events(chat_id=chat_id, limit=safe_limit)
                for item in traces.get("items", []) or []:
                    evidence = item.get("failure_evidence", {}) or {}
                    items.append(
                        self._timeline_item(
                            domain="cognition",
                            kind="trace",
                            timestamp=float(item.get("created_at", item.get("timestamp", 0.0)) or 0.0),
                            chat_id=str(item.get("chat_id", chat_id) or chat_id),
                            title=str(item.get("stage") or "trace"),
                            summary=self._safe_preview(evidence.get("failure_reason") or evidence.get("failure_kind") or item.get("summary") or ""),
                            level="error" if evidence.get("has_failure") else str(item.get("level", "info") or "info"),
                            source="trace",
                            detail={"failure_kind": evidence.get("failure_kind"), "attempted_models": evidence.get("attempted_models", [])},
                            raw=item,
                        )
                    )
            if "memory" in include_set:
                memory = await self.memory_observability_timeline(chat_id=chat_id, limit=safe_limit)
                for item in memory.get("items", []) or []:
                    items.append(
                        self._timeline_item(
                            domain="memory",
                            kind="memory",
                            timestamp=float(item.get("timestamp", 0.0) or 0.0),
                            chat_id=str(item.get("chat_id", chat_id) or chat_id),
                            title=str(item.get("display_title") or item.get("stage") or "memory"),
                            summary=self._safe_preview(item.get("summary") or item.get("reason") or ""),
                            level=str(item.get("level", "info") or "info"),
                            source=str(item.get("component") or "memory"),
                            detail={"stage": item.get("stage"), "memory_id": item.get("memory_id")},
                            raw=item,
                        )
                    )
            items.sort(key=lambda item: float(item.get("timestamp", 0.0) or 0.0), reverse=True)
            if normalized_level:
                items = [item for item in items if str(item.get("level", "") or "").lower() == normalized_level]
            items = items[:safe_limit]
            return {"status": "ok", "items": items, "total": len(items), "runtime_bound": self.plugin_api.has_bound_facade()}
        domain_filters = {"memory" if item == "memory" else "cognition" for item in kind_filters} if kind_filters else {"cognition", "memory"}
        result = await self.observability_timeline(
            chat_id=chat_id,
            domains=list(domain_filters),
            kinds=list(kind_filters) if kind_filters else ["decision", "tool", "trace", "action", "maintenance"],
            levels=[level] if str(level or "").strip() else [],
            limit=limit,
        )
        if kind_filters:
            result["items"] = [item for item in list(result.get("items", []) or []) if str(item.get("kind", "") or "") in kind_filters]
            result["total"] = len(result["items"])
        return result

    async def tools_policy(self) -> dict[str, Any]:
        status = await self.tools_status()
        status["data"]["rules"] = [
            "chat tier exposes light social tools",
            "full tier requires explicit tool intent or planner decision",
            "sys3 tier is reserved for direct work mode/tool-call routes",
            "state and cooldown modifiers may remove tools before execution",
        ]
        return status

    @staticmethod
    def _feedback_id(signal: Any) -> str:
        return "|".join(
            [
                str(getattr(signal, "chat_id", "") or ""),
                str(getattr(signal, "source", "") or ""),
                str(int(float(getattr(signal, "timestamp", 0.0) or 0.0))),
                str(abs(hash((getattr(signal, "summary", ""), getattr(signal, "guidance", ""))))),
            ]
        )

    async def list_memory_feedback(self, chat_id: str | None = None, source: str | None = None, limit: int = 50) -> dict[str, Any]:
        engine = self.plugin_api.get_memory_engine()
        if not engine:
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": False}
        signals = []
        source_filter = {source} if source else None
        if chat_id and hasattr(engine, "get_cognitive_feedback"):
            signals = await engine.get_cognitive_feedback(chat_id, limit=limit, sources=source_filter)
        else:
            for cached_items in getattr(engine, "_cognitive_feedback_cache", {}).values():
                signals.extend(cached_items)
            if source:
                signals = [item for item in signals if getattr(item, "source", "") == source]
            signals = sorted(signals, key=lambda item: float(getattr(item, "timestamp", 0.0) or 0.0), reverse=True)[:limit]
        items = []
        for signal in signals:
            item = self._as_dict(signal)
            item["id"] = self._feedback_id(signal)
            items.append(item)
        return {"status": "ok", "items": items, "total": len(items), "runtime_bound": True}

    async def disable_memory_feedback(self, feedback_id: str) -> dict[str, Any]:
        engine = self.plugin_api.get_memory_engine()
        if not engine:
            return {"status": "ok", "changed": False, "runtime_bound": False}
        for cached_items in getattr(engine, "_cognitive_feedback_cache", {}).values():
            for signal in cached_items:
                if self._feedback_id(signal) == feedback_id:
                    if hasattr(engine, "disable_cognitive_feedback"):
                        engine.disable_cognitive_feedback(signal)
                    return {"status": "ok", "changed": True, "runtime_bound": True}
        return {"status": "ok", "changed": False, "runtime_bound": True}

    async def memory_feedback_sources(self) -> dict[str, Any]:
        feedback = await self.list_memory_feedback(limit=300)
        sources: dict[str, dict[str, Any]] = {}
        for item in feedback.get("items", []):
            source = str(item.get("source", "unknown") or "unknown")
            bucket = sources.setdefault(source, {"source": source, "count": 0, "latest_timestamp": 0.0})
            bucket["count"] += 1
            bucket["latest_timestamp"] = max(float(bucket["latest_timestamp"]), float(item.get("timestamp", 0.0) or 0.0))
        return {"status": "ok", "items": list(sources.values()), "total": len(sources)}

    async def proactive_status(self) -> dict[str, Any]:
        task = self.plugin_api.get_proactive_task()
        data = task.describe_status() if task and hasattr(task, "describe_status") else {"running": False}
        return {"status": "ok", "data": data, "runtime_bound": task is not None}

    async def scheduler_status_view(self) -> dict[str, Any]:
        task = self.plugin_api.get_proactive_task()
        kernel = self.plugin_api.get_chat_loop_kernel()
        proactive = task.describe_status() if task and hasattr(task, "describe_status") else {}
        kernel_status = kernel.describe_status_sync() if kernel and hasattr(kernel, "describe_status_sync") else {}
        overview = {
            "scheduler_poll_mode": proactive.get("scheduler_poll_mode", ""),
            "scheduler_poll_interval": proactive.get("scheduler_poll_interval", 0.0),
            "due_chat_count": proactive.get("due_chat_count", 0),
            "maintenance_budget_total": proactive.get("maintenance_budget_total", 0),
            "maintenance_budget_remaining": proactive.get("maintenance_budget_remaining", 0),
            "batch_fill_rate": proactive.get("batch_fill_rate", 0.0),
            "forced_promotion_count": proactive.get("forced_promotion_count", 0),
        }
        return {
            "status": "ok",
            "data": {
                "overview": overview,
                "proactive": proactive,
                "kernel": kernel_status,
                "scheduler_policy": dict(kernel_status.get("scheduler_policy", {}) or {}),
            },
            "runtime_bound": bool(task or kernel),
        }

    async def scheduler_due_selection_view(self) -> dict[str, Any]:
        task = self.plugin_api.get_proactive_task()
        kernel = self.plugin_api.get_chat_loop_kernel()
        proactive = task.describe_status() if task and hasattr(task, "describe_status") else {}
        kernel_status = kernel.describe_status_sync() if kernel and hasattr(kernel, "describe_status_sync") else {}
        return {
            "status": "ok",
            "data": {
                "report": dict(kernel_status.get("last_due_selection_report", {}) or {}),
                "summary": dict(kernel_status.get("last_due_selection_summary", {}) or {}),
                "poll_mode_transition": dict(proactive.get("poll_mode_transition", {}) or {}),
            },
            "runtime_bound": bool(task or kernel),
        }

    async def scheduler_chat_view(self, chat_id: str) -> dict[str, Any]:
        kernel = self.plugin_api.get_chat_loop_kernel()
        if not kernel or not hasattr(kernel, "peek_loop_state"):
            return {"status": "ok", "data": {}, "runtime_bound": False}
        state = await kernel.peek_loop_state(chat_id)
        if state is None:
            return {
                "status": "ok",
                "data": {
                    "chat_id": chat_id,
                    "state_present": False,
                    "scheduler_pending_signals": {},
                },
                "runtime_bound": True,
            }
        data = self._as_dict(state)
        data["chat_id"] = chat_id
        data["state_present"] = True
        data["scheduler_pending_signals"] = self._scheduler_pending_signal_slice(data.get("pending_signals", {}))
        return {"status": "ok", "data": data, "runtime_bound": True}

    async def proactive_intents(self, limit: int = 50) -> dict[str, Any]:
        task = self.plugin_api.get_proactive_task()
        dispatcher = getattr(task, "proactive_dispatcher", None) if task else None
        if not dispatcher or not hasattr(dispatcher, "list_intents"):
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": task is not None}
        try:
            safe_limit = max(1, min(int(limit or 50), 200))
        except (TypeError, ValueError):
            safe_limit = 50
        items = dispatcher.list_intents(limit=safe_limit)
        return {"status": "ok", "items": items, "total": len(items), "runtime_bound": True}

    async def dream_status(self) -> dict[str, Any]:
        task = self.plugin_api.get_proactive_task()
        scheduler = getattr(task, "dream_scheduler", None) if task else None
        data = scheduler.describe_status() if scheduler and hasattr(scheduler, "describe_status") else {"dream_agent_bound": False}
        return {"status": "ok", "data": data, "runtime_bound": scheduler is not None}

    async def run_dream_once(self) -> dict[str, Any]:
        task = self.plugin_api.get_proactive_task()
        scheduler = getattr(task, "dream_scheduler", None) if task else None
        if not scheduler or not getattr(scheduler, "dream_agent", None) or not getattr(scheduler, "dream_generator", None):
            return {"status": "error", "message": "Dream dependencies are not bound", "runtime_bound": scheduler is not None}
        safe_create_task(scheduler.run_once())
        return {"status": "ok", "scheduled": True, "runtime_bound": True}

    async def diary_status(self) -> dict[str, Any]:
        task = self.plugin_api.get_proactive_task()
        return {
            "status": "ok",
            "data": {
                "available": bool(task and getattr(task, "diary_service", None)),
                "last_diary_date": getattr(task, "_last_diary_date", "") if task else "",
            },
            "runtime_bound": task is not None,
        }

    async def run_diary_once(self) -> dict[str, Any]:
        task = self.plugin_api.get_proactive_task()
        service = getattr(task, "diary_service", None) if task else None
        state_engine = self.plugin_api.get_state_engine()
        if not service or not state_engine:
            return {"status": "error", "message": "Diary dependencies are not bound", "runtime_bound": task is not None}
        safe_create_task(service.run_once(state_engine.get_active_states()))
        return {"status": "ok", "scheduled": True, "runtime_bound": True}

    async def wakeup_status(self) -> dict[str, Any]:
        task = self.plugin_api.get_proactive_task()
        service = getattr(task, "wakeup_service", None) if task else None
        return {
            "status": "ok",
            "data": {
                "available": service is not None,
                "silence_threshold": getattr(getattr(self.plugin_api.get_runtime_config(), "life", None), "silence_threshold", 0),
                "wakeup_cooldown": getattr(getattr(self.plugin_api.get_runtime_config(), "life", None), "wakeup_cooldown", 0),
            },
            "runtime_bound": self.plugin_api.has_bound_facade(),
        }

    async def learning_status(self) -> dict[str, Any]:
        evolution = self.plugin_api.get_evolution()
        diagnostics = evolution.describe_learning_runtime() if evolution and hasattr(evolution, "describe_learning_runtime") else {}
        backlog = await evolution.backlog_overview() if evolution and hasattr(evolution, "backlog_overview") else {}
        expression_stats = await self._expression_pattern_stats()
        jargon_stats = await self._jargon_stats()
        return {
            "status": "ok",
            "data": {
                "reflector": self.plugin_api.get_reflector() is not None,
                "reflect_tracker": self.plugin_api.get_reflect_tracker() is not None,
                "auto_check_task": self.plugin_api.get_auto_check_task() is not None,
                "expression_patterns": expression_stats,
                "jargons": jargon_stats,
                "diagnostics": diagnostics,
                "backlog": backlog,
            },
            "runtime_bound": self.plugin_api.has_bound_facade(),
        }

    async def expression_stats(self) -> dict[str, Any]:
        stats = await self._expression_pattern_stats()
        return {
            "status": "ok",
            "data": stats,
        }

    async def expression_cooldowns(self) -> dict[str, Any]:
        planner = self.plugin_api.get_planner()
        selector = getattr(planner, "expression_selector", None) if planner else None
        return {
            "status": "ok",
            "data": {
                "recent_patterns": self._as_dict(selector).get("_recent_patterns", {}) if selector else {},
            },
            "runtime_bound": selector is not None,
        }

    async def run_reflect_once(self, chat_id: str) -> dict[str, Any]:
        reflector = self.plugin_api.get_reflector()
        if not reflector:
            return {"status": "error", "message": "Reflector is not bound", "runtime_bound": self.plugin_api.has_bound_facade()}
        if hasattr(reflector, "reflect_batch"):
            await reflector.reflect_batch(chat_id)
        if hasattr(reflector, "auto_audit"):
            await reflector.auto_audit(chat_id)
        auto_check = self.plugin_api.get_auto_check_task()
        if auto_check and hasattr(auto_check, "run_once"):
            await auto_check.run_once(chat_id)
        return {"status": "ok", "runtime_bound": True}

    async def active_chats(self, max_age_seconds: float = 1800) -> dict[str, Any]:
        coordinator = self.plugin_api.get_runtime_coordinator()
        if not coordinator or not hasattr(coordinator, "list_active_chats"):
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": False}
        chat_ids = await coordinator.list_active_chats(max_age_seconds)
        return {"status": "ok", "items": chat_ids, "total": len(chat_ids), "runtime_bound": True}

    async def chat_activity(self, chat_id: str) -> dict[str, Any]:
        coordinator = self.plugin_api.get_runtime_coordinator()
        data = await coordinator.get_activity_snapshot(chat_id) if coordinator and hasattr(coordinator, "get_activity_snapshot") else {}
        return {"status": "ok", "data": data, "runtime_bound": coordinator is not None}

    async def chat_runtime(self, chat_id: str) -> dict[str, Any]:
        activity = await self.chat_activity(chat_id)
        data = dict(activity.get("data", {}) or {})
        coordinator = self.plugin_api.get_runtime_coordinator()
        if coordinator and hasattr(coordinator, "get_wait_target_name"):
            data["wait_target_name"] = await coordinator.get_wait_target_name(chat_id)
        return {"status": "ok", "data": data, "runtime_bound": coordinator is not None}

    async def clear_chat_runtime(self, chat_id: str) -> dict[str, Any]:
        coordinator = self.plugin_api.get_runtime_coordinator()
        changed = False
        if coordinator and hasattr(coordinator, "clear_runtime_state"):
            changed = await coordinator.clear_runtime_state(chat_id)
        manager = self.plugin_api.get_heartflow_manager()
        if manager:
            # P9-2: use hasattr guards instead of silent getattr().pop() on private attrs
            for attr_name in ("_pulses_by_chat", "_impulse_decisions_by_chat"):
                if hasattr(manager, attr_name):
                    private_dict = getattr(manager, attr_name)
                    if isinstance(private_dict, dict):
                        private_dict.pop(chat_id, None)
            state = manager.get_state(chat_id) if hasattr(manager, "get_state") else None
            if state:
                state.cooldown_tags = []
                changed = True
        return {"status": "ok", "changed": changed, "runtime_bound": self.plugin_api.has_bound_facade()}


__all__ = ["AdminUiService"]
