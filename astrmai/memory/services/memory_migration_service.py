from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from astrbot.api import logger

from ..contracts.memory_query import MemoryWriteRequest
from .memory_index_projector import MemoryIndexProjector
from .v2_store import MemoryV2Store


class MemoryMigrationService:
    """Final-state migration facade for legacy memory sources.

    Legacy tables/files remain readable sources, but this service imports their
    useful records into canonical SQL memory and verifies the projection layer.
    """

    def __init__(
        self,
        store: MemoryV2Store,
        *,
        index_projector: MemoryIndexProjector | None = None,
        engine: Any | None = None,
    ):
        self.store = store
        self.index_projector = index_projector
        self.engine = engine
        self._latest_report: dict[str, Any] = {}

    async def dry_run(self, import_sources: list[str] | None = None) -> dict[str, Any]:
        await self.store.initialize()
        sources = self._sources(import_sources)
        report = {
            "mode": "dry_run",
            "generated_at": time.time(),
            "sources": {},
            "totals": {"importable": 0, "duplicates": 0, "skipped": 0},
            "kind_mapping": {
                "documents": "metadata.kind or memory",
                "MemoryEvent": "memory_kind or event",
                "persona_cache": "persona_lore",
                "Jargon": "jargon",
                "ExpressionPattern": "expression_pattern",
            },
        }
        if "documents" in sources:
            report["sources"]["documents"] = await self._scan_documents()
        if "memory_events" in sources:
            report["sources"]["MemoryEvent"] = await self._scan_memory_events()
        if "persona_cache" in sources:
            report["sources"]["persona_cache"] = await self._scan_persona_cache()
        if "jargon" in sources:
            report["sources"]["Jargon"] = await self._scan_jargons()
        if "expression_patterns" in sources:
            report["sources"]["ExpressionPattern"] = await self._scan_expression_patterns()
        for item in report["sources"].values():
            report["totals"]["importable"] += int(item.get("importable", 0) or 0)
            report["totals"]["duplicates"] += int(item.get("duplicates", 0) or 0)
            report["totals"]["skipped"] += int(item.get("skipped", 0) or 0)
        self._latest_report = report
        return report

    async def execute(self, import_sources: list[str] | None = None) -> dict[str, Any]:
        await self.store.initialize()
        sources = self._sources(import_sources)
        report = {
            "mode": "execute",
            "started_at": time.time(),
            "imported": {},
            "errors": [],
        }
        try:
            if "documents" in sources:
                report["imported"]["documents"] = await self.store.import_legacy_documents()
            if "persona_cache" in sources:
                report["imported"]["persona_cache"] = await self.store.import_persona_cache()
            if "memory_events" in sources:
                report["imported"]["MemoryEvent"] = await self._import_memory_events()
            if "jargon" in sources:
                report["imported"]["Jargon"] = await self._import_jargons()
            if "expression_patterns" in sources:
                report["imported"]["ExpressionPattern"] = await self._import_expression_patterns()
            if self.index_projector:
                report["rebuilt_projection"] = await self.index_projector.rebuild_all()
            report["verification"] = await self.verify()
            await self.store.record_migration("2_final_execute", status="applied", detail=json.dumps(report["imported"]))
        except Exception as exc:
            report["errors"].append(str(exc))
            await self.store.record_migration("2_final_execute", status="failed", detail=str(exc)[:500])
            logger.warning(f"[MemoryMigrationService] execute degraded: {exc}")
        report["finished_at"] = time.time()
        self._latest_report = report
        return report

    async def verify(self) -> dict[str, Any]:
        await self.store.initialize()
        report = {
            "mode": "verify",
            "generated_at": time.time(),
            "migration": await self.store.migration_report(),
            "index": {},
            "legacy": {},
            "jargon": {},
            "expression_pattern": {},
        }
        if self.index_projector:
            report["index"] = await self.index_projector.check_consistency()
        report["legacy"]["unmapped_memory_events"] = await self._count_unmapped_memory_events()
        report["legacy"]["unmapped_jargons"] = await self._count_unmapped_jargons()
        report["legacy"]["unmapped_expression_patterns"] = await self._count_unmapped_expression_patterns()
        report["jargon"] = {
            "active_missing_projection": await self._count_indexed_jargon_subset(report.get("index", {}), "missing_projection_ids"),
            "orphan_projection": await self._count_indexed_jargon_subset(report.get("index", {}), "orphan_projection_ids"),
            "inactive_projection": await self._count_indexed_jargon_subset(report.get("index", {}), "inactive_projection_ids"),
            "missing_meaning": await self._count_jargon_missing_metadata("meaning"),
            "missing_review_status": await self._count_jargon_missing_metadata("review_status"),
            "active_non_approved_metadata": await self._count_jargon_active_non_approved_metadata(),
            "pending_human_without_review_suggestion": await self._count_jargon_pending_human_missing_suggestion(),
            "visibility_anomalies": await self._count_jargon_visibility_anomalies(),
        }
        report["expression_pattern"] = {
            "active_missing_projection": await self._count_indexed_kind_subset(report.get("index", {}), "missing_projection_ids", "expression_pattern"),
            "orphan_projection": await self._count_indexed_kind_subset(report.get("index", {}), "orphan_projection_ids", "expression_pattern"),
            "inactive_projection": await self._count_indexed_kind_subset(report.get("index", {}), "inactive_projection_ids", "expression_pattern"),
            "missing_situation": await self._count_expression_missing_metadata("situation"),
            "missing_review_status": await self._count_expression_missing_metadata("review_status"),
            "visibility_anomalies": await self._count_expression_visibility_anomalies(),
        }
        self._latest_report = report
        return report

    async def repair(self, report: dict | None = None) -> dict[str, Any]:
        repaired = {
            "mode": "repair",
            "started_at": time.time(),
            "index": {},
            "legacy": {},
            "errors": [],
        }
        try:
            if self.index_projector:
                index_report = (report or {}).get("index") if isinstance(report, dict) else None
                repaired["index"] = await self.index_projector.repair_consistency(index_report)
            repaired["legacy"]["note"] = "legacy rows are readonly; canonical mapping is preserved through source_ref"
            repaired["jargon"] = {
                "filled_review_status": await self._repair_jargon_review_status(),
            }
            repaired["expression_pattern"] = {
                "filled_review_status": await self._repair_expression_review_status(),
            }
        except Exception as exc:
            repaired["errors"].append(str(exc))
        repaired["finished_at"] = time.time()
        self._latest_report = repaired
        return repaired

    async def latest_report(self) -> dict[str, Any]:
        if self._latest_report:
            return self._latest_report
        return await self.verify()

    @staticmethod
    def _sources(import_sources: list[str] | None) -> set[str]:
        values = {str(item).strip() for item in import_sources or [] if str(item).strip()}
        return values or {"documents", "memory_events", "persona_cache", "jargon", "expression_patterns"}

    async def _scan_documents(self) -> dict[str, Any]:
        result = {"total": 0, "importable": 0, "duplicates": 0, "skipped": 0, "skip_reasons": {}}
        try:
            async with aiosqlite.connect(self.store.db_path) as db:
                cursor = await db.execute("PRAGMA table_info(documents)")
                columns = {str(row[1]) for row in await cursor.fetchall()}
                text_col = "page_content" if "page_content" in columns else ("content" if "content" in columns else "text")
                if text_col not in columns:
                    result["skip_reasons"]["missing_text_column"] = 1
                    return result
                cursor = await db.execute(f"SELECT id, {text_col}, metadata FROM documents")
                rows = await cursor.fetchall()
            result["total"] = len(rows)
            for doc_id, text, metadata_raw in rows:
                content = str(text or "").strip()
                if not content:
                    result["skipped"] += 1
                    result["skip_reasons"]["empty_content"] = result["skip_reasons"].get("empty_content", 0) + 1
                    continue
                metadata = self._json_dict(metadata_raw)
                if metadata.get("canonical_id"):
                    result["duplicates"] += 1
                    continue
                if await self.store.find_ids_by_source_ref(f"documents:{doc_id}"):
                    result["duplicates"] += 1
                    continue
                result["importable"] += 1
        except Exception as exc:
            result["skip_reasons"]["scan_error"] = str(exc)
        return result

    async def _scan_memory_events(self) -> dict[str, Any]:
        result = {"total": 0, "importable": 0, "duplicates": 0, "skipped": 0, "skip_reasons": {}}
        try:
            async with aiosqlite.connect(self.store.db_path) as db:
                cursor = await db.execute("PRAGMA table_info(MemoryEvent)")
                columns = {str(row[1]) for row in await cursor.fetchall()}
                if not columns:
                    return result
                id_col = "event_id" if "event_id" in columns else "id"
                text_col = "narrative" if "narrative" in columns else ("reflection" if "reflection" in columns else "")
                if not text_col:
                    result["skip_reasons"]["missing_content_column"] = 1
                    return result
                cursor = await db.execute(f"SELECT id, {id_col}, {text_col} FROM MemoryEvent")
                rows = await cursor.fetchall()
            result["total"] = len(rows)
            for row_id, event_id, text in rows:
                content = str(text or "").strip()
                if not content:
                    result["skipped"] += 1
                    result["skip_reasons"]["empty_content"] = result["skip_reasons"].get("empty_content", 0) + 1
                    continue
                source_ref = f"MemoryEvent:{event_id or row_id}"
                if await self.store.find_ids_by_source_ref(source_ref):
                    result["duplicates"] += 1
                    continue
                result["importable"] += 1
        except Exception as exc:
            result["skip_reasons"]["scan_error"] = str(exc)
        return result

    async def _scan_persona_cache(self) -> dict[str, Any]:
        result = {"total": 0, "importable": 0, "duplicates": 0, "skipped": 0, "skip_reasons": {}}
        cache_path = Path(self.store.data_path) / "persona_cache.json"
        if not cache_path.exists():
            result["skip_reasons"]["missing_file"] = 1
            return result
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8") or "{}")
            if not isinstance(data, dict):
                result["skip_reasons"]["invalid_json_shape"] = 1
                return result
            result["total"] = len(data)
            for persona_id, payload in data.items():
                if not isinstance(payload, dict):
                    result["skipped"] += 1
                    result["skip_reasons"]["invalid_payload"] = result["skip_reasons"].get("invalid_payload", 0) + 1
                    continue
                if await self.store.find_ids_by_source_ref(f"persona_cache:{persona_id}"):
                    result["duplicates"] += 1
                    continue
                content = "\n".join(str(payload.get(key) or "").strip() for key in ("first_person_rewrite", "summary", "style")).strip()
                result["importable" if content else "skipped"] += 1
                if not content:
                    result["skip_reasons"]["empty_content"] = result["skip_reasons"].get("empty_content", 0) + 1
        except Exception as exc:
            result["skip_reasons"]["scan_error"] = str(exc)
        return result

    async def _scan_jargons(self) -> dict[str, Any]:
        result = {"total": 0, "importable": 0, "duplicates": 0, "skipped": 0, "skip_reasons": {}}
        try:
            async with aiosqlite.connect(self.store.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("PRAGMA table_info(Jargon)")
                columns = {str(row[1]) for row in await cursor.fetchall()}
                if not columns:
                    return result
                cursor = await db.execute("SELECT * FROM Jargon ORDER BY id DESC")
                rows = await cursor.fetchall()
            result["total"] = len(rows)
            for row in rows:
                item = dict(row)
                content = str(item.get("content") or "").strip()
                if not content:
                    result["skipped"] += 1
                    result["skip_reasons"]["empty_content"] = result["skip_reasons"].get("empty_content", 0) + 1
                    continue
                source_ref = f"Jargon:{item.get('id')}"
                if await self.store.find_ids_by_source_ref(source_ref):
                    result["duplicates"] += 1
                    continue
                result["importable"] += 1
        except Exception as exc:
            result["skip_reasons"]["scan_error"] = str(exc)
        return result

    async def _scan_expression_patterns(self) -> dict[str, Any]:
        result = {"total": 0, "importable": 0, "duplicates": 0, "skipped": 0, "skip_reasons": {}}
        try:
            async with aiosqlite.connect(self.store.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("PRAGMA table_info(ExpressionPattern)")
                columns = {str(row[1]) for row in await cursor.fetchall()}
                if not columns:
                    return result
                cursor = await db.execute("SELECT * FROM ExpressionPattern ORDER BY id DESC")
                rows = await cursor.fetchall()
            result["total"] = len(rows)
            for row in rows:
                item = dict(row)
                expression = str(item.get("expression") or "").strip()
                situation = str(item.get("situation") or "").strip()
                if not expression or not situation:
                    result["skipped"] += 1
                    result["skip_reasons"]["empty_expression_or_situation"] = result["skip_reasons"].get("empty_expression_or_situation", 0) + 1
                    continue
                source_ref = f"ExpressionPattern:{item.get('id')}"
                if await self.store.find_ids_by_source_ref(source_ref):
                    result["duplicates"] += 1
                    continue
                result["importable"] += 1
        except Exception as exc:
            result["skip_reasons"]["scan_error"] = str(exc)
        return result

    async def _import_memory_events(self) -> int:
        if self.engine and hasattr(self.engine, "import_legacy_memory_events"):
            return await self.engine.import_legacy_memory_events()
        version = "2_memory_event_import"
        if await self.store.migration_applied(version):
            return 0
        imported = 0
        try:
            async with aiosqlite.connect(self.store.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("PRAGMA table_info(MemoryEvent)")
                columns = {str(row[1]) for row in await cursor.fetchall()}
                if not columns:
                    await self.store.record_migration(version, status="applied", detail="MemoryEvent table unavailable")
                    return 0
                cursor = await db.execute("SELECT * FROM MemoryEvent ORDER BY id DESC LIMIT 1000")
                rows = await cursor.fetchall()
            for row in rows:
                item = dict(row)
                content = str(item.get("narrative") or item.get("reflection") or "").strip()
                if not content:
                    continue
                event_ref = item.get("event_id") or item.get("id") or ""
                memory_id = await self.store.upsert(
                    MemoryWriteRequest(
                        source="memory_event",
                        kind=str(item.get("memory_kind") or "event"),
                        session_id=str(item.get("session_id") or ""),
                        content=content,
                        summary=content[:240],
                        tags=self._json_list(item.get("tags")),
                        importance=self._normalize_importance(item.get("importance")),
                        confidence=0.75,
                        metadata={"legacy_event_id": event_ref, "legacy_row_id": item.get("id")},
                        dedup_key=f"memory_event:{event_ref}",
                        source_ref=f"MemoryEvent:{event_ref}",
                    )
                )
                if self.index_projector and memory_id:
                    await self.index_projector.project(memory_id)
                imported += 1
            await self.store.record_migration(version, status="applied", detail=f"imported={imported}")
        except Exception as exc:
            await self.store.record_migration(version, status="failed", detail=str(exc)[:500])
            logger.warning(f"[MemoryMigrationService] MemoryEvent import degraded: {exc}")
        return imported

    async def _count_unmapped_memory_events(self) -> int:
        scan = await self._scan_memory_events()
        return int(scan.get("importable", 0) or 0)

    async def _count_unmapped_jargons(self) -> int:
        scan = await self._scan_jargons()
        return int(scan.get("importable", 0) or 0)

    async def _count_unmapped_expression_patterns(self) -> int:
        scan = await self._scan_expression_patterns()
        return int(scan.get("importable", 0) or 0)

    async def _count_indexed_jargon_subset(self, index_report: dict[str, Any], key: str) -> int:
        return await self._count_indexed_kind_subset(index_report, key, "jargon")

    async def _count_indexed_kind_subset(self, index_report: dict[str, Any], key: str, kind: str) -> int:
        total = 0
        for memory_id in list(index_report.get(key, []) or []):
            candidate = await self.store.get_canonical(str(memory_id), include_inactive=True)
            if candidate and candidate.kind == kind:
                total += 1
        return total

    async def _count_jargon_missing_metadata(self, field: str) -> int:
        rows = await self.store.list_candidates(kinds=["jargon"], limit=5000, include_inactive=True)
        total = 0
        for candidate in rows:
            metadata = dict(candidate.metadata or {})
            if not str(metadata.get(field) or "").strip():
                total += 1
        return total

    async def _count_expression_missing_metadata(self, field: str) -> int:
        rows = await self.store.list_candidates(kinds=["expression_pattern"], limit=5000, include_inactive=True)
        total = 0
        for candidate in rows:
            metadata = dict(candidate.metadata or {})
            if not str(metadata.get(field) or "").strip():
                total += 1
        return total

    async def _repair_jargon_review_status(self) -> int:
        rows = await self.store.list_candidates(kinds=["jargon"], limit=5000, include_inactive=True)
        changed = 0
        for candidate in rows:
            metadata = dict(candidate.metadata or {})
            if str(metadata.get("review_status") or "").strip():
                continue
            metadata["review_status"] = str(candidate.status or "review_pending")
            changed += await self.store.update_memory(candidate.id, metadata=metadata)
        return changed

    async def _repair_expression_review_status(self) -> int:
        rows = await self.store.list_candidates(kinds=["expression_pattern"], limit=5000, include_inactive=True)
        changed = 0
        for candidate in rows:
            metadata = dict(candidate.metadata or {})
            if str(metadata.get("review_status") or "").strip():
                continue
            metadata["review_status"] = "approved" if candidate.status == "active" else str(candidate.status or "pending")
            changed += await self.store.update_memory(candidate.id, metadata=metadata)
        return changed

    async def _count_jargon_visibility_anomalies(self) -> int:
        rows = await self.store.list_candidates(kinds=["jargon"], limit=5000, include_inactive=True)
        total = 0
        for candidate in rows:
            visibility = str(candidate.visibility or "")
            status = str(candidate.status or "")
            if status == "active" and visibility == "maintenance_only":
                total += 1
            elif status in {"review_pending", "rejected"} and visibility != "maintenance_only":
                total += 1
        return total

    async def _count_jargon_active_non_approved_metadata(self) -> int:
        rows = await self.store.list_candidates(kinds=["jargon"], limit=5000, include_inactive=True)
        total = 0
        for candidate in rows:
            metadata = dict(candidate.metadata or {})
            if candidate.status == "active" and str(metadata.get("review_status") or "").strip().lower() != "approved":
                total += 1
        return total

    async def _count_jargon_pending_human_missing_suggestion(self) -> int:
        rows = await self.store.list_candidates(kinds=["jargon"], limit=5000, include_inactive=True)
        total = 0
        for candidate in rows:
            metadata = dict(candidate.metadata or {})
            if str(metadata.get("review_status") or "").strip().lower() == "pending_human" and not str(
                metadata.get("review_suggestion") or ""
            ).strip():
                total += 1
        return total

    async def _count_expression_visibility_anomalies(self) -> int:
        rows = await self.store.list_candidates(kinds=["expression_pattern"], limit=5000, include_inactive=True)
        total = 0
        for candidate in rows:
            visibility = str(candidate.visibility or "")
            status = str(candidate.status or "")
            review_status = str((candidate.metadata or {}).get("review_status") or "")
            if status == "active" and review_status == "approved" and visibility == "maintenance_only":
                total += 1
            elif status in {"review_pending", "rejected"} and visibility != "maintenance_only":
                total += 1
        return total

    async def _import_jargons(self) -> int:
        if self.engine and hasattr(self.engine, "import_legacy_jargons"):
            return await self.engine.import_legacy_jargons()
        version = "2_jargon_import"
        if await self.store.migration_applied(version):
            return 0
        imported = 0
        try:
            async with aiosqlite.connect(self.store.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("PRAGMA table_info(Jargon)")
                columns = {str(row[1]) for row in await cursor.fetchall()}
                if not columns:
                    await self.store.record_migration(version, status="applied", detail="Jargon table unavailable")
                    return 0
                cursor = await db.execute("SELECT * FROM Jargon ORDER BY id DESC LIMIT 1000")
                rows = await cursor.fetchall()
            for row in rows:
                item = dict(row)
                content = str(item.get("content") or "").strip()
                if not content:
                    continue
                meaning = str(item.get("meaning") or "").strip()
                group_id = str(item.get("group_id") or "GLOBAL")
                status = "active" if bool(item.get("is_jargon")) and bool(item.get("is_complete")) and meaning else "review_pending"
                review_status = "approved" if status == "active" else "review_pending"
                memory_id = await self.store.upsert(
                    MemoryWriteRequest(
                        source="migration_jargon",
                        kind="jargon",
                        session_id=group_id,
                        content=content,
                        summary=meaning or content,
                        importance=0.65,
                        confidence=0.75 if status == "active" else 0.55,
                        metadata={
                            "legacy_jargon_id": item.get("id"),
                            "raw_content": str(item.get("raw_content") or content),
                            "meaning": meaning,
                            "count": int(item.get("count") or 1),
                            "review_status": review_status,
                        },
                        dedup_key=f"jargon:{group_id}:{content.lower()}",
                        source_ref=f"Jargon:{item.get('id')}",
                        visibility="auto_and_tool" if status == "active" else "maintenance_only",
                        status=status,
                    )
                )
                if self.index_projector and memory_id and status == "active":
                    await self.index_projector.project(memory_id)
                imported += 1
            await self.store.record_migration(version, status="applied", detail=f"imported={imported}")
        except Exception as exc:
            await self.store.record_migration(version, status="failed", detail=str(exc)[:500])
            logger.warning(f"[MemoryMigrationService] Jargon import degraded: {exc}")
        return imported

    async def _import_expression_patterns(self) -> int:
        if self.engine and hasattr(self.engine, "import_legacy_expression_patterns"):
            return await self.engine.import_legacy_expression_patterns()
        version = "2_expression_pattern_import"
        if await self.store.migration_applied(version):
            return 0
        imported = 0
        try:
            async with aiosqlite.connect(self.store.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("PRAGMA table_info(ExpressionPattern)")
                columns = {str(row[1]) for row in await cursor.fetchall()}
                if not columns:
                    await self.store.record_migration(version, status="applied", detail="ExpressionPattern table unavailable")
                    return 0
                cursor = await db.execute("SELECT * FROM ExpressionPattern ORDER BY id DESC LIMIT 1000")
                rows = await cursor.fetchall()
            for row in rows:
                item = dict(row)
                expression = str(item.get("expression") or "").strip()
                situation = str(item.get("situation") or "").strip()
                if not expression or not situation:
                    continue
                review_status = str(item.get("review_status") or ("approved" if item.get("checked") else "pending")).strip().lower()
                status = "active" if review_status == "approved" else ("rejected" if review_status == "rejected" else "review_pending")
                await self.store.upsert(
                    MemoryWriteRequest(
                        source="migration_expression_pattern",
                        kind="expression_pattern",
                        session_id=str(item.get("group_id") or ""),
                        content=expression,
                        summary=str(item.get("summary") or expression)[:240],
                        importance=min(1.0, max(0.2, float(item.get("weight") or 1.0) / 3.0)),
                        confidence=0.7,
                        metadata={
                            "legacy_pattern_id": item.get("id"),
                            "situation": situation,
                            "style": str(item.get("style") or ""),
                            "content_samples": self._json_list(item.get("content_list")),
                            "shared_scope": str(item.get("shared_scope") or ""),
                            "think_level": int(item.get("think_level") or 0),
                            "review_status": review_status,
                            "review_reason": str(item.get("review_reason") or ""),
                            "review_suggestion": str(item.get("review_suggestion") or ""),
                            "weight": float(item.get("weight") or 1.0),
                            "count": int(item.get("count") or 1),
                            "last_active_time": float(item.get("last_active_time") or 0.0),
                        },
                        dedup_key=f"expression_pattern:{item.get('group_id') or ''}:{str(item.get('shared_scope') or '').lower()}:{situation.lower()}:{expression.lower()}",
                        source_ref=f"ExpressionPattern:{item.get('id')}",
                        visibility="auto_and_tool" if status == "active" else "maintenance_only",
                        status=status,
                    )
                )
                imported += 1
            await self.store.record_migration(version, status="applied", detail=f"imported={imported}")
        except Exception as exc:
            await self.store.record_migration(version, status="failed", detail=str(exc)[:500])
            logger.warning(f"[MemoryMigrationService] ExpressionPattern import degraded: {exc}")
        return imported

    @staticmethod
    def _json_dict(value: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(value or "{}") if isinstance(value, str) else value
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _json_list(value: Any) -> list[str]:
        try:
            parsed = json.loads(value or "[]") if isinstance(value, str) else value
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except Exception:
            pass
        if isinstance(value, str) and value.strip():
            return [item.strip() for item in value.split(",") if item.strip()]
        return []

    @staticmethod
    def _normalize_importance(value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.5
        if number > 1:
            number = number / 10.0
        return max(0.1, min(1.0, number))


__all__ = ["MemoryMigrationService"]
