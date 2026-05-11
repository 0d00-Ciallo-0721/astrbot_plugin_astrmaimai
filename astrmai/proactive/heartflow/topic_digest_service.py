from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger

from .models import HeartflowSessionState, HeartflowTopicDigest


class HeartflowTopicDigestService:
    SOURCE = "heartflow_topic_digest"
    TOPIC_HEAT_THRESHOLD = 0.65
    DIGEST_COOLDOWN_SECONDS = 20 * 60
    HISTORY_LIMIT = 80

    def __init__(self, memory_engine: Any = None, *, semaphore: asyncio.Semaphore | None = None):
        self.memory_engine = memory_engine
        self._semaphore = semaphore
        self._last_digest_by_chat: dict[str, float] = {}
        self._history: list[HeartflowTopicDigest] = []

    async def run_once(self, manager: Any, *, now: float | None = None) -> None:
        if not self.memory_engine or not hasattr(self.memory_engine, "record_cognitive_feedback"):
            return
        if not manager or not hasattr(manager, "list_sessions"):
            return
        if self._semaphore:
            async with self._semaphore:
                await self._run_once_inner(manager, now=time.time() if now is None else now)
            return
        await self._run_once_inner(manager, now=time.time() if now is None else now)

    async def _run_once_inner(self, manager: Any, *, now: float) -> None:
        try:
            sessions = manager.list_sessions(limit=50)
        except Exception as exc:
            logger.debug(f"[HeartflowTopicDigest] session scan degraded: {exc}")
            return
        for session in sessions:
            try:
                await self._maybe_digest_session(manager, session, now=now)
            except Exception as exc:
                logger.debug(f"[HeartflowTopicDigest] digest degraded for {getattr(session, 'chat_id', '')}: {exc}")

    async def _maybe_digest_session(self, manager: Any, session: HeartflowSessionState, *, now: float) -> None:
        chat_id = str(getattr(session, "chat_id", "") or "")
        if not chat_id:
            return
        if not self._is_digest_window(session, now=now):
            return

        next_allowed_ts = float(self._last_digest_by_chat.get(chat_id, 0.0) or 0.0) + self.DIGEST_COOLDOWN_SECONDS
        if now < next_allowed_ts:
            self._remember(
                HeartflowTopicDigest(
                    chat_id=chat_id,
                    timestamp=now,
                    status="skipped",
                    skip_reason="cooldown",
                    next_allowed_ts=next_allowed_ts,
                )
            )
            return

        topic_heat = float(getattr(session, "topic_heat", 0.0) or 0.0)
        if topic_heat < self.TOPIC_HEAT_THRESHOLD:
            self._remember(
                HeartflowTopicDigest(
                    chat_id=chat_id,
                    timestamp=now,
                    status="skipped",
                    skip_reason="low_topic_heat",
                    next_allowed_ts=next_allowed_ts,
                )
            )
            return

        if not self._has_meaningful_participation(manager, session):
            self._remember(
                HeartflowTopicDigest(
                    chat_id=chat_id,
                    timestamp=now,
                    status="skipped",
                    skip_reason="no_bot_or_relationship_signal",
                    next_allowed_ts=next_allowed_ts,
                )
            )
            return

        digest = self._build_digest(manager, session, now=now)
        await self.memory_engine.record_cognitive_feedback(
            session_id=chat_id,
            source=self.SOURCE,
            summary=digest.summary,
            guidance=digest.guidance,
            tags=list(digest.tags or []),
            importance=float(digest.importance or 0.0),
        )
        self._last_digest_by_chat[chat_id] = now
        self._remember(digest)

    def _is_digest_window(self, session: HeartflowSessionState, *, now: float) -> bool:
        if bool(getattr(session, "low_cost_retained", False)):
            return True
        expires_at = float(getattr(session, "expires_at", 0.0) or 0.0)
        return expires_at > 0 and now >= expires_at

    def _has_meaningful_participation(self, manager: Any, session: HeartflowSessionState) -> bool:
        if int(getattr(session, "recent_bot_reply_count", 0) or 0) > 0:
            return True
        if float(getattr(session, "last_bot_reply_ts", 0.0) or 0.0) > 0:
            return True
        if float(getattr(session, "direct_relevance", 0.0) or 0.0) >= 0.55:
            return True
        actions = []
        if hasattr(manager, "list_action_decisions"):
            actions = manager.list_action_decisions(chat_id=session.chat_id, limit=8)
        return any(str(getattr(action, "action_type", "") or "") in {"wait", "no_reply", "cool_down", "complete_topic"} for action in actions)

    def _build_digest(self, manager: Any, session: HeartflowSessionState, *, now: float) -> HeartflowTopicDigest:
        state = manager.get_state(session.chat_id) if hasattr(manager, "get_state") else None
        action = manager.get_latest_action_decision(session.chat_id) if hasattr(manager, "get_latest_action_decision") else None
        focus = str(getattr(state, "current_focus", "") or "")[:120] if state else ""
        action_type = str(getattr(action, "action_type", "") or getattr(session, "last_impulse", "") or "observe")
        topic_heat = float(getattr(session, "topic_heat", 0.0) or 0.0)
        direct_relevance = float(getattr(session, "direct_relevance", 0.0) or 0.0)
        bot_participated = int(getattr(session, "recent_bot_reply_count", 0) or 0) > 0 or float(getattr(session, "last_bot_reply_ts", 0.0) or 0.0) > 0
        importance = max(0.4, min(0.7, 0.4 + topic_heat * 0.16 + direct_relevance * 0.08 + (0.06 if bot_participated else 0.0)))
        summary = (
            f"Recent heartflow topic for {session.chat_id}: focus={focus or 'unknown'}; "
            f"topic_heat={topic_heat:.2f}; action={action_type}; bot_participated={bot_participated}."
        )
        guidance = "Use this only as quiet continuity. Do not quote it, and do not force the old topic without a fresh cue."
        tags = ["heartflow_topic_digest", action_type]
        if topic_heat >= 0.80:
            tags.append("high_topic_heat")
        if bot_participated:
            tags.append("bot_participated")
        if direct_relevance >= 0.55:
            tags.append("direct_relevance")
        return HeartflowTopicDigest(
            chat_id=session.chat_id,
            timestamp=now,
            status="written",
            summary=summary,
            guidance=guidance,
            tags=tags,
            importance=round(importance, 3),
            next_allowed_ts=now + self.DIGEST_COOLDOWN_SECONDS,
        )

    def _remember(self, digest: HeartflowTopicDigest) -> None:
        self._history.append(digest)
        self._history = self._history[-self.HISTORY_LIMIT :]

    def list_digests(self, limit: int = 50) -> list[HeartflowTopicDigest]:
        limit = max(1, min(int(limit or 50), 300))
        return list(self._history)[-limit:][::-1]

    def describe_status(self) -> dict[str, Any]:
        return {
            "enabled": self.memory_engine is not None,
            "history_count": len(self._history),
            "last_digest_by_chat": dict(self._last_digest_by_chat),
            "cooldown_seconds": self.DIGEST_COOLDOWN_SECONDS,
        }


__all__ = ["HeartflowTopicDigestService"]
