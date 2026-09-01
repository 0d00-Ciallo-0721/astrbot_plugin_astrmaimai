from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sqlite_helpers import connect_aiosqlite


@dataclass(frozen=True, slots=True)
class QQActionClaim:
    acquired: bool
    lease_token: str = ""
    status: str = ""
    reason: str = ""


class QQActionLedgerStore:
    """Durable at-most-once claim ledger for external QQ side effects."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def claim(
        self,
        transport_idempotency_key: str,
        *,
        action_instance_id: str,
        action_id: str,
        action_type: str,
        chat_id: str,
        turn_id: str,
        trace_id: str,
        lease_seconds: float = 60.0,
    ) -> QQActionClaim:
        key = str(transport_idempotency_key or "").strip()
        if not key:
            raise ValueError("transport_idempotency_key is required")
        now = time.time()
        lease_until = now + max(1.0, float(lease_seconds))
        lease_token = uuid.uuid4().hex
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT status, lease_until, attempts
                FROM qq_action_ledger
                WHERE transport_idempotency_key = ?
                LIMIT 1
                """,
                (key,),
            )
            existing = await cursor.fetchone()
            await cursor.close()
            if existing is None:
                await db.execute(
                    """
                    INSERT INTO qq_action_ledger(
                        transport_idempotency_key, action_instance_id, action_id,
                        action_type, chat_id, turn_id, trace_id, status,
                        lease_token, lease_until, attempts, last_error,
                        created_at, updated_at, sending_at, sent_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'sending', ?, ?, 1, '', ?, ?, ?, 0, 0)
                    """,
                    (
                        key,
                        str(action_instance_id or ""),
                        str(action_id or ""),
                        str(action_type or ""),
                        str(chat_id or ""),
                        str(turn_id or ""),
                        str(trace_id or ""),
                        lease_token,
                        lease_until,
                        now,
                        now,
                        now,
                    ),
                )
                claim = QQActionClaim(True, lease_token, "sending", "claimed")
            else:
                status = str(existing[0] or "")
                previous_lease_until = float(existing[1] or 0.0)
                attempts = int(existing[2] or 0)
                if status == "failed":
                    updated = await db.execute(
                        """
                        UPDATE qq_action_ledger
                        SET action_instance_id=?, action_id=?, action_type=?,
                            chat_id=?, turn_id=?, trace_id=?, status='sending',
                            lease_token=?, lease_until=?, attempts=?, last_error='',
                            updated_at=?, sending_at=?, sent_at=0, completed_at=0
                        WHERE transport_idempotency_key=? AND status='failed'
                        """,
                        (
                            str(action_instance_id or ""),
                            str(action_id or ""),
                            str(action_type or ""),
                            str(chat_id or ""),
                            str(turn_id or ""),
                            str(trace_id or ""),
                            lease_token,
                            lease_until,
                            attempts + 1,
                            now,
                            now,
                            key,
                        ),
                    )
                    acquired = int(updated.rowcount or 0) == 1
                    await updated.close()
                    claim = QQActionClaim(
                        acquired,
                        lease_token if acquired else "",
                        "sending" if acquired else "failed",
                        "retry_claimed" if acquired else "claim_raced",
                    )
                elif status == "sending" and previous_lease_until <= now:
                    updated = await db.execute(
                        """
                        UPDATE qq_action_ledger
                        SET status='uncertain', lease_token='', lease_until=0,
                            last_error='expired_sending_result_unknown',
                            updated_at=?, completed_at=?
                        WHERE transport_idempotency_key=? AND status='sending'
                          AND lease_until=?
                        """,
                        (now, now, key, previous_lease_until),
                    )
                    changed = int(updated.rowcount or 0) == 1
                    await updated.close()
                    claim = QQActionClaim(
                        False,
                        "",
                        "uncertain" if changed else "sending",
                        "expired_sending" if changed else "claim_raced",
                    )
                elif status == "sent":
                    claim = QQActionClaim(False, "", "sent", "duplicate")
                elif status == "uncertain":
                    claim = QQActionClaim(False, "", "uncertain", "uncertain_outcome")
                else:
                    claim = QQActionClaim(False, "", status or "sending", "in_flight")
            await db.commit()
        return claim

    async def mark_sent(
        self,
        transport_idempotency_key: str,
        *,
        lease_token: str,
    ) -> bool:
        return await self._settle(
            transport_idempotency_key,
            lease_token=lease_token,
            status="sent",
            error="",
        )

    async def mark_failed(
        self,
        transport_idempotency_key: str,
        error: str,
        *,
        lease_token: str,
    ) -> bool:
        return await self._settle(
            transport_idempotency_key,
            lease_token=lease_token,
            status="failed",
            error=error,
        )

    async def mark_uncertain(
        self,
        transport_idempotency_key: str,
        error: str,
        *,
        lease_token: str,
    ) -> bool:
        return await self._settle(
            transport_idempotency_key,
            lease_token=lease_token,
            status="uncertain",
            error=error,
        )

    async def _settle(
        self,
        transport_idempotency_key: str,
        *,
        lease_token: str,
        status: str,
        error: str,
    ) -> bool:
        key = str(transport_idempotency_key or "").strip()
        token = str(lease_token or "").strip()
        if not key or not token:
            return False
        now = time.time()
        sent_at = now if status == "sent" else 0.0
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE qq_action_ledger
                SET status=?, lease_token='', lease_until=0, last_error=?,
                    updated_at=?, sent_at=?, completed_at=?
                WHERE transport_idempotency_key=? AND status='sending'
                  AND lease_token=?
                """,
                (
                    str(status),
                    str(error or "")[:500],
                    now,
                    sent_at,
                    now,
                    key,
                    token,
                ),
            )
            await db.commit()
            changed = int(cursor.rowcount or 0) == 1
            await cursor.close()
        return changed

    async def get(self, transport_idempotency_key: str) -> dict[str, Any] | None:
        key = str(transport_idempotency_key or "").strip()
        if not key:
            return None
        columns = (
            "transport_idempotency_key",
            "action_instance_id",
            "action_id",
            "action_type",
            "chat_id",
            "turn_id",
            "trace_id",
            "status",
            "lease_token",
            "lease_until",
            "attempts",
            "last_error",
            "created_at",
            "updated_at",
            "sending_at",
            "sent_at",
            "completed_at",
        )
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                f"SELECT {', '.join(columns)} FROM qq_action_ledger WHERE transport_idempotency_key=? LIMIT 1",
                (key,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return dict(zip(columns, row)) if row is not None else None


__all__ = ["QQActionClaim", "QQActionLedgerStore"]
