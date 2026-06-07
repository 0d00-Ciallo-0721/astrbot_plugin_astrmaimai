from __future__ import annotations

from typing import Any


class ContextEconomyUiService:
    def __init__(self, plugin_api):
        self.plugin_api = plugin_api

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

    async def context_economy_overview_view(self, limit: int = 20) -> dict[str, Any]:
        snapshot = self._context_economy_snapshot()
        return {
            "status": "ok",
            "data": {
                "overview": self._context_economy_overview(snapshot),
                "templates": self._context_economy_templates(snapshot, limit=limit),
            },
            "runtime_bound": self.plugin_api.facade is not None,
        }

    async def context_economy_templates_view(
        self,
        limit: int = 50,
        template_id: str | None = None,
        workload_family: str | None = None,
        sort_by: str = "rotate",
        sort_dir: str | None = None,
    ) -> dict[str, Any]:
        snapshot = self._context_economy_snapshot()
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
            "runtime_bound": self.plugin_api.facade is not None,
        }
