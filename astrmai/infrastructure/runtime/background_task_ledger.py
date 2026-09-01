from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..persistence.sqlite_helpers import connect_aiosqlite


@dataclass(frozen=True, slots=True)
class TaskLease:
    task_id: str
    task_family: str
    scope_id: str
    lease_token: str
    lease_until: float
    retry_count: int


class BackgroundTaskLedger:
    """SQLite-backed per-family/scope lease and task state store."""

    ACTIVE_STATUSES = frozenset({"queued", "claimed", "running", "retry_wait"})
    TERMINAL_STATUSES = frozenset({
        "succeeded", "failed", "exhausted", "stale", "shutdown", "rejected", "expired", "cancelled",
    })
    ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def claim(
        self,
        *,
        task_family: str,
        scope_id: str,
        input_fingerprint: str = "",
        lease_seconds: float = 300.0,
        payload: dict[str, Any] | None = None,
        checkpoint_before: dict[str, Any] | None = None,
        min_interval_seconds: float = 0.0,
    ) -> TaskLease | None:
        now = time.time()
        token = uuid.uuid4().hex
        task_id = f"task_{uuid.uuid4().hex[:20]}"
        family = str(task_family or "").strip()
        scope = str(scope_id or "").strip()
        fingerprint = str(input_fingerprint or "").strip()
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row_cursor = await db.execute(
                """
                SELECT task_id, lease_until, retry_count, status
                FROM background_task_ledger
                WHERE task_family = ? AND scope_id = ?
                  AND status IN ('queued','claimed','running','retry_wait')
                ORDER BY updated_at DESC LIMIT 1
                """,
                (family, scope),
            )
            row = await row_cursor.fetchone()
            await row_cursor.close()
            if row is not None and float(row[1] or 0.0) > now:
                await db.rollback()
                return None
            if min_interval_seconds > 0:
                success_cursor = await db.execute(
                    """
                    SELECT finished_at FROM background_task_ledger
                    WHERE task_family=? AND scope_id=? AND status='succeeded'
                    ORDER BY finished_at DESC LIMIT 1
                    """,
                    (family, scope),
                )
                success_row = await success_cursor.fetchone()
                await success_cursor.close()
                if success_row is not None and now - float(success_row[0] or 0.0) < float(min_interval_seconds):
                    await db.rollback()
                    return None
            if row is not None:
                await db.execute(
                    "UPDATE background_task_ledger SET status='stale', last_error='lease_expired', lease_until=0, lease_token='', updated_at=? WHERE task_id=?",
                    (now, str(row[0] or "")),
                )
            existing_cursor = await db.execute(
                "SELECT task_id, retry_count FROM background_task_ledger WHERE task_family=? AND scope_id=? AND input_fingerprint=? LIMIT 1",
                (family, scope, fingerprint),
            )
            existing = await existing_cursor.fetchone()
            await existing_cursor.close()
            retry_count = int(row[2] or 0) if row is not None else int(existing[1] or 0) if existing is not None else 0
            if existing is not None:
                await db.execute(
                    """
                    UPDATE background_task_ledger
                    SET task_id=?, scheduled_at=?, started_at=?, finished_at=0,
                        lease_until=?, lease_token=?, status='running', retry_count=?,
                        last_error='', checkpoint_before=?, checkpoint_after='{}',
                        llm_call_count=0, payload_json=?, updated_at=?
                    WHERE task_family=? AND scope_id=? AND input_fingerprint=?
                    """,
                    (
                        task_id, now, now, now + max(1.0, float(lease_seconds)),
                        token, retry_count,
                        json.dumps(checkpoint_before or {}, ensure_ascii=False, default=str),
                        json.dumps(payload or {}, ensure_ascii=False, default=str), now,
                        family, scope, fingerprint,
                    ),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO background_task_ledger(
                        task_id, task_family, scope_id, scheduled_at, started_at,
                        lease_until, lease_token, input_fingerprint, status,
                        retry_count, checkpoint_before, payload_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id, family, scope, now, now,
                        now + max(1.0, float(lease_seconds)), token, fingerprint,
                        retry_count,
                        json.dumps(checkpoint_before or {}, ensure_ascii=False, default=str),
                        json.dumps(payload or {}, ensure_ascii=False, default=str), now, now,
                    ),
                )
            await db.commit()
        return TaskLease(
            task_id,
            family,
            scope,
            token,
            now + max(1.0, float(lease_seconds)),
            retry_count,
        )

    async def finish(
        self,
        lease: TaskLease,
        *,
        status: str = "succeeded",
        checkpoint_after: dict[str, Any] | None = None,
        llm_call_count: int = 0,
        error: str = "",
        retry_after_seconds: float = 0.0,
    ) -> bool:
        now = time.time()
        normalized_status = str(status or "succeeded").strip().lower() or "succeeded"
        if normalized_status not in self.ALL_STATUSES:
            raise ValueError(f"unsupported background task status: {normalized_status}")
        # retry_wait is an unfinished state: clear the old execution lease
        # and use the requested backoff as the next claim gate.
        next_lease_until = (
            now + max(0.0, float(retry_after_seconds or 0.0))
            if normalized_status == "retry_wait"
            else 0.0
        )
        finished_at = 0.0 if normalized_status == "retry_wait" else now
        retry_increment = 1 if normalized_status == "retry_wait" else 0
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE background_task_ledger
                SET status=?, finished_at=?, checkpoint_after=?, llm_call_count=?,
                    last_error=?, lease_until=?, lease_token='',
                    retry_count=retry_count+?, updated_at=?
                WHERE task_id=? AND lease_token=?
                """,
                (
                    normalized_status, finished_at,
                    json.dumps(checkpoint_after or {}, ensure_ascii=False, default=str),
                    max(0, int(llm_call_count or 0)), str(error or "")[:500],
                    next_lease_until, retry_increment, now,
                    lease.task_id, lease.lease_token,
                ),
            )
            await db.commit()
            changed = int(cursor.rowcount or 0) > 0
            await cursor.close()
        return changed

    async def record_rejected(
        self,
        *,
        task_family: str,
        scope_id: str,
        run_id: str,
        error: str = "shutdown",
    ) -> str:
        """Record a task that was refused before a lease could be claimed."""
        now = time.time()
        family = str(task_family or "").strip() or "unknown"
        scope = str(scope_id or "").strip() or "global"
        resolved_run_id = str(run_id or "").strip() or uuid.uuid4().hex
        task_id = f"task_{uuid.uuid4().hex[:20]}"
        fingerprint = f"rejected:{resolved_run_id}"
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO background_task_ledger(
                    task_id, task_family, scope_id, scheduled_at, started_at,
                    finished_at, input_fingerprint, status, last_error,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?, 'rejected', ?, ?, ?, ?)
                """,
                (
                    task_id, family, scope, now, now, fingerprint,
                    str(error or "shutdown")[:500],
                    json.dumps(
                        {"run_id": resolved_run_id, "rejected_before_claim": True},
                        ensure_ascii=False,
                    ),
                    now, now,
                ),
            )
            await db.commit()
        return task_id

    async def recover_expired_leases(self, *, task_family: str = "", scope_id: str = "") -> int:
        """Fence expired workers so a new process can safely reclaim work.

        Recovery is deliberately explicit and transactional.  Expired rows are
        marked ``stale`` instead of silently being overwritten, preserving an
        audit trail for reload/shutdown diagnostics.
        """
        clauses = ["status IN ('queued','claimed','running','retry_wait')", "lease_until > 0", "lease_until <= ?"]
        values: list[Any] = [time.time()]
        if task_family:
            clauses.append("task_family = ?")
            values.append(str(task_family))
        if scope_id:
            clauses.append("scope_id = ?")
            values.append(str(scope_id))
        now = time.time()
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE background_task_ledger SET status='stale', last_error='lease_expired', "
                "lease_until=0, lease_token='', updated_at=? WHERE "
                + " AND ".join(clauses),
                [now, *values],
            )
            changed = int(cursor.rowcount or 0)
            await cursor.close()
            await db.commit()
        return changed

    async def describe(self, *, task_family: str = "", scope_id: str = "") -> dict[str, Any]:
        clauses: list[str] = []
        values: list[str] = []
        if task_family:
            clauses.append("task_family = ?")
            values.append(str(task_family))
        if scope_id:
            clauses.append("scope_id = ?")
            values.append(str(scope_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                f"SELECT status, COUNT(*) FROM background_task_ledger{where} GROUP BY status",
                values,
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return {str(row[0] or "unknown"): int(row[1] or 0) for row in rows}

    async def describe_diagnostics(
        self, *, task_family: str = "", scope_id: str = ""
    ) -> dict[str, Any]:
        """Return a stable, management-facing summary of task state.

        Unlike ``describe`` (kept backward compatible for existing callers),
        this method always exposes every known status and timing counters so a
        zero count is distinguishable from a missing metric.
        """
        clauses: list[str] = []
        values: list[Any] = []
        if task_family:
            clauses.append("task_family = ?")
            values.append(str(task_family))
        if scope_id:
            clauses.append("scope_id = ?")
            values.append(str(scope_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                f"SELECT status, COUNT(*), MIN(scheduled_at), MIN(updated_at) "
                f"FROM background_task_ledger{where} GROUP BY status",
                values,
            )
            rows = await cursor.fetchall()
            await cursor.close()
            family_cursor = await db.execute(
                f"SELECT task_family, status, COUNT(*) FROM background_task_ledger{where} "
                "GROUP BY task_family, status ORDER BY task_family, status",
                values,
            )
            family_rows = await family_cursor.fetchall()
            await family_cursor.close()
        now = time.time()
        by_status: dict[str, int] = {status: 0 for status in sorted(self.ALL_STATUSES)}
        oldest_age_ms: dict[str, float] = {}
        updated_age_ms: dict[str, float] = {}
        for status, count, scheduled_at, updated_at in rows:
            key = str(status or "unknown")
            by_status[key] = int(count or 0)
            scheduled = float(scheduled_at or 0.0)
            updated = float(updated_at or 0.0)
            oldest_age_ms[key] = round(max(0.0, now - scheduled) * 1000.0, 1) if scheduled else 0.0
            updated_age_ms[key] = round(max(0.0, now - updated) * 1000.0, 1) if updated else 0.0
        by_family: dict[str, dict[str, int]] = {}
        for family, status, count in family_rows:
            family_key = str(family or "unknown")
            family_status = by_family.setdefault(
                family_key, {item: 0 for item in sorted(self.ALL_STATUSES)}
            )
            family_status[str(status or "unknown")] = int(count or 0)
        return {
            "status_counts": by_status,
            "oldest_age_ms_by_status": oldest_age_ms,
            "oldest_updated_age_ms_by_status": updated_age_ms,
            "by_task_family": by_family,
            "total": sum(by_status.values()),
            "active_total": sum(by_status.get(status, 0) for status in self.ACTIVE_STATUSES),
            "terminal_total": sum(by_status.get(status, 0) for status in self.TERMINAL_STATUSES),
        }

    async def list_recent(
        self,
        *,
        task_family: str = "",
        scope_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if task_family:
            clauses.append("task_family = ?")
            values.append(str(task_family))
        if scope_id:
            clauses.append("scope_id = ?")
            values.append(str(scope_id))
        if status:
            clauses.append("status = ?")
            values.append(str(status).strip().lower())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        values.append(max(1, min(int(limit), 500)))
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT task_id, task_family, scope_id, scheduled_at, started_at,
                       finished_at, lease_until, input_fingerprint,
                       checkpoint_before, checkpoint_after, llm_call_count,
                       status, retry_count, last_error, payload_json, updated_at
                FROM background_task_ledger
                """ + where + " ORDER BY updated_at DESC LIMIT ?",
                values,
            )
            rows = await cursor.fetchall()
            await cursor.close()
        fields = (
            "task_id", "task_family", "scope_id", "scheduled_at", "started_at",
            "finished_at", "lease_until", "input_fingerprint",
            "checkpoint_before", "checkpoint_after", "llm_call_count",
            "status", "retry_count", "last_error", "payload_json", "updated_at",
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(zip(fields, row))
            payload_raw = item.get("payload_json")
            try:
                payload = json.loads(str(payload_raw or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            item["run_id"] = str(payload.get("run_id") or "")
            item["priority"] = int(payload.get("priority", 0) or 0)
            item["queue_wait_ms"] = max(
                0.0,
                (float(item.get("started_at", 0.0) or 0.0) - float(item.get("scheduled_at", 0.0) or 0.0)) * 1000.0,
            )
            end_at = float(item.get("finished_at", 0.0) or 0.0)
            status = str(item.get("status") or "")
            if end_at <= 0.0 and status == "running":
                end_at = time.time()
            if status == "rejected" and not float(item.get("started_at", 0.0) or 0.0):
                item["execution_ms"] = 0.0
            else:
                item["execution_ms"] = max(
                    0.0,
                    (end_at - float(item.get("started_at", 0.0) or 0.0)) * 1000.0,
                )
            item["parse_status"] = str(payload.get("parse_status") or "")
            for name in ("checkpoint_before", "checkpoint_after", "payload_json"):
                try:
                    decoded = json.loads(str(item.get(name) or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    decoded = {}
                item[name.removesuffix("_json")] = decoded if isinstance(decoded, dict) else {}
                if name.endswith("_json"):
                    item.pop(name, None)
            result.append(item)
        return result


__all__ = ["BackgroundTaskLedger", "TaskLease"]
