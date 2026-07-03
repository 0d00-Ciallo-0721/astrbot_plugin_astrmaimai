from __future__ import annotations

import inspect
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from astrbot.api import logger

try:  # pragma: no cover - Quart is supplied by AstrBot at runtime.
    from quart import request as quart_request
except Exception:  # pragma: no cover
    quart_request = None

from .backend.adapters.plugin_api import PluginApiAdapter, set_active_facade
from .backend.db import get_db
from .backend.services.admin_ui_service import AdminUiService
from .backend.services.dashboard_service import DashboardService
from .backend.services.memory_ui_service import MemoryUiService
from .backend.services.persona_ui_service import PersonaUiService
from .backend.services.review_ui_service import ReviewUiService
from .backend.services.user_ui_service import UserUiService


PLUGIN_API_PREFIX = "/astrmai/admin"


def _maybe_await(value: Any) -> Awaitable[Any]:
    if inspect.isawaitable(value):
        return value

    async def _wrap() -> Any:
        return value

    return _wrap()


def _make_page_request(path_params: dict[str, Any] | None = None) -> Any:
    query_params: dict[str, Any] = {}
    request_obj = None
    if quart_request is not None:
        try:
            query_params = dict(getattr(quart_request, "args", {}) or {})
            request_obj = quart_request
        except Exception:
            query_params = {}
            request_obj = None
    return SimpleNamespace(
        path_params=dict(path_params or {}),
        query_params=query_params,
        json=(request_obj.get_json if request_obj is not None and hasattr(request_obj, "get_json") else None),
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def _page_handler(handler: Callable[[Any], Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    async def _wrapped(*_args: Any, **path_values: Any) -> Any:
        return _json_safe(await handler(_make_page_request(path_values)))

    return _wrapped


def _werkzeug_path_alias(path: str) -> str:
    return re.sub(r"\{([^{}]+)\}", r"<\1>", path)


class AstrMaiAdminPageApi:
    def __init__(self, facade: Any):
        self.plugin_api = PluginApiAdapter(facade=facade)

    @staticmethod
    def _query(request: Any) -> dict[str, Any]:
        query_params = getattr(request, "query_params", {}) or {}
        try:
            return dict(query_params)
        except Exception:
            return {}

    @staticmethod
    def _path(request: Any) -> dict[str, Any]:
        path_params = getattr(request, "path_params", {}) or {}
        try:
            return dict(path_params)
        except Exception:
            return {}

    @staticmethod
    async def _body(request: Any) -> dict[str, Any]:
        if isinstance(request, dict):
            return dict(request)
        json_method = getattr(request, "json", None)
        if callable(json_method):
            try:
                data = await _maybe_await(json_method())
                return data if isinstance(data, dict) else {}
            except Exception as exc:
                logger.warning(f"[AstrMai] _body parse failed: {exc}")
                return {}
        return {}

    @staticmethod
    def _int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _strings(value: Any) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, (list, tuple, set)) else [value]
        return [str(item).strip() for item in values if item is not None and str(item).strip()]

    def _admin(self) -> AdminUiService:
        return AdminUiService(self.plugin_api, get_db)

    def _reviews(self) -> ReviewUiService:
        return ReviewUiService(self.plugin_api, get_db)

    def _memory(self) -> MemoryUiService:
        return MemoryUiService(get_db, self.plugin_api)

    async def dashboard(self, request: Any) -> dict[str, Any]:
        return await DashboardService(self.plugin_api, get_db).get_snapshot()

    async def runtime_status(self, request: Any) -> dict[str, Any]:
        return await self._admin().runtime_status()

    async def runtime_capabilities(self, request: Any) -> dict[str, Any]:
        return await self._admin().runtime_capabilities()

    async def runtime_models(self, request: Any) -> dict[str, Any]:
        return await self._admin().runtime_models()

    async def runtime_health(self, request: Any) -> dict[str, Any]:
        return await self._admin().runtime_health()

    async def tools_status(self, request: Any) -> dict[str, Any]:
        return await self._admin().tools_status()

    async def tools_policy(self, request: Any) -> dict[str, Any]:
        return await self._admin().tools_policy()

    async def recent_tool_calls(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        return await self._admin().recent_tool_traces(limit=self._int(query.get("limit"), 50))

    async def chat_recent_tool_calls(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        path = self._path(request)
        return await self._admin().recent_tool_traces(
            chat_id=str(path.get("chat_id", "")),
            limit=self._int(query.get("limit"), 50),
        )

    async def recent_decisions(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        return await self._admin().recent_decisions(limit=self._int(query.get("limit"), 50))

    async def chat_recent_decisions(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        path = self._path(request)
        return await self._admin().recent_decisions(
            chat_id=str(path.get("chat_id", "")),
            limit=self._int(query.get("limit"), 50),
        )

    async def recent_turns(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        return await self._admin().recent_turn_traces(limit=self._int(query.get("limit"), 50))

    async def chat_recent_turns(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        path = self._path(request)
        return await self._admin().recent_turn_traces(
            chat_id=str(path.get("chat_id", "")),
            limit=self._int(query.get("limit"), 50),
        )

    async def chat_trace_events(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        path = self._path(request)
        return await self._admin().chat_trace_events(
            chat_id=str(path.get("chat_id", "")),
            limit=self._int(query.get("limit"), 80),
        )

    async def chat_unified_timeline(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        path = self._path(request)
        include = [item.strip() for item in str(query.get("include") or "").split(",") if item.strip()]
        return await self._admin().cognition_unified_timeline(
            chat_id=str(path.get("chat_id", "")),
            limit=self._int(query.get("limit"), 80),
            level=str(query.get("level", "") or ""),
            include=include,
        )

    async def observability_overview(self, request: Any) -> dict[str, Any]:
        return await self._admin().observability_overview()

    async def observability_timeline(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        return await self._admin().observability_timeline(
            chat_id=str(query.get("chat_id", "") or "") or None,
            domains=[item.strip() for item in str(query.get("domains") or "").split(",") if item.strip()],
            levels=[item.strip() for item in str(query.get("levels") or "").split(",") if item.strip()],
            kinds=[item.strip() for item in str(query.get("kinds") or "").split(",") if item.strip()],
            limit=self._int(query.get("limit"), 80),
        )

    async def observability_chat(self, request: Any) -> dict[str, Any]:
        return await self._admin().observability_chat(str(self._path(request).get("chat_id", "")))

    async def observability_errors(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        return await self._admin().observability_errors(
            chat_id=str(query.get("chat_id", "") or "") or None,
            limit=self._int(query.get("limit"), 50),
        )

    async def observability_search(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        return await self._admin().observability_search(
            q=str(query.get("q", "") or ""),
            chat_id=str(query.get("chat_id", "") or ""),
            domains=[item.strip() for item in str(query.get("domains") or "").split(",") if item.strip()],
            kinds=[item.strip() for item in str(query.get("kinds") or "").split(",") if item.strip()],
            levels=[item.strip() for item in str(query.get("levels") or "").split(",") if item.strip()],
            tags=[item.strip() for item in str(query.get("tags") or "").split(",") if item.strip()],
            limit=self._int(query.get("limit"), 80),
        )

    async def scheduler_status(self, request: Any) -> dict[str, Any]:
        return await self._admin().scheduler_status_view()

    async def scheduler_due_selection(self, request: Any) -> dict[str, Any]:
        return await self._admin().scheduler_due_selection_view()

    async def scheduler_chat(self, request: Any) -> dict[str, Any]:
        return await self._admin().scheduler_chat_view(str(self._path(request).get("chat_id", "")))

    async def heartflow_status(self, request: Any) -> dict[str, Any]:
        return await self._admin().heartflow_status()

    async def heartflow_chats(self, request: Any) -> dict[str, Any]:
        return await self._admin().heartflow_chats()

    async def heartflow_impulses(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        return await self._admin().heartflow_impulses(limit=self._int(query.get("limit"), 50))

    async def heartflow_chat(self, request: Any) -> dict[str, Any]:
        return await self._admin().heartflow_chat(str(self._path(request).get("chat_id", "")))

    async def heartflow_chat_impulses(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        return await self._admin().heartflow_impulses(
            chat_id=str(self._path(request).get("chat_id", "")),
            limit=self._int(query.get("limit"), 20),
        )

    async def heartflow_timeline(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        return await self._admin().heartflow_timeline(limit=self._int(query.get("limit"), 80))

    async def heartflow_chat_timeline(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        return await self._admin().heartflow_timeline(
            chat_id=str(self._path(request).get("chat_id", "")),
            limit=self._int(query.get("limit"), 50),
        )

    async def heartflow_topic_digests(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        return await self._admin().heartflow_topic_digests(limit=self._int(query.get("limit"), 50))

    async def heartflow_hidden_context(self, request: Any) -> dict[str, Any]:
        return await self._admin().heartflow_hidden_context(str(self._path(request).get("chat_id", "")))

    async def clear_heartflow_cooldowns(self, request: Any) -> dict[str, Any]:
        return await self._admin().clear_heartflow_cooldowns(str(self._path(request).get("chat_id", "")))

    async def learning_status(self, request: Any) -> dict[str, Any]:
        return await self._admin().learning_status()

    async def expression_stats(self, request: Any) -> dict[str, Any]:
        return await self._admin().expression_stats()

    async def expression_cooldowns(self, request: Any) -> dict[str, Any]:
        return await self._admin().expression_cooldowns()

    async def run_reflect_once(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        body = await self._body(request)
        return await self._admin().run_reflect_once(str(body.get("chat_id") or query.get("chat_id") or ""))

    async def proactive_status(self, request: Any) -> dict[str, Any]:
        return await self._admin().proactive_status()

    async def proactive_intents(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        return await self._admin().proactive_intents(limit=self._int(query.get("limit"), 50))

    async def dream_status(self, request: Any) -> dict[str, Any]:
        return await self._admin().dream_status()

    async def run_dream_once(self, request: Any) -> dict[str, Any]:
        return await self._admin().run_dream_once()

    async def diary_status(self, request: Any) -> dict[str, Any]:
        return await self._admin().diary_status()

    async def run_diary_once(self, request: Any) -> dict[str, Any]:
        return await self._admin().run_diary_once()

    async def wakeup_status(self, request: Any) -> dict[str, Any]:
        return await self._admin().wakeup_status()

    async def list_memory_feedback(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        return await self._admin().list_memory_feedback(
            chat_id=query.get("chat_id"),
            source=query.get("source"),
            limit=self._int(query.get("limit"), 50),
        )

    async def memory_feedback_sources(self, request: Any) -> dict[str, Any]:
        return await self._admin().memory_feedback_sources()

    async def disable_memory_feedback(self, request: Any) -> dict[str, Any]:
        return await self._admin().disable_memory_feedback(str(self._path(request).get("feedback_id", "")))

    async def active_chats(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        return await self._admin().active_chats(max_age_seconds=self._float(query.get("max_age_seconds"), 1800.0))

    async def chat_activity(self, request: Any) -> dict[str, Any]:
        return await self._admin().chat_activity(str(self._path(request).get("chat_id", "")))

    async def chat_runtime(self, request: Any) -> dict[str, Any]:
        return await self._admin().chat_runtime(str(self._path(request).get("chat_id", "")))

    async def clear_chat_runtime(self, request: Any) -> dict[str, Any]:
        return await self._admin().clear_chat_runtime(str(self._path(request).get("chat_id", "")))

    async def pending_reviews(self, request: Any) -> Any:
        return await self._reviews().list_pending()

    async def reviews(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        return await self._reviews().list_reviews(
            status=query.get("status") or None,
            group_id=query.get("group_id") or None,
            keyword=query.get("keyword") or None,
            page=self._int(query.get("page"), 1),
            page_size=self._int(query.get("page_size"), 20),
        )

    async def submit_review(self, request: Any) -> dict[str, Any]:
        body = await self._body(request)
        return await self._reviews().submit_review(
            str(self._path(request).get("id", "") or ""),
            str(body.get("action", "")),
            body.get("replacement"),
            body.get("weight"),
            body.get("reason"),
        )

    async def batch_review(self, request: Any) -> dict[str, Any]:
        body = await self._body(request)
        ids = self._strings(body.get("ids"))
        return await self._reviews().batch_review(ids, str(body.get("action", "")))

    async def create_review(self, request: Any) -> dict[str, Any]:
        return await self._reviews().create_review(await self._body(request))

    async def update_review(self, request: Any) -> dict[str, Any]:
        return await self._reviews().update_review_record(
            str(self._path(request).get("id", "") or ""),
            await self._body(request),
        )

    async def delete_review(self, request: Any) -> dict[str, Any]:
        return await self._reviews().delete_review_record(str(self._path(request).get("id", "") or ""))

    async def list_memory_events(self, request: Any) -> Any:
        return await self._memory().list_events()

    async def list_canonical_memories(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        return await self._memory().list_canonical(
            session_id=str(query.get("session_id", "") or ""),
            persona_id=str(query.get("persona_id", "") or ""),
            kind=str(query.get("kind", "") or ""),
            status=str(query.get("status", "") or ""),
            limit=self._int(query.get("limit"), 100),
            offset=self._int(query.get("offset"), 0),
        )

    async def canonical_memory(self, request: Any) -> dict[str, Any]:
        return await self._memory().get_canonical(str(self._path(request).get("memory_id", "")))

    async def delete_canonical_memory(self, request: Any) -> dict[str, Any]:
        return await self._memory().delete_canonical(str(self._path(request).get("memory_id", "")))

    async def restore_canonical_memory(self, request: Any) -> dict[str, Any]:
        return await self._memory().restore_canonical(str(self._path(request).get("memory_id", "")))

    async def stale_canonical_memory(self, request: Any) -> dict[str, Any]:
        return await self._memory().mark_canonical_stale(str(self._path(request).get("memory_id", "")))

    async def merge_canonical_memory(self, request: Any) -> dict[str, Any]:
        body = await self._body(request)
        return await self._memory().merge_canonical(
            str(self._path(request).get("memory_id", "")),
            target_id=str(body.get("target_id", "") or ""),
        )

    async def memory_migration_report(self, request: Any) -> dict[str, Any]:
        return await self._memory().migration_report()

    async def memory_migration_dry_run(self, request: Any) -> dict[str, Any]:
        body = await self._body(request)
        return await self._memory().migration_dry_run(sources=self._strings(body.get("import_sources")))

    async def memory_migration_execute(self, request: Any) -> dict[str, Any]:
        body = await self._body(request)
        return await self._memory().migration_execute(sources=self._strings(body.get("import_sources")))

    async def memory_migration_verify(self, request: Any) -> dict[str, Any]:
        return await self._memory().migration_verify()

    async def memory_migration_repair(self, request: Any) -> dict[str, Any]:
        body = await self._body(request)
        return await self._memory().migration_repair(report=body.get("report"))

    async def memory_index_status(self, request: Any) -> dict[str, Any]:
        return await self._memory().index_status()

    async def repair_memory_index(self, request: Any) -> dict[str, Any]:
        return await self._memory().repair_index()

    async def rebuild_memory_index(self, request: Any) -> dict[str, Any]:
        body = await self._body(request)
        return await self._memory().rebuild_index(session_id=str(body.get("session_id", "") or ""))

    async def run_memory_maintenance(self, request: Any) -> dict[str, Any]:
        return await self._memory().run_maintenance(policy=await self._body(request))

    async def create_memory_event(self, request: Any) -> dict[str, Any]:
        return await self._memory().create_event(await self._body(request))

    async def delete_memory_event(self, request: Any) -> dict[str, Any]:
        return await self._memory().delete_event(self._int(self._path(request).get("id")))

    async def list_reflections(self, request: Any) -> Any:
        month = str(self._query(request).get("month", "") or "")
        return await self._memory().list_reflections(month)

    async def create_reflection(self, request: Any) -> dict[str, Any]:
        return await self._memory().create_reflection(await self._body(request))

    async def update_reflection(self, request: Any) -> dict[str, Any]:
        return await self._memory().update_reflection(str(self._path(request).get("date", "")), await self._body(request))

    async def delete_reflection(self, request: Any) -> dict[str, Any]:
        return await self._memory().delete_reflection(str(self._path(request).get("date", "")))

    async def list_nodes(self, request: Any) -> Any:
        return await self._memory().list_nodes()

    async def create_node(self, request: Any) -> dict[str, Any]:
        return await self._memory().create_node(await self._body(request))

    async def update_node(self, request: Any) -> dict[str, Any]:
        return await self._memory().update_node(self._int(self._path(request).get("id")), await self._body(request))

    async def delete_node(self, request: Any) -> dict[str, Any]:
        return await self._memory().delete_node(self._int(self._path(request).get("id")))

    async def list_jargon(self, request: Any) -> Any:
        query = self._query(request)
        return await self._memory().list_jargon(
            status=str(query.get("status", "") or ""),
            group_id=str(query.get("group_id", "") or ""),
            query=str(query.get("query", "") or ""),
        )

    async def create_jargon(self, request: Any) -> dict[str, Any]:
        return await self._memory().create_jargon(await self._body(request))

    async def update_jargon(self, request: Any) -> dict[str, Any]:
        return await self._memory().update_jargon(str(self._path(request).get("id", "")), await self._body(request))

    async def delete_jargon(self, request: Any) -> dict[str, Any]:
        return await self._memory().delete_jargon(str(self._path(request).get("id", "")))

    async def approve_jargon(self, request: Any) -> dict[str, Any]:
        return await self._memory().approve_jargon(str(self._path(request).get("id", "")))

    async def reject_jargon(self, request: Any) -> dict[str, Any]:
        return await self._memory().reject_jargon(str(self._path(request).get("id", "")))

    async def users(self, request: Any) -> Any:
        return await UserUiService(get_db).list_users()

    async def user(self, request: Any) -> dict[str, Any]:
        record = await UserUiService(get_db).get_user(str(self._path(request).get("user_id", "")))
        return record or {"status": "error", "message": "User not found"}

    async def update_user(self, request: Any) -> dict[str, Any]:
        return await UserUiService(get_db).update_user(str(self._path(request).get("user_id", "")), await self._body(request))

    async def delete_user(self, request: Any) -> dict[str, Any]:
        return await UserUiService(get_db).delete_user(str(self._path(request).get("user_id", "")))

    async def add_user_slice(self, request: Any) -> dict[str, Any]:
        body = await self._body(request)
        result = await UserUiService(get_db).add_slice(
            str(self._path(request).get("user_id", "")),
            str(body.get("type", "")),
            str(body.get("content", "")),
        )
        return result or {"status": "error", "message": "User not found"}

    async def update_user_slice(self, request: Any) -> dict[str, Any]:
        body = await self._body(request)
        result = await UserUiService(get_db).update_slice(
            str(self._path(request).get("user_id", "")),
            self._int(self._path(request).get("index")),
            str(body.get("type", "")),
            str(body.get("content", "")),
        )
        return result or {"status": "error", "message": "User not found"}

    async def delete_user_slice(self, request: Any) -> dict[str, Any]:
        query = self._query(request)
        result = await UserUiService(get_db).delete_slice(
            str(self._path(request).get("user_id", "")),
            self._int(self._path(request).get("index")),
            str(query.get("type", "")),
        )
        return result or {"status": "error", "message": "User not found"}

    async def delete_user_slice_post(self, request: Any) -> dict[str, Any]:
        body = await self._body(request)
        result = await UserUiService(get_db).delete_slice(
            str(self._path(request).get("user_id", "")),
            self._int(self._path(request).get("index")),
            str(body.get("type", "")),
        )
        return result or {"status": "error", "message": "User not found"}

    async def persona_slices(self, request: Any) -> dict[str, Any]:
        return await PersonaUiService(self.plugin_api).get_persona_slices()


def register_astrmai_admin_pages(context: Any, facade: Any) -> None:
    """Register ~85 admin API endpoints under /astrmai/admin.

    Security model: all endpoints rely on AstrBot Plugin Page isolation
    (iframe sandbox + SAMEORIGIN + CSP headers).  No standalone auth
    middleware is implemented — access is gated by the AstrBot WebUI
    admin panel login.
    """
    if not hasattr(context, "register_web_api"):
        return

    set_active_facade(facade)
    api = AstrMaiAdminPageApi(facade)

    routes: list[tuple[str, str, Callable[[Any], Awaitable[Any]], str]] = [
        ("GET", "/heartflow/impulses", api.heartflow_impulses, "AstrMai Heartflow impulse safety decisions"),
        ("GET", "/memories/canonical", api.list_canonical_memories, "AstrMai canonical memories"),
        ("GET", "/memories/canonical/{memory_id}", api.canonical_memory, "AstrMai canonical memory detail"),
        ("POST", "/memories/canonical/{memory_id}/restore", api.restore_canonical_memory, "AstrMai restore canonical memory"),
        ("POST", "/memories/canonical/{memory_id}/stale", api.stale_canonical_memory, "AstrMai mark canonical memory stale"),
        ("POST", "/memories/canonical/{memory_id}/merge", api.merge_canonical_memory, "AstrMai merge canonical memory"),
        ("POST", "/memories/canonical/{memory_id}/delete", api.delete_canonical_memory, "AstrMai soft delete canonical memory"),
        ("DELETE", "/memories/canonical/{memory_id}", api.delete_canonical_memory, "AstrMai soft delete canonical memory"),
        ("GET", "/memories/diagnostics/migrations", api.memory_migration_report, "AstrMai memory migration report"),
        ("POST", "/memories/migration/dry-run", api.memory_migration_dry_run, "AstrMai memory migration dry run"),
        ("POST", "/memories/migration/execute", api.memory_migration_execute, "AstrMai memory migration execute"),
        ("GET", "/memories/migration/verify", api.memory_migration_verify, "AstrMai memory migration verify"),
        ("POST", "/memories/migration/repair", api.memory_migration_repair, "AstrMai memory migration repair"),
        ("GET", "/memories/diagnostics/index", api.memory_index_status, "AstrMai memory index status"),
        ("POST", "/memories/diagnostics/index/repair", api.repair_memory_index, "AstrMai repair memory index"),
        ("POST", "/memories/index/rebuild", api.rebuild_memory_index, "AstrMai rebuild memory index"),
        ("POST", "/memories/maintenance/run", api.run_memory_maintenance, "AstrMai run memory maintenance"),
        ("GET", "/heartflow/chats/{chat_id}/impulses", api.heartflow_chat_impulses, "AstrMai chat Heartflow impulse safety decisions"),
        ("GET", "/heartflow/timeline", api.heartflow_timeline, "AstrMai Heartflow timeline"),
        ("GET", "/heartflow/chats/{chat_id}/timeline", api.heartflow_chat_timeline, "AstrMai chat Heartflow timeline"),
        ("GET", "/heartflow/topic-digests", api.heartflow_topic_digests, "AstrMai Heartflow topic digests"),
        ("GET", "/dashboard", api.dashboard, "AstrMai dashboard"),
        ("GET", "/runtime/status", api.runtime_status, "AstrMai runtime status"),
        ("GET", "/runtime/capabilities", api.runtime_capabilities, "AstrMai runtime capabilities"),
        ("GET", "/runtime/models", api.runtime_models, "AstrMai runtime models"),
        ("GET", "/runtime/health", api.runtime_health, "AstrMai runtime health"),
        ("GET", "/tools/status", api.tools_status, "AstrMai tools status"),
        ("GET", "/tools/policy", api.tools_policy, "AstrMai tools policy"),
        ("GET", "/tools/recent-calls", api.recent_tool_calls, "AstrMai recent tool calls"),
        ("GET", "/tools/chats/{chat_id}/recent-calls", api.chat_recent_tool_calls, "AstrMai chat tool calls"),
        ("GET", "/cognition/recent-decisions", api.recent_decisions, "AstrMai recent cognition decisions"),
        ("GET", "/cognition/chats/{chat_id}/recent-decisions", api.chat_recent_decisions, "AstrMai chat cognition decisions"),
        ("GET", "/cognition/recent-turns", api.recent_turns, "AstrMai recent turn context traces"),
        ("GET", "/cognition/chats/{chat_id}/turns", api.chat_recent_turns, "AstrMai chat turn context traces"),
        ("GET", "/cognition/chats/{chat_id}/trace-events", api.chat_trace_events, "AstrMai chat raw trace events"),
        ("GET", "/cognition/chats/{chat_id}/unified-timeline", api.chat_unified_timeline, "AstrMai unified cognition timeline"),
        ("GET", "/cognition/observability/overview", api.observability_overview, "AstrMai observability overview"),
        ("GET", "/cognition/observability/timeline", api.observability_timeline, "AstrMai global observability timeline"),
        ("GET", "/cognition/observability/chats/{chat_id}", api.observability_chat, "AstrMai chat observability"),
        ("GET", "/cognition/observability/errors", api.observability_errors, "AstrMai observability errors"),
        ("GET", "/cognition/observability/search", api.observability_search, "AstrMai observability search"),
        ("GET", "/cognition/scheduler/status", api.scheduler_status, "AstrMai scheduler status diagnostics"),
        ("GET", "/cognition/scheduler/due-selection", api.scheduler_due_selection, "AstrMai scheduler due selection diagnostics"),
        ("GET", "/cognition/scheduler/chats/{chat_id}", api.scheduler_chat, "AstrMai scheduler chat loop diagnostics"),
        ("GET", "/heartflow/status", api.heartflow_status, "AstrMai heartflow status"),
        ("GET", "/heartflow/chats", api.heartflow_chats, "AstrMai heartflow chats"),
        ("GET", "/heartflow/chats/{chat_id}", api.heartflow_chat, "AstrMai heartflow chat detail"),
        ("GET", "/heartflow/chats/{chat_id}/hidden-context", api.heartflow_hidden_context, "AstrMai heartflow hidden context"),
        ("POST", "/heartflow/chats/{chat_id}/cooldowns/clear", api.clear_heartflow_cooldowns, "AstrMai clear heartflow cooldowns"),
        ("GET", "/learning/status", api.learning_status, "AstrMai learning status"),
        ("GET", "/learning/expression-stats", api.expression_stats, "AstrMai expression stats"),
        ("GET", "/learning/cooldowns", api.expression_cooldowns, "AstrMai expression cooldowns"),
        ("POST", "/learning/reflect/run-once", api.run_reflect_once, "AstrMai run reflect once"),
        ("GET", "/proactive/status", api.proactive_status, "AstrMai proactive status"),
        ("GET", "/proactive/intents", api.proactive_intents, "AstrMai proactive intents"),
        ("GET", "/proactive/dream/status", api.dream_status, "AstrMai dream status"),
        ("POST", "/proactive/dream/run-once", api.run_dream_once, "AstrMai run dream once"),
        ("GET", "/proactive/diary/status", api.diary_status, "AstrMai diary status"),
        ("POST", "/proactive/diary/run-once", api.run_diary_once, "AstrMai run diary once"),
        ("GET", "/proactive/wakeup/status", api.wakeup_status, "AstrMai wakeup status"),
        ("GET", "/memory-feedback", api.list_memory_feedback, "AstrMai 璁板繂鍙嶉"),
        ("GET", "/memory-feedback/sources", api.memory_feedback_sources, "AstrMai 璁板繂鍙嶉鏉ユ簮"),
        ("POST", "/memory-feedback/{feedback_id}/disable", api.disable_memory_feedback, "AstrMai 绂佺敤璁板繂鍙嶉"),


        ("GET", "/chats/active", api.active_chats, "AstrMai active chats"),
        ("GET", "/chats/{chat_id}/activity", api.chat_activity, "AstrMai chat activity"),
        ("GET", "/chats/{chat_id}/runtime", api.chat_runtime, "AstrMai chat runtime"),
        ("POST", "/chats/{chat_id}/runtime/clear", api.clear_chat_runtime, "AstrMai clear chat runtime"),
        ("GET", "/reviews/pending", api.pending_reviews, "AstrMai pending reviews"),
        ("GET", "/reviews", api.reviews, "AstrMai review list"),
        ("POST", "/reviews/{id}/submit", api.submit_review, "AstrMai submit review"),
        ("POST", "/reviews/batch", api.batch_review, "AstrMai batch review"),
        ("POST", "/reviews", api.create_review, "AstrMai create review"),
        ("PUT", "/reviews/{id}", api.update_review, "AstrMai update review"),
        ("POST", "/reviews/{id}/delete", api.delete_review, "AstrMai delete review"),
        ("DELETE", "/reviews/{id}", api.delete_review, "AstrMai delete review"),
        ("GET", "/memories/events", api.list_memory_events, "AstrMai memory events"),
        ("POST", "/memories/events", api.create_memory_event, "AstrMai create memory event"),
        ("POST", "/memories/events/{id}/delete", api.delete_memory_event, "AstrMai delete memory event"),
        ("DELETE", "/memories/events/{id}", api.delete_memory_event, "AstrMai delete memory event"),
        ("GET", "/memories/reflections", api.list_reflections, "AstrMai memory reflections"),
        ("POST", "/memories/reflections", api.create_reflection, "AstrMai create reflection"),
        ("PUT", "/memories/reflections/{date}", api.update_reflection, "AstrMai update reflection"),
        ("POST", "/memories/reflections/{date}/delete", api.delete_reflection, "AstrMai delete reflection"),
        ("DELETE", "/memories/reflections/{date}", api.delete_reflection, "AstrMai delete reflection"),
        ("GET", "/memories/nodes", api.list_nodes, "AstrMai memory nodes"),
        ("POST", "/memories/nodes", api.create_node, "AstrMai create memory node"),
        ("PUT", "/memories/nodes/{id}", api.update_node, "AstrMai update memory node"),
        ("POST", "/memories/nodes/{id}/delete", api.delete_node, "AstrMai delete memory node"),
        ("DELETE", "/memories/nodes/{id}", api.delete_node, "AstrMai delete memory node"),
        ("GET", "/memories/jargon", api.list_jargon, "AstrMai jargon list"),
        ("POST", "/memories/jargon", api.create_jargon, "AstrMai create jargon"),
        ("POST", "/memories/jargon/{id}/approve", api.approve_jargon, "AstrMai approve jargon"),
        ("POST", "/memories/jargon/{id}/reject", api.reject_jargon, "AstrMai reject jargon"),
        ("PUT", "/memories/jargon/{id}", api.update_jargon, "AstrMai update jargon"),
        ("POST", "/memories/jargon/{id}/delete", api.delete_jargon, "AstrMai delete jargon"),
        ("DELETE", "/memories/jargon/{id}", api.delete_jargon, "AstrMai delete jargon"),
        ("GET", "/users", api.users, "AstrMai users"),
        ("GET", "/users/{user_id}", api.user, "AstrMai user detail"),
        ("POST", "/users/{user_id}", api.update_user, "AstrMai update user"),
        ("PATCH", "/users/{user_id}", api.update_user, "AstrMai update user"),
        ("POST", "/users/{user_id}/delete", api.delete_user, "AstrMai delete user"),
        ("DELETE", "/users/{user_id}", api.delete_user, "AstrMai delete user"),
        ("POST", "/users/{user_id}/slices", api.add_user_slice, "AstrMai add user slice"),
        ("PUT", "/users/{user_id}/slices/{index}", api.update_user_slice, "AstrMai update user slice"),
        ("DELETE", "/users/{user_id}/slices/{index}", api.delete_user_slice, "AstrMai delete user slice"),
        ("POST", "/users/{user_id}/slices/{index}/delete", api.delete_user_slice_post, "AstrMai delete user slice"),
        ("GET", "/persona/slices", api.persona_slices, "AstrMai persona slices"),
    ]

    registered: set[tuple[str, str]] = set()
    for method, path, handler, description in routes:
        for route_path in dict.fromkeys((path, _werkzeug_path_alias(path))):
            full_path = f"{PLUGIN_API_PREFIX}{route_path}"
            key = (method, full_path)
            if key in registered:
                continue
            registered.add(key)
            context.register_web_api(full_path, _page_handler(handler), [method], description)


__all__ = ["AstrMaiAdminPageApi", "PLUGIN_API_PREFIX", "register_astrmai_admin_pages"]
