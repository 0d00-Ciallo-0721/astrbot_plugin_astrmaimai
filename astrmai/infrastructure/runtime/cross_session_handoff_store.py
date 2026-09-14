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
    status: str = "active"
    owner: str = ""
    lease_token: str = ""
    lease_until: float = 0.0
    revision: int = 1
    updated_at: float = 0.0
    failure_stage: str = ""
    failure_kind: str = ""
    error_type: str = ""
    error_summary: str = ""

    def __post_init__(self) -> None:
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.expires_at:
            self.expires_at = self.created_at + 1800.0
        if not self.handoff_id:
            self.handoff_id = uuid.uuid4().hex
        self.status = str(self.status or "active").strip().lower() or "active"
        self.revision = max(1, int(self.revision or 1))
        if not self.updated_at:
            self.updated_at = self.created_at


class CrossSessionHandoffStore:
    DEFAULT_TTL_SECONDS = 1800.0
    MAX_HANDOFFS_PER_RECIPIENT = 4
    MAX_RECIPIENTS = 256
    MAX_OBSERVED_TURNS = 3
    REQUIRED_LEASE_COLUMNS = frozenset(
        {"owner", "lease_token", "lease_until", "revision"}
    )

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._handoffs: dict[tuple[str, str], list[CrossSessionHandoff]] = {}
        self._lock = asyncio.Lock()
        self.db_path = Path(db_path) if db_path else None
        self._hydrated_keys: set[tuple[str, str]] = set()
        self.last_failure: dict[str, str] = {}

    @staticmethod
    def _key(platform_id: str, target_id: str) -> tuple[str, str]:
        return (
            str(platform_id or "default").strip() or "default",
            str(target_id or "").strip(),
        )

    def _drop_cached_handoff_locked(
        self,
        key: tuple[str, str],
        handoff_id: str,
    ) -> None:
        remaining = [
            entry
            for entry in self._handoffs.get(key, [])
            if entry.handoff_id != handoff_id
        ]
        if remaining:
            self._handoffs[key] = remaining
        else:
            self._handoffs.pop(key, None)
        self._hydrated_keys.discard(key)

    def _prune_expired_locked(self, now: float) -> list[CrossSessionHandoff]:
        expired: list[CrossSessionHandoff] = []
        for key, entries in self._handoffs.items():
            for entry in entries:
                if (
                    entry.status in {"active", "claimed"}
                    and float(entry.expires_at or 0.0) <= now
                ):
                    entry.status = "expired"
                    entry.owner = ""
                    entry.lease_token = ""
                    entry.lease_until = 0.0
                    entry.failure_stage = entry.failure_stage or "lookup"
                    entry.failure_kind = entry.failure_kind or "handoff_expired"
                    entry.revision += 1
                    entry.updated_at = now
                    expired.append(replace(entry))
            self._handoffs[key] = entries[-self.MAX_HANDOFFS_PER_RECIPIENT :]
        return expired

    def _persist_sync(self, handoff: CrossSessionHandoff, status: str | None = None) -> bool:
        if self.db_path is None:
            return True
        now = time.time()
        with connect_sqlite(self.db_path) as db:
            columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(cross_session_handoff)").fetchall()
            }
            base_columns = [
                "handoff_id", "platform_id", "source_umo", "source_sender_id",
                "source_sender_name", "target_umo", "target_id", "target_name",
                "outbound_message", "context_summary", "delivery_mode",
                "observed_turns", "status", "created_at", "expires_at", "updated_at",
            ]
            optional = [
                "owner", "lease_token", "lease_until", "revision",
                "failure_stage", "failure_kind", "error_type", "error_summary",
            ]
            selected = [name for name in [*base_columns, *optional] if name in columns]
            values = {
                "handoff_id": handoff.handoff_id,
                "platform_id": handoff.platform_id,
                "source_umo": handoff.source_umo,
                "source_sender_id": handoff.source_sender_id,
                "source_sender_name": handoff.source_sender_name,
                "target_umo": handoff.target_umo,
                "target_id": handoff.target_id,
                "target_name": handoff.target_name,
                "outbound_message": handoff.outbound_message,
                "context_summary": handoff.context_summary,
                "delivery_mode": handoff.delivery_mode,
                "observed_turns": handoff.observed_turns,
                "status": status or handoff.status,
                "created_at": handoff.created_at,
                "expires_at": handoff.expires_at,
                "updated_at": float(handoff.updated_at or now),
                "owner": handoff.owner,
                "lease_token": handoff.lease_token,
                "lease_until": handoff.lease_until,
                "revision": handoff.revision,
                "failure_stage": handoff.failure_stage,
                "failure_kind": handoff.failure_kind,
                "error_type": handoff.error_type,
                "error_summary": handoff.error_summary[:500],
            }
            supports_revision = "revision" in columns
            updates = [name for name in selected if name != "handoff_id"]
            placeholders = ", ".join("?" for _ in selected)
            conflict_guard = (
                " WHERE excluded.revision > cross_session_handoff.revision"
                if supports_revision
                else " WHERE 0"
            )
            cursor = db.execute(
                f"""
                INSERT INTO cross_session_handoff ({', '.join(selected)})
                VALUES ({placeholders})
                ON CONFLICT(handoff_id) DO UPDATE SET
                    {', '.join(f'{name}=excluded.{name}' for name in updates)}
                    {conflict_guard}
                """,
                tuple(values[name] for name in selected),
            )
            db.commit()
            if cursor.rowcount == 1:
                return True
            persisted = db.execute(
                f"SELECT {', '.join(selected)} FROM cross_session_handoff WHERE handoff_id=?",
                (handoff.handoff_id,),
            ).fetchone()
            return persisted == tuple(values[name] for name in selected)

    def _load_key_sync(self, key: tuple[str, str], now: float) -> list[CrossSessionHandoff]:
        if self.db_path is None:
            return []
        with connect_sqlite(self.db_path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """
                SELECT * FROM cross_session_handoff
                WHERE platform_id=? AND target_id=? AND status IN ('active','expired','stale','claimed','sending','failed')
                ORDER BY created_at DESC LIMIT ?
                """,
                (key[0], key[1], self.MAX_HANDOFFS_PER_RECIPIENT),
            ).fetchall()
        def value(row: sqlite3.Row, name: str, default: Any = "") -> Any:
            return row[name] if name in row.keys() else default

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
                status=str(row["status"] or "active"),
                owner=str(value(row, "owner") or ""),
                lease_token=str(value(row, "lease_token") or ""),
                lease_until=float(value(row, "lease_until") or 0.0),
                revision=int(value(row, "revision", 1) or 1),
                updated_at=float(row["updated_at"] or 0.0),
                failure_stage=str(value(row, "failure_stage") or ""),
                failure_kind=str(value(row, "failure_kind") or ""),
                error_type=str(value(row, "error_type") or ""),
                error_summary=str(value(row, "error_summary") or ""),
            )
            for row in reversed(rows)
        ]

    def _has_lease_schema_sync(self) -> bool:
        if self.db_path is None:
            return True
        with connect_sqlite(self.db_path) as db:
            columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(cross_session_handoff)").fetchall()
            }
        return self.REQUIRED_LEASE_COLUMNS.issubset(columns)

    def _claim_sync(
        self,
        handoff_id: str,
        *,
        expected_revision: int,
        owner: str,
        lease_token: str,
        lease_until: float,
        now: float,
    ) -> bool:
        if self.db_path is None:
            return True
        with connect_sqlite(self.db_path) as db:
            columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(cross_session_handoff)").fetchall()
            }
            if not self.REQUIRED_LEASE_COLUMNS.issubset(columns):
                return False
            cursor = db.execute(
                """
                UPDATE cross_session_handoff
                SET status='claimed', owner=?, lease_token=?, lease_until=?,
                    revision=revision+1, updated_at=?, failure_stage='', failure_kind='',
                    error_type='', error_summary=''
                WHERE handoff_id=? AND revision=? AND expires_at>?
                  AND (status='active' OR (status='claimed' AND lease_until<=?))
                """,
                (owner, lease_token, lease_until, now, handoff_id, expected_revision, now, now),
            )
            db.commit()
            return cursor.rowcount == 1

    def _acknowledge_sync(
        self,
        handoff_id: str,
        *,
        lease_token: str,
        expected_revision: int,
        next_status: str,
        observed_turns: int,
        now: float,
    ) -> bool:
        if self.db_path is None:
            return True
        with connect_sqlite(self.db_path) as db:
            columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(cross_session_handoff)").fetchall()
            }
            if not self.REQUIRED_LEASE_COLUMNS.issubset(columns):
                return False
            cursor = db.execute(
                """
                UPDATE cross_session_handoff
                SET status=?, observed_turns=?, owner='', lease_token='', lease_until=0,
                    revision=revision+1, updated_at=?, failure_stage='', failure_kind='',
                    error_type='', error_summary=''
                WHERE handoff_id=? AND status='claimed' AND lease_token=?
                  AND revision=? AND expires_at>? AND lease_until>?
                """,
                (
                    next_status,
                    observed_turns,
                    now,
                    handoff_id,
                    lease_token,
                    expected_revision,
                    now,
                    now,
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def _complete_sync(
        self,
        handoff_id: str,
        *,
        owner: str,
        lease_token: str,
        expected_revision: int,
        now: float,
    ) -> bool:
        if self.db_path is None:
            return True
        with connect_sqlite(self.db_path) as db:
            columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(cross_session_handoff)").fetchall()
            }
            if not self.REQUIRED_LEASE_COLUMNS.issubset(columns):
                return False
            cursor = db.execute(
                """
                UPDATE cross_session_handoff
                SET status='completed', owner='', lease_token='', lease_until=0,
                    revision=revision+1, updated_at=?, failure_stage='', failure_kind='',
                    error_type='', error_summary=''
                WHERE handoff_id=? AND status='claimed' AND owner=? AND lease_token=?
                  AND revision=? AND expires_at>? AND lease_until>?
                """,
                (
                    now,
                    handoff_id,
                    owner,
                    lease_token,
                    expected_revision,
                    now,
                    now,
                ),
            )
            db.commit()
            return cursor.rowcount == 1

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
        if handoff.expires_at <= 0:
            handoff.expires_at = now + self.DEFAULT_TTL_SECONDS
        elif handoff.expires_at <= now and handoff.status == "active":
            handoff.status = "expired"
            handoff.failure_stage = "put"
            handoff.failure_kind = "handoff_expired"
        async with self._lock:
            expired = self._prune_expired_locked(now)
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
        for entry in expired:
            await asyncio.to_thread(self._persist_sync, entry)
        await asyncio.to_thread(self._persist_sync, handoff)
        return handoff.handoff_id

    async def lookup_for_recipient(
        self, platform_id: str, target_id: str
    ) -> CrossSessionHandoff | None:
        """Return the latest handoff, including an expired/stale decision.

        Historical records are retained for diagnostics; callers that may
        perform a side effect must use ``peek_for_recipient`` or ``claim``.
        """
        key = self._key(platform_id, target_id)
        if not key[1]:
            return None
        now = time.time()
        async with self._lock:
            await self._hydrate_key_locked(key, now)
            expired = self._prune_expired_locked(now)
            entries = self._handoffs.get(key, [])
            result = replace(entries[-1]) if entries else None
        for entry in expired:
            await asyncio.to_thread(self._persist_sync, entry)
        return result

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
            expired = self._prune_expired_locked(now)
            entries = self._handoffs.get(key, [])
            if not entries:
                result = None
            else:
                latest = entries[-1]
                result = (
                    replace(latest)
                    if latest.status == "active" and float(latest.expires_at or 0.0) > now
                    else None
                )
        for entry in expired:
            await asyncio.to_thread(self._persist_sync, entry)
        return result

    async def claim_for_recipient(
        self,
        platform_id: str,
        target_id: str,
        *,
        owner: str,
        expected_revision: int | None = None,
        lease_seconds: float = 30.0,
    ) -> CrossSessionHandoff | None:
        """Claim the latest active handoff for one planner injection."""
        key = self._key(platform_id, target_id)
        owner = str(owner or "").strip()
        if not key[1] or not owner:
            return None
        now = time.time()
        expired: list[CrossSessionHandoff] = []
        async with self._lock:
            if self.db_path is not None and not await asyncio.to_thread(
                self._has_lease_schema_sync
            ):
                self.last_failure = {
                    "failure_stage": "claim",
                    "failure_kind": "lease_schema_unavailable",
                    "error_type": "SchemaNotReady",
                }
                return None
            await self._hydrate_key_locked(key, now)
            expired = self._prune_expired_locked(now)
            entries = self._handoffs.get(key, [])
            if not entries:
                candidate = None
            else:
                candidate = entries[-1]
            if candidate is None or candidate.expires_at <= now:
                result = None
            elif candidate.status == "claimed" and candidate.lease_until > now:
                self.last_failure = {
                    "failure_stage": "claim",
                    "failure_kind": "lease_held",
                    "error_type": "ClaimConflict",
                }
                result = None
            elif candidate.status not in {"active", "claimed"}:
                self.last_failure = {
                    "failure_stage": "claim",
                    "failure_kind": (
                        "handoff_expired"
                        if candidate.status == "expired"
                        else "invalid_status"
                    ),
                    "error_type": "ClaimBlocked",
                }
                result = None
            elif expected_revision is not None and int(expected_revision) != candidate.revision:
                self.last_failure = {
                    "failure_stage": "claim",
                    "failure_kind": "revision_mismatch",
                    "error_type": "ClaimConflict",
                }
                result = None
            else:
                prior_revision = candidate.revision
                lease_token = uuid.uuid4().hex
                lease_until = now + max(1.0, float(lease_seconds or 0.0))
                claimed = await asyncio.to_thread(
                    self._claim_sync,
                    candidate.handoff_id,
                    expected_revision=prior_revision,
                    owner=owner,
                    lease_token=lease_token,
                    lease_until=lease_until,
                    now=now,
                )
                if not claimed:
                    self.last_failure = {
                        "failure_stage": "claim",
                        "failure_kind": "persistent_cas_conflict",
                        "error_type": "ClaimConflict",
                    }
                    self._drop_cached_handoff_locked(key, candidate.handoff_id)
                    result = None
                else:
                    candidate.status = "claimed"
                    candidate.owner = owner
                    candidate.lease_token = lease_token
                    candidate.lease_until = lease_until
                    candidate.revision = prior_revision + 1
                    candidate.updated_at = now
                    candidate.failure_stage = ""
                    candidate.failure_kind = ""
                    self.last_failure = {}
                    result = replace(candidate)
        for entry in expired:
            await asyncio.to_thread(self._persist_sync, entry)
        return result

    async def acknowledge(
        self,
        handoff_id: str,
        *,
        lease_token: str,
        expected_revision: int,
    ) -> bool:
        normalized_id = str(handoff_id or "").strip()
        normalized_lease = str(lease_token or "").strip()
        if not normalized_id or not normalized_lease or int(expected_revision or 0) <= 0:
            return False
        async with self._lock:
            for key, entries in list(self._handoffs.items()):
                for index, entry in enumerate(entries):
                    if entry.handoff_id != normalized_id:
                        continue
                    now = time.time()
                    if entry.expires_at <= now:
                        self.last_failure = {
                            "failure_stage": "acknowledge",
                            "failure_kind": "handoff_expired",
                            "error_type": "HandoffExpired",
                        }
                        return False
                    if (
                        entry.status != "claimed"
                        or normalized_lease != entry.lease_token
                        or int(expected_revision) != entry.revision
                        or entry.lease_until <= now
                    ):
                        self.last_failure = {
                            "failure_stage": "acknowledge",
                            "failure_kind": "lease_or_revision_mismatch",
                            "error_type": "SettlementConflict",
                        }
                        return False
                    observed_turns = entry.observed_turns + 1
                    status = (
                        "completed"
                        if observed_turns >= self.MAX_OBSERVED_TURNS
                        else "active"
                    )
                    settled = await asyncio.to_thread(
                        self._acknowledge_sync,
                        entry.handoff_id,
                        lease_token=normalized_lease,
                        expected_revision=int(expected_revision),
                        next_status=status,
                        observed_turns=observed_turns,
                        now=now,
                    )
                    if not settled:
                        self.last_failure = {
                            "failure_stage": "acknowledge",
                            "failure_kind": "persistent_cas_conflict",
                            "error_type": "SettlementConflict",
                        }
                        self._drop_cached_handoff_locked(key, entry.handoff_id)
                        return False
                    entry.observed_turns = observed_turns
                    if status == "completed":
                        entries.pop(index)
                        if not entries:
                            self._handoffs.pop(key, None)
                    entry.status = status
                    entry.owner = ""
                    entry.lease_token = ""
                    entry.lease_until = 0.0
                    entry.revision += 1
                    entry.updated_at = now
                    self.last_failure = {}
                    return True
        return False

    async def complete_for_recipient(
        self,
        platform_id: str,
        target_id: str,
        *,
        handoff_id: str,
        owner: str,
        lease_token: str,
        expected_revision: int,
    ) -> bool:
        key = self._key(platform_id, target_id)
        async with self._lock:
            await self._hydrate_key_locked(key, time.time())
            entries = self._handoffs.get(key, [])
            completed = next(
                (entry for entry in entries if entry.handoff_id == str(handoff_id or "")),
                None,
            )
            now = time.time()
            if (
                completed is None
                or completed.status != "claimed"
                or completed.owner != str(owner or "").strip()
                or completed.lease_token != str(lease_token or "").strip()
                or completed.revision != int(expected_revision or 0)
                or completed.expires_at <= now
                or completed.lease_until <= now
            ):
                self.last_failure = {
                    "failure_stage": "complete",
                    "failure_kind": "lease_or_revision_mismatch",
                    "error_type": "SettlementConflict",
                }
                return False
            settled = await asyncio.to_thread(
                self._complete_sync,
                completed.handoff_id,
                owner=completed.owner,
                lease_token=completed.lease_token,
                expected_revision=completed.revision,
                now=now,
            )
            if not settled:
                self.last_failure = {
                    "failure_stage": "complete",
                    "failure_kind": "persistent_cas_conflict",
                    "error_type": "SettlementConflict",
                }
                self._drop_cached_handoff_locked(key, completed.handoff_id)
                return False
            entries.remove(completed)
            if not entries:
                self._handoffs.pop(key, None)
            self.last_failure = {}
            return True

    async def clear(self) -> None:
        async with self._lock:
            self._handoffs.clear()
            self._hydrated_keys.clear()
