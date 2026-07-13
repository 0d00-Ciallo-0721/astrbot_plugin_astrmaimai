from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .runtime_contracts import FreshnessState

try:
    from astrbot.api import logger
except Exception:
    logger = logging.getLogger(__name__)


@dataclass
class SendClaimState:
    status: str = "claimed"
    outbound_message_ids: List[str] = field(default_factory=list)
    error: str = ""
    claimed_at: float = field(default_factory=time.time)
    committed_at: float = 0.0


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
    turn_generations: Dict[str, int] = field(default_factory=dict)
    active_turn_tasks: Dict[str, asyncio.Task] = field(default_factory=dict)
    send_claims: Dict[str, SendClaimState] = field(default_factory=dict)


class ChatRuntimeCoordinator:
    MAX_THREAD_GENERATIONS_PER_CHAT = 128
    MAX_SEND_CLAIMS_PER_CHAT = 256

    def __init__(self) -> None:
        self._states: Dict[str, ChatRuntimeState] = {}
        self._lock = asyncio.Lock()
        self._concurrency_metrics: Dict[str, int] = {}

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

    @staticmethod
    def _normalize_thread_key(chat_id: str, thread_id: str) -> tuple[str, str]:
        normalized_chat_id = str(chat_id or "").strip()
        normalized_thread_id = str(thread_id or "").strip() or normalized_chat_id
        return normalized_chat_id, normalized_thread_id

    async def advance_generation(self, chat_id: str, thread_id: str) -> int:
        normalized_chat_id, normalized_thread_id = self._normalize_thread_key(chat_id, thread_id)
        stale_task: asyncio.Task | None = None
        async with self._lock:
            state = self._states.setdefault(normalized_chat_id, ChatRuntimeState())
            if (
                normalized_thread_id not in state.turn_generations
                and len(state.turn_generations) >= self.MAX_THREAD_GENERATIONS_PER_CHAT
            ):
                state.turn_generations.pop(next(iter(state.turn_generations)), None)
            next_generation = int(state.turn_generations.get(normalized_thread_id, 0) or 0) + 1
            state.turn_generations[normalized_thread_id] = next_generation
            stale_task = state.active_turn_tasks.pop(normalized_thread_id, None)
            self._increment_metric_locked("generation_advanced")
            if stale_task is not None and not stale_task.done():
                self._increment_metric_locked("stale_turn_cancelled")
        if stale_task is not None and not stale_task.done():
            stale_task.cancel()
        return next_generation

    async def register_turn_task(self, turn: Any, task: asyncio.Task) -> bool:
        if turn is None:
            return True
        chat_id, thread_id = self._normalize_thread_key(
            getattr(turn, "chat_id", ""),
            getattr(turn, "thread_id", ""),
        )
        generation = int(getattr(turn, "generation", 0) or 0)
        if not chat_id or not thread_id or generation <= 0:
            return True
        previous_task: asyncio.Task | None = None
        async with self._lock:
            state = self._states.setdefault(chat_id, ChatRuntimeState())
            if int(state.turn_generations.get(thread_id, 0) or 0) != generation:
                self._increment_metric_locked("stale_turn_rejected")
                return False
            previous_task = state.active_turn_tasks.get(thread_id)
            state.active_turn_tasks[thread_id] = task
        if previous_task is not None and previous_task is not task and not previous_task.done():
            previous_task.cancel()
        return True

    async def unregister_turn_task(self, turn: Any, task: asyncio.Task) -> None:
        if turn is None:
            return
        chat_id, thread_id = self._normalize_thread_key(
            getattr(turn, "chat_id", ""),
            getattr(turn, "thread_id", ""),
        )
        async with self._lock:
            state = self._states.get(chat_id)
            if state is not None and state.active_turn_tasks.get(thread_id) is task:
                state.active_turn_tasks.pop(thread_id, None)

    async def current_generation(self, chat_id: str, thread_id: str) -> int:
        normalized_chat_id, normalized_thread_id = self._normalize_thread_key(chat_id, thread_id)
        async with self._lock:
            state = self._states.get(normalized_chat_id)
            if not state:
                return 0
            return int(state.turn_generations.get(normalized_thread_id, 0) or 0)

    async def is_current_turn(self, turn: Any) -> bool:
        if turn is None:
            return True
        try:
            chat_id = str(getattr(turn, "chat_id", "") or "").strip()
            thread_id = str(getattr(turn, "thread_id", "") or "").strip()
            generation = int(getattr(turn, "generation", 0) or 0)
        except Exception:
            logger.debug("[ChatRuntimeCoordinator] malformed turn identity; preserving legacy reply behavior", exc_info=True)
            return True
        if not chat_id or not thread_id or generation <= 0:
            return True
        return await self.current_generation(chat_id, thread_id) == generation

    async def claim_send(self, chat_id: str, send_key: str) -> bool:
        normalized_chat_id = str(chat_id or "").strip()
        normalized_send_key = str(send_key or "").strip()
        if not normalized_send_key:
            return False
        async with self._lock:
            state = self._states.setdefault(normalized_chat_id, ChatRuntimeState())
            existing = state.send_claims.get(normalized_send_key)
            if existing is not None and existing.status == "failed":
                existing.status = "claimed"
                existing.claimed_at = time.time()
                existing.committed_at = 0.0
                existing.outbound_message_ids = []
                existing.error = ""
                self._increment_metric_locked("send_claim_retried")
                return True
            if existing is not None:
                self._increment_metric_locked("send_claim_exists")
                return False
            if len(state.send_claims) >= self.MAX_SEND_CLAIMS_PER_CHAT:
                state.send_claims.pop(next(iter(state.send_claims)), None)
            state.send_claims[normalized_send_key] = SendClaimState()
            self._increment_metric_locked("send_claimed")
            return True

    async def commit_send(self, chat_id: str, send_key: str, outbound_message_ids: List[str] | None = None) -> None:
        normalized_chat_id = str(chat_id or "").strip()
        normalized_send_key = str(send_key or "").strip()
        if not normalized_send_key:
            return
        async with self._lock:
            state = self._states.setdefault(normalized_chat_id, ChatRuntimeState())
            claim = state.send_claims.setdefault(normalized_send_key, SendClaimState())
            claim.status = "committed"
            claim.outbound_message_ids = list(dict.fromkeys(str(item) for item in (outbound_message_ids or []) if str(item)))
            claim.committed_at = time.time()
            self._increment_metric_locked("send_committed")

    async def mark_send_failed(self, chat_id: str, send_key: str, error: str = "") -> None:
        normalized_chat_id = str(chat_id or "").strip()
        normalized_send_key = str(send_key or "").strip()
        if not normalized_send_key:
            return
        async with self._lock:
            state = self._states.setdefault(normalized_chat_id, ChatRuntimeState())
            claim = state.send_claims.setdefault(normalized_send_key, SendClaimState())
            claim.status = "failed"
            claim.error = str(error or "")[:300]
            self._increment_metric_locked("send_failed")

    async def get_send_claim(self, chat_id: str, send_key: str) -> Optional[dict]:
        normalized_chat_id = str(chat_id or "").strip()
        normalized_send_key = str(send_key or "").strip()
        if not normalized_send_key:
            return None
        async with self._lock:
            state = self._states.get(normalized_chat_id)
            if not state:
                return None
            claim = state.send_claims.get(normalized_send_key)
            if not claim:
                return None
            return {
                "status": claim.status,
                "outbound_message_ids": claim.outbound_message_ids[:],
                "error": claim.error,
                "claimed_at": float(claim.claimed_at or 0.0),
                "committed_at": float(claim.committed_at or 0.0),
            }

    def _increment_metric_locked(self, event_name: str, amount: int = 1) -> None:
        key = str(event_name or "unknown")
        self._concurrency_metrics[key] = int(self._concurrency_metrics.get(key, 0) or 0) + max(
            0, int(amount or 0)
        )

    async def record_concurrency_event(self, event_name: str, amount: int = 1) -> None:
        async with self._lock:
            self._increment_metric_locked(event_name, amount)

    async def get_concurrency_metrics(self) -> dict[str, int]:
        async with self._lock:
            return dict(self._concurrency_metrics)

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
                "turn_generations": dict(state.turn_generations),
                "active_turn_task_count": len(state.active_turn_tasks),
                "send_claim_count": len(state.send_claims),
            }

    async def clear_runtime_state(self, chat_id: str) -> bool:
        tasks: list[asyncio.Task] = []
        async with self._lock:
            state = self._states.pop(chat_id, None)
            if state is not None:
                tasks = list(state.active_turn_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        return state is not None

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
