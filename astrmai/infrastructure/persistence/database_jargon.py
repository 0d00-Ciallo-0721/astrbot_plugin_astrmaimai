import json
import sqlite3
import time
import uuid
from typing import Dict, List, Optional

from sqlmodel import desc, select

from .orm_models import Jargon
from .sqlite_helpers import connect_sqlite


class JargonPersistenceMixin:
    @staticmethod
    def _canonical_statuses() -> tuple[str, ...]:
        return ("active", "stale")

    @staticmethod
    def _canonical_jargon_status(jargon: Jargon) -> str:
        meaning = str(getattr(jargon, "meaning", "") or "").strip()
        return "active" if bool(getattr(jargon, "is_jargon", False)) and meaning else "review_pending"

    @staticmethod
    def _canonical_jargon_visibility(status: str) -> str:
        return "auto_and_tool" if str(status or "") == "active" else "maintenance_only"

    def _ensure_canonical_jargon_schema_sync(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canonical_memories (
                id TEXT PRIMARY KEY,
                session_id TEXT DEFAULT '',
                persona_id TEXT DEFAULT '',
                source TEXT DEFAULT '',
                kind TEXT DEFAULT '',
                content TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                tags TEXT DEFAULT '[]',
                importance REAL DEFAULT 0.5,
                confidence REAL DEFAULT 0.8,
                status TEXT DEFAULT 'active',
                decay_score REAL DEFAULT 1.0,
                create_time REAL DEFAULT 0,
                update_time REAL DEFAULT 0,
                last_access_time REAL DEFAULT 0,
                access_count INTEGER DEFAULT 0,
                superseded_by TEXT DEFAULT '',
                deleted_reason TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                dedup_key TEXT DEFAULT '',
                source_ref TEXT DEFAULT '',
                visibility TEXT DEFAULT 'auto_and_tool'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_canonical_memories_dedup ON canonical_memories(dedup_key)")

    def _save_jargon_to_canonical_sync(self, jargon: Jargon):
        content = str(getattr(jargon, "content", "") or "").strip()
        if not content:
            return None
        now = time.time()
        group_id = str(getattr(jargon, "group_id", "") or "")
        meaning = str(getattr(jargon, "meaning", "") or "").strip()
        incoming_count = max(1, int(getattr(jargon, "count", 1) or 1))
        status = self._canonical_jargon_status(jargon)
        visibility = self._canonical_jargon_visibility(status)
        dedup_key = f"jargon:{group_id}:{content.lower()}"
        source_ref = f"legacy_jargon:{group_id}:{content.lower()}"
        with connect_sqlite(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")  # ponytail: prevent SQLITE_BUSY with concurrent readers
            self._ensure_canonical_jargon_schema_sync(conn)
            cursor = conn.execute(
                """
                SELECT id, metadata
                FROM canonical_memories
                WHERE dedup_key = ? OR source_ref = ?
                ORDER BY update_time DESC
                LIMIT 1
                """,
                (dedup_key, source_ref),
            )
            row = cursor.fetchone()
            metadata = {
                "raw_content": str(getattr(jargon, "raw_content", "") or content),
                "meaning": meaning,
                "count": incoming_count,
                "review_status": status,
                "legacy_write_redirect": True,
            }
            memory_id = str(row["id"]) if row else f"mem_{uuid.uuid4().hex}"
            if row:
                try:
                    existing_metadata = json.loads(row["metadata"] or "{}")
                    if not isinstance(existing_metadata, dict):
                        existing_metadata = {}
                except Exception:
                    existing_metadata = {}
                metadata["count"] = max(1, int(existing_metadata.get("count") or 1)) + incoming_count
                metadata["meaning"] = meaning or str(existing_metadata.get("meaning") or "").strip()
                conn.execute(
                    """
                    UPDATE canonical_memories
                    SET session_id = ?, source = ?, kind = ?, content = ?, summary = ?,
                        importance = MAX(importance, ?), confidence = MAX(confidence, ?),
                        status = ?, update_time = ?, last_access_time = ?, access_count = COALESCE(access_count, 0) + 1,
                        metadata = ?, dedup_key = ?, source_ref = ?, visibility = ?
                    WHERE id = ?
                    """,
                    (
                        group_id,
                        "legacy_jargon_write",
                        "jargon",
                        content,
                        metadata["meaning"] or content,
                        0.6,
                        0.7,
                        status,
                        now,
                        now,
                        json.dumps(metadata, ensure_ascii=False),
                        dedup_key,
                        source_ref,
                        visibility,
                        memory_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO canonical_memories (
                        id, session_id, persona_id, source, kind, content, summary,
                        tags, importance, confidence, status, decay_score,
                        create_time, update_time, last_access_time, access_count,
                        metadata, dedup_key, source_ref, visibility
                    )
                    VALUES (?, ?, '', ?, ?, ?, ?, '[]', ?, ?, ?, 1.0, ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        group_id,
                        "legacy_jargon_write",
                        "jargon",
                        content,
                        meaning or content,
                        0.6,
                        0.7,
                        status,
                        now,
                        now,
                        now,
                        json.dumps(metadata, ensure_ascii=False),
                        dedup_key,
                        source_ref,
                        visibility,
                    ),
                )
            conn.commit()
        jargon.is_jargon = status == "active"
        jargon.is_complete = status == "active"
        jargon.meaning = metadata["meaning"]
        jargon.count = int(metadata["count"] or incoming_count)
        return self._clone_model(jargon)

    def _canonical_jargon_rows(self, group_id: str, *, limit: int = 20, include_stale: bool = False) -> List[dict]:
        statuses = ("active", "stale") if include_stale else ("active",)
        try:
            with connect_sqlite(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                if group_id:
                    cursor = conn.execute(
                        f"""
                        SELECT id, session_id, content, summary, status, create_time, update_time, metadata
                        FROM canonical_memories
                        WHERE kind = 'jargon'
                          AND status IN ({','.join('?' for _ in statuses)})
                          AND (session_id = ? OR session_id = '')
                        ORDER BY update_time DESC
                        LIMIT ?
                        """,
                        (*statuses, group_id, limit),
                    )
                else:
                    cursor = conn.execute(
                        f"""
                        SELECT id, session_id, content, summary, status, create_time, update_time, metadata
                        FROM canonical_memories
                        WHERE kind = 'jargon'
                          AND status IN ({','.join('?' for _ in statuses)})
                        ORDER BY update_time DESC
                        LIMIT ?
                        """,
                        (*statuses, limit),
                    )
                rows = [dict(row) for row in cursor.fetchall()]
        except Exception:
            return []
        result: List[dict] = []
        for row in rows:
            try:
                metadata = json.loads(row.get("metadata") or "{}")
                if not isinstance(metadata, dict):
                    metadata = {}
            except Exception:
                metadata = {}
            result.append(
                {
                    "id": row.get("id"),
                    "group_id": str(row.get("session_id") or group_id or ""),
                    "content": str(row.get("content") or ""),
                    "raw_content": str(metadata.get("raw_content") or row.get("content") or ""),
                    "meaning": str(metadata.get("meaning") or row.get("summary") or ""),
                    "count": int(metadata.get("count") or 1),
                    "is_jargon": row.get("status") == "active",
                    "is_complete": row.get("status") == "active",
                    "status": str(row.get("status") or "active"),
                    "scene": str(metadata.get("scene") or ""),
                    "examples": list(metadata.get("examples") or []),
                    "created_at": float(row.get("create_time") or 0.0),
                    "updated_at": float(row.get("update_time") or 0.0),
                }
            )
        return result

    def save_jargon(self, jargon: Jargon):
        return self._save_jargon_to_canonical_sync(jargon)

    def get_jargons(self, group_id: str, limit: int = 20, only_confirmed: bool = True) -> List[Jargon]:
        canonical_rows = self._canonical_jargon_rows(group_id, limit=limit, include_stale=not only_confirmed)
        if canonical_rows:
            return [
                Jargon(
                    id=None,
                    content=row["content"],
                    raw_content=row["raw_content"],
                    meaning=row["meaning"],
                    is_jargon=bool(row["is_jargon"]),
                    count=int(row["count"] or 1),
                    is_complete=bool(row["is_complete"]),
                    group_id=row["group_id"],
                    created_at=float(row["created_at"] or time.time()),
                    updated_at=float(row["updated_at"] or time.time()),
                )
                for row in canonical_rows
                if row["content"] and (not only_confirmed or row["status"] == "active")
            ]
        with self.get_session() as session:
            statement = select(Jargon).where(Jargon.group_id == group_id)
            if only_confirmed:
                statement = statement.where(Jargon.is_jargon == True)
            statement = statement.order_by(desc(Jargon.updated_at)).limit(limit)
            results = session.exec(statement).all()
            return [Jargon.model_validate(item.model_dump()) for item in results]

    def get_recent_jargons(self, group_id: str, hours: int = 24) -> List[Jargon]:
        with self.get_session() as session:
            cutoff_time = time.time() - (hours * 3600)
            statement = select(Jargon).where(
                Jargon.group_id == group_id,
                Jargon.is_jargon == True,
                Jargon.updated_at >= cutoff_time,
            ).order_by(desc(Jargon.updated_at))
            results = session.exec(statement).all()
            return [Jargon.model_validate(item.model_dump()) for item in results]

    def get_jargon(self, group_id: str, word: str) -> Optional[str]:
        if not group_id or not word:
            return None
        for row in self._canonical_jargon_rows(group_id, limit=100, include_stale=False):
            if str(row["content"] or "").strip() == str(word or "").strip() and row["meaning"]:
                return row["meaning"]
        with self.get_session() as session:
            statement = select(Jargon).where(
                Jargon.group_id == group_id,
                Jargon.content == word,
            ).order_by(desc(Jargon.updated_at))
            result = session.exec(statement).first()
            if result and result.meaning:
                return result.meaning
        return None

    def search_jargons(self, keyword: str, limit: int = 3) -> List[Jargon]:
        if not keyword:
            return []
        canonical_matches = []
        for row in self._canonical_jargon_rows("", limit=max(limit * 12, 20), include_stale=True):
            content = str(row["content"] or "").lower()
            meaning = str(row["meaning"] or "").lower()
            keyword_lower = keyword.lower()
            if keyword_lower in content or keyword_lower in meaning:
                canonical_matches.append(
                    Jargon(
                        id=None,
                        content=row["content"],
                        raw_content=row["raw_content"],
                        meaning=row["meaning"],
                        is_jargon=bool(row["is_jargon"]),
                        count=int(row["count"] or 1),
                        is_complete=bool(row["is_complete"]),
                        group_id=row["group_id"],
                        created_at=float(row["created_at"] or time.time()),
                        updated_at=float(row["updated_at"] or time.time()),
                    )
                )
            if len(canonical_matches) >= limit:
                return canonical_matches
        keyword_lower = keyword.lower()
        with self.get_session() as session:
            statement = select(Jargon).order_by(desc(Jargon.updated_at))
            results = session.exec(statement).all()
            matches = []
            for item in results:
                content = (item.content or "").lower()
                meaning = (item.meaning or "").lower()
                if keyword_lower in content or keyword_lower in meaning:
                    matches.append(Jargon.model_validate(item.model_dump()))
                if len(matches) >= limit:
                    break
            return matches

    async def save_jargon_async(self, jargon: Jargon):
        memory_engine = getattr(self, "memory_engine", None)
        writer = getattr(memory_engine, "write_service", None) if memory_engine else None
        if writer and hasattr(writer, "write"):
            from ...memory.contracts.memory_query import MemoryWriteRequest

            content = str(getattr(jargon, "content", "") or "").strip()
            if not content:
                return None
            meaning = str(getattr(jargon, "meaning", "") or "").strip()
            status = "active" if bool(getattr(jargon, "is_jargon", False)) and meaning else "review_pending"
            return await writer.write(
                MemoryWriteRequest(
                    source="legacy_jargon_write",
                    kind="jargon",
                    session_id=str(getattr(jargon, "group_id", "") or ""),
                    content=content,
                    summary=meaning or content,
                    importance=0.6,
                    confidence=0.7,
                    metadata={
                        "raw_content": str(getattr(jargon, "raw_content", "") or content),
                        "meaning": meaning,
                        "count": int(getattr(jargon, "count", 1) or 1),
                        "review_status": status,
                        "legacy_write_redirect": True,
                    },
                    dedup_key=f"jargon:{getattr(jargon, 'group_id', '')}:{content.lower()}",
                    source_ref=f"legacy_jargon:{getattr(jargon, 'group_id', '')}:{content.lower()}",
                    visibility="auto_and_tool" if status == "active" else "maintenance_only",
                    status=status,
                )
            )
        return await self._run_blocking(self._save_jargon_to_canonical_sync, jargon, with_lock=True)

    async def get_recent_jargons_async(self, group_id: str, hours: int = 24):
        return await self._run_blocking(self.get_recent_jargons, group_id, hours)

    async def get_jargons_async(self, group_id: str, limit: int = 20, only_confirmed: bool = True):
        return await self._run_blocking(self.get_jargons, group_id, limit, only_confirmed)

    async def load_jargon_list(self, group_id: str, limit: int = 20) -> List[Dict[str, str]]:
        canonical_rows = self._canonical_jargon_rows(group_id, limit=limit, include_stale=False)
        if canonical_rows:
            return [
                {
                    "id": row["id"],
                    "text": row["content"],
                    "meaning": row["meaning"],
                    "situation": row["scene"],
                    "status": row["status"],
                }
                for row in canonical_rows
                if row["content"] and row["meaning"] and row["status"] == "active"
            ]
        items = await self.get_jargons_async(group_id, limit=limit, only_confirmed=True)
        return [
            {"text": item.content, "meaning": item.meaning, "situation": ""}
            for item in items
            if item.content and item.meaning
        ]
