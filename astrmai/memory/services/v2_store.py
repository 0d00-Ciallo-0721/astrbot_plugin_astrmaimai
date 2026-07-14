from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..contracts.memory_query import MemoryCandidate, MemoryWriteRequest
from ...infrastructure.persistence.sqlite_helpers import connect_aiosqlite
from .memory_scoring import DEFAULT_MEMORY_SCORING


ACTIVE_STATUS = "active"
STALE_STATUS = "stale"
SUPERSEDED_STATUS = "superseded"
DELETED_STATUS = "deleted"
MERGED_STATUS = "merged"
DEPRECATED_STATUS = "deprecated"
REVIEW_PENDING_STATUS = "review_pending"
REJECTED_STATUS = "rejected"


class MemoryUpsertResult(dict):
    @property
    def memory_id(self) -> str:
        return str(self.get("memory_id") or "")

    @property
    def superseded_old_ids(self) -> list[str]:
        return list(self.get("superseded_old_ids") or [])

    @property
    def new_record_is_superseded(self) -> bool:
        return bool(self.get("new_record_is_superseded", False))


class MemoryV2Store:
    """SQL-backed canonical memory store.

    The existing vector/BM25 documents table remains an index projection. This
    store owns the authoritative v2 records and can be used even when the vector
    index is offline.
    """

    def __init__(self, db_path: str, *, data_path: Path | None = None, legacy_db_path: str | None = None):
        self.db_path = str(db_path)
        self.data_path = Path(data_path) if data_path else Path(db_path).parent
        self.legacy_db_path = str(legacy_db_path) if legacy_db_path else None
        self._initialized = False
        self._migrated_from_legacy = False
        self.last_physical_delete_ids: list[str] = []
        self.index_projector = None
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_locks_guard = asyncio.Lock()

    @staticmethod
    def _resolve_search_limit(top_k: int, candidate_limit: int | None) -> int:
        result_limit = max(int(top_k or 5), 1)
        if candidate_limit is None:
            return max(result_limit * 8, 20)
        return max(int(candidate_limit), result_limit, 1)

    @staticmethod
    def _normalize_fts_scores(rows: list[tuple]) -> list[float]:
        raw_scores = [float(row[20] or 0.0) if len(row) > 20 else 0.0 for row in rows]
        if not raw_scores:
            return []
        best = min(raw_scores)
        worst = max(raw_scores)
        if worst <= best:
            return [1.0 for _score in raw_scores]
        scale = worst - best
        return [min(max((worst - score) / scale, 0.0), 1.0) for score in raw_scores]

    @staticmethod
    def _normalize_lock_scope(session_id: str) -> str:
        clean = str(session_id or "").strip()
        return clean if clean else "__global__"

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        scope = self._normalize_lock_scope(session_id)
        async with self._session_locks_guard:
            lock = self._session_locks.get(scope)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[scope] = lock
                if len(self._session_locks) > 200:
                    oldest = next(iter(self._session_locks))
                    # ponytail: skip locks held by active coroutines to avoid evicting in-use sessions (R17)
                    if not self._session_locks[oldest].locked():
                        self._session_locks.pop(oldest, None)
            return lock

    async def _acquire_session_scopes(self, scopes: list[str]) -> AsyncExitStack:
        stack = AsyncExitStack()
        normalized = sorted({self._normalize_lock_scope(item) for item in scopes if self._normalize_lock_scope(item)})
        for scope in normalized:
            await stack.enter_async_context(await self._get_session_lock(scope))
        return stack

    async def _resolve_session_id_for_memory_id(self, memory_id: str) -> str:
        await self.initialize()
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute("SELECT session_id FROM canonical_memories WHERE id = ? LIMIT 1", (memory_id,))
            row = await cursor.fetchone()
        return self._normalize_lock_scope(str(row[0] or "") if row else "")

    async def _resolve_session_ids_for_memory_ids(self, memory_ids: list[str]) -> list[str]:
        ids = [str(item) for item in memory_ids if str(item).strip()]
        if not ids:
            return ["__global__"]
        await self.initialize()
        placeholders = ",".join("?" for _ in ids)
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                f"SELECT DISTINCT session_id FROM canonical_memories WHERE id IN ({placeholders})",
                tuple(ids),
            )
            rows = await cursor.fetchall()
        scopes = [self._normalize_lock_scope(str(row[0] or "")) for row in rows if row is not None]
        return scopes or ["__global__"]

    @staticmethod
    def _build_fts_query(text: str) -> str:
        import re

        segments = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", str(text or ""))
        terms: list[str] = []
        for seg in segments:
            if re.fullmatch(r"[a-zA-Z0-9]+", seg):
                terms.append(f'"{seg}"')
            else:
                if len(seg) == 1:
                    terms.append(f'"{seg}"')
                else:
                    for i in range(len(seg) - 1):
                        terms.append(f'"{seg[i:i+2]}"')
        return " OR ".join(terms)

    @staticmethod
    def _fallback_search_terms(text: str) -> list[str]:
        import re

        weak_terms = {
            "什么",
            "那个",
            "这个",
            "上次",
            "记得",
            "还记",
            "还记得",
            "一下",
            "知道",
            "告诉",
            "吗",
            "呢",
            "啊",
            "呀",
        }
        terms: list[str] = []
        seen: set[str] = set()

        def add(term: str) -> None:
            normalized = str(term or "").strip().lower()
            if normalized and normalized not in weak_terms and normalized not in seen:
                seen.add(normalized)
                terms.append(normalized)

        for raw in str(text or "").split():
            add(raw)
        for seg in re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", str(text or "")):
            add(seg)
            if not re.fullmatch(r"[a-zA-Z0-9]+", seg) and len(seg) > 1:
                for i in range(len(seg) - 1):
                    add(seg[i:i + 2])
        return terms

    @staticmethod
    def _fallback_min_overlap(query_text: str, terms: list[str]) -> int:
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in str(query_text or ""))
        if has_cjk and len(terms) >= 4:
            return 2
        return 1

    async def _sync_fts(self, db, memory_id: str, *, delete_only: bool = False) -> None:
        await db.execute("DELETE FROM canonical_fts WHERE memory_id = ?", (memory_id,))
        if delete_only:
            return
        cursor = await db.execute(
            "SELECT content, summary, tags FROM canonical_memories WHERE id = ? AND status = 'active'",
            (memory_id,),
        )
        row = await cursor.fetchone()
        if row:
            await db.execute(
                "INSERT INTO canonical_fts(memory_id, content, summary, tags) VALUES (?, ?, ?, ?)",
                (memory_id, row[0] or "", row[1] or "", row[2] or ""),
            )

    async def initialize(self) -> None:
        if self._initialized:
            return
        backup_dir = await self._backup_legacy_once()
        await self._migrate_from_legacy_db()
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS canonical_memories (
                    id TEXT PRIMARY KEY,
                    session_id TEXT DEFAULT '',
                    sender_id TEXT DEFAULT '',
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
            await self._migrate_schema(db)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS ix_canonical_memories_session ON canonical_memories(session_id, status)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS ix_canonical_memories_persona ON canonical_memories(persona_id, kind, status)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS ix_canonical_memories_dedup ON canonical_memories(dedup_key)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_dedup_aliases (
                    alias_key TEXT PRIMARY KEY,
                    canonical_memory_id TEXT NOT NULL,
                    created_at REAL DEFAULT 0
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_v2_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT DEFAULT ''
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_v2_migrations (
                    version TEXT PRIMARY KEY,
                    backup_dir TEXT DEFAULT '',
                    status TEXT DEFAULT '',
                    detail TEXT DEFAULT '',
                    applied_at REAL DEFAULT 0
                )
                """
            )
            await db.execute(
                "INSERT OR REPLACE INTO memory_v2_meta(key, value) VALUES ('schema_version', '2')"
            )
            await db.execute(
                """
                INSERT OR REPLACE INTO memory_v2_migrations(version, backup_dir, status, detail, applied_at)
                VALUES ('2', ?, 'applied', 'canonical schema ready', ?)
                """,
                (str(backup_dir or ""), self._now()),
            )
            await db.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS canonical_fts
                USING fts5(
                    memory_id UNINDEXED,
                    content,
                    summary,
                    tags,
                    tokenize='unicode61 remove_diacritics 2'
                )
                """
            )
            await self._ensure_fts_projection(db)
            await db.commit()
        self._initialized = True

    async def _ensure_fts_projection(self, db) -> None:
        canonical_cursor = await db.execute("SELECT COUNT(*) FROM canonical_memories WHERE status = 'active'")
        canonical_row = await canonical_cursor.fetchone()
        projected_cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM canonical_fts fts
            JOIN canonical_memories cm ON cm.id = fts.memory_id
            WHERE cm.status = 'active'
            """
        )
        projected_row = await projected_cursor.fetchone()
        invalid_cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM canonical_fts fts
            LEFT JOIN canonical_memories cm ON cm.id = fts.memory_id
            WHERE cm.id IS NULL OR cm.status != 'active'
            """
        )
        invalid_row = await invalid_cursor.fetchone()
        missing_cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM canonical_memories cm
            WHERE cm.status = 'active'
              AND NOT EXISTS (SELECT 1 FROM canonical_fts fts WHERE fts.memory_id = cm.id)
            """
        )
        missing_row = await missing_cursor.fetchone()
        duplicate_cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT memory_id
                FROM canonical_fts
                GROUP BY memory_id
                HAVING COUNT(*) > 1
            )
            """
        )
        duplicate_row = await duplicate_cursor.fetchone()
        canonical_count = int(canonical_row[0] if canonical_row else 0)
        projected_count = int(projected_row[0] if projected_row else 0)
        invalid_count = int(invalid_row[0] if invalid_row else 0)
        missing_count = int(missing_row[0] if missing_row else 0)
        duplicate_count = int(duplicate_row[0] if duplicate_row else 0)
        if (
            canonical_count == projected_count
            and invalid_count == 0
            and missing_count == 0
            and duplicate_count == 0
        ):
            return
        await db.execute("DELETE FROM canonical_fts")
        await db.execute(
            """
            INSERT INTO canonical_fts(memory_id, content, summary, tags)
            SELECT id, COALESCE(content, ''), COALESCE(summary, ''), COALESCE(tags, '[]')
            FROM canonical_memories
            WHERE status = 'active'
            """
        )

    async def _migrate_schema(self, db) -> None:
        cursor = await db.execute("PRAGMA table_info(canonical_memories)")
        columns = {str(row[1]) for row in await cursor.fetchall()}
        migrations = {
            "sender_id": "ALTER TABLE canonical_memories ADD COLUMN sender_id TEXT DEFAULT ''",
            "source_ref": "ALTER TABLE canonical_memories ADD COLUMN source_ref TEXT DEFAULT ''",
            "visibility": "ALTER TABLE canonical_memories ADD COLUMN visibility TEXT DEFAULT 'auto_and_tool'",
        }
        for column, sql in migrations.items():
            if column not in columns:
                await db.execute(sql)

    async def _backup_legacy_once(self) -> Path | None:
        marker = self.data_path / ".memory_v2_backup_done"
        if marker.exists():
            try:
                value = marker.read_text(encoding="utf-8").strip()
                return Path(value) if value else None
            except Exception:
                return None
        try:
            self.data_path.mkdir(parents=True, exist_ok=True)
            backup_dir = self.data_path / f"backup_before_v2_{time.strftime('%Y%m%d_%H%M%S')}"
            backup_dir.mkdir(parents=True, exist_ok=True)
            for name in ("docs.db", "vectors.index", "persona_cache.json"):
                source = self.data_path / name
                if source.exists():
                    shutil.copy2(source, backup_dir / name)
            marker.write_text(str(backup_dir), encoding="utf-8")
            logger.info(f"[MemoryV2] legacy memory backup prepared at {backup_dir}")
            return backup_dir
        except Exception as exc:
            logger.warning(f"[MemoryV2] legacy backup skipped: {exc}")
            return None

    async def _migrate_from_legacy_db(self) -> None:
        if self._migrated_from_legacy:
            return
        if not self.legacy_db_path or not os.path.isfile(self.legacy_db_path):
            return
        if os.path.abspath(self.legacy_db_path) == os.path.abspath(self.db_path):
            self._migrated_from_legacy = True
            return
        try:
            target = connect_aiosqlite(self.db_path)
            async with target as tgt_db:
                await tgt_db.execute("ATTACH DATABASE ? AS legacy_src", (self.legacy_db_path,))
                cursor = await tgt_db.execute(
                    "SELECT name FROM legacy_src.sqlite_master WHERE type='table' AND name='canonical_memories'"
                )
                row = await cursor.fetchone()
                await cursor.close()
                if not row:
                    await tgt_db.execute("DETACH DATABASE legacy_src")
                    self._migrated_from_legacy = True
                    return
                await tgt_db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS canonical_memories (
                        id TEXT PRIMARY KEY,
                        session_id TEXT DEFAULT '',
                        sender_id TEXT DEFAULT '',
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
                source_columns_cursor = await tgt_db.execute("PRAGMA legacy_src.table_info(canonical_memories)")
                source_columns = {str(item[1]) for item in await source_columns_cursor.fetchall()}
                target_columns = [
                    "id", "session_id", "sender_id", "persona_id", "source", "kind", "content", "summary",
                    "tags", "importance", "confidence", "status", "decay_score", "create_time", "update_time",
                    "last_access_time", "access_count", "superseded_by", "deleted_reason", "metadata", "dedup_key",
                    "source_ref", "visibility",
                ]
                defaults = {
                    "session_id": "''", "sender_id": "''", "persona_id": "''", "source": "''", "kind": "''",
                    "content": "''", "summary": "''", "tags": "'[]'", "importance": "0.5",
                    "confidence": "0.8", "status": "'active'", "decay_score": "1.0", "create_time": "0",
                    "update_time": "0", "last_access_time": "0", "access_count": "0", "superseded_by": "''",
                    "deleted_reason": "''", "metadata": "'{}'", "dedup_key": "''", "source_ref": "''",
                    "visibility": "'auto_and_tool'",
                }
                select_columns = [column if column in source_columns else defaults.get(column, "''") for column in target_columns]
                await tgt_db.execute(
                    f"INSERT OR IGNORE INTO main.canonical_memories ({', '.join(target_columns)}) "
                    f"SELECT {', '.join(select_columns)} FROM legacy_src.canonical_memories"
                )
                await tgt_db.commit()
                await tgt_db.execute("DETACH DATABASE legacy_src")
            count = 0
            async with connect_aiosqlite(self.db_path) as db:
                cursor = await db.execute("SELECT COUNT(*) FROM canonical_memories")
                row = await cursor.fetchone()
                count = int(row[0]) if row else 0
            logger.info(
                f"[MemoryV2] migrated {count} canonical_memories rows from "
                f"{self.legacy_db_path} 鈫?{self.db_path}"
            )
            self._migrated_from_legacy = True
        except Exception as exc:
            logger.warning(f"[MemoryV2] legacy migration skipped: {exc}")

    @staticmethod
    def _now() -> float:
        return time.time()

    @staticmethod
    def _json_list(value: Any) -> str:
        if isinstance(value, str):
            return json.dumps([value], ensure_ascii=False)
        if not isinstance(value, list):
            value = []
        return json.dumps([str(item) for item in value if str(item).strip()], ensure_ascii=False)

    @staticmethod
    def _json_dict(value: Any) -> str:
        return json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False)

    @staticmethod
    def _looks_like_authority_eav(dedup_key: str, request: MemoryWriteRequest) -> bool:
        clean_key = str(dedup_key or "").strip()
        if not clean_key or str(request.kind or "").strip().lower() != "fact":
            return False
        parts = [part.strip() for part in clean_key.split(":")]
        if len(parts) != 3 or not all(parts):
            return False
        metadata = dict(request.metadata or {})
        if bool(metadata.get("authority_eav")):
            return True
        source = str(request.source or "").strip().lower()
        return source in {"instant_gate", "instant_gate_llm", "dream_audit_pipeline", "authority_backfill"}

    async def upsert(self, request: MemoryWriteRequest) -> MemoryUpsertResult:
        await self.initialize()
        now = self._now()
        memory_id = f"mem_{uuid.uuid4().hex[:16]}"
        summary = request.summary or request.content[:240]
        dedup_key = request.dedup_key.strip()
        sender_id = str(request.sender_id or "").strip()
        created_at = float(request.created_at or 0.0) or now
        visibility = request.visibility if request.visibility in {"auto_and_tool", "tool_only", "maintenance_only"} else "auto_and_tool"
        status = str(request.status or ACTIVE_STATUS).strip() or ACTIVE_STATUS
        if status not in {
            ACTIVE_STATUS,
            STALE_STATUS,
            SUPERSEDED_STATUS,
            DELETED_STATUS,
            MERGED_STATUS,
            DEPRECATED_STATUS,
            REVIEW_PENDING_STATUS,
            REJECTED_STATUS,
        }:
            status = ACTIVE_STATUS
        authority_eav = self._looks_like_authority_eav(dedup_key, request)

        scopes = [request.session_id]
        if sender_id:
            scopes.append(f"sender:{sender_id}")
        if dedup_key and not authority_eav:
            scopes.append(f"dedup:{dedup_key}")
        async with await self._acquire_session_scopes(scopes) as _locks:
            async with connect_aiosqlite(self.db_path) as db:
                if dedup_key and not authority_eav:
                    cursor = await db.execute(
                        """
                        SELECT id, content, summary, access_count
                        FROM canonical_memories
                        WHERE dedup_key = ? AND status IN ('active', 'stale', 'review_pending')
                        LIMIT 1
                        """,
                        (dedup_key,),
                    )
                    row = await cursor.fetchone()
                    if row:
                        memory_id = str(row[0])
                        merged_content = str(request.content or row[1] or "")
                        merged_summary = str(summary or row[2] or "")[:500]
                        await db.execute(
                            """
                            UPDATE canonical_memories
                            SET content = ?, summary = ?, source = ?, kind = ?,
                                importance = MAX(importance, ?),
                                confidence = MAX(confidence, ?),
                                status = ?,
                                update_time = ?, last_access_time = ?,
                                access_count = COALESCE(access_count, 0) + 1,
                                tags = ?, metadata = ?, source_ref = ?, visibility = ?
                            WHERE id = ?
                            """,
                            (
                                merged_content,
                                merged_summary,
                                request.source,
                                request.kind,
                                float(request.importance or 0.5),
                                float(request.confidence or 0.8),
                                status,
                                now,
                                now,
                                self._json_list(request.tags),
                                self._json_dict(request.metadata),
                                request.source_ref,
                                visibility,
                                memory_id,
                            ),
                        )
                        await self._sync_fts(db, memory_id)
                        await db.commit()
                        return MemoryUpsertResult(
                            memory_id=memory_id,
                            superseded_old_ids=[],
                            new_record_is_superseded=False,
                        )

                await db.execute(
                    """
                    INSERT INTO canonical_memories (
                        id, session_id, sender_id, persona_id, source, kind, content, summary,
                        tags, importance, confidence, status, decay_score,
                        create_time, update_time, last_access_time, access_count,
                        metadata, dedup_key, source_ref, visibility
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        request.session_id,
                        sender_id,
                        request.persona_id,
                        request.source,
                        request.kind,
                        request.content,
                        summary,
                        self._json_list(request.tags),
                        float(request.importance or 0.5),
                        float(request.confidence or 0.8),
                        status,
                        created_at,
                        now,
                        now,
                        self._json_dict(request.metadata),
                        dedup_key,
                        request.source_ref,
                        visibility,
                    ),
                )
                superseded_old_ids: list[str] = []
                new_record_is_superseded = False
                if authority_eav:
                    superseded_old_ids = await self.mark_superseded_by_key(
                        dedup_key,
                        memory_id,
                        created_at=created_at,
                        sender_id=sender_id,
                        session_id=request.session_id,
                        db=db,
                    )
                    cursor = await db.execute(
                        "SELECT status FROM canonical_memories WHERE id = ? LIMIT 1",
                        (memory_id,),
                    )
                    row = await cursor.fetchone()
                    new_record_is_superseded = str(row[0] or "") == SUPERSEDED_STATUS if row else False
                if not new_record_is_superseded:
                    await self._sync_fts(db, memory_id)
                await db.commit()
        return MemoryUpsertResult(
            memory_id=memory_id,
            superseded_old_ids=superseded_old_ids,
            new_record_is_superseded=new_record_is_superseded,
        )

    async def get_by_id(self, memory_id: str, *, allow_stale: bool = False) -> MemoryCandidate | None:
        await self.initialize()
        statuses = [ACTIVE_STATUS]
        if allow_stale:
            statuses.append(STALE_STATUS)
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                f"""
                SELECT id, kind, source, summary, content, session_id, sender_id, persona_id,
                       tags, importance, confidence, status, create_time,
                       update_time, last_access_time, metadata, visibility, access_count, decay_score, superseded_by
                FROM canonical_memories
                WHERE id = ? AND status IN ({','.join('?' for _ in statuses)})
                LIMIT 1
                """,
                (memory_id, *statuses),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_candidate(row)

    async def batch_get_by_ids(self, memory_ids: list[str], *, allow_stale: bool = False) -> dict[str, MemoryCandidate]:
        await self.initialize()
        ids = list(dict.fromkeys(str(item or "").strip() for item in memory_ids if str(item or "").strip()))
        if not ids:
            return {}
        statuses = [ACTIVE_STATUS]
        if allow_stale:
            statuses.append(STALE_STATUS)
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                f"""
                SELECT id, kind, source, summary, content, session_id, sender_id, persona_id,
                       tags, importance, confidence, status, create_time,
                       update_time, last_access_time, metadata, visibility, access_count, decay_score, superseded_by
                FROM canonical_memories
                WHERE id IN ({','.join('?' for _ in ids)}) AND status IN ({','.join('?' for _ in statuses)})
                """,
                (*ids, *statuses),
            )
            rows = await cursor.fetchall()
        return {str(row[0]): self._row_to_candidate(row) for row in rows}

    async def get_canonical(self, memory_id: str, *, include_inactive: bool = False) -> MemoryCandidate | None:
        await self.initialize()
        where = "id = ?"
        params: tuple[Any, ...] = (memory_id,)
        if not include_inactive:
            where += " AND status IN ('active', 'stale')"
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                f"""
                SELECT id, kind, source, summary, content, session_id, sender_id, persona_id,
                       tags, importance, confidence, status, create_time,
                       update_time, last_access_time, metadata, visibility, access_count, decay_score, superseded_by
                FROM canonical_memories
                WHERE {where}
                LIMIT 1
                """,
                params,
            )
            row = await cursor.fetchone()
        return self._row_to_candidate(row) if row else None

    async def get_by_dedup_key(self, dedup_key: str, *, include_inactive: bool = False) -> MemoryCandidate | None:
        await self.initialize()
        clean_key = str(dedup_key or "").strip()
        if not clean_key:
            return None
        where = "dedup_key = ?"
        params: tuple[Any, ...] = (clean_key,)
        if not include_inactive:
            where += " AND status IN ('active', 'stale', 'review_pending')"
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                f"""
                SELECT id, kind, source, summary, content, session_id, sender_id, persona_id,
                       tags, importance, confidence, status, create_time,
                       update_time, last_access_time, metadata, visibility, access_count, decay_score, superseded_by
                FROM canonical_memories
                WHERE {where}
                LIMIT 1
                """,
                params,
            )
            row = await cursor.fetchone()
            if row is None:
                alias_status = "" if include_inactive else "AND c.status IN ('active', 'stale', 'review_pending')"
                cursor = await db.execute(
                    f"""
                    SELECT c.id, c.kind, c.source, c.summary, c.content, c.session_id, c.sender_id, c.persona_id,
                           c.tags, c.importance, c.confidence, c.status, c.create_time,
                           c.update_time, c.last_access_time, c.metadata, c.visibility, c.access_count,
                           c.decay_score, c.superseded_by
                    FROM memory_dedup_aliases AS a
                    JOIN canonical_memories AS c ON c.id = a.canonical_memory_id
                    WHERE a.alias_key = ? {alias_status}
                    LIMIT 1
                    """,
                    (clean_key,),
                )
                row = await cursor.fetchone()
        return self._row_to_candidate(row) if row else None

    async def resolve_dedup_key(self, dedup_key: str) -> str:
        await self.initialize()
        clean_key = str(dedup_key or "").strip()
        if not clean_key:
            return ""
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT c.dedup_key
                FROM memory_dedup_aliases AS a
                JOIN canonical_memories AS c ON c.id = a.canonical_memory_id
                WHERE a.alias_key = ? AND c.status IN ('active', 'stale', 'review_pending')
                LIMIT 1
                """,
                (clean_key,),
            )
            row = await cursor.fetchone()
        return str(row[0] or clean_key) if row else clean_key

    async def replace_dedup_identity(
        self,
        memory_id: str,
        *,
        old_dedup_key: str,
        new_dedup_key: str,
        content: str,
        summary: str,
        metadata: dict[str, Any],
        status: str,
        visibility: str,
    ) -> int:
        await self.initialize()
        old_key = str(old_dedup_key or "").strip()
        new_key = str(new_dedup_key or "").strip()
        if not str(memory_id or "").strip() or not new_key:
            return 0
        scopes = await self._resolve_session_ids_for_memory_ids([memory_id])
        scopes.extend([f"dedup:{old_key}", f"dedup:{new_key}"])
        async with await self._acquire_session_scopes(scopes) as _locks:
            async with connect_aiosqlite(self.db_path) as db:
                cursor = await db.execute(
                    """
                    SELECT id FROM canonical_memories
                    WHERE dedup_key = ? AND id != ?
                      AND status IN ('active', 'stale', 'review_pending')
                    LIMIT 1
                    """,
                    (new_key, memory_id),
                )
                conflict = await cursor.fetchone()
                if conflict:
                    conflict_id = str(conflict[0])
                    await db.execute(
                        """
                        UPDATE canonical_memories
                        SET status = ?, superseded_by = ?, dedup_key = '', update_time = ?
                        WHERE id = ?
                        """,
                        (SUPERSEDED_STATUS, memory_id, self._now(), conflict_id),
                    )
                    await self._sync_fts(db, conflict_id, delete_only=True)
                cursor = await db.execute(
                    """
                    UPDATE canonical_memories
                    SET content = ?, summary = ?, metadata = ?, status = ?, visibility = ?,
                        dedup_key = ?, update_time = ?, last_access_time = ?
                    WHERE id = ?
                    """,
                    (
                        str(content or ""),
                        str(summary or str(content or "")[:240]),
                        self._json_dict(metadata),
                        str(status or ACTIVE_STATUS),
                        visibility if visibility in {"auto_and_tool", "tool_only", "maintenance_only"} else "auto_and_tool",
                        new_key,
                        self._now(),
                        self._now(),
                        memory_id,
                    ),
                )
                if old_key and old_key != new_key:
                    await db.execute(
                        """
                        INSERT INTO memory_dedup_aliases(alias_key, canonical_memory_id, created_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(alias_key) DO UPDATE SET
                            canonical_memory_id = excluded.canonical_memory_id,
                            created_at = excluded.created_at
                        """,
                        (old_key, memory_id, self._now()),
                    )
                await db.execute("DELETE FROM memory_dedup_aliases WHERE alias_key = ?", (new_key,))
                await self._sync_fts(db, memory_id)
                await db.commit()
                return cursor.rowcount

    async def mark_superseded_by_key(
        self,
        dedup_key: str,
        new_memory_id: str,
        *,
        created_at: float,
        sender_id: str = "",
        session_id: str = "",
        db=None,
    ) -> list[str]:
        clean_key = str(dedup_key or "").strip()
        if not clean_key or not str(new_memory_id or "").strip():
            return []
        await self.initialize()
        scopes = [session_id]
        if sender_id:
            scopes.append(f"sender:{sender_id}")

        async def _apply(inner_db) -> list[str]:
            covered_old_ids: list[str] = []
            superseding_old_id = ""
            new_record_is_superseded = False
            cursor = await inner_db.execute(
                """
                SELECT id, create_time
                FROM canonical_memories
                WHERE dedup_key = ? AND id != ? AND status IN ('active', 'stale')
                ORDER BY create_time DESC, update_time DESC
                """,
                (clean_key, new_memory_id),
            )
            rows = await cursor.fetchall()
            for row in rows:
                old_id = str(row[0] or "")
                old_create_time = float(row[1] or 0.0)
                if old_create_time < float(created_at or 0.0):
                    replacement_id = superseding_old_id or new_memory_id
                    await inner_db.execute(
                        """
                        UPDATE canonical_memories
                        SET status = ?, superseded_by = ?, update_time = ?
                        WHERE id = ?
                        """,
                        (SUPERSEDED_STATUS, replacement_id, self._now(), old_id),
                    )
                    covered_old_ids.append(old_id)
                else:
                    if not new_record_is_superseded:
                        await inner_db.execute(
                            """
                            UPDATE canonical_memories
                            SET status = ?, superseded_by = ?, update_time = ?
                            WHERE id = ?
                            """,
                            (SUPERSEDED_STATUS, old_id, self._now(), new_memory_id),
                        )
                        superseding_old_id = old_id
                        new_record_is_superseded = True
                        continue
                    await inner_db.execute(
                        """
                        UPDATE canonical_memories
                        SET status = ?, superseded_by = ?, update_time = ?
                        WHERE id = ?
                        """,
                        (SUPERSEDED_STATUS, superseding_old_id, self._now(), old_id),
                    )
                    covered_old_ids.append(old_id)
            return covered_old_ids

        if db is not None:
            return await _apply(db)

        async with await self._acquire_session_scopes(scopes) as _locks:
            async with connect_aiosqlite(self.db_path) as owned_db:
                result = await _apply(owned_db)
                await owned_db.commit()
                return result

    def _row_to_candidate(self, row) -> MemoryCandidate:
        try:
            tags = json.loads(row[8] or "[]")
            if not isinstance(tags, list):
                tags = []
        except Exception:
            tags = []
        try:
            metadata = json.loads(row[15] or "{}")
            if not isinstance(metadata, dict):
                metadata = {}
        except Exception:
            metadata = {}
        now = self._now()
        created_at = float(row[12] or 0.0)
        age_days = max(0.0, (now - created_at) / 86400) if created_at else 0.0
        return MemoryCandidate(
            id=str(row[0] or ""),
            kind=str(row[1] or ""),
            source=str(row[2] or ""),
            summary=str(row[3] or ""),
            content=str(row[4] or ""),
            session_id=str(row[5] or ""),
            sender_id=str(row[6] or ""),
            persona_id=str(row[7] or ""),
            tags=[str(item) for item in tags],
            importance=float(row[9] or 0.5),
            confidence=float(row[10] or 0.8),
            recency_score=1.0 / (1.0 + age_days),
            status=str(row[11] or ACTIVE_STATUS),
            visibility=str(row[16] or "auto_and_tool"),
            created_at=created_at,
            updated_at=float(row[13] or 0.0),
            last_access_time=float(row[14] or 0.0),
            superseded_by=str(row[19] or "") if len(row) > 19 else "",
            access_count=int(row[17] or 0) if len(row) > 17 else 0,
            decay_score=float(row[18] or 1.0) if len(row) > 18 else 1.0,
            metadata_hydrated=True,
            metadata=metadata,
        )

    async def search(
        self,
        query: str,
        *,
        session_id: str = "",
        persona_id: str = "",
        layers: list[str] | None = None,
        top_k: int = 5,
        candidate_limit: int | None = None,
        exclude_ids: list[str] | None = None,
        allow_stale: bool = False,
        visibility_mode: str = "",
        track_access: bool = True,
    ) -> list[MemoryCandidate]:
        await self.initialize()
        query_text = str(query or "").strip()
        if not query_text:
            return []
        fts_query = self._build_fts_query(query_text)
        lowered_terms = self._fallback_search_terms(query_text)
        statuses = [ACTIVE_STATUS]
        if allow_stale:
            statuses.append(STALE_STATUS)
        exclude = {str(item) for item in exclude_ids or [] if str(item).strip()}
        layer_set = {str(item) for item in layers or [] if str(item).strip()}
        result_limit = max(int(top_k or 5), 1)
        search_limit = self._resolve_search_limit(result_limit, candidate_limit)
        if visibility_mode == "auto":
            allowed_visibility = {"auto_and_tool"}
        elif visibility_mode == "tool":
            allowed_visibility = {"auto_and_tool", "tool_only"}
        else:
            allowed_visibility = {"auto_and_tool", "tool_only"}

        where = ["status IN (" + ",".join("?" for _ in statuses) + ")"]
        params: list[Any] = list(statuses)
        where.append("visibility IN (" + ",".join("?" for _ in allowed_visibility) + ")")
        params.extend(sorted(allowed_visibility))
        if session_id == "__self_lore__":
            where.append("session_id = ?")
            params.append(session_id)
        elif session_id:
            where.append("(session_id = ? OR session_id = '')")
            params.append(session_id)
        if persona_id:
            where.append("(persona_id = ? OR persona_id = '')")
            params.append(persona_id)
        if layer_set:
            where.append("kind IN (" + ",".join("?" for _ in layer_set) + ")")
            params.extend(layer_set)

        async with connect_aiosqlite(self.db_path) as db:
            if fts_query:
                cursor = await db.execute(
                    f"""
                    SELECT cm.id, cm.kind, cm.source, cm.summary, cm.content, cm.session_id, cm.sender_id, cm.persona_id,
                           cm.tags, cm.importance, cm.confidence, cm.status, cm.create_time,
                           cm.update_time, cm.last_access_time, cm.metadata, cm.visibility, cm.access_count, cm.decay_score, cm.superseded_by,
                           bm25(canonical_fts) AS fts_score
                    FROM canonical_fts
                    JOIN canonical_memories cm ON cm.id = canonical_fts.memory_id
                    WHERE canonical_fts MATCH ? AND {' AND '.join(where)}
                    ORDER BY fts_score ASC, cm.update_time DESC
                    LIMIT ?
                    """,
                    (fts_query, *params, search_limit),
                )
                rows = await cursor.fetchall()
                candidates = []
                normalized_scores = self._normalize_fts_scores(rows)
                for row, relevance in zip(rows, normalized_scores):
                    candidate = self._row_to_candidate(row)
                    if candidate.id in exclude:
                        continue
                    candidate.relevance_score = relevance
                    candidate.metadata = dict(candidate.metadata or {})
                    candidate.metadata["matched_by"] = ["canonical_fts"]
                    candidates.append(candidate)
            if not fts_query or not candidates:
                cursor = await db.execute(
                    f"""
                    SELECT id, kind, source, summary, content, session_id, sender_id, persona_id,
                           tags, importance, confidence, status, create_time,
                           update_time, last_access_time, metadata, visibility, access_count, decay_score, superseded_by
                    FROM canonical_memories
                    WHERE {' AND '.join(where)}
                    ORDER BY update_time DESC
                    LIMIT ?
                    """,
                    (*params, search_limit),
                )
                rows = await cursor.fetchall()
                candidates = []
                for row in rows:
                    memory_id = str(row[0])
                    if memory_id in exclude:
                        continue
                    summary = str(row[3] or "")
                    content = str(row[4] or "")
                    haystack = f"{summary}\n{content}".lower()
                    if lowered_terms:
                        overlap = sum(1 for term in lowered_terms if term in haystack)
                        if overlap < self._fallback_min_overlap(query_text, lowered_terms):
                            continue
                        relevance = min(1.0, overlap / max(len(lowered_terms), 1))
                    else:
                        continue
                    candidate = self._row_to_candidate(row)
                    candidate.relevance_score = relevance
                    candidate.metadata = dict(candidate.metadata or {})
                    candidate.metadata["matched_by"] = ["fallback"]
                    candidates.append(candidate)

        candidates.sort(
            key=lambda item: (
                item.relevance_score,
                item.relevance_score * DEFAULT_MEMORY_SCORING.search_weight
                + item.importance * DEFAULT_MEMORY_SCORING.search_importance_weight
                + item.confidence * DEFAULT_MEMORY_SCORING.search_confidence_weight
                + item.recency_score * DEFAULT_MEMORY_SCORING.search_recency_weight
                - (DEFAULT_MEMORY_SCORING.search_stale_penalty if item.status == STALE_STATUS else 0.0)
            ),
            reverse=True,
        )
        return_limit = search_limit if candidate_limit is not None else result_limit
        selected = candidates[:return_limit]
        if selected and track_access:
            await self.finalize_access(selected, allow_stale=allow_stale)
        return selected

    async def finalize_access(self, candidates: list[MemoryCandidate], *, allow_stale: bool = False) -> None:
        selected = [item for item in candidates if str(getattr(item, "id", "") or "").strip()]
        if not selected:
            return
        restore_ids = [
            item.id
            for item in selected
            if allow_stale and item.status == STALE_STATUS and int(getattr(item, "access_count", 0) or 0) > 0
        ]
        for memory_id in restore_ids:
            await self.restore(memory_id, reason="stale_access")
            if getattr(self, "index_projector", None):
                try:
                    await self.index_projector.project(memory_id)
                except Exception:
                    pass
        for item in selected:
            if item.id in restore_ids:
                item.status = ACTIVE_STATUS
        await self.mark_accessed([item.id for item in selected])

    async def list_candidates(
        self,
        *,
        session_id: str = "",
        persona_id: str = "",
        kinds: list[str] | None = None,
        statuses: list[str] | None = None,
        limit: int = 200,
        include_inactive: bool = False,
        visibility_mode: str = "",
    ) -> list[MemoryCandidate]:
        await self.initialize()
        where: list[str] = []
        params: list[Any] = []
        active_statuses = list(statuses or ([] if include_inactive else [ACTIVE_STATUS, STALE_STATUS]))
        if active_statuses:
            where.append("status IN (" + ",".join("?" for _ in active_statuses) + ")")
            params.extend(active_statuses)
        if session_id == "__self_lore__":
            where.append("session_id = ?")
            params.append(session_id)
        elif session_id:
            where.append("(session_id = ? OR session_id = '')")
            params.append(session_id)
        if persona_id:
            where.append("(persona_id = ? OR persona_id = '')")
            params.append(persona_id)
        clean_kinds = [str(item) for item in kinds or [] if str(item).strip()]
        if clean_kinds:
            where.append("kind IN (" + ",".join("?" for _ in clean_kinds) + ")")
            params.extend(clean_kinds)
        if visibility_mode == "auto":
            allowed_visibility = {"auto_and_tool"}
        elif visibility_mode == "tool":
            allowed_visibility = {"auto_and_tool", "tool_only"}
        else:
            allowed_visibility = {"auto_and_tool", "tool_only"}
        if allowed_visibility:
            where.append("visibility IN (" + ",".join("?" for _ in allowed_visibility) + ")")
            params.extend(sorted(allowed_visibility))
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                f"""
                SELECT id, kind, source, summary, content, session_id, sender_id, persona_id,
                       tags, importance, confidence, status, create_time,
                       update_time, last_access_time, metadata, visibility, access_count, decay_score, superseded_by
                FROM canonical_memories
                {where_sql}
                ORDER BY update_time DESC
                LIMIT ?
                """,
                (*params, max(1, min(int(limit or 200), 500))),
            )
            rows = await cursor.fetchall()
        return [self._row_to_candidate(row) for row in rows]

    async def list_canonical(
        self,
        *,
        session_id: str = "",
        persona_id: str = "",
        kind: str = "",
        status: str = "",
        limit: int = 100,
        offset: int = 0,
        include_inactive: bool = True,
    ) -> dict[str, Any]:
        await self.initialize()
        where: list[str] = []
        params: list[Any] = []
        if not include_inactive:
            where.append("status IN ('active', 'stale')")
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if persona_id:
            where.append("persona_id = ?")
            params.append(persona_id)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if status:
            where.append("status = ?")
            params.append(status)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        page_limit = max(1, min(int(limit or 100), 500))
        page_offset = max(0, int(offset or 0))
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM canonical_memories {where_sql}",
                tuple(params),
            )
            total_row = await cursor.fetchone()
            cursor = await db.execute(
                f"""
                SELECT id, kind, source, summary, content, session_id, sender_id, persona_id,
                       tags, importance, confidence, status, create_time,
                       update_time, last_access_time, metadata, visibility, access_count, decay_score, superseded_by
                FROM canonical_memories
                {where_sql}
                ORDER BY update_time DESC
                LIMIT ? OFFSET ?
                """,
                (*params, page_limit, page_offset),
            )
            rows = await cursor.fetchall()
        return {
            "items": [self._candidate_to_dict(self._row_to_candidate(row)) for row in rows],
            "total": int(total_row[0] if total_row else 0),
            "limit": page_limit,
            "offset": page_offset,
        }

    @staticmethod
    def _candidate_to_dict(candidate: MemoryCandidate) -> dict[str, Any]:
        return {
            "id": candidate.id,
            "kind": candidate.kind,
            "source": candidate.source,
            "summary": candidate.summary,
            "content": candidate.content,
            "session_id": candidate.session_id,
            "sender_id": candidate.sender_id,
            "persona_id": candidate.persona_id,
            "tags": list(candidate.tags or []),
            "importance": candidate.importance,
            "confidence": candidate.confidence,
            "relevance_score": candidate.relevance_score,
            "recency_score": candidate.recency_score,
            "status": candidate.status,
            "visibility": candidate.visibility,
            "created_at": candidate.created_at,
            "updated_at": candidate.updated_at,
            "last_access_time": candidate.last_access_time,
            "superseded_by": candidate.superseded_by,
            "access_count": candidate.access_count,
            "decay_score": candidate.decay_score,
            "metadata_hydrated": candidate.metadata_hydrated,
            "metadata": dict(candidate.metadata or {}),
        }

    async def batch_get_memory_meta(self, memory_ids: list[str]) -> dict[str, dict[str, Any]]:
        ids = [str(item) for item in memory_ids if str(item).strip()]
        if not ids:
            return {}
        await self.initialize()
        placeholders = ",".join("?" for _ in ids)
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                f"""
                SELECT id, kind, importance, status, visibility, create_time,
                       update_time, last_access_time, access_count, decay_score, metadata, sender_id, superseded_by
                FROM canonical_memories
                WHERE id IN ({placeholders})
                """,
                tuple(ids),
            )
            rows = await cursor.fetchall()
        payload: dict[str, dict[str, Any]] = {}
        for row in rows:
            metadata: Any = {}
            try:
                metadata = json.loads(row[10] or "{}")
                if not isinstance(metadata, dict):
                    metadata = {}
            except Exception:
                metadata = {}
            payload[str(row[0] or "")] = {
                "kind": str(row[1] or ""),
                "importance": float(row[2] or 0.5),
                "status": str(row[3] or ACTIVE_STATUS),
                "visibility": str(row[4] or "auto_and_tool"),
                "created_at": float(row[5] or 0.0),
                "updated_at": float(row[6] or 0.0),
                "last_access_time": float(row[7] or 0.0),
                "access_count": int(row[8] or 0),
                "decay_score": float(row[9] or 1.0),
                "metadata": metadata,
                "sender_id": str(row[11] or ""),
                "superseded_by": str(row[12] or ""),
            }
        return payload

    async def mark_accessed(self, memory_ids: list[str]) -> None:
        ids = [str(item) for item in memory_ids if str(item).strip()]
        if not ids:
            return
        await self.initialize()
        scopes = await self._resolve_session_ids_for_memory_ids(ids)
        async with await self._acquire_session_scopes(scopes) as _locks:
            now = self._now()
            async with connect_aiosqlite(self.db_path) as db:
                for memory_id in ids:
                    await db.execute(
                        """
                        UPDATE canonical_memories
                        SET last_access_time = ?, access_count = COALESCE(access_count, 0) + 1
                        WHERE id = ?
                        """,
                        (now, memory_id),
                    )
                await db.commit()

    async def apply_decay(
        self,
        *,
        decay_rate: float,
        min_score: float = 0.05,
        stale_grace_seconds: float = 7 * 86400,
        days: int = 1,
        allow_protected_physical_delete: bool = False,
        protected_kinds: tuple[str, ...] = ("persona_lore", "profile"),
        protected_importance: float = 0.95,
    ) -> int:
        await self.initialize()
        self.last_physical_delete_ids = []
        now = self._now()
        decay_factor = max(0.0, min(1.0, (1 - float(decay_rate or 0.0)) ** max(int(days or 1), 1)))
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute(
                """
                UPDATE canonical_memories
                SET decay_score = MAX(0.0, COALESCE(decay_score, 1.0) * ?),
                    status = CASE
                        WHEN status = 'active'
                         AND (COALESCE(decay_score, 1.0) * ?) <= ?
                        THEN 'stale'
                        ELSE status
                    END,
                    update_time = ?
                WHERE status IN ('active', 'stale')
                """,
                (decay_factor, decay_factor, min_score, now),
            )
            delete_where = [
                "status = 'stale'",
                "COALESCE(last_access_time, create_time, 0) <= ?",
            ]
            delete_params: list[Any] = [now - stale_grace_seconds]
            if not allow_protected_physical_delete:
                delete_where.append(
                    f"kind NOT IN ({','.join('?' for _ in protected_kinds)})"
                )
                delete_params.extend(protected_kinds)
                delete_where.append("importance < ?")
                delete_params.append(float(protected_importance))
            cursor = await db.execute(
                f"SELECT id FROM canonical_memories WHERE {' AND '.join(delete_where)}",
                tuple(delete_params),
            )
            self.last_physical_delete_ids = [str(row[0]) for row in await cursor.fetchall()]
            if self.last_physical_delete_ids:
                placeholders = ",".join("?" for _ in self.last_physical_delete_ids)
                await db.execute(
                    f"DELETE FROM canonical_fts WHERE memory_id IN ({placeholders})",
                    tuple(self.last_physical_delete_ids),
                )
                cursor = await db.execute(
                    f"DELETE FROM canonical_memories WHERE id IN ({placeholders})",
                    tuple(self.last_physical_delete_ids),
                )
            else:
                cursor = None
            await db.commit()
            return int(getattr(cursor, "rowcount", 0) or 0)

    async def purge_jargon_candidates(
        self,
        *,
        statuses: tuple[str, ...],
        older_than_seconds: float,
        min_confidence_to_keep: float = 0.9,
        min_count_to_keep: int = 5,
        review_statuses: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        await self.initialize()
        now = self._now()
        cutoff = now - max(float(older_than_seconds or 0.0), 0.0)
        if cutoff > now:  # ponytail: NTP backward jump guard
            logger.warning(f"[AstrMai-v2store] NTP backward jump: cutoff={cutoff} > now={now}, clamping")
            cutoff = 0.0
        deleted_ids: list[str] = []
        protected_skipped = 0
        if not statuses:
            return {"deleted_ids": deleted_ids, "protected_skipped": protected_skipped}
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                f"""
                SELECT id, metadata, confidence
                FROM canonical_memories
                WHERE kind = 'jargon'
                  AND status IN ({','.join('?' for _ in statuses)})
                  AND COALESCE(update_time, create_time, 0) <= ?
                """,
                (*statuses, cutoff),
            )
            rows = await cursor.fetchall()
            for memory_id, metadata_raw, confidence_value in rows:
                try:
                    metadata = json.loads(metadata_raw or "{}")
                    if not isinstance(metadata, dict):
                        metadata = {}
                except Exception:
                    metadata = {}
                review_status = str(metadata.get("review_status") or "").strip().lower()
                if review_statuses and review_status not in {str(item).strip().lower() for item in review_statuses if str(item).strip()}:
                    continue
                confidence = float(metadata.get("confidence") or confidence_value or 0.0)
                count = int(metadata.get("count") or 0)
                if confidence >= float(min_confidence_to_keep or 0.9) or count >= int(min_count_to_keep or 5):
                    protected_skipped += 1
                    continue
                deleted_ids.append(str(memory_id))
            for memory_id in deleted_ids:
                await db.execute("DELETE FROM canonical_memories WHERE id = ?", (memory_id,))
            # ponytail: sync FTS to prevent phantom search results
            await db.execute("DELETE FROM canonical_fts WHERE memory_id NOT IN (SELECT id FROM canonical_memories)")
            await db.commit()
        return {"deleted_ids": deleted_ids, "protected_skipped": protected_skipped}

    async def purge_kind_candidates(
        self,
        *,
        kind: str,
        statuses: tuple[str, ...],
        older_than_seconds: float,
        min_confidence_to_keep: float = 0.9,
        min_count_to_keep: int = 5,
        review_statuses: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        await self.initialize()
        now = self._now()
        cutoff = now - max(float(older_than_seconds or 0.0), 0.0)
        if cutoff > now:  # ponytail: NTP backward jump guard
            logger.warning(f"[AstrMai-v2store] NTP backward jump: cutoff={cutoff} > now={now}, clamping")
            cutoff = 0.0
        deleted_ids: list[str] = []
        protected_skipped = 0
        clean_kind = str(kind or "").strip()
        if not statuses or not clean_kind:
            return {"deleted_ids": deleted_ids, "protected_skipped": protected_skipped}
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                f"""
                SELECT id, metadata, confidence
                FROM canonical_memories
                WHERE kind = ?
                  AND status IN ({','.join('?' for _ in statuses)})
                  AND COALESCE(update_time, create_time, 0) <= ?
                """,
                (clean_kind, *statuses, cutoff),
            )
            rows = await cursor.fetchall()
            for memory_id, metadata_raw, confidence_value in rows:
                try:
                    metadata = json.loads(metadata_raw or "{}")
                    if not isinstance(metadata, dict):
                        metadata = {}
                except Exception:
                    metadata = {}
                review_status = str(metadata.get("review_status") or "").strip().lower()
                if review_statuses and review_status not in {str(item).strip().lower() for item in review_statuses if str(item).strip()}:
                    continue
                confidence = float(metadata.get("confidence") or confidence_value or 0.0)
                count = int(metadata.get("count") or 0)
                if confidence >= float(min_confidence_to_keep or 0.9) or count >= int(min_count_to_keep or 5):
                    protected_skipped += 1
                    continue
                deleted_ids.append(str(memory_id))
            for memory_id in deleted_ids:
                await db.execute("DELETE FROM canonical_memories WHERE id = ?", (memory_id,))
            # ponytail: sync FTS to prevent phantom search results
            await db.execute("DELETE FROM canonical_fts WHERE memory_id NOT IN (SELECT id FROM canonical_memories)")
            await db.commit()
        return {"deleted_ids": deleted_ids, "protected_skipped": protected_skipped}

    async def soft_delete(self, memory_id: str, *, reason: str = "") -> int:
        await self.initialize()
        scopes = await self._resolve_session_ids_for_memory_ids([memory_id])
        async with await self._acquire_session_scopes(scopes) as _locks:
            async with connect_aiosqlite(self.db_path) as db:
                cursor = await db.execute(
                    """
                    UPDATE canonical_memories
                    SET status = 'deleted', deleted_reason = ?, update_time = ?
                    WHERE id = ?
                    """,
                    (reason, self._now(), memory_id),
                )
                await self._sync_fts(db, memory_id, delete_only=True)
                await db.commit()
                return cursor.rowcount

    async def restore(self, memory_id: str, *, reason: str = "manual_restore") -> int:
        await self.initialize()
        now = self._now()
        scopes = await self._resolve_session_ids_for_memory_ids([memory_id])
        async with await self._acquire_session_scopes(scopes) as _locks:
            async with connect_aiosqlite(self.db_path) as db:
                cursor = await db.execute(
                    """
                    UPDATE canonical_memories
                    SET status = 'active',
                        deleted_reason = '',
                        decay_score = MAX(COALESCE(decay_score, 1.0), 0.5),
                        update_time = ?,
                        last_access_time = ?
                    WHERE id = ? AND status IN ('stale', 'deleted')
                    """,
                    (now, now, memory_id),
                )
                if cursor.rowcount:
                    await self._sync_fts(db, memory_id)
                await db.commit()
                return cursor.rowcount

    async def mark_stale(self, memory_id: str, *, reason: str = "manual_stale") -> int:
        await self.initialize()
        scopes = await self._resolve_session_ids_for_memory_ids([memory_id])
        async with await self._acquire_session_scopes(scopes) as _locks:
            async with connect_aiosqlite(self.db_path) as db:
                cursor = await db.execute(
                    """
                    UPDATE canonical_memories
                    SET status = 'stale', deleted_reason = ?, update_time = ?
                    WHERE id = ? AND status = 'active'
                    """,
                    (reason, self._now(), memory_id),
                )
                await self._sync_fts(db, memory_id, delete_only=True)
                await db.commit()
                return cursor.rowcount

    async def soft_delete_by_filter(
        self,
        *,
        kind: str = "",
        session_id: str = "",
        persona_id: str = "",
        reason: str = "",
    ) -> list[str]:
        await self.initialize()
        where = ["status IN ('active', 'stale')"]
        params: list[Any] = []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if persona_id:
            where.append("persona_id = ?")
            params.append(persona_id)
        scopes = [session_id or "__global__"]
        async with await self._acquire_session_scopes(scopes) as _locks:
            async with connect_aiosqlite(self.db_path) as db:
                cursor = await db.execute(
                    f"SELECT id FROM canonical_memories WHERE {' AND '.join(where)}",
                    tuple(params),
                )
                ids = [str(row[0]) for row in await cursor.fetchall()]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    await db.execute(
                        f"""
                        UPDATE canonical_memories
                        SET status = 'deleted', deleted_reason = ?, update_time = ?
                        WHERE id IN ({placeholders})
                        """,
                        (reason, self._now(), *ids),
                    )
                    for memory_id in ids:
                        await self._sync_fts(db, memory_id, delete_only=True)
                    await db.commit()
        return ids

    async def soft_delete_low_importance(self, *, threshold: float, reason: str = "low_importance") -> list[str]:
        await self.initialize()
        async with await self._acquire_session_scopes(["__global__"]) as _locks:
            async with connect_aiosqlite(self.db_path) as db:
                cursor = await db.execute(
                    """
                    SELECT id FROM canonical_memories
                    WHERE status IN ('active', 'stale')
                      AND importance < ?
                    """,
                    (float(threshold),),
                )
                ids = [str(row[0]) for row in await cursor.fetchall()]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    await db.execute(
                        f"""
                        UPDATE canonical_memories
                        SET status = 'deleted', deleted_reason = ?, update_time = ?
                        WHERE id IN ({placeholders})
                        """,
                        (reason, self._now(), *ids),
                    )
                    for memory_id in ids:
                        await self._sync_fts(db, memory_id, delete_only=True)
                    await db.commit()
        return ids

    async def mark_merged(self, memory_ids: list[str], *, superseded_by: str) -> int:
        ids = [str(item) for item in memory_ids if str(item).strip()]
        if not ids:
            return 0
        await self.initialize()
        scopes = await self._resolve_session_ids_for_memory_ids(ids + ([superseded_by] if superseded_by else []))
        async with await self._acquire_session_scopes(scopes) as _locks:
            async with connect_aiosqlite(self.db_path) as db:
                count = 0
                for memory_id in ids:
                    cursor = await db.execute(
                        """
                        UPDATE canonical_memories
                        SET status = 'merged', superseded_by = ?, update_time = ?
                        WHERE id = ?
                        """,
                        (superseded_by, self._now(), memory_id),
                    )
                    count += cursor.rowcount
                for memory_id in ids:
                    await self._sync_fts(db, memory_id, delete_only=True)
                await db.commit()
                return count

    async def find_ids_by_source_ref(self, source_ref: str) -> list[str]:
        await self.initialize()
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT id FROM canonical_memories
                WHERE source_ref = ? AND status IN ('active', 'stale')
                """,
                (source_ref,),
            )
            return [str(row[0]) for row in await cursor.fetchall()]

    async def update_content(self, memory_id: str, *, content: str, summary: str = "") -> int:
        await self.initialize()
        now = self._now()
        scopes = await self._resolve_session_ids_for_memory_ids([memory_id])
        async with await self._acquire_session_scopes(scopes) as _locks:
            async with connect_aiosqlite(self.db_path) as db:
                cursor = await db.execute(
                    """
                    UPDATE canonical_memories
                    SET content = ?, summary = ?, update_time = ?, status = 'active',
                        decay_score = MAX(COALESCE(decay_score, 1.0), 0.5),
                        last_access_time = ?
                    WHERE id = ? AND status IN ('active', 'stale')
                    """,
                    (content, summary or str(content or "")[:240], now, now, memory_id),
                )
                await self._sync_fts(db, memory_id)
                await db.commit()
                return cursor.rowcount

    async def update_memory(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        summary: str | None = None,
        tags: list[str] | None = None,
        importance: float | None = None,
        confidence: float | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
        visibility: str | None = None,
        dedup_key: str | None = None,
        source_ref: str | None = None,
    ) -> int:
        await self.initialize()
        payload: dict[str, Any] = {"update_time": self._now()}
        if content is not None:
            payload["content"] = str(content or "")
            payload["summary"] = str(summary or str(content or "")[:240])
        elif summary is not None:
            payload["summary"] = str(summary or "")
        if tags is not None:
            payload["tags"] = self._json_list(tags)
        if importance is not None:
            payload["importance"] = float(importance)
        if confidence is not None:
            payload["confidence"] = float(confidence)
        if status is not None:
            clean_status = str(status or "").strip()
            if clean_status in {
                ACTIVE_STATUS,
                STALE_STATUS,
                DELETED_STATUS,
                MERGED_STATUS,
                DEPRECATED_STATUS,
                REVIEW_PENDING_STATUS,
                REJECTED_STATUS,
            }:
                payload["status"] = clean_status
        if metadata is not None:
            payload["metadata"] = self._json_dict(metadata)
        if visibility is not None and visibility in {"auto_and_tool", "tool_only", "maintenance_only"}:
            payload["visibility"] = visibility
        if dedup_key is not None:
            payload["dedup_key"] = str(dedup_key or "")
        if source_ref is not None:
            payload["source_ref"] = str(source_ref or "")
        if len(payload) <= 1:
            return 0
        columns = list(payload.keys())
        assignments = ", ".join(f"{column} = ?" for column in columns)
        scopes = await self._resolve_session_ids_for_memory_ids([memory_id])
        async with await self._acquire_session_scopes(scopes) as _locks:
            async with connect_aiosqlite(self.db_path) as db:
                cursor = await db.execute(
                    f"UPDATE canonical_memories SET {assignments} WHERE id = ?",
                    (*[payload[column] for column in columns], memory_id),
                )
                await self._sync_fts(db, memory_id)
                await db.commit()
                return cursor.rowcount

    async def list_projectable(self, *, session_id: str = "") -> list[MemoryCandidate]:
        await self.initialize()
        where = ["status = 'active'", "visibility IN ('auto_and_tool', 'tool_only')"]
        params: list[Any] = []
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                f"""
                SELECT id, kind, source, summary, content, session_id, sender_id, persona_id,
                       tags, importance, confidence, status, create_time,
                       update_time, last_access_time, metadata, visibility, access_count, decay_score, superseded_by
                FROM canonical_memories
                WHERE {' AND '.join(where)}
                ORDER BY update_time DESC
                """,
                tuple(params),
            )
            rows = await cursor.fetchall()
        return [self._row_to_candidate(row) for row in rows]

    async def migration_applied(self, version: str) -> bool:
        await self.initialize()
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM memory_v2_migrations WHERE version = ? AND status = 'applied'",
                (version,),
            )
            return await cursor.fetchone() is not None

    async def record_migration(self, version: str, *, status: str, detail: str = "") -> None:
        await self.initialize()
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO memory_v2_migrations(version, backup_dir, status, detail, applied_at)
                VALUES (?, COALESCE((SELECT backup_dir FROM memory_v2_migrations WHERE version = '2'), ''), ?, ?, ?)
                """,
                (version, status, detail, self._now()),
            )
            await db.commit()

    async def migration_report(self) -> dict[str, Any]:
        await self.initialize()
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                "SELECT version, backup_dir, status, detail, applied_at FROM memory_v2_migrations ORDER BY applied_at DESC"
            )
            migrations = [
                {
                    "version": str(row[0] or ""),
                    "backup_dir": str(row[1] or ""),
                    "status": str(row[2] or ""),
                    "detail": str(row[3] or ""),
                    "applied_at": float(row[4] or 0.0),
                }
                for row in await cursor.fetchall()
            ]
            counts: dict[str, int] = {}
            for status in (
                ACTIVE_STATUS,
                STALE_STATUS,
                DELETED_STATUS,
                MERGED_STATUS,
                DEPRECATED_STATUS,
                REVIEW_PENDING_STATUS,
                REJECTED_STATUS,
            ):
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM canonical_memories WHERE status = ?",
                    (status,),
                )
                row = await cursor.fetchone()
                counts[status] = int(row[0] if row else 0)
            legacy_counts: dict[str, int] = {}
            for table in ("documents", "MemoryEvent", "Jargon"):
                try:
                    cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")
                    row = await cursor.fetchone()
                    legacy_counts[table] = int(row[0] if row else 0)
                except Exception:
                    legacy_counts[table] = 0
        return {
            "schema_version": 2,
            "backup_dir": migrations[-1]["backup_dir"] if migrations else "",
            "migrations": migrations,
            "canonical_counts": counts,
            "legacy_counts": legacy_counts,
            "persona_cache_exists": (self.data_path / "persona_cache.json").exists(),
        }

    async def import_legacy_documents(self, *, limit: int = 1000) -> int:
        version = "2_legacy_documents_import"
        if await self.migration_applied(version):
            return 0
        await self.initialize()
        imported = 0
        source_path = str(self.legacy_db_path or self.db_path or "").strip()
        if not source_path or not os.path.isfile(source_path):
            await self.record_migration(version, status="failed", detail="legacy documents source unavailable")
            return 0
        try:
            async with connect_aiosqlite(source_path) as db:
                cursor = await db.execute("PRAGMA table_info(documents)")
                columns = {str(row[1]) for row in await cursor.fetchall()}
                text_col = "page_content" if "page_content" in columns else ("content" if "content" in columns else "text")
                if text_col not in columns or "metadata" not in columns:
                    await self.record_migration(version, status="failed", detail="documents table unavailable")
                    return 0
                cursor = await db.execute(
                    f"SELECT id, {text_col}, metadata FROM documents ORDER BY id DESC LIMIT ?",
                    (int(limit or 1000),),
                )
                rows = await cursor.fetchall()
            for doc_id, text, metadata_raw in rows:
                content = str(text or "").strip()
                if not content:
                    continue
                try:
                    metadata = json.loads(metadata_raw or "{}") if isinstance(metadata_raw, str) else {}
                    if not isinstance(metadata, dict):
                        metadata = {}
                except Exception:
                    metadata = {}
                canonical_id = str(metadata.get("canonical_id") or "")
                if canonical_id:
                    continue
                session_id = str(metadata.get("session_id") or "")
                persona_id = str(metadata.get("persona_id") or "")
                kind = str(metadata.get("kind") or ("persona_lore" if session_id == "__self_lore__" else "memory"))
                request = MemoryWriteRequest(
                    source=str(metadata.get("source") or "legacy_documents"),
                    kind=kind,
                    session_id=session_id,
                    persona_id=persona_id,
                    content=content,
                    summary=content[:240],
                    importance=float(metadata.get("importance") or 0.5),
                    confidence=0.65,
                    metadata={"legacy_doc_id": doc_id, **metadata},
                    dedup_key=f"legacy_documents:{doc_id}",
                    source_ref=f"documents:{doc_id}",
                )
                await self.upsert(request)
                imported += 1
            await self.record_migration(version, status="applied", detail=f"imported={imported}")
        except Exception as exc:
            await self.record_migration(version, status="failed", detail=str(exc)[:500])
            logger.warning(f"[MemoryV2] legacy documents import degraded: {exc}")
        return imported

    async def import_persona_cache(self) -> int:
        version = "2_persona_cache_import"
        if await self.migration_applied(version):
            return 0
        cache_path = self.data_path / "persona_cache.json"
        if not cache_path.exists():
            await self.record_migration(version, status="applied", detail="persona cache unavailable")
            return 0
        imported = 0
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8") or "{}")
            if not isinstance(data, dict):
                data = {}
            for persona_id, payload in data.items():
                if not isinstance(payload, dict):
                    continue
                parts = []
                for key in ("first_person_rewrite", "summary", "style"):
                    value = str(payload.get(key) or "").strip()
                    if value:
                        parts.append(value)
                shards = payload.get("shards")
                if isinstance(shards, dict):
                    parts.extend(str(value).strip() for value in shards.values() if str(value).strip())
                content = "\n".join(dict.fromkeys(parts)).strip()
                if not content:
                    continue
                await self.upsert(
                    MemoryWriteRequest(
                        source="persona_cache",
                        kind="persona_lore",
                        session_id="__self_lore__",
                        persona_id=str(persona_id),
                        content=content,
                        summary=content[:240],
                        importance=1.0,
                        confidence=0.75,
                        dedup_key=f"persona_cache:{persona_id}",
                        source_ref=f"persona_cache:{persona_id}",
                    )
                )
                imported += 1
            await self.record_migration(version, status="applied", detail=f"imported={imported}")
        except Exception as exc:
            await self.record_migration(version, status="failed", detail=str(exc)[:500])
            logger.warning(f"[MemoryV2] persona cache import degraded: {exc}")
        return imported
