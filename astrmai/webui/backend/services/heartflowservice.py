from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from typing import Any

from ..adapters.plugin_api import PluginApiAdapter

_logger = logging.getLogger(__name__)


class HeartflowService:
    def __init__(self, plugin_api: PluginApiAdapter):
        self._api = plugin_api

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

    async def heartflow_status(self) -> dict[str, Any]:
        manager = self._api.get_heartflow_manager()
        manager_status = manager.describe_status() if manager and hasattr(manager, "describe_status") else {"enabled": False}
        kernel = self._api.get_chat_loop_kernel()
        kernel_status = kernel.describe_status_sync() if kernel and hasattr(kernel, "describe_status_sync") else {}
        manager_active = int(manager_status.get("active_chats", 0) or 0)
        kernel_tracked = int(kernel_status.get("tracked_chats", 0) or 0)
        if manager is None:
            operational_state = "unavailable"
            operational_reason = "heartflow_manager_unbound"
        elif manager_active > 0:
            operational_state = "active"
            operational_reason = "heartflow_state_present"
        elif kernel_tracked > 0:
            operational_state = "scheduler_active_manager_idle"
            operational_reason = "tracked_chats_have_not_produced_heartflow_state"
        else:
            operational_state = "idle"
            operational_reason = "no_tracked_chat"
        data = {
            **dict(manager_status or {}),
            "operational_state": operational_state,
            "operational_reason": operational_reason,
            "manager": dict(manager_status or {}),
            "kernel": {
                "tracked_chats": kernel_tracked,
                "last_due_selection_summary": dict(kernel_status.get("last_due_selection_summary", {}) or {}),
            },
        }
        return {"status": "ok", "data": data, "runtime_bound": manager is not None}

    async def heartflow_chats(self) -> dict[str, Any]:
        manager = self._api.get_heartflow_manager()
        if not manager:
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": False}
        states = []
        states_dict = getattr(manager, "get_all_states", None)
        if callable(states_dict):
            try:
                states_dict = states_dict()
            except Exception:
                states_dict = {}
        manager_states = states_dict if isinstance(states_dict, dict) else getattr(manager, "_states", {})
        for chat_id, state in manager_states.items():
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
        manager = self._api.get_heartflow_manager()
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
        manager = self._api.get_heartflow_manager()
        if not manager or not hasattr(manager, "list_impulse_decisions"):
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": False}
        items = [self._as_dict(item) for item in manager.list_impulse_decisions(chat_id=chat_id, limit=limit)]
        return {"status": "ok", "items": items, "total": len(items), "runtime_bound": True}

    async def heartflow_timeline(self, chat_id: str | None = None, limit: int = 80) -> dict[str, Any]:
        manager = self._api.get_heartflow_manager()
        if not manager or not hasattr(manager, "list_timeline"):
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": False}
        items = list(manager.list_timeline(chat_id=chat_id, limit=limit))
        return {"status": "ok", "items": items, "total": len(items), "runtime_bound": True}

    async def heartflow_topic_digests(self, limit: int = 50) -> dict[str, Any]:
        service = self._api.get_heartflow_topic_digest_service()
        if not service or not hasattr(service, "list_digests"):
            return {"status": "ok", "items": [], "total": 0, "runtime_bound": False}
        items = [self._as_dict(item) for item in service.list_digests(limit=limit)]
        return {"status": "ok", "items": items, "total": len(items), "runtime_bound": True}

    async def heartflow_hidden_context(self, chat_id: str) -> dict[str, Any]:
        manager = self._api.get_heartflow_manager()
        context = manager.get_hidden_context(chat_id) if manager and hasattr(manager, "get_hidden_context") else ""
        return {"status": "ok", "data": {"chat_id": chat_id, "hidden_context": context}, "runtime_bound": manager is not None}

    async def clear_heartflow_cooldowns(self, chat_id: str) -> dict[str, Any]:
        manager = self._api.get_heartflow_manager()
        if not manager:
            return {"status": "ok", "changed": False, "runtime_bound": False}
        # P9-2: use hasattr guards instead of silent getattr().pop() on private attrs
        for attr_name in ("_pulses_by_chat", "_impulse_decisions_by_chat", "_action_decisions_by_chat"):
            if hasattr(manager, attr_name):
                private_dict = getattr(manager, attr_name)
                if isinstance(private_dict, dict):
                    private_dict.pop(chat_id, None)
            else:
                _logger.debug("clear_heartflow_cooldowns: manager has no %s (API may have changed)", attr_name)
        state = manager.get_state(chat_id) if hasattr(manager, "get_state") else None
        if state:
            state.cooldown_tags = []
        session = manager.get_session(chat_id) if hasattr(manager, "get_session") else None
        if session:
            session.consecutive_observe_count = 0
            session.consecutive_no_reply_count = 0
            session.consecutive_prepare_count = 0
        return {"status": "ok", "changed": True, "runtime_bound": True}


__all__ = ["HeartflowService"]
