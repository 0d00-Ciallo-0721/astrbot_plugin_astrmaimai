from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class ProactiveHistoryStore:
    """Small SQLite-backed store for durable proactive dispatch history."""

    def __init__(self, db_path: Any) -> None:
        self.db_path = Path(db_path) if db_path else None
        if self.db_path:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_table()

    def _connect(self) -> sqlite3.Connection:
        if self.db_path is None:
            raise RuntimeError("proactive history persistence is unavailable")
        return sqlite3.connect(str(self.db_path), timeout=5.0)

    def _ensure_table(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS proactive_dispatch_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id TEXT NOT NULL,
                    created_at REAL NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS ix_proactive_dispatch_history_created "
                "ON proactive_dispatch_history(created_at DESC, id DESC)"
            )

    @staticmethod
    def _encode(record: dict[str, Any]) -> str:
        return json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _decode(row: tuple[Any, ...]) -> dict[str, Any] | None:
        try:
            payload = json.loads(str(row[3] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        payload["_history_id"] = int(row[0])
        return payload

    def append(self, record: dict[str, Any]) -> int | None:
        if self.db_path is None:
            return None
        intent = dict(record.get("intent", {}) or {})
        intent_id = str(intent.get("intent_id", "") or "")
        created_at = float(record.get("created_at", 0.0) or 0.0)
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO proactive_dispatch_history(intent_id, created_at, payload_json) VALUES (?, ?, ?)",
                (intent_id, created_at, self._encode(record)),
            )
            return int(cursor.lastrowid)

    def update(self, history_id: int, record: dict[str, Any]) -> None:
        if self.db_path is None or not history_id:
            return
        intent = dict(record.get("intent", {}) or {})
        intent_id = str(intent.get("intent_id", "") or "")
        created_at = float(record.get("created_at", 0.0) or 0.0)
        with self._connect() as db:
            db.execute(
                """
                UPDATE proactive_dispatch_history
                SET intent_id=?, created_at=?, payload_json=?
                WHERE id=?
                """,
                (intent_id, created_at, self._encode(record), int(history_id)),
            )

    def page(self, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit or 50), 500))
        before_id: int | None = None
        if cursor:
            try:
                before_id = max(0, int(str(cursor)))
            except (TypeError, ValueError):
                before_id = None
        with self._connect() as db:
            total = int(db.execute("SELECT COUNT(*) FROM proactive_dispatch_history").fetchone()[0] or 0)
            if before_id:
                rows = db.execute(
                    """
                    SELECT id, intent_id, created_at, payload_json
                    FROM proactive_dispatch_history
                    WHERE id < ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (before_id, safe_limit + 1),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT id, intent_id, created_at, payload_json
                    FROM proactive_dispatch_history
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (safe_limit + 1,),
                ).fetchall()
        decoded = [item for row in rows if (item := self._decode(row)) is not None]
        has_more = len(decoded) > safe_limit
        items = decoded[:safe_limit]
        next_cursor = str(items[-1]["_history_id"]) if has_more and items else None
        return {
            "items": items,
            "total": total,
            "next_cursor": next_cursor,
            "has_more": bool(has_more),
        }

    def count(self) -> int:
        if self.db_path is None:
            return 0
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM proactive_dispatch_history").fetchone()[0] or 0)


__all__ = ["ProactiveHistoryStore"]
