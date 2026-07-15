from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

from ..contracts.qq_action import PendingQQAction
from ..planning.tool_contracts import record_tool_lifecycle


class QQActionDispatcher:
    """Commits queued QQ side effects after the visible reply is accepted."""

    ACTION_TTL_SECONDS = 900.0
    MAX_EXECUTED_KEYS = 1024
    TOOL_NAMES = {
        "poke": "proactive_poke",
        "message_emoji_like": "message_emoji_like_action",
        "group_sign": "group_sign_action",
        "withdraw": "regret_and_withdraw_action",
    }

    @classmethod
    def _tool_name(cls, action_type: str) -> str:
        return cls.TOOL_NAMES.get(str(action_type or ""), str(action_type or ""))

    def __init__(self, config=None, runtime_coordinator=None):
        self.config = config
        self.runtime_coordinator = runtime_coordinator
        self._executed_keys: dict[str, float] = {}

    def refresh_config(self, config) -> None:
        self.config = config

    def _enabled(self) -> bool:
        conversation = getattr(self.config, "conversation", None)
        return bool(getattr(conversation, "qq_native_tools_enabled", True)) and bool(
            getattr(conversation, "qq_deferred_action_commit_enabled", True)
        )

    @staticmethod
    def _coerce_identifier(value: Any) -> Any:
        text = str(value or "").strip()
        return int(text) if text.isdigit() else text

    @staticmethod
    def _queued_actions(event) -> list[PendingQQAction]:
        raw_actions = event.get_extra("astrmai_pending_actions", []) if hasattr(event, "get_extra") else []
        actions: list[PendingQQAction] = []
        for item in raw_actions if isinstance(raw_actions, list) else []:
            if not isinstance(item, dict):
                continue
            parsed = PendingQQAction.from_mapping(item)
            if parsed is not None:
                actions.append(parsed)
        return actions

    def _prune_keys(self) -> None:
        cutoff = time.time() - self.ACTION_TTL_SECONDS
        self._executed_keys = {key: ts for key, ts in self._executed_keys.items() if ts >= cutoff}
        if len(self._executed_keys) > self.MAX_EXECUTED_KEYS:
            items = sorted(self._executed_keys.items(), key=lambda item: item[1], reverse=True)
            self._executed_keys = dict(items[: self.MAX_EXECUTED_KEYS])

    async def _is_current_turn(self, event) -> bool:
        coordinator = self.runtime_coordinator
        turn = event.get_extra("astrmai_turn_identity", None) if hasattr(event, "get_extra") else None
        if coordinator is None or not hasattr(coordinator, "is_current_turn") or turn is None:
            return True
        try:
            return bool(await coordinator.is_current_turn(turn))
        except Exception:
            logger.debug("[QQActionDispatcher] turn freshness check degraded", exc_info=True)
            return False

    async def _previous_outbound_message_ids(self, chat_id: str, send_key: str) -> list[str]:
        coordinator = self.runtime_coordinator
        if coordinator is None or not hasattr(coordinator, "get_latest_committed_outbound"):
            return []
        try:
            return list(await coordinator.get_latest_committed_outbound(chat_id, exclude_send_key=send_key))
        except Exception:
            logger.debug("[QQActionDispatcher] previous outbound lookup degraded", exc_info=True)
            return []

    @staticmethod
    def _append_result(event, action: PendingQQAction, status: str, detail: str = "") -> None:
        results = event.get_extra("astrmai_qq_action_results", []) if hasattr(event, "get_extra") else []
        results = list(results) if isinstance(results, list) else []
        results.append(
            {
                "action": action.action_type,
                "status": status,
                "detail": str(detail or "")[:160],
            }
        )
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_qq_action_results", results[-32:])

    @staticmethod
    def _record_committed_tool(event, action_type: str) -> None:
        tool_name = QQActionDispatcher._tool_name(action_type)
        if not tool_name or not hasattr(event, "get_extra") or not hasattr(event, "set_extra"):
            return
        trace = event.get_extra("astrmai_tool_execution_trace", [])
        trace = list(trace) if isinstance(trace, list) else []
        trace.append({"tool_name": tool_name, "status": "success"})
        event.set_extra("astrmai_tool_execution_trace", trace[-32:])
        record_tool_lifecycle(
            event,
            tool_name,
            "action_committed",
            source="deferred_dispatcher",
            status="success",
        )

    async def _commit_one(
        self,
        api,
        event,
        action: PendingQQAction,
        *,
        chat_id: str,
        send_key: str,
    ) -> None:
        action_type = action.action_type
        if action_type == "poke":
            kwargs = {"user_id": self._coerce_identifier(action.target_id)}
            if action.group_id:
                kwargs["group_id"] = self._coerce_identifier(action.group_id)
            await api.call_action("send_poke", **kwargs)
        elif action_type == "message_emoji_like":
            await api.call_action(
                "set_msg_emoji_like",
                message_id=self._coerce_identifier(action.message_id),
                emoji_id=str(action.payload.get("emoji_id") or ""),
                set=True,
            )
        elif action_type == "group_sign":
            await api.call_action("set_group_sign", group_id=str(action.group_id))
        elif action_type == "withdraw":
            message_id = str(action.message_id or "").strip()
            if not message_id:
                previous_ids = await self._previous_outbound_message_ids(chat_id, send_key)
                message_id = next(
                    (
                        str(item).strip()
                        for item in reversed(previous_ids)
                        if str(item).strip().isdigit()
                    ),
                    "",
                )
            if not message_id:
                raise RuntimeError("没有可撤回的上一条 AstrMai 回复")
            await api.call_action("delete_msg", message_id=self._coerce_identifier(message_id))
        else:
            return
        self._record_committed_tool(event, action_type)

    async def commit(self, event, chat_id: str, *, send_key: str = "") -> list[dict[str, str]]:
        if not self._enabled():
            return []
        actions = [
            action
            for action in self._queued_actions(event)
            if action.action_type in {"poke", "message_emoji_like", "group_sign", "withdraw"}
        ]
        if not actions:
            return []
        if not await self._is_current_turn(event):
            for action in actions:
                self._append_result(event, action, "skipped", "stale_turn")
                record_tool_lifecycle(
                    event,
                    self._tool_name(action.action_type),
                    "action_commit",
                    source="deferred_dispatcher",
                    status="skipped",
                    reason="stale_turn",
                )
            return list(event.get_extra("astrmai_qq_action_results", []) or [])

        client = getattr(event, "bot", None)
        api = getattr(client, "api", None)
        if api is None:
            for action in actions:
                self._append_result(event, action, "failed", "napcat_api_unavailable")
                record_tool_lifecycle(
                    event,
                    self._tool_name(action.action_type),
                    "action_commit",
                    source="deferred_dispatcher",
                    status="failed",
                    reason="napcat_api_unavailable",
                )
            return list(event.get_extra("astrmai_qq_action_results", []) or [])

        self._prune_keys()
        trace_id = str(event.get_extra("astrmai_trace_id", "") if hasattr(event, "get_extra") else "")
        message_obj = getattr(event, "message_obj", None)
        inbound_message_id = str(getattr(message_obj, "message_id", "") or "")
        turn_key = send_key or trace_id or inbound_message_id or f"{chat_id}:{id(event)}"
        for action in actions:
            key = action.idempotency_key(turn_key)
            if key in self._executed_keys:
                self._append_result(event, action, "skipped", "duplicate")
                record_tool_lifecycle(
                    event,
                    self._tool_name(action.action_type),
                    "action_commit",
                    source="deferred_dispatcher",
                    status="skipped",
                    reason="duplicate",
                )
                continue
            try:
                await self._commit_one(api, event, action, chat_id=chat_id, send_key=send_key)
                self._executed_keys[key] = time.time()
                self._append_result(event, action, "success")
                logger.info(
                    f"[QQActionDispatcher] committed action={action.action_type} "
                    f"target={action.target_id or action.group_id or action.message_id or 'current'}"
                )
            except Exception as exc:
                logger.warning(f"[QQActionDispatcher] {action.action_type} failed: {exc}")
                self._append_result(event, action, "failed", str(exc))
                record_tool_lifecycle(
                    event,
                    self._tool_name(action.action_type),
                    "action_commit",
                    source="deferred_dispatcher",
                    status="failed",
                    reason=str(exc),
                )
        return list(event.get_extra("astrmai_qq_action_results", []) or [])


__all__ = ["QQActionDispatcher"]
