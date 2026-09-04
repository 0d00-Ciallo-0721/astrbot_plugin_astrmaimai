from __future__ import annotations

import asyncio
import logging
import time
import uuid
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
class ActivityRecord:
    sequence: int
    timestamp: float
    sender_id: str = ""
    sender_name: str = ""
    preview: str = ""
    thread_signature: str = ""
    event_id: str = ""
    is_direct: bool = False


@dataclass
class ExecutorLease:
    """Traceable ownership of one executor slot.

    The lease, rather than the legacy integer counter, is the settlement
    authority.  ``executor_pending`` remains a compatibility projection.
    """

    token: str
    chat_id: str
    thread_id: str = ""
    turn_id: str = ""
    generation: int = 0
    owner_task: asyncio.Task | None = None
    queued_at: float = field(default_factory=time.monotonic)
    acquired_at: float = 0.0
    state: str = "queued"
    lock: asyncio.Lock | None = None

    @property
    def acquired(self) -> bool:
        return self.state == "acquired"

    def locked(self) -> bool:
        """Compatibility probe for legacy lock consumers."""
        return bool(self.lock is not None and self.lock.locked())


@dataclass
class ChatRuntimeState:
    sys2_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sys2_thread_locks: Dict[str, asyncio.Lock] = field(default_factory=dict)
    executor_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    executor_thread_locks: Dict[str, asyncio.Lock] = field(default_factory=dict)
    executor_pending: int = 0
    executor_leases: Dict[str, ExecutorLease] = field(default_factory=dict)
    wait_targets: List[str] = field(default_factory=list)
    wait_target_name: str = ""
    latest_activity_ts: float = 0.0
    latest_activity_sender_id: str = ""
    latest_activity_sender_name: str = ""
    latest_activity_preview: str = ""
    latest_activity_thread_signature: str = ""
    activity_times: List[float] = field(default_factory=list)
    activity_sequence: int = 0
    activity_records: List[ActivityRecord] = field(default_factory=list)
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
        self._generation_sequence = 0
        self._shutdown = False
        self._active_turn_tasks: set[asyncio.Task] = set()

    async def _get_state(self, chat_id: str) -> ChatRuntimeState:
        async with self._lock:
            if self._shutdown:
                return ChatRuntimeState()
            if chat_id not in self._states:
                self._states[chat_id] = ChatRuntimeState()
            return self._states[chat_id]

    @staticmethod
    def _state_has_locked_locks(state: ChatRuntimeState) -> bool:
        locks = [state.sys2_lock, state.executor_lock]
        locks.extend(state.sys2_thread_locks.values())
        locks.extend(state.executor_thread_locks.values())
        return any(callable(getattr(lock, "locked", None)) and lock.locked() for lock in locks)

    async def get_sys2_lock(
        self, chat_id: str, thread_id: str = ""
    ) -> Optional[asyncio.Lock]:
        normalized_chat_id = str(chat_id or "").strip()
        normalized_thread_id = str(thread_id or "").strip()
        async with self._lock:
            if self._shutdown:
                return None
            state = self._states.setdefault(normalized_chat_id, ChatRuntimeState())
            if not normalized_thread_id:
                return state.sys2_lock
            if (
                normalized_thread_id not in state.sys2_thread_locks
                and len(state.sys2_thread_locks) >= self.MAX_THREAD_GENERATIONS_PER_CHAT
            ):
                # The cap is a soft memory bound. Never evict an occupied lock;
                # if every cached generation is active, temporarily grow the
                # cache rather than breaking per-thread serialization.
                evict_key = next(
                        (
                            key
                            for key, lock in state.sys2_thread_locks.items()
                            if key not in state.active_turn_tasks and not lock.locked()
                    ),
                    None,
                )
                if evict_key is not None:
                    state.sys2_thread_locks.pop(evict_key, None)
            return state.sys2_thread_locks.setdefault(normalized_thread_id, asyncio.Lock())

    async def try_acquire_executor(
        self,
        chat_id: str,
        max_pending: int = 2,
        thread_id: str = "",
        turn_id: str = "",
        generation: int = 0,
    ) -> Optional[ExecutorLease]:
        normalized_chat_id = str(chat_id or "").strip()
        normalized_thread_id = str(thread_id or "").strip()
        lease: ExecutorLease
        async with self._lock:
            if self._shutdown:
                return None
            state = self._states.setdefault(normalized_chat_id, ChatRuntimeState())
            if len(state.executor_leases) >= max_pending:
                return None
            if normalized_thread_id:
                if (
                    normalized_thread_id not in state.executor_thread_locks
                    and len(state.executor_thread_locks) >= self.MAX_THREAD_GENERATIONS_PER_CHAT
                ):
                    evict_key = next(
                        (
                            key
                            for key, lock in state.executor_thread_locks.items()
                            if key not in state.active_turn_tasks and not lock.locked()
                        ),
                        None,
                    )
                    if evict_key is not None:
                        state.executor_thread_locks.pop(evict_key, None)
                executor_lock = state.executor_thread_locks.setdefault(
                    normalized_thread_id,
                    asyncio.Lock(),
                )
            else:
                executor_lock = state.executor_lock
            lease = ExecutorLease(
                token=uuid.uuid4().hex,
                chat_id=normalized_chat_id,
                thread_id=normalized_thread_id,
                turn_id=str(turn_id or "").strip(),
                generation=int(generation or 0),
                owner_task=asyncio.current_task(),
                lock=executor_lock,
            )
            state.executor_leases[lease.token] = lease
            state.executor_pending = len(state.executor_leases)
        try:
            await executor_lock.acquire()
            # Mark the local handle before the next await.  If cancellation
            # lands while reacquiring the coordinator mutex, release can still
            # distinguish an owned lock from a queued lease.
            lease.state = "acquired"
            lease.acquired_at = time.monotonic()
            async with self._lock:
                current = self._states.get(normalized_chat_id)
                current_lease = current.executor_leases.get(lease.token) if current else None
                if current_lease is None or current_lease.state == "released":
                    # A concurrent reconciliation settled this owner while it
                    # was waiting; do not hand out a stale lock.
                    if executor_lock.locked():
                        executor_lock.release()
                    return None
                current_lease.state = "acquired"
                current_lease.acquired_at = float(lease.acquired_at or time.monotonic())
                current_lease.owner_task = asyncio.current_task()
                current.executor_pending = len(current.executor_leases)
        except asyncio.CancelledError:
            # Settlement runs independently of the cancelled acquire task so
            # the queued lease cannot remain in executor_pending.
            settlement = asyncio.create_task(
                self.release_executor(normalized_chat_id, lease=lease)
            )
            def _consume_settlement(task: asyncio.Task) -> None:
                if task.cancelled():
                    return
                try:
                    task.exception()
                except Exception:
                    return
            settlement.add_done_callback(_consume_settlement)
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError:
                # A second cancellation must not cancel the settlement task.
                pass
            raise
        return lease

    async def release_executor(
        self,
        chat_id: str,
        thread_id: str = "",
        *,
        lease: ExecutorLease | str | None = None,
        generation: int | None = None,
        turn_id: str = "",
    ) -> bool:
        executor_lease: ExecutorLease | None = None
        normalized_chat_id = str(chat_id or "").strip()
        async with self._lock:
            state = self._states.get(normalized_chat_id)
            if not state:
                return False
            token = lease.token if isinstance(lease, ExecutorLease) else str(lease or "").strip()
            if token:
                executor_lease = state.executor_leases.get(token)
            else:
                normalized_thread_id = str(thread_id or "").strip()
                candidates = [
                    item for item in state.executor_leases.values()
                    if (not normalized_thread_id or item.thread_id == normalized_thread_id)
                ]
                if candidates:
                    executor_lease = min(candidates, key=lambda item: item.queued_at)
            if executor_lease is None:
                return False
            if generation is not None and int(generation or 0) != int(executor_lease.generation or 0):
                return False
            if turn_id and str(turn_id) != str(executor_lease.turn_id):
                return False
            was_acquired = executor_lease.acquired
            state.executor_leases.pop(executor_lease.token, None)
            executor_lease.state = "released"
            executor_lease.owner_task = None
            state.executor_pending = len(state.executor_leases)
            normalized_thread_id = str(thread_id or "").strip()
            executor_lock = executor_lease.lock or (
                state.executor_thread_locks.get(normalized_thread_id)
                if normalized_thread_id else state.executor_lock
            )
            if was_acquired and executor_lock is not None and executor_lock.locked():
                executor_lock.release()
            return True

    async def reconcile_executor_leases(self, chat_id: str | None = None) -> int:
        """Settle leases whose owner task has ended without running finally."""
        settled = 0
        async with self._lock:
            states = (
                {str(chat_id or "").strip(): self._states.get(str(chat_id or "").strip())}
                if chat_id is not None else dict(self._states)
            )
            for state in states.values():
                if state is None:
                    continue
                for item in list(state.executor_leases.values()):
                    owner = item.owner_task
                    if owner is not None and not owner.done():
                        continue
                    was_acquired = item.acquired
                    state.executor_leases.pop(item.token, None)
                    item.state = "reconciled"
                    item.owner_task = None
                    if was_acquired and item.lock is not None and item.lock.locked():
                        item.lock.release()
                    settled += 1
                state.executor_pending = len(state.executor_leases)
        if settled:
            await self.record_concurrency_event("executor_lease_reconciled", settled)
        return settled

    async def update_wait_targets(self, chat_id: str, targets: List[str], target_name: str = "") -> None:
        async with self._lock:
            if self._shutdown:
                return
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
            if self._shutdown:
                return 0
            state = self._states.setdefault(normalized_chat_id, ChatRuntimeState())
            if (
                normalized_thread_id not in state.turn_generations
                and len(state.turn_generations) >= self.MAX_THREAD_GENERATIONS_PER_CHAT
            ):
                state.turn_generations.pop(next(iter(state.turn_generations)), None)
            self._generation_sequence += 1
            next_generation = self._generation_sequence
            state.turn_generations[normalized_thread_id] = next_generation
            stale_task = state.active_turn_tasks.pop(normalized_thread_id, None)
            if stale_task is not None:
                self._active_turn_tasks.discard(stale_task)
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
            if self._shutdown:
                return False
            state = self._states.setdefault(chat_id, ChatRuntimeState())
            if int(state.turn_generations.get(thread_id, 0) or 0) != generation:
                self._increment_metric_locked("stale_turn_rejected")
                return False
            previous_task = state.active_turn_tasks.get(thread_id)
            state.active_turn_tasks[thread_id] = task
            if previous_task is not None:
                self._active_turn_tasks.discard(previous_task)
            self._active_turn_tasks.add(task)
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
                self._active_turn_tasks.discard(task)

    def active_turn_task_count_sync(self) -> int:
        return sum(1 for task in self._active_turn_tasks if not task.done())

    async def current_generation(self, chat_id: str, thread_id: str) -> int:
        normalized_chat_id, normalized_thread_id = self._normalize_thread_key(chat_id, thread_id)
        async with self._lock:
            state = self._states.get(normalized_chat_id)
            if not state:
                return 0
            return int(state.turn_generations.get(normalized_thread_id, 0) or 0)

    async def is_current_turn(self, turn: Any) -> bool:
        async with self._lock:
            if self._shutdown:
                return False
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
            if self._shutdown:
                return False
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
                terminal_key = next(
                    (
                        key
                        for key, claim in state.send_claims.items()
                        if claim.status in {"committed", "failed"}
                    ),
                    None,
                )
                if terminal_key is None:
                    self._increment_metric_locked("send_claim_capacity_rejected")
                    return False
                state.send_claims.pop(terminal_key, None)
                self._increment_metric_locked("send_claim_terminal_evicted")
            state.send_claims[normalized_send_key] = SendClaimState()
            self._increment_metric_locked("send_claimed")
            return True

    async def commit_send(self, chat_id: str, send_key: str, outbound_message_ids: List[str] | None = None) -> bool:
        normalized_chat_id = str(chat_id or "").strip()
        normalized_send_key = str(send_key or "").strip()
        if not normalized_send_key:
            return False
        async with self._lock:
            if self._shutdown:
                return False
            state = self._states.setdefault(normalized_chat_id, ChatRuntimeState())
            claim = state.send_claims.setdefault(normalized_send_key, SendClaimState())
            claim.status = "committed"
            claim.outbound_message_ids = list(dict.fromkeys(str(item) for item in (outbound_message_ids or []) if str(item)))
            claim.committed_at = time.time()
            self._increment_metric_locked("send_committed")
            return True

    async def mark_send_failed(self, chat_id: str, send_key: str, error: str = "") -> bool:
        normalized_chat_id = str(chat_id or "").strip()
        normalized_send_key = str(send_key or "").strip()
        if not normalized_send_key:
            return False
        async with self._lock:
            if self._shutdown:
                return False
            state = self._states.setdefault(normalized_chat_id, ChatRuntimeState())
            claim = state.send_claims.setdefault(normalized_send_key, SendClaimState())
            claim.status = "failed"
            claim.error = str(error or "")[:300]
            self._increment_metric_locked("send_failed")
            return True

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

    async def get_latest_committed_outbound(
        self,
        chat_id: str,
        *,
        exclude_send_key: str = "",
    ) -> List[str]:
        normalized_chat_id = str(chat_id or "").strip()
        excluded = str(exclude_send_key or "").strip()
        async with self._lock:
            state = self._states.get(normalized_chat_id)
            if not state:
                return []
            candidates = [
                claim
                for key, claim in state.send_claims.items()
                if key != excluded and claim.status == "committed" and claim.outbound_message_ids
            ]
            if not candidates:
                return []
            latest = max(candidates, key=lambda item: float(item.committed_at or item.claimed_at or 0.0))
            return latest.outbound_message_ids[:]

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
        *,
        event_id: str = "",
        is_direct: bool = False,
    ) -> int:
        async with self._lock:
            if self._shutdown:
                return 0
            state = self._states.setdefault(chat_id, ChatRuntimeState())
            if timestamp < state.latest_activity_ts:
                return int(state.activity_sequence or 0)
            state.activity_sequence = int(state.activity_sequence or 0) + 1
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
            state.activity_records = [
                item
                for item in state.activity_records
                if state.latest_activity_ts - float(item.timestamp or 0.0) <= 1800.0
            ][-63:] + [
                ActivityRecord(
                    sequence=state.activity_sequence,
                    timestamp=state.latest_activity_ts,
                    sender_id=state.latest_activity_sender_id,
                    sender_name=state.latest_activity_sender_name,
                    preview=state.latest_activity_preview,
                    thread_signature=state.latest_activity_thread_signature,
                    event_id=str(event_id or ""),
                    is_direct=bool(is_direct),
                )
            ]
            return state.activity_sequence

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
            now_monotonic = time.monotonic()
            lease_items = list(state.executor_leases.values())
            oldest_lease = min(lease_items, key=lambda item: item.queued_at, default=None)
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
                "activity_watermark": int(state.activity_sequence or 0),
                "activity_record_count": len(state.activity_records),
                "recent_activity_count_60s": len(recent_60s),
                "recent_activity_count": len(recent_5m),
                "executor_pending": len(state.executor_leases),
                "executor_lease_count": len(lease_items),
                "oldest_executor_lease_age_ms": (
                    round(max(0.0, now_monotonic - oldest_lease.queued_at) * 1000.0, 1)
                    if oldest_lease is not None else 0.0
                ),
                "executor_lease_owners": sorted({
                    str(item.owner_task.get_name())
                    for item in lease_items
                    if item.owner_task is not None and hasattr(item.owner_task, "get_name")
                }),
                "executor_leases": [
                    {
                        "token": item.token,
                        "thread_id": item.thread_id,
                        "turn_id": item.turn_id,
                        "generation": item.generation,
                        "state": item.state,
                        "queued_at": float(item.queued_at),
                        "acquired_at": float(item.acquired_at or 0.0),
                        "owner_task": (
                            item.owner_task.get_name()
                            if item.owner_task is not None and hasattr(item.owner_task, "get_name")
                            else ""
                        ),
                        "owner_done": bool(item.owner_task.done()) if item.owner_task is not None else True,
                    }
                    for item in state.executor_leases.values()
                ],
                "wait_targets": state.wait_targets[:],
                "turn_generations": dict(state.turn_generations),
                "active_turn_task_count": len(state.active_turn_tasks),
                "send_claim_count": len(state.send_claims),
            }

    async def clear_runtime_state(self, chat_id: str) -> bool:
        tasks: list[asyncio.Task] = []
        current_task = asyncio.current_task()
        async with self._lock:
            state = self._states.get(chat_id)
            if state is not None:
                tasks = [task for task in state.active_turn_tasks.values() if task is not current_task]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            current_state = self._states.get(chat_id)
            if current_state is state and state is not None:
                if not state.active_turn_tasks and not state.executor_leases and not self._state_has_locked_locks(state):
                    self._states.pop(chat_id, None)
        return state is not None

    async def shutdown(self, *, timeout_sec: float = 1.0) -> int:
        current_task = asyncio.current_task()
        async with self._lock:
            if self._shutdown:
                return 0
            self._shutdown = True
            tasks = [
                task
                for state in self._states.values()
                for task in state.active_turn_tasks.values()
                if task is not current_task and not task.done()
            ]
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=max(0.0, float(timeout_sec)))
            for task in done:
                try:
                    task.result()
                except (asyncio.CancelledError, Exception):
                    pass
            for task in pending:
                task.add_done_callback(self._consume_shutdown_task)
        # Keep states visible while late owners settle; this avoids hiding a
        # stale pending lease behind shutdown.  Explicit reopen starts a fresh
        # coordinator generation and clears them.
        async with self._lock:
            for state in self._states.values():
                for thread_id, task in list(state.active_turn_tasks.items()):
                    if task.done():
                        state.active_turn_tasks.pop(thread_id, None)
                        self._active_turn_tasks.discard(task)
        await self.reconcile_executor_leases()
        async with self._lock:
            removable = [
                chat_id
                for chat_id, state in self._states.items()
                if not state.active_turn_tasks
                and not state.executor_leases
                and not self._state_has_locked_locks(state)
            ]
            for chat_id in removable:
                self._states.pop(chat_id, None)
            if not self._states:
                self._active_turn_tasks.clear()
        return len(tasks)

    @staticmethod
    def _consume_shutdown_task(task: asyncio.Task[Any]) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    async def reopen(self) -> None:
        """Re-enable the coordinator after an explicit plugin reinitialize."""
        async with self._lock:
            self._states.clear()
            self._shutdown = False

    async def prune_inactive(self, max_idle_sec: float = 1800) -> int:
        now = time.time()
        async with self._lock:
            stale_ids = [
                chat_id for chat_id, state in self._states.items()
                if (
                    now - state.latest_activity_ts > max_idle_sec
                    and not state.active_turn_tasks
                    and not state.executor_leases
                    and not self._state_has_locked_locks(state)
                )
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
        allow_parallel_threads: bool = False,
        focus_sender_id: str = "",
        focus_watermark: int = 0,
    ) -> tuple[FreshnessState, str]:
        state = await self._get_state(chat_id)
        if focus_timestamp <= 0:
            return FreshnessState.FRESH, ""

        latest_ts = float(state.latest_activity_ts or 0.0)
        latest_signature = str(state.latest_activity_thread_signature or "")
        if focus_watermark > 0 and focus_sender_id:
            newer_records = [
                record
                for record in state.activity_records
                if int(record.sequence or 0) > int(focus_watermark or 0)
            ]
            same_actor_direct = [
                record
                for record in newer_records
                if record.sender_id == str(focus_sender_id or "") and record.is_direct
            ]
            if same_actor_direct:
                newest = same_actor_direct[-1]
                delta = max(0.0, float(newest.timestamp or 0.0) - float(focus_timestamp or 0.0))
                actor = newest.sender_name or newest.sender_id or "unknown"
                return (
                    FreshnessState.STALE_BUT_SALVAGEABLE,
                    f"same_actor_direct_update:{actor}:{delta:.1f}s",
                )
        different_known_thread = bool(
            thread_signature
            and latest_signature
            and thread_signature != latest_signature
        )
        if allow_parallel_threads and different_known_thread:
            return FreshnessState.FRESH, "newer_activity_other_thread_ignored"
        if max_age_seconds > 0 and latest_ts and (latest_ts - focus_timestamp) > max(max_age_seconds, 0.0):
            actor = state.latest_activity_sender_name or state.latest_activity_sender_id or "unknown"
            return FreshnessState.EXPIRED, f"reply_age_exceeded:{actor}:{latest_ts - focus_timestamp:.1f}s"

        if latest_ts <= 0:
            return FreshnessState.FRESH, ""

        newer_delta = latest_ts - focus_timestamp
        if newer_delta <= 4.0:
            return FreshnessState.FRESH, ""

        same_thread = bool(thread_signature and latest_signature and thread_signature == latest_signature)
        if same_thread and newer_delta <= max(6.0, salvage_window_seconds):
            return FreshnessState.FRESH, ""

        actor = state.latest_activity_sender_name or state.latest_activity_sender_id or "unknown"
        reason_kind = (
            "superseded_by_newer_activity_same_thread"
            if same_thread
            else "superseded_by_newer_activity_unknown_thread"
        )
        if newer_delta <= max(6.0, salvage_window_seconds):
            return FreshnessState.STALE_BUT_SALVAGEABLE, f"{reason_kind}:{actor}:{newer_delta:.1f}s"
        return FreshnessState.EXPIRED, f"{reason_kind}:{actor}:{newer_delta:.1f}s"
