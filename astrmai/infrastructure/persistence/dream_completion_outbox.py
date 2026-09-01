from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .sqlite_helpers import connect_aiosqlite


class DreamCompletionOutboxStore:
    """Durable stage checkpoint for Dream write-back and visible delivery."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def save(self, request_key: str, payload: dict[str, Any]) -> None:
        now = time.time()
        run_id = str(payload.get("run_id") or request_key)
        encoded = json.dumps(dict(payload), ensure_ascii=False, default=str)
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO dream_completion_outbox(
                    request_key, run_id, session_id, payload_json, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(request_key) DO UPDATE SET
                    run_id=excluded.run_id,
                    session_id=excluded.session_id,
                    payload_json=excluded.payload_json,
                    status='pending',
                    updated_at=excluded.updated_at
                """,
                (
                    str(request_key or ""),
                    run_id,
                    str(payload.get("session_id") or ""),
                    encoded,
                    now,
                    now,
                ),
            )
            await db.commit()

    async def save_claimed(
        self,
        request_key: str,
        payload: dict[str, Any],
        *,
        lease_seconds: float = 300.0,
    ) -> str:
        now = time.time()
        token = uuid.uuid4().hex
        run_id = str(payload.get("run_id") or request_key)
        encoded = json.dumps(dict(payload), ensure_ascii=False, default=str)
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                INSERT INTO dream_completion_outbox(
                    request_key, run_id, session_id, payload_json, status,
                    created_at, updated_at, lease_until, lease_token
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)
                ON CONFLICT(request_key) DO UPDATE SET
                    lease_until=excluded.lease_until,
                    lease_token=excluded.lease_token,
                    status='running',
                    updated_at=excluded.updated_at
                WHERE dream_completion_outbox.lease_until=0
                   OR dream_completion_outbox.lease_until<=?
                """,
                (
                    str(request_key or ""),
                    run_id,
                    str(payload.get("session_id") or ""),
                    encoded,
                    now,
                    now,
                    now + max(1.0, float(lease_seconds)),
                    token,
                    now,
                ),
            )
            await db.commit()
            changed = int(cursor.rowcount or 0) == 1
            await cursor.close()
        return token if changed else ""

    async def list_pending(self, *, limit: int = 20) -> list[tuple[str, dict[str, Any]]]:
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                "SELECT request_key, payload_json FROM dream_completion_outbox WHERE status='pending' ORDER BY updated_at ASC LIMIT ?",
                (max(1, int(limit)),),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        result: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            try:
                payload = json.loads(str(row[1] or "{}"))
                if isinstance(payload, dict):
                    result.append((str(row[0] or ""), payload))
            except Exception:
                continue
        return result

    async def claim_pending(
        self, *, limit: int = 20, lease_seconds: float = 300.0
    ) -> list[tuple[str, dict[str, Any], str]]:
        now = time.time()
        lease_until = now + max(1.0, float(lease_seconds))
        claimed: list[tuple[str, dict[str, Any], str]] = []
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                UPDATE dream_completion_outbox
                SET status='pending', lease_until=0, lease_token='', updated_at=?
                WHERE status='running' AND lease_until>0 AND lease_until<=?
                """,
                (now, now),
            )
            cursor = await db.execute(
                """
                SELECT request_key, payload_json
                FROM dream_completion_outbox
                WHERE status='pending'
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                request_key = str(row[0] or "")
                token = uuid.uuid4().hex
                updated = await db.execute(
                    """
                    UPDATE dream_completion_outbox
                    SET status='running', lease_until=?, lease_token=?, updated_at=?
                    WHERE request_key=? AND status='pending'
                    """,
                    (lease_until, token, now, request_key),
                )
                changed = int(updated.rowcount or 0) == 1
                await updated.close()
                if not changed:
                    continue
                try:
                    payload = json.loads(str(row[1] or "{}"))
                    if not isinstance(payload, dict):
                        raise ValueError("payload is not an object")
                except Exception:
                    await db.execute(
                        """
                        UPDATE dream_completion_outbox
                        SET status='failed', lease_until=0, lease_token='', updated_at=?
                        WHERE request_key=? AND lease_token=?
                        """,
                        (now, request_key, token),
                    )
                    continue
                claimed.append((request_key, payload, token))
            await db.commit()
        return claimed

    async def update_claimed(
        self,
        request_key: str,
        payload: dict[str, Any],
        *,
        lease_token: str,
        lease_seconds: float = 300.0,
    ) -> bool:
        if not lease_token:
            return False
        now = time.time()
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE dream_completion_outbox
                SET run_id=?, session_id=?, payload_json=?, status='running',
                    lease_until=?, updated_at=?
                WHERE request_key=? AND status='running' AND lease_token=?
                """,
                (
                    str(payload.get("run_id") or request_key),
                    str(payload.get("session_id") or ""),
                    json.dumps(dict(payload), ensure_ascii=False, default=str),
                    now + max(1.0, float(lease_seconds)),
                    now,
                    str(request_key or ""),
                    str(lease_token),
                ),
            )
            await db.commit()
            changed = int(cursor.rowcount or 0) == 1
            await cursor.close()
        return changed

    async def delete(self, request_key: str, *, lease_token: str = "") -> bool:
        async with connect_aiosqlite(self.db_path) as db:
            where = "request_key = ?"
            values = [str(request_key or "")]
            if lease_token:
                where += " AND status='running' AND lease_token=?"
                values.append(str(lease_token))
            cursor = await db.execute(
                f"DELETE FROM dream_completion_outbox WHERE {where}",
                values,
            )
            await db.commit()
            changed = int(cursor.rowcount or 0) == 1
            await cursor.close()
        return changed


__all__ = ["DreamCompletionOutboxStore"]
