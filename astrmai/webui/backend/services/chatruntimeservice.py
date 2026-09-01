from __future__ import annotations

import asyncio
import inspect
from typing import Any

from ..adapters.plugin_api import PluginApiAdapter
from ....shared.helpers.plugin_helpers import safe_create_task


class ChatRuntimeService:
    def __init__(self, plugin_api: PluginApiAdapter):
        self._api = plugin_api

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

    async def proactive_status(self) -> dict[str, Any]:
        task = self._api.get_proactive_task()
        data = task.describe_status() if task and hasattr(task, "describe_status") else {"running": False}
        return {"status": "ok", "data": data, "runtime_bound": task is not None}

    async def proactive_intents(self, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        task = self._api.get_proactive_task()
        dispatcher = getattr(task, "proactive_dispatcher", None) if task else None
        if not dispatcher or not hasattr(dispatcher, "list_intents"):
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": task is not None}
        safe_limit = max(1, min(int(limit or 50), 500))
        if hasattr(dispatcher, "list_intents_page_async"):
            page = await dispatcher.list_intents_page_async(limit=safe_limit, cursor=cursor)
            return {"status": "ok", **dict(page or {}), "runtime_bound": True}
        if hasattr(dispatcher, "list_intents_page"):
            page = dispatcher.list_intents_page(limit=safe_limit, cursor=cursor)
            return {"status": "ok", **dict(page or {}), "runtime_bound": True}
        items = dispatcher.list_intents(limit=min(safe_limit, 200))
        return {"status": "ok", "items": items, "total": len(items), "next_cursor": None, "has_more": False, "runtime_bound": True}

    async def dream_status(self) -> dict[str, Any]:
        task = self._api.get_proactive_task()
        scheduler = getattr(task, "dream_scheduler", None) if task else None
        data = scheduler.describe_status() if scheduler and hasattr(scheduler, "describe_status") else {"dream_agent_bound": False}
        return {"status": "ok", "data": data, "runtime_bound": scheduler is not None}

    async def run_dream_once(self) -> dict[str, Any]:
        task = self._api.get_proactive_task()
        scheduler = getattr(task, "dream_scheduler", None) if task else None
        if not scheduler or not getattr(scheduler, "dream_agent", None) or not getattr(scheduler, "dream_generator", None):
            return {"status": "error", "message": "Dream dependencies are not bound", "runtime_bound": scheduler is not None}
        launcher = getattr(task, "_fire_background_task", None)
        if callable(launcher):
            launcher(
                scheduler.run_once,
                task_name="proactive.dream_manual",
                scope_id="__global__",
            )
        else:
            safe_create_task(scheduler.run_once())
        return {"status": "ok", "scheduled": True, "runtime_bound": True}

    async def diary_status(self) -> dict[str, Any]:
        task = self._api.get_proactive_task()
        return {
            "status": "ok",
            "data": {
                "available": bool(task and getattr(task, "diary_service", None)),
                "last_diary_date": getattr(task, "_last_diary_date", "") if task else "",
            },
            "runtime_bound": task is not None,
        }

    async def run_diary_once(self) -> dict[str, Any]:
        task = self._api.get_proactive_task()
        service = getattr(task, "diary_service", None) if task else None
        state_engine = self._api.get_state_engine()
        if not service or not state_engine:
            return {"status": "error", "message": "Diary dependencies are not bound", "runtime_bound": task is not None}
        launcher = getattr(task, "_fire_background_task", None)
        if callable(launcher):
            launcher(
                lambda: service.run_once(state_engine.get_active_states()),
                task_name="proactive.diary_manual",
                scope_id="__global__",
            )
        else:
            safe_create_task(service.run_once(state_engine.get_active_states()))
        return {"status": "ok", "scheduled": True, "runtime_bound": True}

    async def wakeup_status(self) -> dict[str, Any]:
        task = self._api.get_proactive_task()
        service = getattr(task, "wakeup_service", None) if task else None
        return {
            "status": "ok",
            "data": {
                "available": service is not None,
                "silence_threshold": getattr(getattr(self._api.get_runtime_config(), "life", None), "silence_threshold", 0),
                "wakeup_cooldown": getattr(getattr(self._api.get_runtime_config(), "life", None), "wakeup_cooldown", 0),
            },
            "runtime_bound": self._api.has_bound_facade(),
        }

    async def run_reflect_once(self, chat_id: str) -> dict[str, Any]:
        runner = getattr(self._api, "get_expression_governance_runner", lambda: None)()
        if runner is not None and hasattr(runner, "run_scope_once"):
            await runner.run_scope_once(chat_id, force=True)
            return {"status": "ok", "runtime_bound": True, "forced": True}
        # Compatibility facade for older hosts/tests that have not bound the
        # unified runner yet. Production runtimes use the branch above, so the
        # governance lease remains the single admission path there.
        reflector = getattr(self._api, "get_reflector", lambda: None)()
        auto_check = getattr(self._api, "get_auto_check_task", lambda: None)()
        if reflector is not None:
            reflect_batch = getattr(reflector, "reflect_batch", None)
            auto_audit = getattr(reflector, "auto_audit", None)
            if callable(reflect_batch):
                await reflect_batch(chat_id)
            if callable(auto_audit):
                audit_kwargs = (
                    {"force": True}
                    if "force" in inspect.signature(auto_audit).parameters
                    else {}
                )
                await auto_audit(chat_id, **audit_kwargs)
            run_once = getattr(auto_check, "run_once", None)
            if callable(run_once):
                run_kwargs = (
                    {"force": True}
                    if "force" in inspect.signature(run_once).parameters
                    else {}
                )
                await run_once(chat_id, **run_kwargs)
            return {
                "status": "ok",
                "runtime_bound": True,
                "forced": True,
                "compatibility_path": True,
            }
        return {
            "status": "error",
            "message": "Governance runner is not bound",
            "runtime_bound": self._api.has_bound_facade(),
        }

    async def active_chats(self, max_age_seconds: float = 1800) -> dict[str, Any]:
        coordinator = self._api.get_runtime_coordinator()
        if not coordinator or not hasattr(coordinator, "list_active_chats"):
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": False}
        chat_ids = await coordinator.list_active_chats(max_age_seconds)
        return {"status": "ok", "items": chat_ids, "total": len(chat_ids), "runtime_bound": True}

    async def chat_activity(self, chat_id: str) -> dict[str, Any]:
        coordinator = self._api.get_runtime_coordinator()
        data = await coordinator.get_activity_snapshot(chat_id) if coordinator and hasattr(coordinator, "get_activity_snapshot") else {}
        return {"status": "ok", "data": data, "runtime_bound": coordinator is not None}

    async def chat_runtime(self, chat_id: str) -> dict[str, Any]:
        activity = await self.chat_activity(chat_id)
        data = dict(activity.get("data", {}) or {})
        coordinator = self._api.get_runtime_coordinator()
        if coordinator and hasattr(coordinator, "get_wait_target_name"):
            data["wait_target_name"] = await coordinator.get_wait_target_name(chat_id)
        return {"status": "ok", "data": data, "runtime_bound": coordinator is not None}

    async def clear_chat_runtime(self, chat_id: str) -> dict[str, Any]:
        coordinator = self._api.get_runtime_coordinator()
        changed = False
        if coordinator and hasattr(coordinator, "clear_runtime_state"):
            changed = await coordinator.clear_runtime_state(chat_id)
        manager = self._api.get_heartflow_manager()
        if manager:
            for attr_name in ("_pulses_by_chat", "_impulse_decisions_by_chat"):
                if hasattr(manager, attr_name):
                    private_dict = getattr(manager, attr_name)
                    if isinstance(private_dict, dict):
                        private_dict.pop(chat_id, None)
            state = manager.get_state(chat_id) if hasattr(manager, "get_state") else None
            if state:
                state.cooldown_tags = []
                changed = True
        return {"status": "ok", "changed": changed, "runtime_bound": self._api.has_bound_facade()}

    async def list_memory_feedback(
        self,
        chat_id: str | None = None,
        source: str | None = None,
        limit: int = 30,
        offset: int = 0,
    ) -> dict[str, Any]:
        engine = self._api.get_memory_engine()
        if not engine:
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": False}
        if hasattr(engine, "list_cognitive_feedback_records"):
            page = await engine.list_cognitive_feedback_records(
                session_id=str(chat_id or ""),
                source=str(source or ""),
                limit=limit,
                offset=offset,
            )
            return {"status": "ok", **dict(page or {}), "runtime_bound": True}
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
            item = dict(getattr(signal, "__dict__", {}) or {})
            item["id"] = self._feedback_id(signal)
            items.append(item)
        return {"status": "ok", "items": items, "total": len(items), "runtime_bound": True}

    async def disable_memory_feedback(self, feedback_id: str) -> dict[str, Any]:
        engine = self._api.get_memory_engine()
        if not engine:
            return {"status": "ok", "changed": False, "runtime_bound": False}
        if hasattr(engine, "disable_cognitive_feedback_record") and str(feedback_id or "").startswith("mem_"):
            changed = await engine.disable_cognitive_feedback_record(feedback_id)
            return {"status": "ok", "changed": bool(changed), "runtime_bound": True, "persisted": True}
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
            bucket = sources.setdefault(
                source,
                {
                    "source": source,
                    "source_label": str(item.get("source_label") or source),
                    "count": 0,
                    "latest_timestamp": 0.0,
                },
            )
            bucket["count"] += 1
            bucket["latest_timestamp"] = max(float(bucket["latest_timestamp"]), float(item.get("timestamp", 0.0) or 0.0))
        return {"status": "ok", "items": list(sources.values()), "total": len(sources)}


__all__ = ["ChatRuntimeService"]
