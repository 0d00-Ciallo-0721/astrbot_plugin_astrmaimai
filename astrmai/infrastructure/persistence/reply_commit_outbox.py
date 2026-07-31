from __future__ import annotations

import asyncio
import json
import time
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

    async def delete(self, commit_id: str) -> None:
        await asyncio.to_thread(self._delete_sync, str(commit_id or ""))

    def _delete_sync(self, commit_id: str) -> None:
        with connect_sqlite(self.db_path) as db:
            db.execute(
                "DELETE FROM reply_commit_outbox WHERE commit_id = ?",
                (commit_id,),
            )
            db.commit()


__all__ = ["ReplyCommitOutboxEntry", "ReplyCommitOutboxStore"]
