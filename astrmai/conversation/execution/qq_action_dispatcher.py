from __future__ import annotations

import time
import hashlib
from typing import Any

from astrbot.api import logger

from ..contracts.qq_action import PendingQQAction, canonical_action_type
from ..planning.tool_contracts import get_tool_capability, canonical_tool_name, record_tool_lifecycle
from ...infrastructure.runtime.outbound_send_guard import outbound_send_allowed


class QQActionDispatcher:
    """Commits queued QQ side effects after the visible reply is accepted."""

    ACTION_TTL_SECONDS = 900.0
    MAX_EXECUTED_KEYS = 1024
    TOOL_NAMES = {
        "poke": "proactive_poke",
        "message_emoji_reaction": "message_emoji_reaction_action",
        "message_emoji_like": "message_emoji_reaction_action",
        "like": "proactive_like_action",
        "withdraw": "regret_and_withdraw_action",
        "quote_reply": "quote_reply_action",
    }

    @classmethod
    def _tool_name(cls, action_type: str) -> str:
        normalized = canonical_action_type(action_type)
        return cls.TOOL_NAMES.get(normalized, normalized)

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
    async def _call_api(api, action: str, **kwargs):
        result = await api.call_action(action, **kwargs)
        if isinstance(result, dict):
            if "retcode" in result:
                raw_retcode = result.get("retcode")
                try:
                    retcode = int(raw_retcode)
                except (TypeError, ValueError):
                    raise RuntimeError(f"api_unknown:{action}:invalid_retcode") from None
                if retcode != 0:
                    raise RuntimeError(f"api_rejected:{action}:retcode={retcode}")
            status = str(result.get("status") or "").strip().lower()
            if status in {"failed", "error", "false", "-1"} or result.get("success") is False:
                raise RuntimeError(f"api_rejected:{action}")
            if status and status not in {"ok", "success", "succeeded", "true", "0"}:
                raise RuntimeError(f"api_unknown:{action}:status={status}")
        return result

    @staticmethod
    def _is_unsupported_endpoint(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(token in text for token in ("not implemented", "unsupported", "unknown action", "action not found", "no such action"))

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
        # Persist generated instance IDs for legacy mappings so a subsequent
        # dispatcher retry reuses the same transport key.
        if isinstance(raw_actions, list) and hasattr(event, "set_extra"):
            normalized = [action.to_dict() for action in actions]
            if normalized != raw_actions:
                event.set_extra("astrmai_pending_actions", normalized)
        return actions

    def _prune_keys(self) -> None:
        cutoff = time.time() - self.ACTION_TTL_SECONDS
        self._executed_keys = {key: ts for key, ts in self._executed_keys.items() if ts >= cutoff}
        if len(self._executed_keys) > self.MAX_EXECUTED_KEYS:
            items = sorted(self._executed_keys.items(), key=lambda item: item[1], reverse=True)
            self._executed_keys = dict(items[: self.MAX_EXECUTED_KEYS])

    @staticmethod
    def _action_identity(action: PendingQQAction, turn_key: str, trace_id: str) -> dict[str, str]:
        key = action.idempotency_key(turn_key)
        return {
            "action_id": "qqact_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20],
            "transport_idempotency_key": key,
            "turn_id": turn_key,
            "trace_id": trace_id,
        }

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
    def _append_result(
        event,
        action: PendingQQAction,
        status: str,
        detail: str = "",
        *,
        action_id: str = "",
        transport_idempotency_key: str = "",
        failure_kind: str = "",
        turn_id: str = "",
        trace_id: str = "",
    ) -> None:
        results = event.get_extra("astrmai_qq_action_results", []) if hasattr(event, "get_extra") else []
        results = list(results) if isinstance(results, list) else []
        now = time.time()
        previous = next((item for item in reversed(results) if item.get("action_id") == action_id), None)
        created_at = previous.get("created_at", action.requested_at) if previous else (action.requested_at or now)
        results.append(
            {
                "action_id": action_id,
                "action": action.action_type,
                "canonical_tool_name": canonical_tool_name(QQActionDispatcher._tool_name(action.action_type)),
                "status": status,
                "detail": str(detail or "")[:160],
                "failure_kind": str(failure_kind or ""),
                "turn_id": str(turn_id or ""),
                "trace_id": str(trace_id or ""),
                "transport_idempotency_key": transport_idempotency_key,
                "created_at": created_at,
                "queued_at": previous.get("queued_at", now) if previous else now,
                "sending_at": now if status == "sending" else (previous.get("sending_at", 0.0) if previous else 0.0),
                "sent_at": now if status == "sent" else (previous.get("sent_at", 0.0) if previous else 0.0),
                "completed_at": now if status in {"sent", "failed", "skipped", "retryable", "settlement_degraded"} else 0.0,
            }
        )
        if hasattr(event, "get_extra") and hasattr(event, "set_extra"):
            metric_prefix = {
                "message_emoji_reaction": "emoji_reaction",
                "like": "like",
            }.get(canonical_action_type(action.action_type))
            if metric_prefix:
                metric_name = {
                    "sending": f"{metric_prefix}_requested",
                    "sent": f"{metric_prefix}_sent",
                    "failed": f"{metric_prefix}_failed",
                    "retryable": f"{metric_prefix}_failed",
                    "skipped": f"{metric_prefix}_shutdown_skipped" if failure_kind == "shutdown" else "",
                }.get(status, "")
                if metric_name:
                    try:
                        current = int(event.get_extra(metric_name, 0) or 0)
                    except (TypeError, ValueError):
                        current = 0
                    event.set_extra(metric_name, current + 1)
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_qq_action_results", results[-32:])

    @staticmethod
    def _record_committed_tool(event, action_type: str) -> None:
        tool_name = QQActionDispatcher._tool_name(action_type)
        if not tool_name or not hasattr(event, "get_extra") or not hasattr(event, "set_extra"):
            return
        trace = event.get_extra("astrmai_tool_execution_trace", [])
        trace = list(trace) if isinstance(trace, list) else []
        spec = get_tool_capability(tool_name)
        trace.append(
            {
                "tool_name": tool_name,
                "family": str(getattr(spec, "family", "") or ""),
                "status": "sent",
                "source_domain": "qq_runtime",
                "operation": str(action_type or ""),
                "reason": "",
            }
        )
        event.set_extra("astrmai_tool_execution_trace", trace[-32:])
        record_tool_lifecycle(
            event,
            tool_name,
            "action_committed",
            source="deferred_dispatcher",
            status="sent",
        )

    async def _send_qq_message(self, api, action: PendingQQAction, message: list[dict[str, Any]]) -> None:
        if action.group_id:
            try:
                await self._call_api(
                    api,
                    "send_msg",
                    message_type="group",
                    group_id=self._coerce_identifier(action.group_id),
                    message=message,
                )
                return
            except Exception as exc:
                if not self._is_unsupported_endpoint(exc):
                    raise
                await self._call_api(
                    api,
                    "send_group_msg",
                    group_id=self._coerce_identifier(action.group_id),
                    message=message,
                )
                return
        target_id = action.target_id
        if not target_id:
            raise RuntimeError("缺少目标会话")
        try:
            await self._call_api(
                api,
                "send_msg",
                message_type="private",
                user_id=self._coerce_identifier(target_id),
                message=message,
            )
        except Exception as exc:
            if not self._is_unsupported_endpoint(exc):
                raise
            await self._call_api(
                api,
                "send_private_msg",
                user_id=self._coerce_identifier(target_id),
                message=message,
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
        if not outbound_send_allowed(event):
            raise RuntimeError("shutdown_rejected")
        action_type = action.action_type
        if action_type == "poke":
            kwargs = {"user_id": self._coerce_identifier(action.target_id)}
            if action.group_id:
                kwargs["group_id"] = self._coerce_identifier(action.group_id)
            await self._call_api(api, "send_poke", **kwargs)
        elif canonical_action_type(action_type) == "message_emoji_reaction":
            await self._call_api(
                api,
                "set_msg_emoji_like",
                message_id=self._coerce_identifier(action.message_id),
                emoji_id=str(action.payload.get("emoji_id") or ""),
                set=True,
            )
        elif canonical_action_type(action_type) == "like":
            target_id = str(action.target_id or "").strip()
            if not target_id.isdigit():
                raise RuntimeError("failed_target_missing")
            try:
                times = max(1, min(20, int(action.payload.get("times", 1) or 1)))
            except (TypeError, ValueError):
                times = 1
            await self._call_api(
                api,
                "send_like",
                user_id=self._coerce_identifier(target_id),
                times=times,
            )
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
            await self._call_api(api, "delete_msg", message_id=self._coerce_identifier(message_id))
        elif action_type == "quote_reply":
            message_id = str(action.message_id or "").strip()
            text = str(action.payload.get("text") or "").strip()
            if not message_id:
                raise RuntimeError("缺少引用消息 ID")
            if not text:
                raise RuntimeError("缺少引用回复正文")
            await self._send_qq_message(
                api,
                action,
                [
                    {"type": "reply", "data": {"id": self._coerce_identifier(message_id)}},
                    {"type": "text", "data": {"text": text}},
                ],
            )
        else:
            return
        self._record_committed_tool(event, action_type)

    async def commit(self, event, chat_id: str, *, send_key: str = "") -> list[dict[str, str]]:
        if not self._enabled():
            return []
        actions = [
            action
            for action in self._queued_actions(event)
            if canonical_action_type(action.action_type) in {"poke", "message_emoji_reaction", "like", "withdraw", "quote_reply"}
        ]
        if not actions:
            return []
        trace_id = str(event.get_extra("astrmai_trace_id", "") if hasattr(event, "get_extra") else "")
        message_obj = getattr(event, "message_obj", None)
        inbound_message_id = str(getattr(message_obj, "message_id", "") or "")
        turn_key = send_key or trace_id or inbound_message_id or f"{chat_id}:{id(event)}"
        if not outbound_send_allowed(event):
            for action in actions:
                self._append_result(
                    event,
                    action,
                    "skipped",
                    "shutdown_rejected",
                    failure_kind="shutdown",
                    **self._action_identity(action, turn_key, trace_id),
                )
                record_tool_lifecycle(
                    event,
                    self._tool_name(action.action_type),
                    "action_commit",
                    source="deferred_dispatcher",
                    status="skipped",
                    reason="shutdown_rejected",
                )
            return list(event.get_extra("astrmai_qq_action_results", []) or [])
        if not await self._is_current_turn(event):
            for action in actions:
                self._append_result(
                    event,
                    action,
                    "skipped",
                    "stale_turn",
                    failure_kind="stale_turn",
                    **self._action_identity(action, turn_key, trace_id),
                )
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
                self._append_result(
                    event,
                    action,
                    "failed",
                    "napcat_api_unavailable",
                    failure_kind="api_unavailable",
                    **self._action_identity(action, turn_key, trace_id),
                )
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
        for action in actions:
            result_kwargs = self._action_identity(action, turn_key, trace_id)
            key = result_kwargs["transport_idempotency_key"]
            if not outbound_send_allowed(event):
                self._append_result(event, action, "skipped", "shutdown_rejected", failure_kind="shutdown", **result_kwargs)
                record_tool_lifecycle(
                    event,
                    self._tool_name(action.action_type),
                    "action_commit",
                    source="deferred_dispatcher",
                    status="skipped",
                    reason="shutdown_rejected",
                )
                continue
            if key in self._executed_keys:
                self._append_result(event, action, "skipped", "duplicate", failure_kind="duplicate_prevented", **result_kwargs)
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
                self._append_result(event, action, "sending", **result_kwargs)
                await self._commit_one(api, event, action, chat_id=chat_id, send_key=send_key)
                self._executed_keys[key] = time.time()
                self._append_result(event, action, "sent", **result_kwargs)
                logger.info(
                    f"[QQActionDispatcher] committed action={action.action_type} "
                    f"target={action.target_id or action.group_id or action.message_id or 'current'}"
                )
            except Exception as exc:
                logger.warning(f"[QQActionDispatcher] {action.action_type} failed: {exc}")
                detail = str(exc)
                failure_kind = "retryable" if any(token in detail.lower() for token in ("timeout", "rate", "429", "tempor", "503")) else "api"
                self._append_result(event, action, "retryable" if failure_kind == "retryable" else "failed", detail, failure_kind=failure_kind, **result_kwargs)
                record_tool_lifecycle(
                    event,
                    self._tool_name(action.action_type),
                    "action_commit",
                    source="deferred_dispatcher",
                    status="retryable" if failure_kind == "retryable" else "failed",
                    reason=str(exc),
                )
        return list(event.get_extra("astrmai_qq_action_results", []) or [])


__all__ = ["QQActionDispatcher"]
