from __future__ import annotations

import inspect
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

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
            except Exception:
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
            self._int(self._path(request).get("id")),
            str(body.get("action", "")),
            body.get("replacement"),
            body.get("weight"),
            body.get("reason"),
        )

    async def batch_review(self, request: Any) -> dict[str, Any]:
        body = await self._body(request)
        ids = [self._int(item) for item in body.get("ids", []) if str(item).strip()]
        return await self._reviews().batch_review(ids, str(body.get("action", "")))

    async def create_review(self, request: Any) -> dict[str, Any]:
        return await self._reviews().create_review(await self._body(request))

    async def update_review(self, request: Any) -> dict[str, Any]:
        return await self._reviews().update_review_record(self._int(self._path(request).get("id")), await self._body(request))

    async def delete_review(self, request: Any) -> dict[str, Any]:
        return await self._reviews().delete_review_record(self._int(self._path(request).get("id")))

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
        return await self._memory().migration_dry_run(sources=list(body.get("import_sources") or []))

    async def memory_migration_execute(self, request: Any) -> dict[str, Any]:
        body = await self._body(request)
        return await self._memory().migration_execute(sources=list(body.get("import_sources") or []))

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
        return await self._memory().list_jargon()

    async def create_jargon(self, request: Any) -> dict[str, Any]:
        return await self._memory().create_jargon(await self._body(request))

    async def update_jargon(self, request: Any) -> dict[str, Any]:
        return await self._memory().update_jargon(self._int(self._path(request).get("id")), await self._body(request))

    async def delete_jargon(self, request: Any) -> dict[str, Any]:
        return await self._memory().delete_jargon(self._int(self._path(request).get("id")))

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
        ("GET", "/dashboard", api.dashboard, "AstrMai 管理页仪表盘"),
        ("GET", "/runtime/status", api.runtime_status, "AstrMai 运行状态"),
        ("GET", "/runtime/capabilities", api.runtime_capabilities, "AstrMai 能力矩阵"),
        ("GET", "/runtime/models", api.runtime_models, "AstrMai 模型状态"),
        ("GET", "/runtime/health", api.runtime_health, "AstrMai 健康诊断"),
        ("GET", "/tools/status", api.tools_status, "AstrMai 工具状态"),
        ("GET", "/tools/policy", api.tools_policy, "AstrMai 工具策略"),
        ("GET", "/tools/recent-calls", api.recent_tool_calls, "AstrMai 最近工具调用"),
        ("GET", "/tools/chats/{chat_id}/recent-calls", api.chat_recent_tool_calls, "AstrMai chat 工具调用"),
        ("GET", "/cognition/recent-decisions", api.recent_decisions, "AstrMai 最近认知决策"),
        ("GET", "/cognition/chats/{chat_id}/recent-decisions", api.chat_recent_decisions, "AstrMai chat 认知决策"),
        ("GET", "/cognition/recent-turns", api.recent_turns, "AstrMai recent turn context traces"),
        ("GET", "/cognition/chats/{chat_id}/turns", api.chat_recent_turns, "AstrMai chat turn context traces"),
        ("GET", "/heartflow/status", api.heartflow_status, "AstrMai 心流状态"),
        ("GET", "/heartflow/chats", api.heartflow_chats, "AstrMai 心流 chat 列表"),
        ("GET", "/heartflow/chats/{chat_id}", api.heartflow_chat, "AstrMai 心流 chat 详情"),
        ("GET", "/heartflow/chats/{chat_id}/hidden-context", api.heartflow_hidden_context, "AstrMai 心流隐藏上下文"),
        ("POST", "/heartflow/chats/{chat_id}/cooldowns/clear", api.clear_heartflow_cooldowns, "AstrMai 清理心流冷却"),
        ("GET", "/learning/status", api.learning_status, "AstrMai 学习状态"),
        ("GET", "/learning/expression-stats", api.expression_stats, "AstrMai 表达统计"),
        ("GET", "/learning/cooldowns", api.expression_cooldowns, "AstrMai 表达冷却"),
        ("POST", "/learning/reflect/run-once", api.run_reflect_once, "AstrMai 立即反思"),
        ("GET", "/proactive/status", api.proactive_status, "AstrMai 主动系统状态"),
        ("GET", "/proactive/intents", api.proactive_intents, "AstrMai 主动意图轨迹"),
        ("GET", "/proactive/dream/status", api.dream_status, "AstrMai Dream 状态"),
        ("POST", "/proactive/dream/run-once", api.run_dream_once, "AstrMai 立即 Dream"),
        ("GET", "/proactive/diary/status", api.diary_status, "AstrMai Diary 状态"),
        ("POST", "/proactive/diary/run-once", api.run_diary_once, "AstrMai 立即 Diary"),
        ("GET", "/proactive/wakeup/status", api.wakeup_status, "AstrMai Wakeup 状态"),
        ("GET", "/memory-feedback", api.list_memory_feedback, "AstrMai 记忆反馈"),
        ("GET", "/memory-feedback/sources", api.memory_feedback_sources, "AstrMai 记忆反馈来源"),
        ("POST", "/memory-feedback/{feedback_id}/disable", api.disable_memory_feedback, "AstrMai 禁用记忆反馈"),
        ("DELETE", "/memory-feedback/{feedback_id}", api.disable_memory_feedback, "AstrMai 兼容禁用记忆反馈"),
        ("GET", "/chats/active", api.active_chats, "AstrMai 活跃 chat"),
        ("GET", "/chats/{chat_id}/activity", api.chat_activity, "AstrMai chat 活动"),
        ("GET", "/chats/{chat_id}/runtime", api.chat_runtime, "AstrMai chat 运行态"),
        ("POST", "/chats/{chat_id}/runtime/clear", api.clear_chat_runtime, "AstrMai 清理 chat 运行态"),
        ("GET", "/reviews/pending", api.pending_reviews, "AstrMai 待审核表达"),
        ("GET", "/reviews", api.reviews, "AstrMai 表达审核列表"),
        ("POST", "/reviews/{id}/submit", api.submit_review, "AstrMai 提交表达审核"),
        ("POST", "/reviews/batch", api.batch_review, "AstrMai 批量表达审核"),
        ("POST", "/reviews", api.create_review, "AstrMai 新建表达"),
        ("PUT", "/reviews/{id}", api.update_review, "AstrMai 更新表达"),
        ("POST", "/reviews/{id}/delete", api.delete_review, "AstrMai 删除表达"),
        ("DELETE", "/reviews/{id}", api.delete_review, "AstrMai 删除表达"),
        ("GET", "/memories/events", api.list_memory_events, "AstrMai 记忆事件"),
        ("POST", "/memories/events", api.create_memory_event, "AstrMai 新建记忆事件"),
        ("POST", "/memories/events/{id}/delete", api.delete_memory_event, "AstrMai 删除记忆事件"),
        ("DELETE", "/memories/events/{id}", api.delete_memory_event, "AstrMai 删除记忆事件"),
        ("GET", "/memories/reflections", api.list_reflections, "AstrMai 每日反思"),
        ("POST", "/memories/reflections", api.create_reflection, "AstrMai 新建每日反思"),
        ("PUT", "/memories/reflections/{date}", api.update_reflection, "AstrMai 更新每日反思"),
        ("POST", "/memories/reflections/{date}/delete", api.delete_reflection, "AstrMai 删除每日反思"),
        ("DELETE", "/memories/reflections/{date}", api.delete_reflection, "AstrMai 删除每日反思"),
        ("GET", "/memories/nodes", api.list_nodes, "AstrMai 记忆节点"),
        ("POST", "/memories/nodes", api.create_node, "AstrMai 新建记忆节点"),
        ("PUT", "/memories/nodes/{id}", api.update_node, "AstrMai 更新记忆节点"),
        ("POST", "/memories/nodes/{id}/delete", api.delete_node, "AstrMai 删除记忆节点"),
        ("DELETE", "/memories/nodes/{id}", api.delete_node, "AstrMai 删除记忆节点"),
        ("GET", "/memories/jargon", api.list_jargon, "AstrMai 黑话字典"),
        ("POST", "/memories/jargon", api.create_jargon, "AstrMai 新建黑话"),
        ("PUT", "/memories/jargon/{id}", api.update_jargon, "AstrMai 更新黑话"),
        ("POST", "/memories/jargon/{id}/delete", api.delete_jargon, "AstrMai 删除黑话"),
        ("DELETE", "/memories/jargon/{id}", api.delete_jargon, "AstrMai 删除黑话"),
        ("GET", "/users", api.users, "AstrMai 用户画像"),
        ("GET", "/users/{user_id}", api.user, "AstrMai 用户详情"),
        ("POST", "/users/{user_id}", api.update_user, "AstrMai 更新用户画像"),
        ("PATCH", "/users/{user_id}", api.update_user, "AstrMai 更新用户画像"),
        ("POST", "/users/{user_id}/delete", api.delete_user, "AstrMai 删除用户画像"),
        ("DELETE", "/users/{user_id}", api.delete_user, "AstrMai 删除用户画像"),
        ("POST", "/users/{user_id}/slices", api.add_user_slice, "AstrMai 新增用户切片"),
        ("PUT", "/users/{user_id}/slices/{index}", api.update_user_slice, "AstrMai 更新用户切片"),
        ("DELETE", "/users/{user_id}/slices/{index}", api.delete_user_slice, "AstrMai 删除用户切片"),
        ("POST", "/users/{user_id}/slices/{index}/delete", api.delete_user_slice_post, "AstrMai 删除用户切片"),
        ("GET", "/persona/slices", api.persona_slices, "AstrMai 角色切片诊断"),
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
