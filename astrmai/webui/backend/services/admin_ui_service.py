from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Callable

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

    def _runtime(self) -> Any:
        return self.plugin_api.get_runtime()

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

    @staticmethod
    def _planner(runtime: Any) -> Any:
        return getattr(runtime, "system2_planner", None) if runtime else None

    @staticmethod
    def _gateway(runtime: Any) -> Any:
        return getattr(runtime, "gateway", None) if runtime else None

    @staticmethod
    def _proactive_task(runtime: Any) -> Any:
        return getattr(runtime, "proactive_task", None) if runtime else None

    @staticmethod
    def _chat_loop_kernel(runtime: Any) -> Any:
        if runtime and getattr(runtime, "chat_loop_kernel", None) is not None:
            return getattr(runtime, "chat_loop_kernel", None)
        task = AdminUiService._proactive_task(runtime)
        return getattr(task, "chat_loop_kernel", None) if task else None

    @staticmethod
    def _heartflow_manager(runtime: Any) -> Any:
        task = AdminUiService._proactive_task(runtime)
        return getattr(task, "heartflow_manager", None) if task else None

    @staticmethod
    def _heartflow_topic_digest_service(runtime: Any) -> Any:
        task = AdminUiService._proactive_task(runtime)
        return getattr(task, "heartflow_topic_digest_service", None) if task else None

    @classmethod
    def _scheduler_pending_signal_slice(cls, pending_signals: dict[str, Any] | None) -> dict[str, Any]:
        payload = dict(pending_signals or {})
        return {
            key: value
            for key, value in payload.items()
            if key in cls.SCHEDULER_SIGNAL_KEYS
        }

    async def _safe_count(self, table: str, where: str = "", params: tuple = ()) -> int:
        if not self.db_factory:
            return 0
        try:
            async with self.db_factory() as db:
                query = f"SELECT COUNT(*) FROM {table}"
                if where:
                    query += f" WHERE {where}"
                async with db.execute(query, params) as cursor:
                    row = await cursor.fetchone()
                    return int(row[0] if row else 0)
        except (sqlite3.OperationalError, ValueError, TypeError):
            return 0

    async def _expression_pattern_stats(self) -> dict[str, int]:
        if not self.db_factory:
            return {"total": 0, "pending": 0, "approved": 0, "rejected": 0}
        try:
            async with self.db_factory() as db:
                async with db.execute(
                    """
                    SELECT status, metadata
                    FROM canonical_memories
                    WHERE kind = 'expression_pattern'
                    """
                ) as cursor:
                    rows = await cursor.fetchall()
        except (sqlite3.OperationalError, ValueError, TypeError):
            return {"total": 0, "pending": 0, "approved": 0, "rejected": 0}
        stats = {"total": 0, "pending": 0, "approved": 0, "rejected": 0}
        for status_value, metadata_raw in rows:
            stats["total"] += 1
            status = str(status_value or "").strip().lower()
            try:
                metadata = json.loads(metadata_raw or "{}")
            except Exception:
                metadata = {}
            review_status = str((metadata or {}).get("review_status") or "").strip().lower()
            if status == "review_pending" or review_status in {"pending", "pending_human", "revision_needed"}:
                stats["pending"] += 1
            elif status == "active" and review_status == "approved":
                stats["approved"] += 1
            elif status == "rejected" or review_status == "rejected":
                stats["rejected"] += 1
        return stats

    async def runtime_status(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "data": await self.plugin_api.get_runtime_diagnostics(),
            "runtime_bound": self._runtime() is not None,
        }

    async def runtime_capabilities(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "data": await self.plugin_api.get_capability_overview(),
            "runtime_bound": self._runtime() is not None,
        }

    async def runtime_models(self) -> dict[str, Any]:
        diagnostics = await self.plugin_api.get_runtime_diagnostics()
        return {
            "status": "ok",
            "data": diagnostics.get("models", {}),
            "runtime_bound": self._runtime() is not None,
        }

    async def runtime_health(self) -> dict[str, Any]:
        diagnostics = await self.plugin_api.get_runtime_diagnostics()
        status = diagnostics.get("status", {}) if isinstance(diagnostics, dict) else {}
        runtime = self._runtime()
        active_chats = 0
        coordinator = getattr(runtime, "runtime_coordinator", None) if runtime else None
        if coordinator and hasattr(coordinator, "list_active_chats"):
            try:
                active_chats = len(await coordinator.list_active_chats(1800))
            except Exception:
                active_chats = 0
        expression_stats = await self._expression_pattern_stats()
        return {
            "status": "ok",
            "data": {
                "running": bool(status.get("lifecycle_started", False)),
                "boot_phase": status.get("boot_phase", ""),
                "degraded_count": len(status.get("degraded_components", {}) or {}),
                "active_chats": active_chats,
                "pending_reviews": expression_stats["pending"],
                "total_memory_events": await self._safe_count("MemoryEvent"),
                "total_canonical_memories": await self._safe_count("canonical_memories"),
            },
            "runtime_bound": runtime is not None,
        }

    @staticmethod
    def _context_economy_snapshot(runtime: Any) -> dict[str, Any]:
        gateway = AdminUiService._gateway(runtime)
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

    async def context_economy_overview_view(self, limit: int = 20) -> dict[str, Any]:
        runtime = self._runtime()
        snapshot = self._context_economy_snapshot(runtime)
        return {
            "status": "ok",
            "data": {
                "overview": self._context_economy_overview(snapshot),
                "templates": self._context_economy_templates(snapshot, limit=limit),
            },
            "runtime_bound": self._gateway(runtime) is not None,
        }

    async def context_economy_templates_view(
        self,
        limit: int = 50,
        template_id: str | None = None,
        workload_family: str | None = None,
        sort_by: str = "rotate",
        sort_dir: str | None = None,
    ) -> dict[str, Any]:
        runtime = self._runtime()
        snapshot = self._context_economy_snapshot(runtime)
        family_value = str(workload_family or "")
        template_value = str(template_id or "")
        items = self._context_economy_templates(
            snapshot,
            limit=limit,
            template_id=template_value,
            workload_family=family_value,
            sort_by=sort_by,
            sort_dir=str(sort_dir or ""),
        )
        total_items = self._context_economy_templates(
            snapshot,
            limit=200,
            template_id=template_value,
            workload_family=family_value,
            sort_by=sort_by,
            sort_dir=str(sort_dir or ""),
        )
        return {
            "status": "ok",
            "items": items,
            "total": len(total_items),
            "available_workload_families": self._context_economy_workload_families(snapshot),
            "runtime_bound": self._gateway(runtime) is not None,
        }

    async def heartflow_status(self) -> dict[str, Any]:
        manager = self._heartflow_manager(self._runtime())
        data = manager.describe_status() if manager and hasattr(manager, "describe_status") else {"enabled": False}
        return {"status": "ok", "data": data, "runtime_bound": manager is not None}

    async def heartflow_chats(self) -> dict[str, Any]:
        manager = self._heartflow_manager(self._runtime())
        if not manager:
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": False}
        states = []
        for chat_id, state in getattr(manager, "_states", {}).items():
            item = self._as_dict(state)
            item["chat_id"] = chat_id
            if hasattr(manager, "get_session"):
                item["session"] = self._as_dict(manager.get_session(chat_id))
            if hasattr(manager, "get_latest_action_decision"):
                item["latest_action_decision"] = self._as_dict(manager.get_latest_action_decision(chat_id))
            if hasattr(manager, "get_latest_impulse_decision"):
                item["latest_impulse_decision"] = self._as_dict(manager.get_latest_impulse_decision(chat_id))
            states.append(item)
        states.sort(key=lambda item: float(item.get("last_activity_ts", 0.0) or 0.0), reverse=True)
        return {"status": "ok", "items": states, "total": len(states), "runtime_bound": True}

    async def heartflow_chat(self, chat_id: str) -> dict[str, Any]:
        manager = self._heartflow_manager(self._runtime())
        state = manager.get_state(chat_id) if manager and hasattr(manager, "get_state") else None
        session = manager.get_session(chat_id) if manager and hasattr(manager, "get_session") else None
        pulse = manager.get_latest_pulse(chat_id) if manager and hasattr(manager, "get_latest_pulse") else None
        decision = manager.get_latest_impulse_decision(chat_id) if manager and hasattr(manager, "get_latest_impulse_decision") else None
        action = manager.get_latest_action_decision(chat_id) if manager and hasattr(manager, "get_latest_action_decision") else None
        return {
            "status": "ok",
            "data": {
                "state": self._as_dict(state),
                "session": self._as_dict(session),
                "latest_pulse": self._as_dict(pulse),
                "latest_impulse_decision": self._as_dict(decision),
                "latest_action_decision": self._as_dict(action),
            },
            "runtime_bound": manager is not None,
        }

    async def heartflow_impulses(self, chat_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        manager = self._heartflow_manager(self._runtime())
        if not manager or not hasattr(manager, "list_impulse_decisions"):
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": False}
        items = [self._as_dict(item) for item in manager.list_impulse_decisions(chat_id=chat_id, limit=limit)]
        return {"status": "ok", "items": items, "total": len(items), "runtime_bound": True}

    async def heartflow_timeline(self, chat_id: str | None = None, limit: int = 80) -> dict[str, Any]:
        manager = self._heartflow_manager(self._runtime())
        if not manager or not hasattr(manager, "list_timeline"):
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": False}
        items = list(manager.list_timeline(chat_id=chat_id, limit=limit))
        return {"status": "ok", "items": items, "total": len(items), "runtime_bound": True}

    async def heartflow_topic_digests(self, limit: int = 50) -> dict[str, Any]:
        service = self._heartflow_topic_digest_service(self._runtime())
        if not service or not hasattr(service, "list_digests"):
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": False}
        items = [self._as_dict(item) for item in service.list_digests(limit=limit)]
        return {"status": "ok", "items": items, "total": len(items), "runtime_bound": True}

    async def heartflow_hidden_context(self, chat_id: str) -> dict[str, Any]:
        manager = self._heartflow_manager(self._runtime())
        context = manager.get_hidden_context(chat_id) if manager and hasattr(manager, "get_hidden_context") else ""
        return {"status": "ok", "data": {"chat_id": chat_id, "hidden_context": context}, "runtime_bound": manager is not None}

    async def clear_heartflow_cooldowns(self, chat_id: str) -> dict[str, Any]:
        manager = self._heartflow_manager(self._runtime())
        if not manager:
            return {"status": "ok", "changed": False, "runtime_bound": False}
        getattr(manager, "_pulses_by_chat", {}).pop(chat_id, None)
        getattr(manager, "_impulse_decisions_by_chat", {}).pop(chat_id, None)
        getattr(manager, "_action_decisions_by_chat", {}).pop(chat_id, None)
        state = manager.get_state(chat_id) if hasattr(manager, "get_state") else None
        if state:
            state.cooldown_tags = []
        session = manager.get_session(chat_id) if hasattr(manager, "get_session") else None
        if session:
            session.consecutive_observe_count = 0
            session.consecutive_no_reply_count = 0
            session.consecutive_prepare_count = 0
        return {"status": "ok", "changed": True, "runtime_bound": True}

    async def recent_decisions(self, chat_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        planner = self._planner(self._runtime())
        items = list(getattr(planner, "cognitive_decision_history", []) or [])
        if chat_id:
            items = [item for item in items if str(item.get("chat_id", "")) == chat_id]
        items = items[-max(1, min(limit, 300)) :][::-1]
        return {"status": "ok", "items": items, "total": len(items), "runtime_bound": planner is not None}

    async def recent_turn_traces(self, chat_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        planner = self._planner(self._runtime())
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
        return {"status": "ok", "items": items, "total": len(items), "runtime_bound": planner is not None}

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
        planner = self._planner(self._runtime())
        items = list(getattr(planner, "tool_trace_history", []) or [])
        if chat_id:
            items = [item for item in items if str(item.get("chat_id", "")) == chat_id]
        items = items[-max(1, min(limit, 300)) :][::-1]
        return {"status": "ok", "items": items, "total": len(items), "runtime_bound": planner is not None}

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
        runtime = self._runtime()
        engine = getattr(runtime, "memory_engine", None) if runtime else None
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
        runtime = self._runtime()
        engine = getattr(runtime, "memory_engine", None) if runtime else None
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
        task = self._proactive_task(self._runtime())
        data = task.describe_status() if task and hasattr(task, "describe_status") else {"running": False}
        return {"status": "ok", "data": data, "runtime_bound": task is not None}

    async def scheduler_status_view(self) -> dict[str, Any]:
        runtime = self._runtime()
        task = self._proactive_task(runtime)
        kernel = self._chat_loop_kernel(runtime)
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
        runtime = self._runtime()
        task = self._proactive_task(runtime)
        kernel = self._chat_loop_kernel(runtime)
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
        runtime = self._runtime()
        kernel = self._chat_loop_kernel(runtime)
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
        task = self._proactive_task(self._runtime())
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
        task = self._proactive_task(self._runtime())
        scheduler = getattr(task, "dream_scheduler", None) if task else None
        data = scheduler.describe_status() if scheduler and hasattr(scheduler, "describe_status") else {"dream_agent_bound": False}
        return {"status": "ok", "data": data, "runtime_bound": scheduler is not None}

    async def run_dream_once(self) -> dict[str, Any]:
        task = self._proactive_task(self._runtime())
        scheduler = getattr(task, "dream_scheduler", None) if task else None
        if not scheduler or not getattr(scheduler, "dream_agent", None) or not getattr(scheduler, "dream_generator", None):
            return {"status": "error", "message": "Dream dependencies are not bound", "runtime_bound": scheduler is not None}
        asyncio.create_task(scheduler.run_once())
        return {"status": "ok", "scheduled": True, "runtime_bound": True}

    async def diary_status(self) -> dict[str, Any]:
        task = self._proactive_task(self._runtime())
        return {
            "status": "ok",
            "data": {
                "available": bool(task and getattr(task, "diary_service", None)),
                "last_diary_date": getattr(task, "_last_diary_date", "") if task else "",
            },
            "runtime_bound": task is not None,
        }

    async def run_diary_once(self) -> dict[str, Any]:
        runtime = self._runtime()
        task = self._proactive_task(runtime)
        service = getattr(task, "diary_service", None) if task else None
        if not service or not getattr(runtime, "state_engine", None):
            return {"status": "error", "message": "Diary dependencies are not bound", "runtime_bound": task is not None}
        asyncio.create_task(service.run_once(runtime.state_engine.get_active_states()))
        return {"status": "ok", "scheduled": True, "runtime_bound": True}

    async def wakeup_status(self) -> dict[str, Any]:
        runtime = self._runtime()
        task = self._proactive_task(runtime)
        service = getattr(task, "wakeup_service", None) if task else None
        return {
            "status": "ok",
            "data": {
                "available": service is not None,
                "silence_threshold": getattr(getattr(getattr(runtime, "config", None), "life", None), "silence_threshold", 0),
                "wakeup_cooldown": getattr(getattr(getattr(runtime, "config", None), "life", None), "wakeup_cooldown", 0),
            },
            "runtime_bound": runtime is not None,
        }

    async def learning_status(self) -> dict[str, Any]:
        runtime = self._runtime()
        return {
            "status": "ok",
            "data": {
                "reflector": getattr(runtime, "reflector", None) is not None if runtime else False,
                "reflect_tracker": getattr(runtime, "reflect_tracker", None) is not None if runtime else False,
                "auto_check_task": getattr(runtime, "auto_check_task", None) is not None if runtime else False,
            },
            "runtime_bound": runtime is not None,
        }

    async def expression_stats(self) -> dict[str, Any]:
        stats = await self._expression_pattern_stats()
        return {
            "status": "ok",
            "data": stats,
        }

    async def expression_cooldowns(self) -> dict[str, Any]:
        runtime = self._runtime()
        planner = self._planner(runtime)
        selector = getattr(planner, "expression_selector", None) if planner else None
        return {
            "status": "ok",
            "data": {
                "recent_patterns": self._as_dict(selector).get("_recent_patterns", {}) if selector else {},
            },
            "runtime_bound": selector is not None,
        }

    async def run_reflect_once(self, chat_id: str) -> dict[str, Any]:
        runtime = self._runtime()
        reflector = getattr(runtime, "reflector", None) if runtime else None
        if not reflector:
            return {"status": "error", "message": "Reflector is not bound", "runtime_bound": runtime is not None}
        if hasattr(reflector, "reflect_batch"):
            await reflector.reflect_batch(chat_id)
        if hasattr(reflector, "auto_audit"):
            await reflector.auto_audit(chat_id)
        auto_check = getattr(runtime, "auto_check_task", None)
        if auto_check and hasattr(auto_check, "run_once"):
            await auto_check.run_once(chat_id)
        return {"status": "ok", "runtime_bound": True}

    async def active_chats(self, max_age_seconds: float = 1800) -> dict[str, Any]:
        runtime = self._runtime()
        coordinator = getattr(runtime, "runtime_coordinator", None) if runtime else None
        if not coordinator or not hasattr(coordinator, "list_active_chats"):
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": False}
        chat_ids = await coordinator.list_active_chats(max_age_seconds)
        return {"status": "ok", "items": chat_ids, "total": len(chat_ids), "runtime_bound": True}

    async def chat_activity(self, chat_id: str) -> dict[str, Any]:
        runtime = self._runtime()
        coordinator = getattr(runtime, "runtime_coordinator", None) if runtime else None
        data = await coordinator.get_activity_snapshot(chat_id) if coordinator and hasattr(coordinator, "get_activity_snapshot") else {}
        return {"status": "ok", "data": data, "runtime_bound": coordinator is not None}

    async def chat_runtime(self, chat_id: str) -> dict[str, Any]:
        activity = await self.chat_activity(chat_id)
        data = dict(activity.get("data", {}) or {})
        runtime = self._runtime()
        coordinator = getattr(runtime, "runtime_coordinator", None) if runtime else None
        if coordinator and hasattr(coordinator, "get_wait_target_name"):
            data["wait_target_name"] = await coordinator.get_wait_target_name(chat_id)
        return {"status": "ok", "data": data, "runtime_bound": coordinator is not None}

    async def clear_chat_runtime(self, chat_id: str) -> dict[str, Any]:
        runtime = self._runtime()
        coordinator = getattr(runtime, "runtime_coordinator", None) if runtime else None
        changed = False
        if coordinator and hasattr(coordinator, "clear_runtime_state"):
            changed = await coordinator.clear_runtime_state(chat_id)
        manager = self._heartflow_manager(runtime)
        if manager:
            getattr(manager, "_pulses_by_chat", {}).pop(chat_id, None)
            getattr(manager, "_impulse_decisions_by_chat", {}).pop(chat_id, None)
            state = manager.get_state(chat_id) if hasattr(manager, "get_state") else None
            if state:
                state.cooldown_tags = []
                changed = True
        return {"status": "ok", "changed": changed, "runtime_bound": runtime is not None}


__all__ = ["AdminUiService"]
