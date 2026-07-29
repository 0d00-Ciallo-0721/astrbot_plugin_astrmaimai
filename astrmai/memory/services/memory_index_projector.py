from __future__ import annotations

import asyncio
import json

from astrbot.api import logger

from ..contracts.memory_query import MemoryWriteRequest


class MemoryIndexProjector:
    """Projects canonical SQL memory into the existing hybrid index."""

    def __init__(self, engine):
        self.engine = engine
        self._pending_projection_ids: set[str] = set()
        self._pending_projection_reasons: dict[str, str] = {}
        self._pending_projection_scheduled: dict[str, bool] = {}
        self._retry_task: asyncio.Task | None = None
        self._retry_stop = asyncio.Event()

    def _config_value(self, name: str, default):
        config = getattr(self.engine, "config", None)
        timing_config = getattr(config, "timing", None)
        if timing_config is not None and hasattr(timing_config, name):
            return getattr(timing_config, name)
        memory_config = getattr(config, "memory", None)
        return getattr(memory_config, name, default)

    async def _mark_pending_persisted(self, memory_id: str, reason: str) -> bool:
        self._mark_pending(memory_id, reason)
        store = getattr(self.engine, "v2_store", None)
        schedule = getattr(store, "schedule_projection_retry", None)
        if not callable(schedule):
            return False
        scheduled = bool(
            await schedule(
                memory_id,
                reason,
                base_delay_sec=float(self._config_value("projection_retry_base_delay_sec", 30.0) or 30.0),
                max_delay_sec=float(self._config_value("projection_retry_max_delay_sec", 900.0) or 900.0),
            )
        )
        self._pending_projection_scheduled[memory_id] = scheduled
        return scheduled

    async def _clear_pending_persisted(self, memory_id: str) -> None:
        self._clear_pending(memory_id)
        self._pending_projection_scheduled.pop(memory_id, None)
        store = getattr(self.engine, "v2_store", None)
        complete = getattr(store, "complete_projection_retry", None)
        if callable(complete):
            await complete(memory_id)

    def _mark_pending(self, memory_id: str, reason: str) -> None:
        if not memory_id:
            return
        self._pending_projection_ids.add(memory_id)
        self._pending_projection_reasons[memory_id] = str(reason or "unknown")

    def _clear_pending(self, memory_id: str) -> None:
        self._pending_projection_ids.discard(memory_id)
        self._pending_projection_reasons.pop(memory_id, None)

    def pending_reason(self, memory_id: str) -> str:
        return str(self._pending_projection_reasons.get(str(memory_id or ""), "") or "")

    def retry_scheduled(self, memory_id: str) -> bool:
        return bool(self._pending_projection_scheduled.get(str(memory_id or ""), False))

    def _documents_db_path(self) -> str | None:
        return getattr(self.engine, "db_path", None)

    async def project(self, memory_id: str, request: MemoryWriteRequest | None = None) -> bool:
        if not memory_id:
            return False
        if not getattr(self.engine, "retriever", None):
            scheduled = await self._mark_pending_persisted(memory_id, "retriever_not_ready")
            logger.info(
                f"[MemoryIndexProjector] projection deferred memory_id={memory_id} "
                f"reason=retriever_not_ready repair_scheduled={str(scheduled).lower()}"
            )
            return False
        try:
            if request is None:
                candidate = await self.engine.v2_store.get_by_id(memory_id, allow_stale=False)
                if not candidate:
                    await self.delete_projection(memory_id)
                    await self._clear_pending_persisted(memory_id)
                    return True
                request = MemoryWriteRequest(
                    source=candidate.source,
                    kind=candidate.kind,
                    session_id=candidate.session_id,
                    persona_id=candidate.persona_id,
                    content=candidate.content,
                    summary=candidate.summary,
                    tags=candidate.tags,
                    importance=candidate.importance,
                    confidence=candidate.confidence,
                    metadata=candidate.metadata,
                    dedup_key=str(candidate.metadata.get("dedup_key") or ""),
                    visibility=candidate.visibility,
                )
            await self.delete_projection(memory_id)
            metadata = self.engine._build_memory_metadata(
                session_id=request.session_id,
                persona_id=request.persona_id or None,
                importance=request.importance,
                canonical_id=memory_id,
                source=request.source,
                kind=request.kind,
                status="active",
                visibility=request.visibility,
                source_ref=request.source_ref,
            )
            metadata.update(dict(request.metadata or {}))
            await self.engine.retriever.add_memory(request.content, metadata)
            await self._clear_pending_persisted(memory_id)
            return True
        except Exception as exc:
            reason = f"projection_error:{type(exc).__name__}"
            scheduled = await self._mark_pending_persisted(memory_id, reason)
            logger.warning(
                f"[MemoryIndexProjector] projection degraded memory_id={memory_id} "
                f"reason={reason} repair_scheduled={str(scheduled).lower()}"
            )
            return False

    async def start(self) -> None:
        if self._retry_task is not None and not self._retry_task.done():
            return
        self._retry_stop = asyncio.Event()
        self._retry_task = asyncio.create_task(
            self._retry_loop(),
            name="astrmai-memory-projection-retry",
        )

    async def stop(self) -> None:
        self._retry_stop.set()
        task = self._retry_task
        self._retry_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _retry_loop(self) -> None:
        interval = max(
            5.0,
            float(self._config_value("projection_retry_interval_sec", 60.0) or 60.0),
        )
        while not self._retry_stop.is_set():
            try:
                await self.retry_due(limit=int(self._config_value("projection_retry_batch_size", 20) or 20))
                await asyncio.wait_for(self._retry_stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[MemoryIndexProjector] retry worker degraded: {exc}")
                await asyncio.sleep(min(interval, 30.0))

    async def retry_due(self, *, limit: int = 20) -> dict:
        store = getattr(self.engine, "v2_store", None)
        list_due = getattr(store, "list_due_projection_retries", None)
        if not callable(list_due):
            return {"attempted": 0, "projected": 0, "failed": 0}
        rows = await list_due(limit=max(1, int(limit or 20)))
        result = {"attempted": 0, "projected": 0, "failed": 0}
        for row in rows:
            memory_id = str((row or {}).get("memory_id") or "")
            if not memory_id:
                continue
            result["attempted"] += 1
            if await self.project(memory_id):
                result["projected"] += 1
            else:
                result["failed"] += 1
        if result["attempted"]:
            logger.info(
                "[MemoryIndexProjector] retry batch "
                f"attempted={result['attempted']} projected={result['projected']} failed={result['failed']}"
            )
        return result

    async def delete_projection(self, memory_id: str) -> int:
        return await self.cleanup_deleted([memory_id])

    async def cleanup_deleted(self, memory_ids: list[str]) -> int:
        if not memory_ids or not hasattr(self.engine, "_execute_documents_write"):
            return 0
        deleted = 0
        faiss_db = getattr(self.engine, "faiss_db", None)
        faiss_delete = getattr(faiss_db, "delete", None) if faiss_db is not None else None
        for memory_id in memory_ids:
            try:
                rows = await self.engine._run_documents_query(
                    "SELECT id, doc_id FROM documents WHERE json_extract(metadata, '$.canonical_id') = ?",
                    (memory_id,),
                    db_path=self._documents_db_path(),
                )
                int_ids = [row[0] for row in rows if row]
                doc_keys = [str(row[1]) for row in rows if row and len(row) > 1 and row[1]]
                # OPT-05/ML-04: 必须走 FaissVecDB.delete 才会同步删除 embedding——
                # 旧实现只删 documents 行与 FTS，嵌入向量成为幽灵、top-k 名额被
                # 已删条目挤占且索引文件只增不减。顺序关键：faiss.delete 内部按
                # doc_id 反查 int id，必须先于任何 documents 行删除执行。
                if callable(faiss_delete):
                    for doc_key in doc_keys:
                        try:
                            await faiss_delete(doc_key)
                            deleted += 1
                        except Exception:
                            logger.debug(
                                f"[MemoryIndexProjector] faiss delete degraded doc_id={doc_key}",
                                exc_info=True,
                            )
                removed_rows = await self.engine._execute_documents_write(
                    "DELETE FROM documents WHERE json_extract(metadata, '$.canonical_id') = ?",
                    (memory_id,),
                    db_path=self._documents_db_path(),
                )
                if not callable(faiss_delete):
                    deleted += removed_rows
                await self._delete_fts_rows(int_ids)
                await self._clear_pending_persisted(memory_id)
            except Exception as exc:
                logger.warning(f"[MemoryIndexProjector] cleanup degraded for {memory_id}: {exc}")
        return deleted

    async def rebuild_all(self) -> int:
        if not await self.engine._ensure_faiss_initialized():
            return 0
        await self._clear_projected_documents()
        count = 0
        for candidate in await self.engine.v2_store.list_projectable():
            await self.project(candidate.id)
            count += 1
        return count

    async def rebuild_session(self, session_id: str) -> int:
        if not await self.engine._ensure_faiss_initialized():
            return 0
        await self._clear_projected_documents(session_id=session_id)
        count = 0
        for candidate in await self.engine.v2_store.list_projectable(session_id=session_id):
            await self.project(candidate.id)
            count += 1
        return count

    async def check_consistency(self) -> dict:
        persisted_pending: dict[str, str] = {}
        snapshot = getattr(self.engine.v2_store, "projection_retry_snapshot", None)
        if callable(snapshot):
            try:
                persisted_pending = await snapshot()
            except Exception as exc:
                logger.warning(f"[MemoryIndexProjector] projection outbox snapshot degraded: {exc}")
        pending_ids = set(self._pending_projection_ids) | set(persisted_pending)
        pending_reasons = dict(persisted_pending)
        pending_reasons.update(self._pending_projection_reasons)
        report = {
            "missing_projection_ids": [],
            "orphan_projection_ids": [],
            "inactive_projection_ids": [],
            "duplicate_projection_ids": [],
            "projection_count": 0,
            "canonical_projectable_count": 0,
            "pending_projection_count": len(pending_ids),
            "pending_projection_reasons": pending_reasons,
        }
        try:
            projectable = await self.engine.v2_store.list_projectable()
            projectable_ids = {item.id for item in projectable}
            report["canonical_projectable_count"] = len(projectable_ids)
            projection_rows = await self._projection_rows()
            report["projection_count"] = len(projection_rows)
            by_canonical: dict[str, list[int]] = {}
            for doc_id, canonical_id in projection_rows:
                by_canonical.setdefault(canonical_id, []).append(doc_id)
            projected_ids = set(by_canonical)
            report["missing_projection_ids"] = sorted(projectable_ids - projected_ids)
            if pending_ids:
                report["missing_projection_ids"] = sorted(
                    set(report["missing_projection_ids"]) | (pending_ids & projectable_ids)
                )
            for canonical_id, doc_ids in by_canonical.items():
                if len(doc_ids) > 1:
                    report["duplicate_projection_ids"].append(canonical_id)
                candidate = await self.engine.v2_store.get_canonical(canonical_id, include_inactive=True)
                if not candidate:
                    report["orphan_projection_ids"].append(canonical_id)
                    continue
                if candidate.status != "active" or candidate.visibility not in {"auto_and_tool", "tool_only"}:
                    report["inactive_projection_ids"].append(canonical_id)
        except Exception as exc:
            report["error"] = str(exc)
        return report

    async def repair_consistency(self, report: dict | None = None) -> dict:
        report = report or await self.check_consistency()
        repaired = {
            "rebuilt_missing": 0,
            "deleted_orphan": 0,
            "deleted_inactive": 0,
            "deduplicated": 0,
            "remaining_pending": len(self._pending_projection_ids),
            "remaining_pending_reasons": dict(self._pending_projection_reasons),
        }
        for memory_id in report.get("missing_projection_ids", []) or []:
            if await self.project(str(memory_id)):
                repaired["rebuilt_missing"] += 1
        removable = set(report.get("orphan_projection_ids", []) or []) | set(report.get("inactive_projection_ids", []) or [])
        if removable:
            repaired["deleted_orphan"] = await self.cleanup_deleted(sorted(removable & set(report.get("orphan_projection_ids", []) or [])))
            repaired["deleted_inactive"] = await self.cleanup_deleted(sorted(removable & set(report.get("inactive_projection_ids", []) or [])))
        for memory_id in report.get("duplicate_projection_ids", []) or []:
            if await self.project(str(memory_id)):
                repaired["deduplicated"] += 1
        repaired["remaining_pending"] = len(self._pending_projection_ids)
        repaired["remaining_pending_reasons"] = dict(self._pending_projection_reasons)
        return repaired

    async def _clear_projected_documents(self, *, session_id: str = "") -> int:
        if not hasattr(self.engine, "_execute_documents_write"):
            return 0
        if session_id:
            rows = await self.engine._run_documents_query(
                """
                SELECT id FROM documents
                WHERE json_extract(metadata, '$.canonical_id') IS NOT NULL
                  AND json_extract(metadata, '$.session_id') = ?
                """,
                (session_id,),
                db_path=self._documents_db_path(),
            )
            deleted = await self.engine._execute_documents_write(
                """
                DELETE FROM documents
                WHERE json_extract(metadata, '$.canonical_id') IS NOT NULL
                  AND json_extract(metadata, '$.session_id') = ?
                """,
                (session_id,),
                db_path=self._documents_db_path(),
            )
            await self._delete_fts_rows([row[0] for row in rows if row])
            return deleted
        rows = await self.engine._run_documents_query(
            "SELECT id FROM documents WHERE json_extract(metadata, '$.canonical_id') IS NOT NULL",
            db_path=self._documents_db_path(),
        )
        deleted = await self.engine._execute_documents_write(
            "DELETE FROM documents WHERE json_extract(metadata, '$.canonical_id') IS NOT NULL",
            db_path=self._documents_db_path(),
        )
        await self._delete_fts_rows([row[0] for row in rows if row])
        return deleted

    async def _projection_rows(self) -> list[tuple[int, str]]:
        if not hasattr(self.engine, "_run_documents_query"):
            return []
        try:
            rows = await self.engine._run_documents_query(
                "SELECT id, metadata FROM documents WHERE json_extract(metadata, '$.canonical_id') IS NOT NULL",
                db_path=self._documents_db_path(),
            )
        except Exception as exc:
            logger.warning(f"[MemoryIndexProjector] consistency scan degraded: {exc}")
            raise
        result: list[tuple[int, str]] = []
        for row in rows:
            try:
                doc_id = int(row[0])
                metadata = row[1] or "{}"
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)
                canonical_id = str((metadata or {}).get("canonical_id") or "")
                if canonical_id:
                    result.append((doc_id, canonical_id))
            except Exception:
                continue
        return result

    async def _delete_fts_rows(self, doc_ids: list[int]) -> int:
        ids = [item for item in doc_ids if item is not None]
        if not ids or not hasattr(self.engine, "_execute_documents_write"):
            return 0
        deleted = 0
        for doc_id in ids:
            try:
                deleted += await self.engine._execute_documents_write(
                    "DELETE FROM memories_fts WHERE doc_id = ?",
                    (doc_id,),
                    db_path=self._documents_db_path(),
                )
            except Exception as exc:
                logger.warning(f"[MemoryIndexProjector] fts cleanup degraded for {doc_id}: {exc}")
        return deleted


__all__ = ["MemoryIndexProjector"]
