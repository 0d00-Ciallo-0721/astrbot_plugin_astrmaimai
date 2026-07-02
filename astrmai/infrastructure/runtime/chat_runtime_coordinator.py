from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .runtime_contracts import FreshnessState


@dataclass
class ChatRuntimeState:
    sys2_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    executor_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    executor_pending: int = 0
    wait_targets: List[str] = field(default_factory=list)
    wait_target_name: str = ""
    latest_activity_ts: float = 0.0
    latest_activity_sender_id: str = ""
    latest_activity_sender_name: str = ""
    latest_activity_preview: str = ""
    latest_activity_thread_signature: str = ""
    activity_times: List[float] = field(default_factory=list)


class ChatRuntimeCoordinator:
    def __init__(self) -> None:
        self._states: Dict[str, ChatRuntimeState] = {}
        self._lock = asyncio.Lock()

    async def _get_state(self, chat_id: str) -> ChatRuntimeState:
        async with self._lock:
            if chat_id not in self._states:
                self._states[chat_id] = ChatRuntimeState()
            return self._states[chat_id]

    async def get_sys2_lock(self, chat_id: str) -> asyncio.Lock:
        state = await self._get_state(chat_id)
        return state.sys2_lock

    async def try_acquire_executor(self, chat_id: str, max_pending: int = 2) -> Optional[asyncio.Lock]:
        async with self._lock:
            state = self._states.setdefault(chat_id, ChatRuntimeState())
            if state.executor_pending >= max_pending:
                return None
            state.executor_pending += 1
            executor_lock = state.executor_lock
        try:
            await executor_lock.acquire()
        except asyncio.CancelledError:
            # ponytail: decrement on cancel to prevent permanent blockage
            async with self._lock:
                if chat_id in self._states:
                    self._states[chat_id].executor_pending = max(0, self._states[chat_id].executor_pending - 1)
            raise
        return executor_lock

    async def release_executor(self, chat_id: str) -> None:
        executor_lock: Optional[asyncio.Lock] = None
        async with self._lock:
            state = self._states.get(chat_id)
            if not state:
                return
            state.executor_pending = max(0, state.executor_pending - 1)
            executor_lock = state.executor_lock
        if executor_lock is not None and executor_lock.locked():
            executor_lock.release()

    async def update_wait_targets(self, chat_id: str, targets: List[str], target_name: str = "") -> None:
        async with self._lock:
            state = self._states.setdefault(chat_id, ChatRuntimeState())
            state.wait_targets = list(dict.fromkeys([str(target) for target in targets if str(target)]))
            state.wait_target_name = target_name or ""

    async def get_wait_targets(self, chat_id: str) -> List[str]:
        state = await self._get_state(chat_id)
        return state.wait_targets[:]

    async def get_wait_target_name(self, chat_id: str) -> str:
        state = await self._get_state(chat_id)
        return state.wait_target_name

    async def mark_activity(
        self,
        chat_id: str,
        timestamp: float,
        sender_id: str = "",
        sender_name: str = "",
        preview: str = "",
        thread_signature: str = "",
    ) -> None:
        async with self._lock:
            state = self._states.setdefault(chat_id, ChatRuntimeState())
            if timestamp < state.latest_activity_ts:
                return
            state.latest_activity_ts = float(timestamp or 0.0)
            state.latest_activity_sender_id = str(sender_id or "")
            state.latest_activity_sender_name = str(sender_name or "")
            state.latest_activity_preview = str(preview or "")
            state.latest_activity_thread_signature = str(thread_signature or "")
            state.activity_times = [
                item
                for item in state.activity_times
                if state.latest_activity_ts - float(item or 0.0) <= 1800.0
            ][-31:] + [state.latest_activity_ts]

    async def get_latest_activity(self, chat_id: str) -> tuple[float, str, str, str]:
        state = await self._get_state(chat_id)
        return (
            state.latest_activity_ts,
            state.latest_activity_sender_id,
            state.latest_activity_sender_name,
            state.latest_activity_preview,
        )

    async def list_active_chats(self, max_age_seconds: float = 1800) -> List[str]:
        now = time.time()
        async with self._lock:
            active: list[tuple[float, str]] = []
            for chat_id, state in self._states.items():
                latest_ts = float(state.latest_activity_ts or 0.0)
                if latest_ts <= 0:
                    continue
                if max_age_seconds and now - latest_ts > max_age_seconds:
                    continue
                active.append((latest_ts, chat_id))
        active.sort(reverse=True)
        return [chat_id for _, chat_id in active]

    async def get_activity_snapshot(self, chat_id: str) -> dict:
        now = time.time()
        async with self._lock:
            state = self._states.get(chat_id)
            if not state:
                return {}
            activity_times = [float(item or 0.0) for item in state.activity_times if float(item or 0.0) > 0.0]
            recent_60s = [item for item in activity_times if now - item <= 60.0]
            recent_5m = [item for item in activity_times if now - item <= 300.0]
            return {
                "chat_id": chat_id,
                "latest_activity_ts": float(state.latest_activity_ts or 0.0),
                "latest_activity_sender_id": state.latest_activity_sender_id,
                "latest_activity_sender_name": state.latest_activity_sender_name,
                "latest_activity_preview": state.latest_activity_preview,
                "latest_activity_thread_signature": state.latest_activity_thread_signature,
                "activity_times": activity_times[:],
                "recent_activity_count_60s": len(recent_60s),
                "recent_activity_count": len(recent_5m),
                "executor_pending": int(state.executor_pending or 0),
                "wait_targets": state.wait_targets[:],
            }

    async def clear_runtime_state(self, chat_id: str) -> bool:
        async with self._lock:
            state = self._states.get(chat_id)
            if not state:
                return False
            state.executor_pending = 0
            state.wait_targets = []
            state.wait_target_name = ""
            state.activity_times = []
            return True

    async def prune_inactive(self, max_idle_sec: float = 1800) -> int:
        now = time.time()
        async with self._lock:
            stale_ids = [
                chat_id for chat_id, state in self._states.items()
                if now - state.latest_activity_ts > max_idle_sec
            ]
            for chat_id in stale_ids:
                del self._states[chat_id]
        return len(stale_ids)

    async def evaluate_reply_freshness(
        self,
        chat_id: str,
        focus_timestamp: float,
        *,
        max_age_seconds: float,
        thread_signature: str = "",
        salvage_window_seconds: float = 6.0,
    ) -> tuple[FreshnessState, str]:
        state = await self._get_state(chat_id)
        if focus_timestamp <= 0:
            return FreshnessState.FRESH, ""

        latest_ts = float(state.latest_activity_ts or 0.0)
        if max_age_seconds > 0 and latest_ts and (latest_ts - focus_timestamp) > max(max_age_seconds, 0.0):
            actor = state.latest_activity_sender_name or state.latest_activity_sender_id or "unknown"
            return FreshnessState.EXPIRED, f"reply_age_exceeded:{actor}:{latest_ts - focus_timestamp:.1f}s"

        if latest_ts <= 0:
            return FreshnessState.FRESH, ""

        newer_delta = latest_ts - focus_timestamp
        if newer_delta <= 4.0:
            return FreshnessState.FRESH, ""

        latest_signature = str(state.latest_activity_thread_signature or "")
        same_thread = bool(thread_signature and latest_signature and thread_signature == latest_signature)
        if same_thread and newer_delta <= max(6.0, salvage_window_seconds):
            return FreshnessState.FRESH, ""

        actor = state.latest_activity_sender_name or state.latest_activity_sender_id or "unknown"
        if newer_delta <= max(6.0, salvage_window_seconds):
            return FreshnessState.STALE_BUT_SALVAGEABLE, f"superseded_by_newer_activity:{actor}:{newer_delta:.1f}s"
        return FreshnessState.EXPIRED, f"superseded_by_newer_activity:{actor}:{newer_delta:.1f}s"
