from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sqlite_helpers import connect_aiosqlite


@dataclass(frozen=True, slots=True)
class ReflectionOutboxEntry:
    reflection_id: str
    payload: dict[str, Any]
    attempts: int = 0
    next_retry_at: float = 0.0
    lease_token: str = ""
    lease_until: float = 0.0


class ReflectionOutboxStore:
    """Durable queue for expression reflection observations."""

    def __init__(self, db_path: str | Path, *, retry_base_seconds: float = 60.0) -> None:
        self.db_path = str(db_path)
        self.retry_base_seconds = max(1.0, float(retry_base_seconds))

    async def enqueue(self, payload: dict[str, Any]) -> str:
        reflection_id = str(payload.get("reflection_id") or uuid.uuid4().hex)
        data = dict(payload)
        data["reflection_id"] = reflection_id
        now = time.time()
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO reflection_outbox(
                    reflection_id, chat_id, pattern_id, payload_json, status,
                    attempts, next_retry_at, lease_until, lease_token,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', 0, 0, 0, '', ?, ?)
                """,
                (
                    reflection_id,
                    str(data.get("chat_id") or ""),
                    str(data.get("pattern_id") or ""),
                    json.dumps(data, ensure_ascii=False, default=str),
                    now,
                    now,
                ),
            )
            await db.commit()
        return reflection_id

    async def list_due(self, *, chat_id: str = "", limit: int = 200) -> list[ReflectionOutboxEntry]:
        now = time.time()
        clauses = ["(status = 'queued' OR status = 'retry_wait')", "next_retry_at <= ?"]
        values: list[Any] = [now]
        if chat_id:
            clauses.append("chat_id = ?")
            values.append(str(chat_id))
        values.append(max(1, int(limit)))
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                f"SELECT reflection_id, payload_json, attempts, next_retry_at FROM reflection_outbox WHERE {' AND '.join(clauses)} ORDER BY created_at ASC LIMIT ?",
                values,
            )
            rows = await cursor.fetchall()
            await cursor.close()
        entries: list[ReflectionOutboxEntry] = []
        for row in rows:
            try:
                payload = json.loads(str(row[1] or "{}"))
                if not isinstance(payload, dict):
                    continue
            except Exception:
                continue
            entries.append(
                ReflectionOutboxEntry(
                    str(row[0] or ""),
                    payload,
                    int(row[2] or 0),
                    float(row[3] or 0.0),
                )
            )
        return entries

    async def claim_due(
        self,
        *,
        chat_id: str = "",
        limit: int = 200,
        lease_seconds: float = 300.0,
    ) -> list[ReflectionOutboxEntry]:
        now = time.time()
        lease_until = now + max(1.0, float(lease_seconds))
        clauses = ["(status = 'queued' OR status = 'retry_wait')", "next_retry_at <= ?"]
        values: list[Any] = [now]
        if chat_id:
            clauses.append("chat_id = ?")
            values.append(str(chat_id))
        values.append(max(1, int(limit)))
        claimed: list[ReflectionOutboxEntry] = []
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                UPDATE reflection_outbox
                SET status='queued', lease_until=0, lease_token='', updated_at=?
                WHERE status='running' AND lease_until > 0 AND lease_until <= ?
                """,
                (now, now),
            )
            cursor = await db.execute(
                f"SELECT reflection_id, payload_json, attempts, next_retry_at FROM reflection_outbox WHERE {' AND '.join(clauses)} ORDER BY created_at ASC LIMIT ?",
                values,
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                reflection_id = str(row[0] or "")
                token = uuid.uuid4().hex
                updated = await db.execute(
                    """
                    UPDATE reflection_outbox
                    SET status='running', lease_until=?, lease_token=?, updated_at=?
                    WHERE reflection_id=?
                      AND (status='queued' OR status='retry_wait')
                    """,
                    (lease_until, token, now, reflection_id),
                )
                changed = int(updated.rowcount or 0) == 1
                await updated.close()
                if not changed:
                    continue
                try:
                    payload = json.loads(str(row[1] or "{}"))
                    if not isinstance(payload, dict):
                        raise ValueError("payload is not an object")
                except Exception as exc:
                    await db.execute(
                        """
                        UPDATE reflection_outbox
                        SET status='failed', lease_until=0, lease_token='',
                            last_error=?, updated_at=?
                        WHERE reflection_id=? AND lease_token=?
                        """,
                        (f"invalid_payload:{exc}"[:500], now, reflection_id, token),
                    )
                    continue
                claimed.append(
                    ReflectionOutboxEntry(
                        reflection_id,
                        payload,
                        int(row[2] or 0),
                        float(row[3] or 0.0),
                        token,
                        lease_until,
                    )
                )
            await db.commit()
        return claimed

    async def list_scope_ids(self, *, limit: int = 500) -> list[str]:
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT chat_id, MIN(created_at) AS first_created_at
                FROM reflection_outbox
                WHERE status IN ('queued', 'retry_wait', 'running')
                GROUP BY chat_id
                ORDER BY first_created_at ASC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [str(row[0] or "GLOBAL").strip() or "GLOBAL" for row in rows]

    async def mark_done(self, reflection_id: str, *, lease_token: str = "") -> bool:
        async with connect_aiosqlite(self.db_path) as db:
            where = "reflection_id = ?"
            values: list[Any] = [str(reflection_id or "")]
            if lease_token:
                where += " AND status='running' AND lease_token=?"
                values.append(str(lease_token))
            cursor = await db.execute(
                f"DELETE FROM reflection_outbox WHERE {where}",
                values,
            )
            await db.commit()
            changed = int(cursor.rowcount or 0) == 1
            await cursor.close()
        return changed

    async def mark_retry(
        self,
        reflection_id: str,
        attempts: int,
        error: str,
        *,
        lease_token: str = "",
    ) -> bool:
        now = time.time()
        delay = min(3600.0, self.retry_base_seconds * (2 ** max(0, int(attempts) - 1)))
        async with connect_aiosqlite(self.db_path) as db:
            where = "reflection_id=?"
            values: list[Any] = [
                max(1, int(attempts)),
                now + delay,
                str(error or "")[:500],
                now,
                str(reflection_id or ""),
            ]
            if lease_token:
                where += " AND status='running' AND lease_token=?"
                values.append(str(lease_token))
            cursor = await db.execute(
                """
                UPDATE reflection_outbox
                SET status='retry_wait', attempts=?, next_retry_at=?,
                    lease_until=0, lease_token='', last_error=?, updated_at=?
                WHERE """ + where,
                values,
            )
            await db.commit()
            changed = int(cursor.rowcount or 0) == 1
            await cursor.close()
        return changed

    async def release_lease(
        self,
        reflection_id: str,
        *,
        lease_token: str,
        next_retry_at: float = 0.0,
        error: str = "cancelled",
    ) -> bool:
        """Return a claimed reflection to the queue without consuming an attempt."""
        token = str(lease_token or "").strip()
        if not token:
            return False
        now = time.time()
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE reflection_outbox
                SET status='queued', next_retry_at=?, lease_until=0,
                    lease_token='', last_error=?, updated_at=?
                WHERE reflection_id=? AND status='running' AND lease_token=?
                """,
                (
                    max(0.0, float(next_retry_at or 0.0)),
                    str(error or "")[:500],
                    now,
                    str(reflection_id or ""),
                    token,
                ),
            )
            await db.commit()
            changed = int(cursor.rowcount or 0) == 1
            await cursor.close()
        return changed


__all__ = ["ReflectionOutboxStore", "ReflectionOutboxEntry"]
