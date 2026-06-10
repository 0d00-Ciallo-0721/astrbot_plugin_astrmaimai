from __future__ import annotations
from typing import Any

from ..adapters.plugin_api import PluginApiAdapter


class CognitionService:
    def __init__(self, plugin_api: PluginApiAdapter):
        self._api = plugin_api

    async def recent_decisions(self, chat_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).recent_decisions(chat_id=chat_id, limit=limit)

    async def recent_turn_traces(self, chat_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).recent_turn_traces(chat_id=chat_id, limit=limit)

    async def recent_tool_traces(self, chat_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).recent_tool_traces(chat_id=chat_id, limit=limit)

    async def chat_trace_events(self, chat_id: str, limit: int = 80) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).chat_trace_events(chat_id, limit=limit)

    async def cognition_unified_timeline(
        self,
        *,
        chat_id: str,
        limit: int = 80,
        include: list[str] | None = None,
        level: str = "",
    ) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).cognition_unified_timeline(
            chat_id=chat_id,
            limit=limit,
            include=include,
            level=level,
        )

    async def observability_overview(self) -> dict[str, Any]:
        from .observabilityservice import ObservabilityService
        return await ObservabilityService(self._api).observability_overview()

    async def observability_timeline(
        self,
        *,
        chat_id: str | None = None,
        domains: list[str] | None = None,
        levels: list[str] | None = None,
        kinds: list[str] | None = None,
        limit: int = 80,
    ) -> dict[str, Any]:
        from .observabilityservice import ObservabilityService
        return await ObservabilityService(self._api).observability_timeline(
            chat_id=chat_id,
            domains=domains,
            levels=levels,
            kinds=kinds,
            limit=limit,
        )

    async def observability_chat(self, chat_id: str) -> dict[str, Any]:
        from .observabilityservice import ObservabilityService
        return await ObservabilityService(self._api).observability_chat(chat_id)

    async def observability_errors(self, chat_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        from .observabilityservice import ObservabilityService
        return await ObservabilityService(self._api).observability_errors(chat_id=chat_id, limit=limit)

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
        from .observabilityservice import ObservabilityService
        return await ObservabilityService(self._api).observability_search(
            q=q,
            chat_id=chat_id,
            domains=domains,
            kinds=kinds,
            levels=levels,
            tags=tags,
            limit=limit,
        )

    async def context_economy_overview_view(self, limit: int = 20) -> dict[str, Any]:
        from .observabilityservice import ObservabilityService
        return await ObservabilityService(self._api).context_economy_overview_view(limit=limit)

    async def context_economy_templates_view(
        self,
        limit: int = 50,
        template_id: str | None = None,
        workload_family: str | None = None,
        sort_by: str = "rotate",
        sort_dir: str | None = None,
    ) -> dict[str, Any]:
        from .observabilityservice import ObservabilityService
        return await ObservabilityService(self._api).context_economy_templates_view(
            limit=limit,
            template_id=template_id,
            workload_family=workload_family,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )

    async def scheduler_status_view(self) -> dict[str, Any]:
        from .schedulerservice import SchedulerService
        return await SchedulerService(self._api).scheduler_status_view()

    async def scheduler_due_selection_view(self) -> dict[str, Any]:
        from .schedulerservice import SchedulerService
        return await SchedulerService(self._api).scheduler_due_selection_view()

    async def scheduler_chat_view(self, chat_id: str) -> dict[str, Any]:
        from .schedulerservice import SchedulerService
        return await SchedulerService(self._api).scheduler_chat_view(chat_id)


__all__ = ["CognitionService"]
