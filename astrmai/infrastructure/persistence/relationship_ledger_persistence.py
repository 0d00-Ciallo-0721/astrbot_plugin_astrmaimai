from __future__ import annotations

import asyncio
from typing import Any

from .sqlite_helpers import connect_sqlite


class RelationshipLedgerPersistenceMixin:
    """Durable, idempotent storage for relationship settlement evidence."""

    async def append_relationship_ledger_entry(self, entry: Any) -> tuple[bool, dict[str, Any]]:
        row = entry.to_persistence_row()

        def _write() -> tuple[bool, dict[str, Any]]:
            with connect_sqlite(self.db_path) as db:
                existing = db.execute(
                    "SELECT * FROM relationship_event_ledger WHERE idempotency_key = ?",
                    (row["idempotency_key"],),
                ).fetchone()
                if existing is not None:
                    columns = [description[0] for description in db.execute("SELECT * FROM relationship_event_ledger LIMIT 0").description]
                    return False, dict(zip(columns, existing))
                columns = list(row)
                db.execute(
                    f"INSERT INTO relationship_event_ledger ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    tuple(row[column] for column in columns),
                )
                db.commit()
                return True, dict(row)

        return await asyncio.to_thread(_write)

    async def get_relationship_ledger_entry(self, idempotency_key: str) -> dict[str, Any] | None:
        key = str(idempotency_key or "").strip()
        if not key:
            return None

        def _read() -> dict[str, Any] | None:
            with connect_sqlite(self.db_path) as db:
                cursor = db.execute(
                    "SELECT * FROM relationship_event_ledger WHERE idempotency_key = ?",
                    (key,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                columns = [description[0] for description in cursor.description]
                return dict(zip(columns, row))

        return await asyncio.to_thread(_read)

    async def list_relationship_ledger_entries(self, user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            return []
        safe_limit = max(1, min(int(limit or 100), 1000))

        def _read() -> list[dict[str, Any]]:
            with connect_sqlite(self.db_path) as db:
                cursor = db.execute(
                    "SELECT * FROM relationship_event_ledger WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (normalized_user_id, safe_limit),
                )
                columns = [description[0] for description in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]

        return await asyncio.to_thread(_read)


__all__ = ["RelationshipLedgerPersistenceMixin"]
