from __future__ import annotations
from typing import Any

from ..adapters.plugin_api import PluginApiAdapter


class SchedulerService:
    def __init__(self, plugin_api: PluginApiAdapter):
        self.plugin_api = plugin_api

    async def scheduler_status_view(self) -> dict[str, Any]:
        kernel = self.plugin_api.get_chat_loop_kernel()
        if not kernel or not hasattr(kernel, "describe_scheduler_status"):
            return {"status": "ok", "data": {}, "runtime_bound": False}
        return {"status": "ok", "data": kernel.describe_scheduler_status(), "runtime_bound": True}

    async def scheduler_due_selection_view(self) -> dict[str, Any]:
        kernel = self.plugin_api.get_chat_loop_kernel()
        if not kernel or not hasattr(kernel, "describe_due_selection"):
            return {"status": "ok", "data": {}, "runtime_bound": False}
        return {"status": "ok", "data": kernel.describe_due_selection(), "runtime_bound": True}

    async def scheduler_chat_view(self, chat_id: str) -> dict[str, Any]:
        kernel = self.plugin_api.get_chat_loop_kernel()
        if not kernel or not hasattr(kernel, "describe_chat_scheduler_state"):
            return {"status": "ok", "data": {}, "runtime_bound": False}
        return {"status": "ok", "data": kernel.describe_chat_scheduler_state(chat_id), "runtime_bound": True}


__all__ = ["SchedulerService"]
