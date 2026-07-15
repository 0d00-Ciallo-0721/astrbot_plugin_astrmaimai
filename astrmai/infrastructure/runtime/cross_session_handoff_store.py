from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, replace


@dataclass(slots=True)
class CrossSessionHandoff:
    platform_id: str
    source_umo: str
    source_sender_id: str
    source_sender_name: str
    target_umo: str
    target_id: str
    target_name: str
    outbound_message: str
    context_summary: str
    delivery_mode: str
    created_at: float = 0.0
    expires_at: float = 0.0
    handoff_id: str = ""
    observed_turns: int = 0

    def __post_init__(self) -> None:
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.expires_at:
            self.expires_at = self.created_at + 1800.0
        if not self.handoff_id:
            self.handoff_id = uuid.uuid4().hex


class CrossSessionHandoffStore:
    DEFAULT_TTL_SECONDS = 1800.0
    MAX_HANDOFFS_PER_RECIPIENT = 4
    MAX_RECIPIENTS = 256
    MAX_OBSERVED_TURNS = 3

    def __init__(self) -> None:
        self._handoffs: dict[tuple[str, str], list[CrossSessionHandoff]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(platform_id: str, target_id: str) -> tuple[str, str]:
        return (
            str(platform_id or "default").strip() or "default",
            str(target_id or "").strip(),
        )

    def _prune_expired_locked(self, now: float) -> None:
        empty_keys: list[tuple[str, str]] = []
        for key, entries in self._handoffs.items():
            active = [entry for entry in entries if float(entry.expires_at or 0.0) > now]
            if active:
                self._handoffs[key] = active[-self.MAX_HANDOFFS_PER_RECIPIENT :]
            else:
                empty_keys.append(key)
        for key in empty_keys:
            self._handoffs.pop(key, None)

    async def put(self, handoff: CrossSessionHandoff) -> str:
        key = self._key(handoff.platform_id, handoff.target_id)
        if not key[1]:
            raise ValueError("target_id is required")
        now = time.time()
        if handoff.expires_at <= now:
            handoff.expires_at = now + self.DEFAULT_TTL_SECONDS
        async with self._lock:
            self._prune_expired_locked(now)
            if key not in self._handoffs and len(self._handoffs) >= self.MAX_RECIPIENTS:
                oldest_key = min(
                    self._handoffs,
                    key=lambda item: min(entry.created_at for entry in self._handoffs[item]),
                )
                self._handoffs.pop(oldest_key, None)
            entries = self._handoffs.setdefault(key, [])
            entries.append(replace(handoff))
            self._handoffs[key] = entries[-self.MAX_HANDOFFS_PER_RECIPIENT :]
        return handoff.handoff_id

    async def peek_for_recipient(
        self,
        platform_id: str,
        target_id: str,
    ) -> CrossSessionHandoff | None:
        key = self._key(platform_id, target_id)
        if not key[1]:
            return None
        now = time.time()
        async with self._lock:
            self._prune_expired_locked(now)
            entries = self._handoffs.get(key, [])
            if not entries:
                return None
            return replace(entries[-1])

    async def acknowledge(self, handoff_id: str) -> bool:
        normalized_id = str(handoff_id or "").strip()
        if not normalized_id:
            return False
        async with self._lock:
            for key, entries in list(self._handoffs.items()):
                for index, entry in enumerate(entries):
                    if entry.handoff_id != normalized_id:
                        continue
                    entry.observed_turns += 1
                    if entry.observed_turns >= self.MAX_OBSERVED_TURNS:
                        entries.pop(index)
                        if not entries:
                            self._handoffs.pop(key, None)
                    return True
        return False

    async def complete_for_recipient(self, platform_id: str, target_id: str) -> bool:
        key = self._key(platform_id, target_id)
        async with self._lock:
            entries = self._handoffs.get(key, [])
            if not entries:
                return False
            entries.pop()
            if not entries:
                self._handoffs.pop(key, None)
            return True

    async def clear(self) -> None:
        async with self._lock:
            self._handoffs.clear()
