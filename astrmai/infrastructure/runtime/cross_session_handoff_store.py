from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..persistence.sqlite_helpers import connect_sqlite


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

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._handoffs: dict[tuple[str, str], list[CrossSessionHandoff]] = {}
        self._lock = asyncio.Lock()
        self.db_path = Path(db_path) if db_path else None
        self._hydrated_keys: set[tuple[str, str]] = set()

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

    def _persist_sync(self, handoff: CrossSessionHandoff, status: str = "active") -> None:
        if self.db_path is None:
            return
        now = time.time()
        with connect_sqlite(self.db_path) as db:
            db.execute(
                """
                INSERT INTO cross_session_handoff (
                    handoff_id, platform_id, source_umo, source_sender_id,
                    source_sender_name, target_umo, target_id, target_name,
                    outbound_message, context_summary, delivery_mode,
                    observed_turns, status, created_at, expires_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(handoff_id) DO UPDATE SET
                    observed_turns=excluded.observed_turns,
                    status=excluded.status,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                """,
                (
                    handoff.handoff_id,
                    handoff.platform_id,
                    handoff.source_umo,
                    handoff.source_sender_id,
                    handoff.source_sender_name,
                    handoff.target_umo,
                    handoff.target_id,
                    handoff.target_name,
                    handoff.outbound_message,
                    handoff.context_summary,
                    handoff.delivery_mode,
                    handoff.observed_turns,
                    status,
                    handoff.created_at,
                    handoff.expires_at,
                    now,
                ),
            )
            db.commit()

    def _load_key_sync(self, key: tuple[str, str], now: float) -> list[CrossSessionHandoff]:
        if self.db_path is None:
            return []
        with connect_sqlite(self.db_path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """
                SELECT * FROM cross_session_handoff
                WHERE platform_id=? AND target_id=? AND status='active' AND expires_at>?
                ORDER BY created_at DESC LIMIT ?
                """,
                (key[0], key[1], now, self.MAX_HANDOFFS_PER_RECIPIENT),
            ).fetchall()
        return [
            CrossSessionHandoff(
                platform_id=row["platform_id"],
                source_umo=row["source_umo"],
                source_sender_id=row["source_sender_id"],
                source_sender_name=row["source_sender_name"],
                target_umo=row["target_umo"],
                target_id=row["target_id"],
                target_name=row["target_name"],
                outbound_message=row["outbound_message"],
                context_summary=row["context_summary"],
                delivery_mode=row["delivery_mode"],
                created_at=float(row["created_at"] or 0.0),
                expires_at=float(row["expires_at"] or 0.0),
                handoff_id=row["handoff_id"],
                observed_turns=int(row["observed_turns"] or 0),
            )
            for row in reversed(rows)
        ]

    def _set_status_sync(self, handoff_id: str, status: str, observed_turns: int | None = None) -> None:
        if self.db_path is None:
            return
        with connect_sqlite(self.db_path) as db:
            if observed_turns is None:
                db.execute(
                    "UPDATE cross_session_handoff SET status=?, updated_at=? WHERE handoff_id=?",
                    (status, time.time(), handoff_id),
                )
            else:
                db.execute(
                    "UPDATE cross_session_handoff SET status=?, observed_turns=?, updated_at=? WHERE handoff_id=?",
                    (status, observed_turns, time.time(), handoff_id),
                )
            db.commit()

    async def _hydrate_key_locked(self, key: tuple[str, str], now: float) -> None:
        if key in self._hydrated_keys or self.db_path is None:
            return
        loaded = await asyncio.to_thread(self._load_key_sync, key, now)
        if loaded:
            self._handoffs[key] = [
                *loaded,
                *(entry for entry in self._handoffs.get(key, []) if entry.handoff_id not in {item.handoff_id for item in loaded}),
            ][-self.MAX_HANDOFFS_PER_RECIPIENT :]
        self._hydrated_keys.add(key)

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
            self._hydrated_keys.add(key)
        await asyncio.to_thread(self._persist_sync, handoff)
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
            await self._hydrate_key_locked(key, now)
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
                    status = "active"
                    if entry.observed_turns >= self.MAX_OBSERVED_TURNS:
                        status = "completed"
                        entries.pop(index)
                        if not entries:
                            self._handoffs.pop(key, None)
                    await asyncio.to_thread(
                        self._set_status_sync,
                        entry.handoff_id,
                        status,
                        entry.observed_turns,
                    )
                    return True
        return False

    async def complete_for_recipient(self, platform_id: str, target_id: str) -> bool:
        key = self._key(platform_id, target_id)
        async with self._lock:
            await self._hydrate_key_locked(key, time.time())
            entries = self._handoffs.get(key, [])
            if not entries:
                return False
            completed = entries.pop()
            if not entries:
                self._handoffs.pop(key, None)
            await asyncio.to_thread(self._set_status_sync, completed.handoff_id, "completed")
            return True

    async def clear(self) -> None:
        async with self._lock:
            self._handoffs.clear()
            self._hydrated_keys.clear()
