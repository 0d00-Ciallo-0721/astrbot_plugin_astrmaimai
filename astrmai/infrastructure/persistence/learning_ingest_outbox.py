from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...learning.contracts.learning_envelope import LearningMessageEnvelope
from .sqlite_helpers import connect_aiosqlite


@dataclass(frozen=True, slots=True)
class LearningIngressEntry:
    event_id: str
    envelope: LearningMessageEnvelope
    attempts: int
    next_retry_at: float
    last_error: str
    lease_token: str = ""
    lease_until: float = 0.0


class LearningIngressOutboxStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        retry_base_seconds: float = 60.0,
        max_attempts: int = 5,
    ) -> None:
        self.db_path = str(db_path)
        self.retry_base_seconds = max(1.0, float(retry_base_seconds))
        self.max_attempts = max(1, int(max_attempts))

    @staticmethod
    async def _ensure_schema(db) -> None:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS learning_ingest_outbox(
                event_id TEXT PRIMARY KEY,
                envelope_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL NOT NULL DEFAULT 0,
                lease_until REAL NOT NULL DEFAULT 0,
                lease_token TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_learning_ingest_due "
            "ON learning_ingest_outbox(status, next_retry_at, created_at)"
        )

    async def contains(self, event_id: str) -> bool:
        async with connect_aiosqlite(self.db_path) as db:
            await self._ensure_schema(db)
            cursor = await db.execute(
                "SELECT 1 FROM learning_ingest_outbox WHERE event_id=? LIMIT 1",
                (str(event_id or ""),),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return row is not None

    async def enqueue(self, envelope: LearningMessageEnvelope) -> bool:
        event_id = str(envelope.event_id or "").strip()
        if not event_id:
            return False
        now = time.time()
        payload = json.dumps(envelope.as_dict(), ensure_ascii=False, separators=(",", ":"), default=str)
        async with connect_aiosqlite(self.db_path) as db:
            await self._ensure_schema(db)
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO learning_ingest_outbox(
                    event_id, envelope_json, status, attempts, next_retry_at,
                    lease_until, last_error, created_at, updated_at
                ) VALUES (?, ?, 'queued', 0, 0, 0, '', ?, ?)
                """,
                (event_id, payload, now, now),
            )
            await db.commit()
            inserted = int(cursor.rowcount or 0) > 0
            await cursor.close()
        return inserted

    async def list_due(self, *, limit: int = 50) -> list[LearningIngressEntry]:
        now = time.time()
        async with connect_aiosqlite(self.db_path) as db:
            await self._ensure_schema(db)
            cursor = await db.execute(
                """
                SELECT event_id, envelope_json, attempts, next_retry_at, last_error
                FROM learning_ingest_outbox
                WHERE (status = 'queued' OR status = 'retry_wait')
                  AND next_retry_at <= ?
                ORDER BY next_retry_at ASC, created_at ASC
                LIMIT ?
                """,
                (now, max(1, int(limit))),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        entries: list[LearningIngressEntry] = []
        for row in rows:
            try:
                payload = json.loads(str(row[1] or "{}"))
                envelope = LearningMessageEnvelope(
                    event_id=str(payload.get("event_id") or row[0] or ""),
                    chat_id=str(payload.get("chat_id") or ""),
                    sender_id=str(payload.get("sender_id") or ""),
                    sender_name=str(payload.get("sender_name") or ""),
                    content=str(payload.get("content") or ""),
                    conversation_event=payload.get("conversation_event") or {},
                    received_at=float(payload.get("received_at") or 0.0),
                )
            except Exception:
                continue
            entries.append(LearningIngressEntry(
                str(row[0] or ""), envelope, int(row[2] or 0),
                float(row[3] or 0.0), str(row[4] or ""),
                "", 0.0,
            ))
        return entries

    async def claim_due(
        self, *, limit: int = 50, lease_seconds: float = 300.0
    ) -> list[LearningIngressEntry]:
        """Atomically claim due rows so only one worker processes each event."""
        now = time.time()
        lease_until = now + max(1.0, float(lease_seconds or 300.0))
        claimed: list[LearningIngressEntry] = []
        async with connect_aiosqlite(self.db_path) as db:
            await self._ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            # A worker may have crashed after claiming a row. Requeue only
            # expired leases; live workers remain exclusively responsible.
            await db.execute(
                """
                UPDATE learning_ingest_outbox
                SET status='queued', lease_until=0, lease_token='', updated_at=?
                WHERE status='running' AND lease_until > 0 AND lease_until <= ?
                """,
                (now, now),
            )
            cursor = await db.execute(
                """
                SELECT event_id, envelope_json, attempts, next_retry_at, last_error
                FROM learning_ingest_outbox
                WHERE (status = 'queued' OR status = 'retry_wait')
                  AND next_retry_at <= ?
                ORDER BY next_retry_at ASC, created_at ASC
                LIMIT ?
                """,
                (now, max(1, int(limit))),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                event_id = str(row[0] or "")
                token = uuid.uuid4().hex
                updated = await db.execute(
                    """
                    UPDATE learning_ingest_outbox
                    SET status='running', lease_until=?, lease_token=?, updated_at=?
                    WHERE event_id=? AND (status='queued' OR status='retry_wait')
                    """,
                    (lease_until, token, now, event_id),
                )
                if int(updated.rowcount or 0) != 1:
                    await updated.close()
                    continue
                await updated.close()
                try:
                    payload = json.loads(str(row[1] or "{}"))
                    envelope = LearningMessageEnvelope(
                        event_id=str(payload.get("event_id") or event_id),
                        chat_id=str(payload.get("chat_id") or ""),
                        sender_id=str(payload.get("sender_id") or ""),
                        sender_name=str(payload.get("sender_name") or ""),
                        content=str(payload.get("content") or ""),
                        conversation_event=payload.get("conversation_event") or {},
                        received_at=float(payload.get("received_at") or 0.0),
                    )
                except Exception as exc:
                    await db.execute(
                        "UPDATE learning_ingest_outbox SET status='failed', lease_until=0, lease_token='', last_error=?, updated_at=? WHERE event_id=?",
                        (f"invalid_envelope:{exc}"[:500], now, event_id),
                    )
                    continue
                claimed.append(LearningIngressEntry(
                    event_id, envelope, int(row[2] or 0), float(row[3] or 0.0),
                    str(row[4] or ""), token, lease_until,
                ))
            await db.commit()
        return claimed

    async def mark_retry(
        self, event_id: str, attempts: int, error: str, *, lease_token: str = ""
    ) -> str:
        normalized_attempts = max(1, int(attempts))
        exhausted = normalized_attempts >= self.max_attempts
        delay = min(3600.0, self.retry_base_seconds * (2 ** max(0, normalized_attempts - 1)))
        now = time.time()
        async with connect_aiosqlite(self.db_path) as db:
            await self._ensure_schema(db)
            where = "event_id = ?"
            values: list[Any] = [
                "exhausted" if exhausted else "retry_wait",
                normalized_attempts,
                0.0 if exhausted else now + delay,
                str(error or "")[:500],
                now,
            ]
            values.append(str(event_id or ""))
            if lease_token:
                where += " AND status='running' AND lease_token = ?"
                values.append(str(lease_token))
            cursor = await db.execute(
                f"UPDATE learning_ingest_outbox SET status=?, attempts=?, next_retry_at=?, last_error=?, lease_until=0, lease_token='', updated_at=? WHERE {where}",
                values,
            )
            await db.commit()
            changed = int(cursor.rowcount or 0) == 1
            await cursor.close()
        if not changed:
            return "lease_lost"
        return "exhausted" if exhausted else "retry_wait"

    async def release_lease(
        self,
        event_id: str,
        *,
        lease_token: str,
        next_retry_at: float = 0.0,
        error: str = "cancelled",
    ) -> bool:
        """Return a claimed event to the queue without consuming an attempt."""
        token = str(lease_token or "").strip()
        if not token:
            return False
        now = time.time()
        async with connect_aiosqlite(self.db_path) as db:
            await self._ensure_schema(db)
            cursor = await db.execute(
                """
                UPDATE learning_ingest_outbox
                SET status='queued', next_retry_at=?, lease_until=0,
                    lease_token='', last_error=?, updated_at=?
                WHERE event_id=? AND status='running' AND lease_token=?
                """,
                (
                    max(0.0, float(next_retry_at or 0.0)),
                    str(error or "")[:500],
                    now,
                    str(event_id or ""),
                    token,
                ),
            )
            await db.commit()
            changed = int(cursor.rowcount or 0) == 1
            await cursor.close()
        return changed

    async def delete(self, event_id: str, *, lease_token: str = "") -> None:
        async with connect_aiosqlite(self.db_path) as db:
            await self._ensure_schema(db)
            where = "event_id = ?"
            values: list[Any] = [str(event_id or "")]
            if lease_token:
                where += " AND status='running' AND lease_token = ?"
                values.append(str(lease_token))
            await db.execute(f"DELETE FROM learning_ingest_outbox WHERE {where}", values)
            await db.commit()


__all__ = ["LearningIngressEntry", "LearningIngressOutboxStore"]
