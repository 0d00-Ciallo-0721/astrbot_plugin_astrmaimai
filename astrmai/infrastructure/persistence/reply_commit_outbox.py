from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ...conversation.contracts.committed_reply import CommittedBotTurn
from .sqlite_helpers import connect_sqlite


@dataclass(frozen=True, slots=True)
class ReplyCommitOutboxEntry:
    commit_id: str
    committed_turn: CommittedBotTurn
    repair_context: dict[str, Any]
    consumer_status: dict[str, str]
    attempts: int
    next_retry_at: float
    last_error: str
    created_at: float
    updated_at: float


class ReplyCommitOutboxStore:
    """Durable repair queue for post-send consumers; it never stores a send action."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        retry_base_seconds: float = 30.0,
        retry_max_seconds: float = 1800.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.retry_base_seconds = max(0.0, float(retry_base_seconds))
        self.retry_max_seconds = max(
            self.retry_base_seconds,
            float(retry_max_seconds),
        )

    def retry_delay(self, attempts: int) -> float:
        exponent = max(0, int(attempts) - 1)
        return min(
            self.retry_max_seconds,
            self.retry_base_seconds * (2**exponent),
        )

    @staticmethod
    def _encode(value: Mapping[str, Any]) -> str:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def _decode_mapping(value: str) -> dict[str, Any]:
        decoded = json.loads(str(value or "{}"))
        return dict(decoded) if isinstance(decoded, dict) else {}

    @classmethod
    def _entry_from_row(cls, row) -> ReplyCommitOutboxEntry:
        turn_payload = cls._decode_mapping(row[1])
        status_payload = cls._decode_mapping(row[3])
        return ReplyCommitOutboxEntry(
            commit_id=str(row[0] or ""),
            committed_turn=CommittedBotTurn.from_dict(turn_payload),
            repair_context=cls._decode_mapping(row[2]),
            consumer_status={
                str(name): str(status)
                for name, status in status_payload.items()
            },
            attempts=max(0, int(row[4] or 0)),
            next_retry_at=float(row[5] or 0.0),
            last_error=str(row[6] or ""),
            created_at=float(row[7] or 0.0),
            updated_at=float(row[8] or 0.0),
        )

    async def get(self, commit_id: str) -> ReplyCommitOutboxEntry | None:
        return await asyncio.to_thread(self._get_sync, str(commit_id or ""))

    def _get_sync(self, commit_id: str) -> ReplyCommitOutboxEntry | None:
        with connect_sqlite(self.db_path) as db:
            row = db.execute(
                """
                SELECT commit_id, committed_turn_json, repair_context_json,
                       consumer_status_json, attempts, next_retry_at,
                       last_error, created_at, updated_at
                FROM reply_commit_outbox
                WHERE commit_id = ?
                """,
                (commit_id,),
            ).fetchone()
        return self._entry_from_row(row) if row is not None else None

    async def save(
        self,
        committed_turn: CommittedBotTurn,
        *,
        repair_context: Mapping[str, Any],
        consumer_status: Mapping[str, str],
        attempts: int = 0,
        next_retry_at: float = 0.0,
        last_error: str = "",
    ) -> None:
        await asyncio.to_thread(
            self._save_sync,
            committed_turn,
            dict(repair_context),
            dict(consumer_status),
            max(0, int(attempts)),
            float(next_retry_at or 0.0),
            str(last_error or ""),
        )

    async def save_claimed(
        self,
        committed_turn: CommittedBotTurn,
        *,
        repair_context: Mapping[str, Any],
        consumer_status: Mapping[str, str],
        lease_seconds: float = 300.0,
    ) -> str:
        """Persist a new commit and atomically lease it to the initial worker."""
        return await asyncio.to_thread(
            self._save_claimed_sync,
            committed_turn,
            dict(repair_context),
            dict(consumer_status),
            max(1.0, float(lease_seconds)),
        )

    def _save_claimed_sync(
        self,
        committed_turn: CommittedBotTurn,
        repair_context: dict[str, Any],
        consumer_status: dict[str, str],
        lease_seconds: float,
    ) -> str:
        now = time.time()
        token = uuid.uuid4().hex
        with connect_sqlite(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                """
                INSERT INTO reply_commit_outbox (
                    commit_id, committed_turn_json, repair_context_json,
                    consumer_status_json, attempts, next_retry_at,
                    last_error, created_at, updated_at, lease_until, lease_token
                ) VALUES (?, ?, ?, ?, 0, 0, '', ?, ?, ?, ?)
                ON CONFLICT(commit_id) DO UPDATE SET
                    lease_until = excluded.lease_until,
                    lease_token = excluded.lease_token,
                    updated_at = excluded.updated_at
                WHERE reply_commit_outbox.lease_until = 0
                   OR (reply_commit_outbox.lease_until <= ?
                       AND reply_commit_outbox.next_retry_at <= ?)
                """,
                (
                    committed_turn.commit_id,
                    self._encode(committed_turn.as_dict()),
                    self._encode(repair_context),
                    self._encode(consumer_status),
                    now,
                    now,
                    now + lease_seconds,
                    token,
                    now,
                    now,
                ),
            ).rowcount
            db.commit()
        return token if int(changed or 0) == 1 else ""

    def _save_sync(
        self,
        committed_turn: CommittedBotTurn,
        repair_context: dict[str, Any],
        consumer_status: dict[str, str],
        attempts: int,
        next_retry_at: float,
        last_error: str,
    ) -> None:
        now = time.time()
        with connect_sqlite(self.db_path) as db:
            db.execute(
                """
                INSERT INTO reply_commit_outbox (
                    commit_id, committed_turn_json, repair_context_json,
                    consumer_status_json, attempts, next_retry_at,
                    last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(commit_id) DO UPDATE SET
                    committed_turn_json = excluded.committed_turn_json,
                    repair_context_json = excluded.repair_context_json,
                    consumer_status_json = excluded.consumer_status_json,
                    attempts = excluded.attempts,
                    next_retry_at = excluded.next_retry_at,
                    last_error = excluded.last_error,
                    lease_until = CASE
                        WHEN excluded.next_retry_at > 0 THEN excluded.next_retry_at
                        ELSE reply_commit_outbox.lease_until
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    committed_turn.commit_id,
                    self._encode(committed_turn.as_dict()),
                    self._encode(repair_context),
                    self._encode(consumer_status),
                    attempts,
                    next_retry_at,
                    last_error,
                    now,
                    now,
                ),
            )
            db.commit()

    async def list_due(
        self,
        *,
        now: float | None = None,
        limit: int = 50,
    ) -> list[ReplyCommitOutboxEntry]:
        return await asyncio.to_thread(
            self._list_due_sync,
            float(time.time() if now is None else now),
            max(1, int(limit)),
        )

    def _list_due_sync(self, now: float, limit: int) -> list[ReplyCommitOutboxEntry]:
        with connect_sqlite(self.db_path) as db:
            rows = db.execute(
                """
                SELECT commit_id, committed_turn_json, repair_context_json,
                       consumer_status_json, attempts, next_retry_at,
                       last_error, created_at, updated_at
                FROM reply_commit_outbox
                WHERE next_retry_at <= ?
                ORDER BY next_retry_at ASC, created_at ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [self._entry_from_row(row) for row in rows]

    async def claim_due(
        self,
        *,
        now: float | None = None,
        limit: int = 50,
        lease_seconds: float = 300.0,
    ) -> list[tuple[ReplyCommitOutboxEntry, str]]:
        """Atomically claim due commits for one repair worker."""
        return await asyncio.to_thread(
            self._claim_due_sync,
            float(time.time() if now is None else now),
            max(1, int(limit)),
            max(1.0, float(lease_seconds)),
        )

    def _claim_due_sync(
        self, now: float, limit: int, lease_seconds: float
    ) -> list[tuple[ReplyCommitOutboxEntry, str]]:
        lease_until = now + lease_seconds
        claimed: list[tuple[ReplyCommitOutboxEntry, str]] = []
        with connect_sqlite(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            # Requeue only leases that have expired; live workers retain them.
            db.execute(
                "UPDATE reply_commit_outbox SET lease_until=0, lease_token='' WHERE lease_until > 0 AND lease_until <= ?",
                (now,),
            )
            rows = db.execute(
                """
                SELECT commit_id, committed_turn_json, repair_context_json,
                       consumer_status_json, attempts, next_retry_at,
                       last_error, created_at, updated_at
                FROM reply_commit_outbox
                WHERE next_retry_at <= ? AND (lease_until = 0 OR lease_until <= ?)
                ORDER BY next_retry_at ASC, created_at ASC LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
            for row in rows:
                token = uuid.uuid4().hex
                changed = db.execute(
                    "UPDATE reply_commit_outbox SET lease_until=?, lease_token=? WHERE commit_id=? AND (lease_until=0 OR lease_until<=?)",
                    (lease_until, token, str(row[0] or ""), now),
                ).rowcount
                if int(changed or 0) == 1:
                    claimed.append((self._entry_from_row(row), token))
            db.commit()
        return claimed

    async def update_claimed(
        self,
        committed_turn: CommittedBotTurn,
        *,
        repair_context: Mapping[str, Any],
        consumer_status: Mapping[str, str],
        attempts: int,
        last_error: str,
        lease_token: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._update_claimed_sync,
            committed_turn,
            dict(repair_context),
            dict(consumer_status),
            max(0, int(attempts)),
            str(last_error or ""),
            str(lease_token or ""),
        )

    def _update_claimed_sync(
        self,
        committed_turn: CommittedBotTurn,
        repair_context: dict[str, Any],
        consumer_status: dict[str, str],
        attempts: int,
        last_error: str,
        lease_token: str,
    ) -> bool:
        if not lease_token:
            return False
        now = time.time()
        with connect_sqlite(self.db_path) as db:
            changed = db.execute(
                """
                UPDATE reply_commit_outbox
                SET committed_turn_json=?, repair_context_json=?,
                    consumer_status_json=?, attempts=?, last_error=?,
                    lease_until=?, updated_at=?
                WHERE commit_id=? AND lease_token=?
                """,
                (
                    self._encode(committed_turn.as_dict()),
                    self._encode(repair_context),
                    self._encode(consumer_status),
                    attempts,
                    last_error,
                    now + 300.0,
                    now,
                    committed_turn.commit_id,
                    lease_token,
                ),
            ).rowcount
            db.commit()
        return int(changed or 0) == 1

    async def release_claim(
        self,
        committed_turn: CommittedBotTurn,
        *,
        repair_context: Mapping[str, Any],
        consumer_status: Mapping[str, str],
        attempts: int,
        next_retry_at: float,
        last_error: str,
        lease_token: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._release_claim_sync,
            committed_turn,
            dict(repair_context),
            dict(consumer_status),
            max(0, int(attempts)),
            float(next_retry_at or 0.0),
            str(last_error or ""),
            str(lease_token or ""),
        )

    def _release_claim_sync(
        self,
        committed_turn: CommittedBotTurn,
        repair_context: dict[str, Any],
        consumer_status: dict[str, str],
        attempts: int,
        next_retry_at: float,
        last_error: str,
        lease_token: str,
    ) -> bool:
        if not lease_token:
            return False
        now = time.time()
        with connect_sqlite(self.db_path) as db:
            changed = db.execute(
                """
                UPDATE reply_commit_outbox
                SET committed_turn_json=?, repair_context_json=?,
                    consumer_status_json=?, attempts=?, next_retry_at=?,
                    last_error=?, lease_until=0, lease_token='', updated_at=?
                WHERE commit_id=? AND lease_token=?
                """,
                (
                    self._encode(committed_turn.as_dict()),
                    self._encode(repair_context),
                    self._encode(consumer_status),
                    attempts,
                    next_retry_at,
                    last_error,
                    now,
                    committed_turn.commit_id,
                    lease_token,
                ),
            ).rowcount
            db.commit()
        return int(changed or 0) == 1

    async def release_lease(
        self,
        commit_id: str,
        *,
        lease_token: str,
        next_retry_at: float = 0.0,
    ) -> bool:
        """Release ownership without overwriting the latest consumer checkpoint."""
        if not lease_token:
            return False
        return await asyncio.to_thread(
            self._release_lease_sync,
            str(commit_id or ""),
            str(lease_token),
            float(next_retry_at or 0.0),
        )

    def _release_lease_sync(
        self,
        commit_id: str,
        lease_token: str,
        next_retry_at: float,
    ) -> bool:
        with connect_sqlite(self.db_path) as db:
            changed = db.execute(
                """
                UPDATE reply_commit_outbox
                SET lease_until=0, lease_token='', next_retry_at=?, updated_at=?
                WHERE commit_id=? AND lease_token=?
                """,
                (next_retry_at, time.time(), commit_id, lease_token),
            ).rowcount
            db.commit()
        return int(changed or 0) == 1

    async def delete(self, commit_id: str, *, lease_token: str = "") -> None:
        await asyncio.to_thread(self._delete_sync, str(commit_id or ""), str(lease_token or ""))

    def _delete_sync(self, commit_id: str, lease_token: str = "") -> None:
        with connect_sqlite(self.db_path) as db:
            if lease_token:
                db.execute(
                    "DELETE FROM reply_commit_outbox WHERE commit_id = ? AND lease_token = ?",
                    (commit_id, lease_token),
                )
            else:
                db.execute("DELETE FROM reply_commit_outbox WHERE commit_id = ?", (commit_id,))
            db.commit()


__all__ = ["ReplyCommitOutboxEntry", "ReplyCommitOutboxStore"]
