from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .sqlite_helpers import connect_aiosqlite


class AttentionDeferredOutboxStore:
    """Durable metadata store for deferred attention work.

    Callable retry factories stay in memory.  The persisted envelope is
    intentionally limited to event metadata, so a restart can reconstruct a
    safe synthetic event without serializing arbitrary Python callables.
    """

    def __init__(self, db_path: str | Path | None) -> None:
        self.db_path = str(db_path) if db_path else ""

    async def _ensure_schema(self) -> None:
        if not self.db_path:
            return
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS attention_deferred_outbox (
                    work_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    task_name TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    event_json TEXT NOT NULL DEFAULT '{}',
                    turn_thread_id TEXT NOT NULL DEFAULT '',
                    turn_generation INTEGER NOT NULL DEFAULT 0,
                    worker_generation INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    expires_at REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'queued',
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_until REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS ix_attention_deferred_due "
                "ON attention_deferred_outbox(status, next_retry_at, created_at)"
            )
            await db.commit()

    async def enqueue(self, item: dict[str, Any], *, event_data: dict[str, Any]) -> bool:
        if not self.db_path:
            return False
        await self._ensure_schema()
        now = time.time()
        work_id = str(item.get("work_id") or "").strip()
        if not work_id:
            return False
        payload = json.dumps(event_data or {}, ensure_ascii=False, default=str)
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO attention_deferred_outbox(
                    work_id, chat_id, task_name, reason, event_json,
                    turn_thread_id, turn_generation, worker_generation,
                    attempts, max_attempts, next_retry_at, expires_at,
                    status, lease_token, lease_until, created_at, updated_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', '', 0, ?, ?, '')
                ON CONFLICT(work_id) DO UPDATE SET
                    chat_id=excluded.chat_id, task_name=excluded.task_name,
                    reason=excluded.reason, event_json=excluded.event_json,
                    turn_thread_id=excluded.turn_thread_id,
                    turn_generation=excluded.turn_generation,
                    worker_generation=excluded.worker_generation,
                    attempts=excluded.attempts, max_attempts=excluded.max_attempts,
                    next_retry_at=excluded.next_retry_at, expires_at=excluded.expires_at,
                    status='queued', lease_token='', lease_until=0,
                    updated_at=excluded.updated_at, last_error=''
                """,
                (
                    work_id,
                    str(item.get("chat_id") or ""),
                    str(item.get("task_name") or "attention.misc"),
                    str(item.get("reason") or "queue_timeout"),
                    payload,
                    str(item.get("turn_thread_id") or ""),
                    int(item.get("turn_generation", 0) or 0),
                    int(item.get("worker_generation", 0) or 0),
                    int(item.get("attempts", 0) or 0),
                    max(1, int(item.get("max_attempts", 3) or 3)),
                    float(item.get("next_retry_at_wall", now) or now),
                    float(item.get("expires_at", now) or now),
                    now,
                    now,
                ),
            )
            await db.commit()
        return True

    async def claim_due(self, *, limit: int = 32, lease_seconds: float = 60.0) -> list[dict[str, Any]]:
        if not self.db_path:
            return []
        await self._ensure_schema()
        now = time.time()
        token = uuid.uuid4().hex
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "UPDATE attention_deferred_outbox SET status='queued', lease_token='', lease_until=0, updated_at=? "
                "WHERE status='inflight' AND lease_until > 0 AND lease_until <= ?",
                (now, now),
            )
            cursor = await db.execute(
                "SELECT work_id, chat_id, task_name, reason, event_json, turn_thread_id, "
                "turn_generation, worker_generation, attempts, max_attempts, next_retry_at, expires_at "
                "FROM attention_deferred_outbox WHERE status='queued' AND next_retry_at <= ? "
                "ORDER BY next_retry_at ASC, created_at ASC LIMIT ?",
                (now, max(1, min(int(limit), 128))),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            if rows:
                ids = [str(row[0]) for row in rows]
                placeholders = ",".join("?" for _ in ids)
                await db.execute(
                    f"UPDATE attention_deferred_outbox SET status='inflight', lease_token=?, lease_until=?, updated_at=? WHERE work_id IN ({placeholders})",
                    [token, now + max(1.0, float(lease_seconds or 60.0)), now, *ids],
                )
            await db.commit()
        fields = (
            "work_id", "chat_id", "task_name", "reason", "event_json", "turn_thread_id",
            "turn_generation", "worker_generation", "attempts", "max_attempts",
            "next_retry_at_wall", "expires_at",
        )
        result = []
        for row in rows:
            item = dict(zip(fields, row))
            try:
                item["event_data"] = json.loads(str(item.pop("event_json") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                item["event_data"] = {}
            item["lease_token"] = token
            result.append(item)
        return result

    async def finish(
        self,
        work_id: str,
        *,
        lease_token: str,
        status: str,
        attempts: int = 0,
        next_retry_at: float = 0.0,
        error: str = "",
    ) -> bool:
        if not self.db_path:
            return False
        await self._ensure_schema()
        normalized = str(status or "failed")
        terminal = normalized not in {"queued", "inflight", "retry_wait"}
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE attention_deferred_outbox SET status=?, attempts=?, next_retry_at=?, "
                "lease_token='', lease_until=0, updated_at=?, last_error=? "
                "WHERE work_id=? AND lease_token=?",
                (
                    normalized if not (normalized == "retry_wait") else "queued",
                    max(0, int(attempts or 0)),
                    float(next_retry_at or 0.0),
                    time.time(),
                    str(error or "")[:500],
                    str(work_id or ""),
                    str(lease_token or ""),
                ),
            )
            changed = int(cursor.rowcount or 0) > 0
            if changed and terminal:
                await db.execute(
                    "DELETE FROM attention_deferred_outbox WHERE work_id=? AND lease_token=''",
                    (str(work_id or ""),),
                )
            await db.commit()
        return changed

    async def describe(self) -> dict[str, Any]:
        if not self.db_path:
            return {"queued": 0, "inflight": 0, "oldest_age_ms": 0.0}
        await self._ensure_schema()
        now = time.time()
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                "SELECT status, COUNT(*), MIN(created_at) FROM attention_deferred_outbox GROUP BY status"
            )
            rows = await cursor.fetchall()
            await cursor.close()
        result: dict[str, Any] = {"queued": 0, "inflight": 0, "oldest_age_ms": 0.0}
        oldest = 0.0
        for status, count, created_at in rows:
            result[str(status or "unknown")] = int(count or 0)
            if created_at and (oldest <= 0 or float(created_at) < oldest):
                oldest = float(created_at)
        if oldest:
            result["oldest_age_ms"] = round(max(0.0, now - oldest) * 1000.0, 1)
        result["total"] = sum(int(value) for key, value in result.items() if key in {"queued", "inflight"})
        return result


__all__ = ["AttentionDeferredOutboxStore"]
