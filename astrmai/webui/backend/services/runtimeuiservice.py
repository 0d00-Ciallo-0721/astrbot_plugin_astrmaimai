from __future__ import annotations

from typing import Any


class RuntimeUiService:
    def __init__(self, plugin_api):
        self.plugin_api = plugin_api

    async def runtime_status(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "data": await self.plugin_api.get_runtime_diagnostics(),
            "runtime_bound": self.plugin_api.facade is not None,
        }

    async def runtime_capabilities(self) -> dict[str, Any]:
        capabilities = await self.plugin_api.get_capability_overview()
        return {"status": "ok", "data": capabilities, "runtime_bound": self.plugin_api.facade is not None}

    async def runtime_models(self) -> dict[str, Any]:
        diagnostics = await self.plugin_api.get_runtime_diagnostics()
        return {"status": "ok", "data": diagnostics.get("models", {}), "runtime_bound": self.plugin_api.facade is not None}

    async def runtime_health(self, expression_stats: dict[str, int]) -> dict[str, Any]:
        diagnostics = await self.plugin_api.get_runtime_diagnostics()
        status = diagnostics.get("status", {}) if isinstance(diagnostics, dict) else {}
        active_chats = 0
        coordinator = self.plugin_api.get_runtime_coordinator()
        if coordinator and hasattr(coordinator, "list_active_chats"):
            try:
                active_chats = len(await coordinator.list_active_chats(1800))
            except Exception:
                active_chats = 0
        return {
            "status": "ok",
            "data": {
                "running": bool(status.get("lifecycle_started", False)),
                "boot_phase": status.get("boot_phase", ""),
                "degraded_count": len(status.get("degraded_components", {}) or {}),
                "active_chat_count": int(active_chats),
                "expression_patterns": dict(expression_stats or {}),
            },
            "runtime_bound": self.plugin_api.facade is not None,
        }
