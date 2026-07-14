from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

from .dispatcher import ProactiveMessageIntent


class GroupSigninService:
    """Daily group sign-in task for currently active group chats."""

    SIGN_HOUR = 8
    STATE_KEY = "group_signin"

    def __init__(self, *, state_engine, persistence, dispatcher, config=None):
        self.state_engine = state_engine
        self.persistence = persistence
        self.dispatcher = dispatcher
        self.config = config
        self._last_run = {"status": "idle", "signed": 0, "partial": 0, "failed": 0}

    @staticmethod
    def _extract_group_id(chat_id: str) -> str:
        text = str(chat_id or "").strip()
        if not text or "GroupMessage" not in text:
            return ""
        parts = text.split(":")
        return str(parts[-1] or "").strip() if len(parts) >= 3 else ""

    @staticmethod
    def _today_string(now_ts: float) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(now_ts))

    @classmethod
    def _within_sign_window(cls, now_ts: float) -> bool:
        local = time.localtime(now_ts)
        return int(local.tm_hour) == cls.SIGN_HOUR

    @classmethod
    def _state_bucket(cls, state) -> dict[str, Any]:
        config = getattr(state, "group_config", None)
        if not isinstance(config, dict):
            config = {}
            state.group_config = config
        bucket = config.get(cls.STATE_KEY)
        if not isinstance(bucket, dict):
            bucket = {}
            config[cls.STATE_KEY] = bucket
        return bucket

    @classmethod
    def _already_signed_today(cls, state, today: str) -> bool:
        bucket = cls._state_bucket(state)
        return str(bucket.get("last_date", "") or "") == str(today or "")

    async def _persist_marker(
        self,
        state,
        *,
        today: str,
        now_ts: float,
        status: str,
        rollback_on_failure: bool = False,
    ) -> bool:
        bucket = self._state_bucket(state)
        previous = dict(bucket)
        bucket["last_date"] = str(today or "")
        bucket["status"] = str(status or "")
        bucket["updated_at"] = float(now_ts)
        if status == "complete":
            bucket["last_success_ts"] = float(now_ts)
        state.is_dirty = True
        try:
            await self.persistence.save_chat_state(str(getattr(state, "chat_id", "") or ""), state)
            return True
        except Exception as exc:
            if rollback_on_failure:
                bucket.clear()
                bucket.update(previous)
            logger.error(
                f"[GroupSigninService] failed to persist sign marker status={status} "
                f"for {getattr(state, 'chat_id', '')}: {exc}"
            )
            return False

    @staticmethod
    def _build_guidance() -> str:
        return (
            "你刚完成今天的群签到。顺着当前群聊气氛，自然地发一句很短的主动消息。"
            "不要提系统、任务、打卡、后台或定时器，也不要@任何人。"
            "优先轻松、低压、容易被忽略的语气。"
        )

    async def _dispatch_after_sign(self, chat_id: str, group_id: str) -> None:
        if not self.dispatcher:
            logger.debug("[GroupSigninService] proactive dispatcher unavailable; skip follow-up message")
            return
        intent = ProactiveMessageIntent(
            chat_id=chat_id,
            source="group_signin",
            reason="daily_group_sign_success",
            guidance=self._build_guidance(),
            suggested_social_intent="join",
            suggested_action_tier="chat",
            urgency=0.22,
            cost=0.0,
            cooldown=0.0,
            metadata={"group_id": group_id, "sign_source": "daily_group_sign"},
        )
        try:
            decision = await self.dispatcher.dispatch(intent)
            if not decision.allowed:
                logger.debug(f"[GroupSigninService] proactive follow-up blocked: {decision.blocked_reason}")
        except Exception as exc:
            logger.error(f"[GroupSigninService] proactive dispatch failed for {chat_id}: {exc}")

    def _resolve_api(self):
        gateway = getattr(self.state_engine, "gateway", None)
        context = getattr(gateway, "context", None)
        candidates = [context, gateway, self]
        for owner in candidates:
            if owner is None:
                continue
            client = getattr(owner, "client", None)
            api = getattr(client, "api", None)
            if api is not None:
                return api
            getter = getattr(owner, "get_client", None)
            if callable(getter):
                try:
                    client = getter()
                except TypeError:
                    client = None
                api = getattr(client, "api", None)
                if api is not None:
                    return api
        return None

    async def _sign_group(self, group_id: str) -> bool:
        api = self._resolve_api()
        if api is None:
            logger.debug("[GroupSigninService] client api unavailable; skip sign")
            return False
        try:
            await api.call_action("set_group_sign", group_id=str(group_id))
            return True
        except Exception as exc:
            logger.error(f"[GroupSigninService] sign failed for group={group_id}: {exc}")
            return False

    async def run_once(self, now_ts: float | None = None) -> None:
        now_ts = time.time() if now_ts is None else float(now_ts)
        if not self._within_sign_window(now_ts):
            return

        today = self._today_string(now_ts)
        stats = {"status": "completed", "signed": 0, "partial": 0, "failed": 0}
        for state in self.state_engine.get_active_states():
            chat_id = str(getattr(state, "chat_id", "") or "").strip()
            group_id = self._extract_group_id(chat_id)
            if not group_id:
                continue
            if self._already_signed_today(state, today):
                continue
            intent_saved = await self._persist_marker(
                state,
                today=today,
                now_ts=now_ts,
                status="intent",
                rollback_on_failure=True,
            )
            if not intent_saved:
                stats["failed"] += 1
                continue
            success = await self._sign_group(group_id)
            if not success:
                bucket = self._state_bucket(state)
                bucket["last_date"] = ""
                await self._persist_marker(
                    state,
                    today="",
                    now_ts=now_ts,
                    status="failed",
                )
                stats["failed"] += 1
                continue
            completed = await self._persist_marker(
                state,
                today=today,
                now_ts=now_ts,
                status="complete",
            )
            if not completed:
                stats["partial"] += 1
                continue
            logger.info(f"[GroupSigninService] signed active group={group_id}")
            await self._dispatch_after_sign(chat_id, group_id)
            stats["signed"] += 1
        if stats["partial"]:
            stats["status"] = "partial"
        elif stats["failed"]:
            stats["status"] = "degraded"
        self._last_run = stats

    def describe_status(self) -> dict[str, Any]:
        return {
            "sign_hour": self.SIGN_HOUR,
            "state_key": self.STATE_KEY,
            "last_run": dict(self._last_run),
        }


__all__ = ["GroupSigninService"]
