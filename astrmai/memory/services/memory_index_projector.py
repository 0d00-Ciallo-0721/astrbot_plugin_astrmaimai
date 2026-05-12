from __future__ import annotations

import json

from astrbot.api import logger

from ..contracts.memory_query import MemoryWriteRequest


class MemoryIndexProjector:
    """Projects canonical SQL memory into the existing hybrid index."""

    def __init__(self, engine):
        self.engine = engine

    async def project(self, memory_id: str, request: MemoryWriteRequest | None = None) -> None:
        if not memory_id or not getattr(self.engine, "retriever", None):
            return
        try:
            if request is None:
                candidate = await self.engine.v2_store.get_by_id(memory_id, allow_stale=False)
                if not candidate:
                    await self.delete_projection(memory_id)
                    return
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
        except Exception as exc:
            logger.debug(f"[MemoryIndexProjector] projection degraded: {exc}")

    async def delete_projection(self, memory_id: str) -> int:
        return await self.cleanup_deleted([memory_id])

    async def cleanup_deleted(self, memory_ids: list[str]) -> int:
        if not memory_ids or not hasattr(self.engine, "_execute_documents_write"):
            return 0
        deleted = 0
        for memory_id in memory_ids:
            try:
                rows = await self.engine._run_documents_query(
                    "SELECT id FROM documents WHERE json_extract(metadata, '$.canonical_id') = ?",
                    (memory_id,),
                )
                doc_ids = [row[0] for row in rows if row]
                deleted += await self.engine._execute_documents_write(
                    "DELETE FROM documents WHERE json_extract(metadata, '$.canonical_id') = ?",
                    (memory_id,),
                )
                await self._delete_fts_rows(doc_ids)
            except Exception as exc:
                logger.debug(f"[MemoryIndexProjector] cleanup degraded for {memory_id}: {exc}")
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
        report = {
            "missing_projection_ids": [],
            "orphan_projection_ids": [],
            "inactive_projection_ids": [],
            "duplicate_projection_ids": [],
            "projection_count": 0,
            "canonical_projectable_count": 0,
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
        }
        for memory_id in report.get("missing_projection_ids", []) or []:
            await self.project(str(memory_id))
            repaired["rebuilt_missing"] += 1
        removable = set(report.get("orphan_projection_ids", []) or []) | set(report.get("inactive_projection_ids", []) or [])
        if removable:
            repaired["deleted_orphan"] = await self.cleanup_deleted(sorted(removable & set(report.get("orphan_projection_ids", []) or [])))
            repaired["deleted_inactive"] = await self.cleanup_deleted(sorted(removable & set(report.get("inactive_projection_ids", []) or [])))
        for memory_id in report.get("duplicate_projection_ids", []) or []:
            await self.project(str(memory_id))
            repaired["deduplicated"] += 1
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
            )
            deleted = await self.engine._execute_documents_write(
                """
                DELETE FROM documents
                WHERE json_extract(metadata, '$.canonical_id') IS NOT NULL
                  AND json_extract(metadata, '$.session_id') = ?
                """,
                (session_id,),
            )
            await self._delete_fts_rows([row[0] for row in rows if row])
            return deleted
        rows = await self.engine._run_documents_query(
            "SELECT id FROM documents WHERE json_extract(metadata, '$.canonical_id') IS NOT NULL"
        )
        deleted = await self.engine._execute_documents_write(
            "DELETE FROM documents WHERE json_extract(metadata, '$.canonical_id') IS NOT NULL"
        )
        await self._delete_fts_rows([row[0] for row in rows if row])
        return deleted

    async def _projection_rows(self) -> list[tuple[int, str]]:
        if not hasattr(self.engine, "_run_documents_query"):
            return []
        try:
            rows = await self.engine._run_documents_query(
                "SELECT id, metadata FROM documents WHERE json_extract(metadata, '$.canonical_id') IS NOT NULL"
            )
        except Exception as exc:
            logger.debug(f"[MemoryIndexProjector] consistency scan degraded: {exc}")
            return []
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
                )
            except Exception as exc:
                logger.debug(f"[MemoryIndexProjector] fts cleanup degraded for {doc_id}: {exc}")
        return deleted


__all__ = ["MemoryIndexProjector"]
