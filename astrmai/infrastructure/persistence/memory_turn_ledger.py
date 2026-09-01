from __future__ import annotations

import time
import uuid
from pathlib import Path

from .sqlite_helpers import connect_aiosqlite


class MemoryTurnLedgerStore:
    """Persistent idempotency ledger for committed reply memory ingestion."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def claim(
        self,
        turn_id: str,
        chat_id: str,
        *,
        lease_seconds: float = 300.0,
    ) -> str:
        normalized = str(turn_id or "").strip()
        if not normalized:
            return "untracked"
        now = time.time()
        lease_until = now + max(1.0, float(lease_seconds))
        lease_token = uuid.uuid4().hex
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            existing_cursor = await db.execute(
                "SELECT status, lease_until FROM memory_turn_ledger WHERE turn_id = ? LIMIT 1",
                (normalized,),
            )
            existing = await existing_cursor.fetchone()
            await existing_cursor.close()
            if existing is None:
                await db.execute(
                    """
                    INSERT INTO memory_turn_ledger(
                        turn_id, chat_id, status, first_seen_at, committed_at,
                        last_error, updated_at, lease_until, lease_token
                    ) VALUES (?, ?, 'recording', ?, 0, '', ?, ?, ?)
                    """,
                    (
                        normalized,
                        str(chat_id or ""),
                        now,
                        now,
                        lease_until,
                        lease_token,
                    ),
                )
                claimed = True
            else:
                status = str(existing[0] or "")
                previous_lease_until = float(existing[1] or 0.0)
                retryable = status == "failed" or (
                    status == "recording" and previous_lease_until <= now
                )
                if retryable:
                    cursor = await db.execute(
                        """
                        UPDATE memory_turn_ledger
                        SET chat_id=?, status='recording', first_seen_at=?,
                            committed_at=0, last_error='', updated_at=?,
                            lease_until=?, lease_token=?
                        WHERE turn_id=? AND status=? AND lease_until=?
                        """,
                        (
                            str(chat_id or ""),
                            now,
                            now,
                            lease_until,
                            lease_token,
                            normalized,
                            status,
                            previous_lease_until,
                        ),
                    )
                    claimed = int(cursor.rowcount or 0) > 0
                    await cursor.close()
                else:
                    claimed = False
            await db.commit()
        return lease_token if claimed else ""

    async def mark_committed(self, turn_id: str, *, lease_token: str = "") -> bool:
        normalized = str(turn_id or "").strip()
        if not normalized:
            return True
        now = time.time()
        async with connect_aiosqlite(self.db_path) as db:
            where = "turn_id = ? AND status = 'recording'"
            values = [now, now, normalized]
            if lease_token and lease_token != "untracked":
                where += " AND lease_token = ?"
                values.append(str(lease_token))
            cursor = await db.execute(
                f"""
                UPDATE memory_turn_ledger
                SET status = 'committed', committed_at = ?, last_error = '',
                    updated_at = ?, lease_until = 0, lease_token = ''
                WHERE {where}
                """,
                values,
            )
            await db.commit()
            changed = int(cursor.rowcount or 0) > 0
            await cursor.close()
        return changed

    async def release_failed(
        self,
        turn_id: str,
        error: str,
        *,
        lease_token: str = "",
    ) -> bool:
        """Release an incomplete claim so durable reply repair can retry it."""
        normalized = str(turn_id or "").strip()
        if not normalized:
            return True
        async with connect_aiosqlite(self.db_path) as db:
            where = "turn_id = ? AND status = 'recording'"
            values = [str(error or "")[:500], time.time(), normalized]
            if lease_token and lease_token != "untracked":
                where += " AND lease_token = ?"
                values.append(str(lease_token))
            cursor = await db.execute(
                f"""
                UPDATE memory_turn_ledger
                SET status = 'failed', last_error = ?, updated_at = ?,
                    lease_until = 0, lease_token = ''
                WHERE {where}
                """,
                values,
            )
            await db.commit()
            changed = int(cursor.rowcount or 0) > 0
            await cursor.close()
        return changed

    async def reclaim_stale(self, *, stale_after_seconds: float = 900.0) -> int:
        """Mark claims left by a crashed worker as retryable failures."""
        cutoff = time.time() - max(1.0, float(stale_after_seconds))
        now = time.time()
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE memory_turn_ledger
                SET status = 'failed', last_error = 'stale_recording_reclaimed',
                    updated_at = ?, lease_until = 0, lease_token = ''
                WHERE status = 'recording'
                  AND (lease_until <= ? OR updated_at < ?)
                """,
                (now, now, cutoff),
            )
            await db.commit()
            changed = int(cursor.rowcount or 0)
            await cursor.close()
        return changed

    async def contains(self, turn_id: str) -> bool:
        normalized = str(turn_id or "").strip()
        if not normalized:
            return False
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM memory_turn_ledger WHERE turn_id = ? LIMIT 1",
                (normalized,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return row is not None


__all__ = ["MemoryTurnLedgerStore"]
