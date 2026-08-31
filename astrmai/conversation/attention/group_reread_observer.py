from __future__ import annotations

import asyncio
import collections
import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..contracts.reread import RereadActionRequest


_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff]")


@dataclass(slots=True)
class _RereadRecord:
    sender_id: str
    event_id: str
    display_text: str
    comparison_text: str
    fingerprint: str
    observed_at: float

    @property
    def text(self) -> str:
        return self.display_text


@dataclass(slots=True)
class _GroupRereadState:
    records: list[_RereadRecord] = field(default_factory=list)
    last_activity_at: float = 0.0
    cooldown_until: float = 0.0
    inflight_token: str = ""
    inflight_trigger_kind: str = ""
    inflight_started_at: float = 0.0
    pending_request: RereadActionRequest | None = None
    pending_records: list[_RereadRecord] = field(default_factory=list)
    pending_expires_at: float = 0.0


class GroupRereadObserver:
    """Tracks short, distinct-member pure-text echo chains in group chats."""

    def __init__(self, *, config=None):
        self.config = config
        self._states: collections.OrderedDict[str, _GroupRereadState] = collections.OrderedDict()
        self._lock = asyncio.Lock()
        self._stats: collections.Counter[str] = collections.Counter()
        self._identity_sequence = 0

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
        value = getattr(self._conversation_config(), "group_reread_cooldown_sec", 45.0)
        return max(0.0, float(45.0 if value is None else value))

    def _max_groups(self) -> int:
        return max(16, int(getattr(self._conversation_config(), "group_reread_max_groups", 256) or 256))

    def _state_ttl_seconds(self) -> float:
        value = getattr(self._conversation_config(), "group_reread_state_ttl_sec", 600.0)
        return max(30.0, float(600.0 if value is None else value))

    def _pending_ttl_seconds(self) -> float:
        return max(30.0, min(self._window_seconds(), self._state_ttl_seconds()))

    def _lease_ttl_seconds(self) -> float:
        value = getattr(self._conversation_config(), "group_reread_lease_ttl_sec", 90.0)
        return max(15.0, float(90.0 if value is None else value))

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

    def _stable_event_identity(self, event: Any, chat_id: str, sender_id: str, comparison_text: str) -> str:
        existing = (
            event.get_extra("astrmai_reread_source_identity", "")
            if hasattr(event, "get_extra")
            else ""
        )
        if existing:
            return str(existing)
        event_id = self._event_id(event)
        if event_id:
            return event_id
        self._identity_sequence += 1
        payload = "\x1f".join(
            (
                str(chat_id),
                str(sender_id),
                comparison_text,
                str(getattr(event, "timestamp", "") or ""),
                str(self._identity_sequence),
                str(id(event)),
            )
        )
        identity = "fallback:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_reread_source_identity", identity)
        return identity

    @staticmethod
    def _is_bot_sender(event: Any) -> bool:
        saw_explicit_false = False
        for candidate in (
            getattr(event, "is_bot", None),
            getattr(getattr(event, "sender", None), "is_bot", None),
            getattr(getattr(event, "message_obj", None), "sender", None),
            getattr(getattr(event, "platform_meta", None), "is_bot", None),
        ):
            if isinstance(candidate, bool):
                if candidate:
                    return True
                saw_explicit_false = True
                continue
            if candidate is not None and isinstance(getattr(candidate, "is_bot", None), bool):
                if candidate.is_bot:
                    return True
                saw_explicit_false = True
        for candidate in (
            getattr(event, "sender", None),
            getattr(getattr(event, "message_obj", None), "sender", None),
            getattr(event, "platform_meta", None),
        ):
            role = str(getattr(candidate, "role", "") or "").strip().lower()
            if role in {"bot", "assistant", "robot"}:
                return True
        return False if saw_explicit_false else False

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

    def _restore_pending_locked(self, state: _GroupRereadState) -> bool:
        """Restore a passive pending request to the observation window."""
        if state.pending_request is None and not state.pending_records:
            return False
        state.records = list(state.pending_records)
        if not state.records and state.pending_request is not None:
            request = state.pending_request
            state.records = [
                _RereadRecord(
                    sender_id=sender,
                    event_id=event_id,
                    display_text=request.text,
                    comparison_text=self.normalize_text(request.text),
                    fingerprint=request.fingerprint,
                    observed_at=time.monotonic(),
                )
                for sender, event_id in zip(request.participant_ids, request.source_event_ids)
            ]
        state.pending_request = None
        state.pending_records = []
        state.pending_expires_at = 0.0
        self._stats["pending_restored"] += 1
        return True

    def _prune_locked(self, now: float) -> None:
        ttl_cutoff = now - self._state_ttl_seconds()
        for chat_id in list(self._states):
            state = self._states[chat_id]
            if (
                state.inflight_token
                and state.inflight_started_at > 0
                and now - state.inflight_started_at >= self._lease_ttl_seconds()
            ):
                trigger_kind = state.inflight_trigger_kind or "unknown"
                state.inflight_token = ""
                state.inflight_trigger_kind = ""
                state.inflight_started_at = 0.0
                self._stats["lease_expired"] += 1
                if trigger_kind == "group_reread_passive" and (
                    state.pending_request is not None or state.pending_records
                ):
                    self._restore_pending_locked(state)
                    self._stats["pending_restored_after_lease_expired"] += 1
            if (state.pending_request is not None or state.pending_records) and state.pending_expires_at <= now:
                state.pending_request = None
                state.pending_records = []
                state.pending_expires_at = 0.0
                state.records = []
                self._stats["pending_expired"] += 1
            protected = bool(
                state.inflight_token
                or state.pending_request
                or state.pending_records
                or state.cooldown_until > now
            )
            if state.last_activity_at < ttl_cutoff and not protected:
                self._states.pop(chat_id, None)
        while len(self._states) > self._max_groups():
            evicted = False
            for chat_id, state in list(self._states.items()):
                if (
                    not state.inflight_token
                    and state.pending_request is None
                    and not state.pending_records
                    and state.cooldown_until <= now
                ):
                    self._states.pop(chat_id, None)
                    evicted = True
                    break
            if not evicted:
                break

    def _get_state_locked(self, chat_id: str, now: float) -> _GroupRereadState:
        state = self._states.get(chat_id)
        if state is None:
            state = _GroupRereadState()
            self._states[chat_id] = state
        else:
            self._states.move_to_end(chat_id)
        state.last_activity_at = now
        return state

    def _admit_state_locked(self, chat_id: str, now: float) -> bool:
        normalized = str(chat_id or "")
        if normalized in self._states or len(self._states) < self._max_groups():
            return True
        for candidate_id, state in list(self._states.items()):
            if (
                not state.inflight_token
                and state.pending_request is None
                and not state.pending_records
                and state.cooldown_until <= now
            ):
                self._states.pop(candidate_id, None)
                self._stats["state_evicted"] += 1
                return True
        self._stats["state_capacity_blocked"] += 1
        return False

    async def record_outbound_text_seed(
        self,
        chat_id: str,
        text: str,
        *,
        bot_id: str,
        event_id: str = "",
    ) -> None:
        comparison = self.normalize_text(text)
        if not self._enabled() or not chat_id or not comparison or not bot_id:
            return
        now = time.monotonic()
        record = _RereadRecord(
            bot_id,
            str(event_id or ""),
            str(text or ""),
            comparison,
            self.fingerprint(comparison),
            now,
        )
        async with self._lock:
            self._prune_locked(now)
            if not self._admit_state_locked(chat_id, now):
                return
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
        display_text = str(getattr(event, "message_str", "") or "")
        comparison_text = self.normalize_text(display_text)
        if not chat_id or not sender_id or not comparison_text:
            self._stats["skipped_missing_identity"] += 1
            return None
        if self._is_bot_sender(event):
            self._stats["skipped_bot_sender"] += 1
            return None
        now = time.monotonic()
        event_id = self._stable_event_identity(event, chat_id, sender_id, comparison_text)
        record = _RereadRecord(
            sender_id,
            event_id,
            display_text,
            comparison_text,
            self.fingerprint(comparison_text),
            now,
        )
        async with self._lock:
            self._prune_locked(now)
            if not self._admit_state_locked(chat_id, now):
                self._stats["observed_capacity_blocked"] += 1
                return None
            state = self._get_state_locked(chat_id, now)
            if state.pending_request is not None or state.pending_records:
                self._stats["pending_blocked"] += 1
                return None
            state.records = [item for item in state.records if now - item.observed_at <= self._window_seconds()]
            if now < state.cooldown_until:
                state.records = []
                self._stats["observed_cooldown_blocked"] += 1
                return None
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
            state.pending_records = records[:]
            state.records = []
            self._stats["threshold_hit"] += 1
        participants = tuple(item.sender_id for item in records)
        source_ids = tuple(item.event_id for item in records if item.event_id)
        includes_bot_seed = bool(records and records[0].sender_id == str(getattr(event, "get_self_id", lambda: "")() or ""))
        threshold = self._threshold()
        participant_count = len(set(participants))
        if includes_bot_seed:
            member_count = max(0, participant_count - 1)
            explanation = f"Bot 先前的纯文本被 {member_count} 位不同群成员连续原样跟读，因此执行一次被动社交跟读；这不表示事实认可、立场承诺或独立判断。"
        else:
            explanation = f"群内 {participant_count} 位不同成员连续原样复读该文本（阈值 {threshold}），因此执行一次被动社交跟读；这不表示事实认可、立场承诺或独立判断。"
        request = RereadActionRequest(
            chat_id=chat_id,
            text=records[-1].display_text,
            fingerprint=records[-1].fingerprint,
            trigger_kind="group_reread_passive",
            source_event_ids=source_ids,
            participant_ids=participants,
            explanation=explanation,
            source_identity=records[-1].event_id,
        )
        async with self._lock:
            state = self._states.get(chat_id)
            if state is not None and state.pending_request is None:
                state.pending_request = request
                state.pending_records = records
                state.pending_expires_at = time.monotonic() + self._pending_ttl_seconds()
            elif state is not None and state.pending_request is not None:
                return state.pending_request
        return request

    async def claim_dispatch(self, chat_id: str, *, trigger_kind: str = "group_reread_passive") -> str | None:
        """Atomically reserve a dispatch lease; cooldown starts only on commit."""
        if not chat_id or (trigger_kind == "group_reread_passive" and not self._enabled()):
            self._stats[f"blocked:{trigger_kind}:disabled"] += 1
            return None
        now = time.monotonic()
        async with self._lock:
            self._prune_locked(now)
            if not self._admit_state_locked(str(chat_id), now):
                self._stats[f"blocked:{trigger_kind}:capacity"] += 1
                return None
            state = self._get_state_locked(str(chat_id), now)
            if state.inflight_token:
                self._stats[f"blocked:{trigger_kind}:inflight"] += 1
                return None
            if trigger_kind == "group_reread_passive" and now < state.cooldown_until:
                self._stats[f"blocked:{trigger_kind}:cooldown"] += 1
                return None
            token = uuid.uuid4().hex
            state.inflight_token = token
            state.inflight_trigger_kind = str(trigger_kind or "unknown")
            state.inflight_started_at = now
            self._stats[f"claimed:{trigger_kind}"] += 1
            return token

    async def restore_pending(self, chat_id: str, token: str | None = None) -> bool:
        """Release a failed dispatch lease and resume the pending observation chain."""
        if not chat_id:
            return False
        async with self._lock:
            state = self._states.get(str(chat_id))
            if state is None or (token and state.inflight_token != str(token)):
                return False
            if token is None and state.inflight_token:
                return False
            trigger_kind = state.inflight_trigger_kind or "unknown"
            state.inflight_token = ""
            state.inflight_trigger_kind = ""
            state.inflight_started_at = 0.0
            if trigger_kind == "group_reread_passive":
                self._restore_pending_locked(state)
            return True

    async def release_dispatch(self, chat_id: str, token: str | None = None) -> bool:
        """Release only the matching in-flight lease; never clear a newer lease."""
        if not chat_id:
            return False
        async with self._lock:
            state = self._states.get(str(chat_id))
            if state is None or not state.inflight_token:
                return False
            if token and state.inflight_token != str(token):
                self._stats["release_stale"] += 1
                return False
            trigger_kind = state.inflight_trigger_kind or "unknown"
            state.inflight_token = ""
            state.inflight_trigger_kind = ""
            state.inflight_started_at = 0.0
            if trigger_kind == "group_reread_passive":
                self._restore_pending_locked(state)
            self._stats[f"released:{trigger_kind}"] += 1
            return True

    async def abandon_pending(self, chat_id: str, token: str | None = None) -> bool:
        """Discard a request whose message was sent but whose settlement exhausted."""
        if not chat_id:
            return False
        async with self._lock:
            state = self._states.get(str(chat_id))
            if state is None or (token and state.inflight_token != str(token)):
                return False
            trigger_kind = state.inflight_trigger_kind or "unknown"
            state.inflight_token = ""
            state.inflight_trigger_kind = ""
            state.inflight_started_at = 0.0
            if trigger_kind == "group_reread_passive":
                state.pending_request = None
                state.pending_records = []
                state.pending_expires_at = 0.0
                state.records = []
            self._stats["pending_abandoned_after_send"] += 1
            return True

    async def commit_dispatch(self, chat_id: str, token: str, *, trigger_kind: str = "") -> bool:
        if not chat_id or not token:
            return False
        now = time.monotonic()
        async with self._lock:
            state = self._states.get(str(chat_id))
            if state is None or state.inflight_token != str(token):
                self._stats["commit_stale"] += 1
                return False
            kind = str(trigger_kind or state.inflight_trigger_kind or "unknown")
            state.inflight_token = ""
            state.inflight_trigger_kind = ""
            state.inflight_started_at = 0.0
            if kind == "group_reread_passive":
                state.pending_request = None
                state.pending_records = []
                state.pending_expires_at = 0.0
                state.records = []
            if kind == "group_reread_passive":
                state.cooldown_until = now + self._cooldown_seconds()
            self._stats[f"sent:{kind}"] += 1
            return True

    async def clear_chat(self, chat_id: str) -> bool:
        async with self._lock:
            return self._states.pop(str(chat_id or ""), None) is not None

    def describe_status(self) -> dict[str, Any]:
        return {
            "active_groups": len(self._states),
            "inflight_groups": sum(bool(state.inflight_token) for state in self._states.values()),
            "cooldown_groups": sum(state.cooldown_until > time.monotonic() for state in self._states.values()),
            "pending_groups": sum(
                bool(state.pending_request or state.pending_records)
                for state in self._states.values()
            ),
            "stats": dict(self._stats),
        }


__all__ = ["GroupRereadObserver"]
