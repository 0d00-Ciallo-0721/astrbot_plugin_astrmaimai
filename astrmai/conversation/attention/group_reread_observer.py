from __future__ import annotations

import asyncio
import collections
import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..contracts.reread import RereadActionRequest


_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff]")


@dataclass(slots=True)
class _RereadRecord:
    sender_id: str
    event_id: str
    text: str
    fingerprint: str
    observed_at: float


@dataclass(slots=True)
class _GroupRereadState:
    records: list[_RereadRecord] = field(default_factory=list)
    last_activity_at: float = 0.0
    cooldown_until: float = 0.0


class GroupRereadObserver:
    """Tracks short, distinct-member pure-text echo chains in group chats."""

    def __init__(self, *, config=None):
        self.config = config
        self._states: collections.OrderedDict[str, _GroupRereadState] = collections.OrderedDict()
        self._lock = asyncio.Lock()
        self._stats: collections.Counter[str] = collections.Counter()

    def refresh_config(self, config) -> None:
        self.config = config

    def _conversation_config(self):
        return getattr(self.config, "conversation", None)

    def _enabled(self) -> bool:
        return bool(getattr(self._conversation_config(), "group_reread_enabled", True))

    def _threshold(self) -> int:
        return max(2, int(getattr(self._conversation_config(), "group_reread_threshold", 5) or 5))

    def _window_seconds(self) -> float:
        return max(5.0, float(getattr(self._conversation_config(), "group_reread_window_sec", 60.0) or 60.0))

    def _cooldown_seconds(self) -> float:
        return max(0.0, float(getattr(self._conversation_config(), "group_reread_cooldown_sec", 45.0) or 45.0))

    def _max_groups(self) -> int:
        return max(16, int(getattr(self._conversation_config(), "group_reread_max_groups", 256) or 256))

    def _state_ttl_seconds(self) -> float:
        return max(30.0, float(getattr(self._conversation_config(), "group_reread_state_ttl_sec", 600.0) or 600.0))

    @staticmethod
    def normalize_text(value: str) -> str:
        text = _ZERO_WIDTH.sub("", str(value or ""))
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def fingerprint(cls, value: str) -> str:
        normalized = cls.normalize_text(value)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _event_id(event: Any) -> str:
        message_obj = getattr(event, "message_obj", None)
        for value in (getattr(message_obj, "message_id", None), getattr(message_obj, "id", None), getattr(event, "message_id", None)):
            normalized = str(value or "").strip()
            if normalized:
                return normalized
        return ""

    @staticmethod
    def _is_pure_text(event: Any) -> bool:
        if bool(getattr(event, "get_extra", lambda *_args: False)("astrmai_is_command", False)):
            return False
        if bool(getattr(event, "get_extra", lambda *_args: False)("heartflow_is_command", False)):
            return False
        if bool(getattr(event, "get_extra", lambda *_args: False)("astrmai_non_conversational", False)):
            return False
        try:
            sender_id = str(event.get_sender_id() or "").strip()
        except Exception:
            sender_id = ""
        if not sender_id or sender_id.startswith("80000000"):
            return False
        if sender_id == str(getattr(event, "get_self_id", lambda: "")() or "").strip():
            return False
        message = getattr(getattr(event, "message_obj", None), "message", None)
        if not message:
            return bool(str(getattr(event, "message_str", "") or "").strip())
        if len(message) != 1:
            return False
        component = message[0]
        component_type = str(getattr(component, "type", component.__class__.__name__)).lstrip("_").lower()
        return component_type in {"plain", "text"}

    def _prune_locked(self, now: float) -> None:
        ttl_cutoff = now - self._state_ttl_seconds()
        for chat_id in list(self._states):
            state = self._states[chat_id]
            if state.last_activity_at < ttl_cutoff:
                self._states.pop(chat_id, None)
        while len(self._states) > self._max_groups():
            self._states.popitem(last=False)

    def _get_state_locked(self, chat_id: str, now: float) -> _GroupRereadState:
        state = self._states.get(chat_id)
        if state is None:
            state = _GroupRereadState()
            self._states[chat_id] = state
        else:
            self._states.move_to_end(chat_id)
        state.last_activity_at = now
        return state

    async def record_outbound_text_seed(
        self,
        chat_id: str,
        text: str,
        *,
        bot_id: str,
        event_id: str = "",
    ) -> None:
        normalized = self.normalize_text(text)
        if not self._enabled() or not chat_id or not normalized or not bot_id:
            return
        now = time.time()
        record = _RereadRecord(bot_id, str(event_id or ""), normalized, self.fingerprint(normalized), now)
        async with self._lock:
            self._prune_locked(now)
            state = self._get_state_locked(chat_id, now)
            state.records = [record]
            self._stats["outbound_seed"] += 1

    async def observe(self, event: Any) -> RereadActionRequest | None:
        if not self._enabled() or not getattr(event, "get_group_id", lambda: "")():
            return None
        if not self._is_pure_text(event):
            self._stats["skipped_non_plain"] += 1
            return None
        chat_id = str(getattr(event, "unified_msg_origin", "") or "").strip()
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
        text = self.normalize_text(getattr(event, "message_str", ""))
        if not chat_id or not sender_id or not text:
            self._stats["skipped_missing_identity"] += 1
            return None
        now = time.time()
        record = _RereadRecord(sender_id, self._event_id(event), text, self.fingerprint(text), now)
        async with self._lock:
            self._prune_locked(now)
            state = self._get_state_locked(chat_id, now)
            state.records = [item for item in state.records if now - item.observed_at <= self._window_seconds()]
            tail = state.records[-1] if state.records else None
            if tail is None or tail.fingerprint != record.fingerprint or sender_id in {item.sender_id for item in state.records}:
                state.records = [record]
                self._stats["window_reset"] += 1
                return None
            state.records.append(record)
            if now < state.cooldown_until or len(state.records) < self._threshold():
                self._stats["observed"] += 1
                return None
            records = state.records[:]
            state.records = []
            self._stats["threshold_hit"] += 1
        participants = tuple(item.sender_id for item in records)
        source_ids = tuple(item.event_id for item in records if item.event_id)
        includes_bot_seed = bool(records and records[0].sender_id == str(getattr(event, "get_self_id", lambda: "")() or ""))
        if includes_bot_seed:
            explanation = "Bot 先前的纯文本被 4 位不同群成员连续原样跟读，因此执行一次被动社交跟读；这不表示事实认可、立场承诺或独立判断。"
        else:
            explanation = "群内 5 位不同成员连续原样复读该文本，因此执行一次被动社交跟读；这不表示事实认可、立场承诺或独立判断。"
        return RereadActionRequest(
            chat_id=chat_id,
            text=records[-1].text,
            fingerprint=records[-1].fingerprint,
            trigger_kind="group_reread_passive",
            source_event_ids=source_ids,
            participant_ids=participants,
            explanation=explanation,
        )

    async def claim_dispatch(self, chat_id: str) -> bool:
        """Atomically claim the shared cooldown for either passive or active reread."""
        if not self._enabled() or not chat_id:
            return False
        now = time.time()
        async with self._lock:
            self._prune_locked(now)
            state = self._get_state_locked(str(chat_id), now)
            if now < state.cooldown_until:
                self._stats["cooldown_blocked"] += 1
                return False
            state.cooldown_until = now + self._cooldown_seconds()
            self._stats["dispatch_claimed"] += 1
            return True

    async def release_dispatch(self, chat_id: str) -> bool:
        """Release a cooldown reservation when no visible message was sent."""
        if not chat_id:
            return False
        now = time.time()
        async with self._lock:
            state = self._states.get(str(chat_id))
            if state is None or state.cooldown_until <= now:
                return False
            state.cooldown_until = 0.0
            self._stats["dispatch_released"] += 1
            return True

    async def clear_chat(self, chat_id: str) -> bool:
        async with self._lock:
            return self._states.pop(str(chat_id or ""), None) is not None

    def describe_status(self) -> dict[str, Any]:
        return {"active_groups": len(self._states), "stats": dict(self._stats)}


__all__ = ["GroupRereadObserver"]
