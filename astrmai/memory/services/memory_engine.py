import aiosqlite
import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
    HAS_FAISS = True
except ImportError:
    FaissVecDB = None
    HAS_FAISS = False
    logger.warning("[AstrMai] faiss unavailable; vector memory features disabled.")

from ..retrieval.bm25 import BM25Retriever
from ..retrieval.hybrid_retriever import HybridRetriever
from ..retrieval.vector_store import VectorRetriever
from ..contracts.memory_query import MemoryQuery, MemoryWriteRequest
from .expression_pattern_service import ExpressionPatternService
from .cognitive_feedback import (
    FEEDBACK_SCHEMA_VERSION,
    normalize_payload,
    render_feedback,
    source_label,
)
from .instant_memory_gate import InstantMemoryGate
from .memory_index_projector import MemoryIndexProjector
from .memory_injection_service import MemoryInjectionService
from .memory_maintenance_service import MemoryMaintenanceService
from .memory_migration_service import MemoryMigrationService
from .memory_observer import MemoryObserver
from .memory_retrieval_service import MemoryRetrievalService
from .memory_turn_pipeline import MemoryTurnPipeline
from .memory_tool_service import MemoryToolService
from .memory_write_service import MemoryWriteService
from .session_memory_summarizer import SessionMemorySummarizer
from .v2_store import MemoryV2Store


@dataclass(slots=True)
class CognitiveFeedbackSignal:
    source: str
    chat_id: str
    summary: str
    guidance: str
    tags: list[str] = field(default_factory=list)
    timestamp: float = 0.0
    importance: float = 0.5
    payload: dict[str, Any] = field(default_factory=dict)


class MemoryEngine:
    """Refactored memory engine with lazy vector bootstrap and stable facade methods."""

    DISABLE_TTL_SEC = 7 * 86400  # ponytail: 7-day TTL for disabled feedback keys

    def __init__(self, context, gateway, embedding_models: list = None, config=None):
        self.context = context
        self.gateway = gateway
        self.config = config if config else gateway.config
        self.db_service = None
        if hasattr(self.config, "provider") and getattr(self.config.provider, "embedding_models", None):
            self.embedding_models = self.config.provider.embedding_models
        else:
            self.embedding_models = embedding_models or []

        self.data_path = Path(get_astrbot_data_path()) / "plugin_data" / "astrmai" / "memory"
        os.makedirs(self.data_path, exist_ok=True)
        self.db_path = str(self.data_path / "docs.db")
        self.v2_db_path = str(self.data_path / "memory_v2.db")

        self.faiss_db = None
        self.vec_retriever = None
        self.bm25_retriever = None
        self.retriever = None
        self.instant_gate = None
        self.memory_pipeline = None
        self.session_summarizer = None
        self.memory_observer = None
        self.observability_hub = None

        self._faiss_lock = asyncio.Lock()
        self._is_ready = False
        self._init_failures = 0
        self._next_retry_time = 0.0
        self._index_consistency_repaired = False
        self._force_index_rebuild = False
        self._learning_event_history = []
        self._cognitive_feedback_cache: dict[str, list[CognitiveFeedbackSignal]] = {}
        self._disabled_cognitive_feedback_keys: dict[str, float] = {}
        self._last_feedback_cleanup_ts = 0.0
        self.v2_store = MemoryV2Store(self.v2_db_path, data_path=self.data_path, legacy_db_path=self.db_path)
        # Sub-components that depend on self are initialized in initialize()

    @staticmethod
    def _configured_embedding_models(config, fallback: list | None = None) -> list:
        configured = []
        if hasattr(config, "provider") and getattr(config.provider, "embedding_models", None):
            configured = getattr(config.provider, "embedding_models", None) or []
        elif fallback is not None:
            configured = fallback
        return [str(item).strip() for item in configured if str(item).strip()]

    def refresh_config(self, config):
        old_embedding_models = list(self.embedding_models or [])
        self.config = config
        if getattr(self, "injection_service", None) is not None:
            self.injection_service.refresh_config(config)
        if getattr(self, "retrieval_service", None) is not None:
            self.retrieval_service.refresh_config(config)
        if getattr(self, "write_service", None) is not None:
            self.write_service.refresh_config(config)
        if getattr(self, "retriever", None) is not None and hasattr(self.retriever, "refresh_config"):
            self.retriever.refresh_config(config)
        pipeline = getattr(self, "memory_pipeline", None)
        pipeline_refresh = getattr(pipeline, "refresh_config", None)
        if callable(pipeline_refresh):
            pipeline_refresh(config)
        else:
            for attr in ("session_summarizer", "instant_gate"):
                component = getattr(self, attr, None)
                refresh = getattr(component, "refresh_config", None)
                if callable(refresh):
                    refresh(config)
        for attr in ("maintenance_service", "tool_service"):
            component = getattr(self, attr, None)
            refresh = getattr(component, "refresh_config", None)
            if callable(refresh):
                refresh(config)
        self.embedding_models = self._configured_embedding_models(config)
        if self.embedding_models != old_embedding_models:
            self.faiss_db = None
            self.vec_retriever = None
            self.retriever = None
            self._is_ready = False
            self._init_failures = 0
            self._next_retry_time = 0.0
            self._index_consistency_repaired = False
            self._force_index_rebuild = True

    def _reset_vector_index_file(self) -> None:
        index_path = self.data_path / "vectors.index"
        try:
            if index_path.exists():
                index_path.unlink()
        except Exception as exc:
            logger.warning(f"[AstrMai] vector index reset degraded: {exc}")

    def _remember_learning_event(self, event_name: str, payload: dict | None) -> None:
        event_payload = dict(payload or {})
        event_payload["_event"] = event_name
        event_payload["_recorded_at"] = time.time()
        self._learning_event_history.append(event_payload)
        if len(self._learning_event_history) > 100:
            self._learning_event_history = self._learning_event_history[-100:]

    async def on_learning_bot_reply_recorded(self, payload: dict) -> None:
        self._remember_learning_event("learning.bot_reply_recorded", payload)

    async def on_learning_mining_completed(self, payload: dict) -> None:
        self._remember_learning_event("learning.mining_completed", payload)

    def _build_memory_metadata(
        self,
        *,
        session_id: str,
        persona_id: str = None,
        importance: float = 0.8,
        **extra,
    ) -> dict:
        metadata = {
            "session_id": session_id,
            "persona_id": persona_id,
            "importance": importance,
            "create_time": time.time(),
            "last_access_time": time.time(),
        }
        metadata.update(extra)
        return metadata

    async def _run_documents_query(self, query: str, params: tuple = (), *, db_path: str | None = None) -> list:
        target = str(db_path or "").strip()
        if not target:
            raise ValueError("db_path must be explicitly provided for _run_documents_query")
        async with aiosqlite.connect(target) as db:
            cursor = await db.execute(query, params)
            return await cursor.fetchall()

    async def _execute_documents_write(self, query: str, params: tuple = (), *, db_path: str | None = None) -> int:
        target = str(db_path or self.db_path)
        async with aiosqlite.connect(target) as db:
            cursor = await db.execute(query, params)
            await db.commit()
            return cursor.rowcount

    async def search_memories(
        self,
        query: str,
        *,
        top_k: int,
        session_id: str = None,
        persona_id: str = None,
        observation: dict | None = None,
    ):
        if not await self._ensure_faiss_initialized():
            if observation is not None:
                observation.clear()
                observation.update(
                    {
                        "vector": {"status": "initialization_failed"},
                        "fallback_source": "none",
                        "fused_result_count": 0,
                    }
                )
            return []
        return await self.retriever.search(
            query,
            k=top_k,
            session_id=session_id,
            persona_id=persona_id,
            observation=observation,
        )

    # Backward-compatible alias — prefer search_memories() in new code.
    _search_memories = search_memories

    @staticmethod
    def _filter_search_results(results, *, min_score: float) -> list:
        return [item for item in results if getattr(item, "score", 1.0) >= min_score]

    @staticmethod
    def _is_cognitive_feedback_content(content: str) -> bool:
        return str(content or "").lstrip().startswith("[cognitive_feedback:")

    @staticmethod
    def _format_bullet_memories(results) -> str:
        return "\n".join(f"- {item.content}" for item in results)

    async def initialize(self):
        await self.v2_store.initialize()
        self.index_projector = MemoryIndexProjector(self)
        self.v2_store.index_projector = self.index_projector
        self.write_service = MemoryWriteService(self.v2_store, self.index_projector, self.config)
        self.retrieval_service = MemoryRetrievalService(self.v2_store, engine=self)
        self.expression_pattern_service = ExpressionPatternService(self.v2_store, self.write_service)
        self.injection_service = MemoryInjectionService(self.retrieval_service, config=self.config)
        self.tool_service = MemoryToolService(self.retrieval_service, config=self.config)
        self.maintenance_service = MemoryMaintenanceService(self.v2_store, self.index_projector, config=self.config)
        self.migration_service = MemoryMigrationService(
            self.v2_store,
            index_projector=self.index_projector,
            engine=self,
        )
        await self.v2_store.import_legacy_documents()
        await self.v2_store.import_persona_cache()
        await self.import_legacy_memory_events()
        await self.import_legacy_jargons()
        await self.import_legacy_expression_patterns()
        await self.migrate_legacy_cognitive_feedback()
        await self._cleanup_cognitive_feedback_records(force=True)
        self.bm25_retriever = BM25Retriever(self.db_path)
        await self.bm25_retriever.initialize()
        logger.info("[AstrMai] memory skeleton initialized; vector store will be lazy-loaded.")

    async def _ensure_faiss_initialized(self):
        if self._is_ready:
            return True

        now = time.time()
        if now < self._next_retry_time:
            return False

        if not HAS_FAISS:
            logger.error("[AstrMai] memory wakeup failed: faiss is unavailable in current environment.")
            self._next_retry_time = now + 86400
            return False

        provider_instance = None
        clean_models = [m.strip() for m in self.embedding_models if m and m.strip()]
        unique_models = list(dict.fromkeys(clean_models))
        for model_id in unique_models:
            if hasattr(self.context, "get_provider_by_id"):
                provider_instance = self.context.get_provider_by_id(model_id)
            if not provider_instance and hasattr(self.context, "get_provider"):
                provider_instance = self.context.get_provider(model_id)
            if provider_instance:
                break

        if not provider_instance:
            self._init_failures += 1
            backoff = min(3600, 30 * (2 ** (self._init_failures - 1)))
            self._next_retry_time = now + backoff
            models_str = ", ".join(unique_models) if unique_models else "unconfigured"
            logger.error(f"[AstrMai] memory wakeup failed: no valid embedding model found [{models_str}]; retry in {backoff}s.")
            return False

        async with self._faiss_lock:
            if self._is_ready:
                return True

            try:
                if self._force_index_rebuild:
                    self._reset_vector_index_file()
                self.faiss_db = FaissVecDB(
                    doc_store_path=str(self.data_path / "docs.db"),
                    index_store_path=str(self.data_path / "vectors.index"),
                    embedding_provider=provider_instance,
                )
                await self.faiss_db.initialize()
            except Exception as exc:
                self._init_failures += 1
                backoff = min(3600, 30 * (2 ** (self._init_failures - 1)))
                self._next_retry_time = now + backoff
                logger.error(f"[AstrMai] FaissVecDB initialization failed: {exc}; retry in {backoff}s.", exc_info=True)
                return False

            self.vec_retriever = VectorRetriever(self.faiss_db, self.config)
            self.retriever = HybridRetriever(self.bm25_retriever, self.vec_retriever, config=self.config)
            self._is_ready = True
            self._init_failures = 0
            if self._force_index_rebuild or not await self.v2_store.migration_applied("2_index_rebuild"):
                try:
                    rebuilt = await self.index_projector.rebuild_all()
                    self._force_index_rebuild = False
                    await self.v2_store.record_migration("2_index_rebuild", status="applied", detail=f"rebuilt={rebuilt}")
                except Exception as exc:
                    await self.v2_store.record_migration("2_index_rebuild", status="failed", detail=str(exc)[:500])
                    logger.warning(f"[MemoryV2] index rebuild degraded: {exc}")
            if not self._index_consistency_repaired:
                try:
                    report = await self.index_projector.check_consistency()
                    needs_repair = any(
                        report.get(key)
                        for key in (
                            "missing_projection_ids",
                            "orphan_projection_ids",
                            "inactive_projection_ids",
                            "duplicate_projection_ids",
                        )
                    )
                    if needs_repair:
                        repaired = await self.index_projector.repair_consistency(report)
                        logger.info(f"[MemoryV2] index consistency repaired: {repaired}")
                    self._index_consistency_repaired = True
                except Exception as exc:
                    logger.warning(f"[MemoryV2] index consistency repair degraded: {exc}")
            logger.info("[AstrMai] hybrid memory engine ready (BM25 + FaissVecDB).")
            return True

    async def add_memory(
        self,
        content: str,
        session_id: str,
        persona_id: str = None,
        importance: float = 0.8,
        sender_id: str = "",
        created_at: float = 0.0,
    ):
        request = MemoryWriteRequest(
            source="legacy_add_memory",
            kind="persona_lore" if session_id == "__self_lore__" else "memory",
            session_id=str(session_id or ""),
            sender_id=str(sender_id or ""),
            persona_id=str(persona_id or ""),
            content=str(content or ""),
            importance=float(importance or 0.8),
            confidence=0.8,
            source_ref="memory_engine.add_memory",
            created_at=float(created_at or 0.0),
        )
        return await self.write_service.write(request)

    @staticmethod
    def _feedback_prefix(source: str) -> str:
        clean_source = str(source or "unknown").strip().lower() or "unknown"
        return f"[cognitive_feedback:{clean_source}]"

    @staticmethod
    def _normalize_feedback_tags(tags: list[str] | None) -> list[str]:
        result: list[str] = []
        for tag in tags or []:
            clean = str(tag or "").strip().lower()
            if clean and clean not in result:
                result.append(clean)
        return result[:12]

    @classmethod
    def _format_cognitive_feedback_content(
        cls,
        *,
        source: str,
        summary: str,
        guidance: str = "",
        tags: list[str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        lines = [
            cls._feedback_prefix(source),
            f"summary: {str(summary or '').strip()}",
        ]
        if str(guidance or "").strip():
            lines.append(f"guidance: {str(guidance or '').strip()}")
        clean_tags = cls._normalize_feedback_tags(tags)
        if clean_tags:
            lines.append("tags: " + ", ".join(clean_tags))
        clean_payload = normalize_payload(payload)
        if clean_payload:
            lines.append("payload: " + json.dumps(clean_payload, ensure_ascii=False, separators=(",", ":")))
        return "\n".join(lines).strip()

    @classmethod
    def _parse_cognitive_feedback_content(
        cls,
        text: str,
        *,
        chat_id: str,
        timestamp: float = 0.0,
        importance: float = 0.5,
        payload: dict[str, Any] | None = None,
    ) -> CognitiveFeedbackSignal | None:
        content = str(text or "").strip()
        if not content.startswith("[cognitive_feedback:"):
            return None
        first_line, *rest = content.splitlines()
        source = first_line.removeprefix("[cognitive_feedback:").removesuffix("]").strip() or "unknown"
        summary = ""
        guidance = ""
        tags: list[str] = []
        parsed_payload = normalize_payload(payload)
        for line in rest:
            if line.startswith("summary:"):
                summary = line.split(":", 1)[1].strip()
            elif line.startswith("guidance:"):
                guidance = line.split(":", 1)[1].strip()
            elif line.startswith("tags:"):
                tags = cls._normalize_feedback_tags(line.split(":", 1)[1].split(","))
            elif line.startswith("payload:") and not parsed_payload:
                try:
                    parsed_payload = normalize_payload(json.loads(line.split(":", 1)[1].strip()))
                except (TypeError, ValueError, json.JSONDecodeError):
                    logger.debug("[MemoryEngine] cognitive feedback payload parse failed", exc_info=True)
        if not summary and not guidance:
            return None
        summary, guidance, _display_tags, parsed_payload = render_feedback(
            source=source,
            summary=summary,
            guidance=guidance,
            tags=tags,
            payload=parsed_payload,
        )
        return CognitiveFeedbackSignal(
            source=source,
            chat_id=chat_id,
            summary=summary,
            guidance=guidance,
            tags=tags,
            timestamp=float(timestamp or 0.0),
            importance=float(importance or 0.5),
            payload=parsed_payload,
        )

    def _remember_cognitive_feedback(self, signal: CognitiveFeedbackSignal) -> None:
        items = self._cognitive_feedback_cache.setdefault(signal.chat_id, [])
        items[:] = [item for item in items if item.source != signal.source]
        items.append(signal)
        if len(items) > 32:
            del items[:-32]
        if len(self._cognitive_feedback_cache) > 100:
            oldest = next(iter(self._cognitive_feedback_cache))
            del self._cognitive_feedback_cache[oldest]

    @staticmethod
    def _cognitive_feedback_key(signal: CognitiveFeedbackSignal) -> tuple[str, str, str, str]:
        return (
            str(signal.chat_id or ""),
            str(signal.source or ""),
            str(signal.summary or ""),
            str(signal.guidance or ""),
        )

    @staticmethod
    def _cognitive_feedback_key_str(signal: CognitiveFeedbackSignal) -> str:
        return f"{signal.chat_id}|{signal.source}|{signal.summary}|{signal.guidance}"

    def disable_cognitive_feedback(self, signal: CognitiveFeedbackSignal) -> None:
        now = time.time()
        key = self._cognitive_feedback_key_str(signal)
        self._disabled_cognitive_feedback_keys[key] = now
        # ponytail: lazy TTL cleanup on each disable
        stale = [k for k, ts in list(self._disabled_cognitive_feedback_keys.items()) if now - ts > 7 * 86400]
        for k in stale:
            del self._disabled_cognitive_feedback_keys[k]

    async def _cleanup_cognitive_feedback_records(self, *, force: bool = False) -> int:
        now = time.time()
        if not force and now - self._last_feedback_cleanup_ts < 3600:
            return 0
        self._last_feedback_cleanup_ts = now
        maintenance = getattr(self, "maintenance_service", None)
        if maintenance is None:
            return 0
        try:
            page = await self.v2_store.list_canonical(
                kind="feedback",
                status="active",
                limit=500,
                include_inactive=True,
            )
            candidates = list(page.get("items", []) or [])
            seen: set[tuple[str, str]] = set()
            delete_reasons: dict[str, str] = {}
            for candidate in candidates:
                memory_id = str(candidate.get("id") or "")
                metadata = dict(candidate.get("metadata") or {})
                valid_until = float(metadata.get("valid_until") or 0.0)
                key = (
                    str(candidate.get("session_id") or ""),
                    str(candidate.get("source") or "unknown").strip().lower(),
                )
                if valid_until > 0 and valid_until < now:
                    delete_reasons[memory_id] = "expired_cognitive_feedback"
                elif key in seen:
                    delete_reasons[memory_id] = "superseded_rolling_feedback"
                else:
                    seen.add(key)
            changed = 0
            for memory_id, reason in delete_reasons.items():
                if memory_id:
                    changed += int(await maintenance.soft_delete(memory_id, reason=reason) or 0)
            return changed
        except Exception as exc:
            logger.warning(f"[MemoryEngine] cognitive feedback cleanup degraded: {exc}")
            return 0

    async def migrate_legacy_cognitive_feedback(self) -> int:
        version = "feedback_schema_v2"
        required = ("migration_applied", "list_canonical", "update_memory", "record_migration")
        if not all(hasattr(self.v2_store, name) for name in required):
            return 0
        if await self.v2_store.migration_applied(version):
            return 0
        migrated = 0
        try:
            page = await self.v2_store.list_canonical(
                kind="feedback",
                status="active",
                limit=500,
                include_inactive=True,
            )
            for candidate in list(page.get("items", []) or []):
                metadata = dict(candidate.get("metadata") or {})
                if int(metadata.get("feedback_schema_version") or 1) >= FEEDBACK_SCHEMA_VERSION:
                    continue
                source = str(candidate.get("source") or "unknown").strip().lower() or "unknown"
                signal = self._parse_cognitive_feedback_content(
                    str(candidate.get("content") or ""),
                    chat_id=str(candidate.get("session_id") or ""),
                    timestamp=float(candidate.get("created_at") or 0.0),
                    importance=float(candidate.get("importance") or 0.5),
                )
                if signal is None:
                    continue
                metadata.update(
                    {
                        "guidance": signal.guidance,
                        "feedback_schema_version": FEEDBACK_SCHEMA_VERSION,
                        "feedback_payload": signal.payload,
                    }
                )
                changed = await self.v2_store.update_memory(
                    str(candidate.get("id") or ""),
                    content=self._format_cognitive_feedback_content(
                        source=source,
                        summary=signal.summary,
                        guidance=signal.guidance,
                        tags=signal.tags,
                        payload=signal.payload,
                    ),
                    summary=signal.summary,
                    tags=signal.tags,
                    metadata=metadata,
                )
                migrated += int(bool(changed))
            await self.v2_store.record_migration(version, status="applied", detail=f"migrated={migrated}")
        except Exception as exc:
            await self.v2_store.record_migration(version, status="failed", detail=str(exc)[:500])
            logger.warning(f"[MemoryV2] cognitive feedback migration degraded: {exc}")
        return migrated

    async def record_cognitive_feedback(
        self,
        session_id: str,
        source: str,
        summary: str,
        guidance: str = "",
        tags: list[str] | None = None,
        importance: float = 0.5,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self._cleanup_cognitive_feedback_records()
        chat_id = str(session_id or "").strip()
        clean_summary = str(summary or "").strip()
        clean_guidance = str(guidance or "").strip()
        if not chat_id or not (clean_summary or clean_guidance):
            return
        clean_source = str(source or "unknown").strip().lower() or "unknown"
        clean_tags = self._normalize_feedback_tags(tags)
        clean_summary, clean_guidance, _display_tags, clean_payload = render_feedback(
            source=clean_source,
            summary=clean_summary,
            guidance=clean_guidance,
            tags=clean_tags,
            payload=payload,
        )
        now = time.time()
        signal = CognitiveFeedbackSignal(
            source=clean_source,
            chat_id=chat_id,
            summary=clean_summary[:500],
            guidance=clean_guidance[:500],
            tags=clean_tags,
            timestamp=now,
            importance=float(importance or 0.5),
            payload=clean_payload,
        )
        self._remember_cognitive_feedback(signal)
        valid_until = now + (72 * 3600)
        await self.write_service.write(
            MemoryWriteRequest(
                source=signal.source,
                kind="feedback",
                session_id=chat_id,
                content=self._format_cognitive_feedback_content(
                    source=signal.source,
                    summary=signal.summary,
                    guidance=signal.guidance,
                    tags=signal.tags,
                    payload=signal.payload,
                ),
                summary=signal.summary,
                tags=signal.tags,
                importance=signal.importance,
                confidence=0.8,
                metadata={
                    "guidance": signal.guidance,
                    "cognitive_feedback": True,
                    "valid_until": valid_until,
                    "feedback_window": "rolling",
                    "feedback_schema_version": FEEDBACK_SCHEMA_VERSION,
                    "feedback_payload": signal.payload,
                },
                dedup_key=f"feedback:{chat_id}:{signal.source}:rolling",
                source_ref=f"cognitive_feedback:{signal.source}",
                visibility="tool_only",
            )
        )

    async def get_cognitive_feedback(
        self,
        session_id: str,
        *,
        limit: int = 3,
        max_age_seconds: float = 72 * 3600,
        sources: set[str] | None = None,
    ) -> list[CognitiveFeedbackSignal]:
        chat_id = str(session_id or "").strip()
        if not chat_id:
            return []
        now = time.time()
        source_filter = {str(item).strip().lower() for item in sources or set() if str(item).strip()}
        signals: list[CognitiveFeedbackSignal] = []

        for item in self._cognitive_feedback_cache.get(chat_id, []):
            if max_age_seconds is not None and now - float(item.timestamp or 0.0) > max_age_seconds:
                continue
            if source_filter and item.source not in source_filter:
                continue
            signals.append(item)

        try:
            where = ["kind = ?", "session_id = ?", "status = 'active'"]
            params: list[Any] = ["feedback", chat_id]
            if max_age_seconds is not None:
                where.append("create_time >= ?")
                params.append(now - max_age_seconds)
            rows = await self._run_documents_query(
                f"""
                SELECT content, metadata, create_time
                FROM canonical_memories
                WHERE {' AND '.join(where)}
                ORDER BY create_time DESC
                LIMIT ?
                """,
                (*params, max(limit * 4, limit)),
                db_path=self.v2_db_path,
            )
            for text, metadata_raw, create_time_raw in rows:
                timestamp = float(create_time_raw or 0.0)
                importance = 0.5
                metadata: dict[str, Any] = {}
                try:
                    metadata = json.loads(metadata_raw or "{}") if isinstance(metadata_raw, str) else {}
                    importance = float(metadata.get("importance") or 0.5)
                    valid_until = float(metadata.get("valid_until") or 0.0)
                    if valid_until > 0 and valid_until < now:
                        continue
                except Exception:
                    logger.debug("[MemoryEngine] cognitive feedback metadata parse failed", exc_info=True)
                    pass
                parse_kwargs: dict[str, Any] = {
                    "chat_id": chat_id,
                    "timestamp": timestamp,
                    "importance": importance,
                }
                metadata_payload = dict(metadata.get("feedback_payload") or {})
                if metadata_payload:
                    parse_kwargs["payload"] = metadata_payload
                parsed = self._parse_cognitive_feedback_content(str(text or ""), **parse_kwargs)
                if not parsed:
                    continue
                if source_filter and parsed.source not in source_filter:
                    continue
                signals.append(parsed)
        except Exception as exc:
            logger.warning(f"[MemoryEngine] cognitive feedback lookup from canonical_memories degraded: {exc}")

        unique: list[CognitiveFeedbackSignal] = []
        seen: set[tuple[str, str, str]] = set()
        for item in sorted(signals, key=lambda signal: signal.timestamp, reverse=True):
            if self._cognitive_feedback_key_str(item) in self._disabled_cognitive_feedback_keys:
                continue
            key = (item.source, item.summary, item.guidance)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
            if len(unique) >= limit:
                break
        return unique

    async def list_cognitive_feedback_records(
        self,
        *,
        session_id: str = "",
        source: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        await self._cleanup_cognitive_feedback_records()
        page = await self.v2_store.list_canonical(
            session_id=str(session_id or ""),
            source=str(source or "").strip().lower(),
            kind="feedback",
            status="active",
            limit=max(1, min(int(limit or 50), 300)),
            offset=max(0, int(offset or 0)),
            include_inactive=False,
        )
        items: list[dict[str, Any]] = []
        now = time.time()
        for candidate in list(page.get("items", []) or []):
            candidate_source = str(candidate.get("source", "unknown") or "unknown").strip().lower()
            metadata = dict(candidate.get("metadata", {}) or {})
            signal = self._parse_cognitive_feedback_content(
                str(candidate.get("content", "") or ""),
                chat_id=str(candidate.get("session_id", "") or ""),
                timestamp=float(candidate.get("created_at", 0.0) or 0.0),
                importance=float(candidate.get("importance", 0.5) or 0.5),
                payload=dict(metadata.get("feedback_payload") or {}),
            )
            raw_summary = str(candidate.get("summary", "") or "")
            raw_guidance = str(metadata.get("guidance", "") or "")
            display_summary, display_guidance, display_tags, payload = render_feedback(
                source=candidate_source,
                summary=signal.summary if signal else raw_summary,
                guidance=signal.guidance if signal else raw_guidance,
                tags=list(candidate.get("tags", []) or []),
                payload=signal.payload if signal else dict(metadata.get("feedback_payload") or {}),
            )
            valid_until = float(metadata.get("valid_until") or 0.0)
            expiry_state = "长期有效"
            if valid_until > 0:
                remaining = valid_until - now
                expiry_state = "已过期" if remaining <= 0 else ("即将过期" if remaining <= 6 * 3600 else "有效")
            items.append(
                {
                    "id": str(candidate.get("id", "") or ""),
                    "chat_id": str(candidate.get("session_id", "") or ""),
                    "session_id": str(candidate.get("session_id", "") or ""),
                    "source": candidate_source,
                    "source_label": source_label(candidate_source),
                    "summary": display_summary,
                    "guidance": display_guidance,
                    "tags": list(candidate.get("tags", []) or []),
                    "display_tags": display_tags,
                    "payload": payload,
                    "feedback_schema_version": int(metadata.get("feedback_schema_version") or 1),
                    "valid_until": valid_until,
                    "expiry_state": expiry_state,
                    "timestamp": float(candidate.get("created_at", 0.0) or 0.0),
                    "importance": float(candidate.get("importance", 0.5) or 0.5),
                    "status": str(candidate.get("status", "active") or "active"),
                    "persisted": True,
                }
            )
        return {
            "items": items,
            "total": int(page.get("total", len(items)) or 0),
            "limit": int(page.get("limit", limit) or limit),
            "offset": int(page.get("offset", offset) or offset),
        }

    async def disable_cognitive_feedback_record(self, memory_id: str) -> bool:
        clean_id = str(memory_id or "").strip()
        if not clean_id:
            return False
        return bool(await self.maintenance_service.soft_delete(clean_id, reason="webui_feedback_disabled"))

    async def clear_persona_lore(self, persona_id: str = None) -> int:
        return await self.maintenance_service.soft_delete_by_filter(
            kind="persona_lore",
            session_id="__self_lore__",
            persona_id=str(persona_id or ""),
            reason="persona_lore_rebuild",
        )

    async def add_persona_lore(self, content: str, persona_id: str = None):
        return await self.write_service.write(
            MemoryWriteRequest(
                source="persona_lore",
                kind="persona_lore",
                session_id="__self_lore__",
                persona_id=str(persona_id or ""),
                content=str(content or ""),
                summary=str(content or "")[:240],
                importance=1.0,
                confidence=0.9,
                dedup_key=f"persona_lore:{persona_id or ''}:{hash(str(content or ''))}",
                source_ref=f"persona_lore:{persona_id or ''}",
            )
        )

    async def recall_persona_lore(self, query: str, persona_id: str = None, top_k: int = 3) -> str:
        memory_query = MemoryQuery(
            query=str(query or ""),
            session_id="__self_lore__",
            persona_id=str(persona_id or ""),
            layers=["persona_lore"],
            top_k=int(top_k or 3),
            include_persona_lore=True,
            allow_stale=True,
        )
        candidates = await self.retrieval_service.retrieve(memory_query)
        if not candidates:
            return "(no relevant persona lore found)"
        return "\n".join(f"[fact] {item.summary or item.content}" for item in candidates)

    async def recall(self, query: str, session_id: str = None, persona_id: str = None, top_k: int = None, layers: list[str] | None = None, exclude_kinds: list[str] | None = None) -> str:
        recall_top_k = top_k if top_k is not None else getattr(getattr(self.config, "memory", None), "recall_top_k", 5)
        memory_query = MemoryQuery(
            query=str(query or ""),
            session_id=str(session_id or ""),
            persona_id=str(persona_id or ""),
            layers=list(layers or []),
            top_k=int(recall_top_k or 5),
            exclude_kinds=list(exclude_kinds) if exclude_kinds is not None else ["feedback"],
        )
        candidates = await self.retrieval_service.retrieve(memory_query)
        # Safety net: exclude_kinds in MemoryQuery handles the v2_store path,
        # but hybrid search results may still contain feedback items.  The
        # content-based check catches those as a defense-in-depth measure.
        candidates = [
            item for item in candidates
            if not self._is_cognitive_feedback_content(getattr(item, "content", ""))
        ]
        if not candidates:
            return f"No relevant memory found for '{query}'."
        logger.info(f"[Memory] recall matched {len(candidates)} high-relevance fragments.")
        return self.retrieval_service.render_recall(memory_query, candidates)

    async def query(self, query: str, session_id: str = "", **kwargs) -> str:
        return await self.recall(query=query, session_id=session_id, top_k=kwargs.get("top_k"))

    async def search(self, query: str, session_id: str = "", **kwargs) -> str:
        return await self.recall(query=query, session_id=session_id, top_k=kwargs.get("top_k"))

    async def query_persona_lore(self, query: str, persona_id: str = "", **kwargs) -> str:
        return await self.recall_persona_lore(query=query, persona_id=persona_id, top_k=kwargs.get("top_k", 3))

    async def start_background_tasks(self):
        if self.index_projector is not None:
            await self.index_projector.start()
        raw_trace_store = getattr(getattr(self, "db_service", None), "raw_trace_store", None)
        self.memory_observer = MemoryObserver(
            raw_trace_store,
            observability_hub=getattr(self, "observability_hub", None),
        )
        self.session_summarizer = SessionMemorySummarizer(self.context, self.gateway, self, config=self.config)
        self.instant_gate = InstantMemoryGate(self.gateway, self, config=self.config)
        self.memory_pipeline = MemoryTurnPipeline(
            context=self.context,
            gateway=self.gateway,
            engine=self,
            session_summarizer=self.session_summarizer,
            instant_gate=self.instant_gate,
            event_bus=getattr(getattr(self, "db_service", None), "event_bus", None) or getattr(self.gateway, "event_bus", None),
            config=self.config,
            observer=self.memory_observer,
        )
        await self.memory_pipeline.start()

    async def stop_background_tasks(self):
        pipeline = getattr(self, "memory_pipeline", None)
        if pipeline is not None:
            await pipeline.stop()
        projector = getattr(self, "index_projector", None)
        if projector is not None:
            await projector.stop()

    async def run_memory_maintenance(self, chat_id: str) -> dict:
        pipeline = getattr(self, "memory_pipeline", None)
        if pipeline is None or not hasattr(pipeline, "run_maintenance_for_session"):
            return {"performed": False, "reason": "memory_pipeline_unavailable"}
        return await pipeline.run_maintenance_for_session(chat_id)

    async def describe_memory_eligibility(self, chat_id: str) -> dict:
        pipeline = getattr(self, "memory_pipeline", None)
        if pipeline is None or not hasattr(pipeline, "describe_session_eligibility"):
            return {
                "eligible": False,
                "candidate_present": False,
                "reason": "memory_pipeline_unavailable",
                "pending_messages": 0,
                "history_size": 0,
                "threshold_messages": 0,
                "cooldown_until": 0.0,
                "last_memory_run_at": 0.0,
                "last_update": 0.0,
            }
        return await pipeline.describe_session_eligibility(chat_id)

    async def apply_daily_decay(self, decay_rate: float, days: int = 1) -> int:
        return await self.maintenance_service.apply_daily_decay(
            decay_rate=decay_rate,
            days=days,
            min_score=getattr(getattr(self.config, "memory", None), "prune_threshold", 0.2),
            stale_grace_seconds=7 * 86400,
        )

    async def import_legacy_memory_events(self, *, limit: int = 1000) -> int:
        version = "2_memory_event_import"
        if await self.v2_store.migration_applied(version):
            return 0
        db_service = getattr(self, "db_service", None)
        if not db_service or not hasattr(db_service, "get_session"):
            logger.warning("[MemoryV2] MemoryEvent import deferred: db service unavailable")
            return 0
        imported = 0
        page_size = max(int(limit or 1000), 1)
        try:
            from ...infrastructure.persistence import MemoryEvent
            from sqlmodel import desc, select

            def _load_events(offset: int):
                with db_service.get_session() as session:
                    statement = (
                        select(MemoryEvent)
                        .order_by(desc(MemoryEvent.created_at))
                        .offset(offset)
                        .limit(page_size)
                    )
                    return [MemoryEvent.model_validate(item.model_dump()) for item in session.exec(statement).all()]

            offset = 0
            while True:
                events = await asyncio.to_thread(_load_events, offset)
                for event in events:
                    content = str(getattr(event, "narrative", "") or "").strip()
                    if not content:
                        continue
                    dedup_key = f"memory_event:{getattr(event, 'event_id', '')}"
                    existing = await self.v2_store.get_by_dedup_key(dedup_key, include_inactive=True)
                    if existing is not None:
                        continue
                    tags = []
                    try:
                        parsed_tags = json.loads(getattr(event, "tags", "") or "[]")
                        if isinstance(parsed_tags, list):
                            tags = [str(item) for item in parsed_tags]
                    except Exception:
                        logger.debug("[MemoryEngine] legacy memory event tags parse failed", exc_info=True)
                        tags = []
                    metadata = {
                        "legacy_event_id": getattr(event, "event_id", ""),
                        "emotion": getattr(event, "emotion", ""),
                        "reflection": getattr(event, "reflection", ""),
                        "memory_kind": getattr(event, "memory_kind", ""),
                        "source_layer": getattr(event, "source_layer", ""),
                    }
                    memory_id = await self.write_service.write(
                        MemoryWriteRequest(
                            source="memory_event",
                            kind=str(getattr(event, "memory_kind", "") or "event"),
                            session_id=str(getattr(event, "session_id", "") or getattr(event, "date", "") or ""),
                            content=content,
                            summary=content[:240],
                            tags=tags,
                            importance=max(0.1, min(1.0, float(getattr(event, "importance", 5) or 5) / 10.0)),
                            confidence=0.75,
                            metadata=metadata,
                            dedup_key=dedup_key,
                            source_ref=f"MemoryEvent:{getattr(event, 'event_id', '')}",
                        )
                    )
                    imported += int(bool(memory_id))
                offset += len(events)
                if len(events) < page_size:
                    break
            await self.v2_store.record_migration(version, status="applied", detail=f"imported={imported}")
        except Exception as exc:
            await self.v2_store.record_migration(version, status="failed", detail=str(exc)[:500])
            logger.warning(f"[MemoryV2] MemoryEvent import degraded: {exc}")
        return imported

    async def import_legacy_jargons(self, *, limit: int = 1000) -> int:
        version = "2_jargon_import"
        if await self.v2_store.migration_applied(version):
            return 0
        db_service = getattr(self, "db_service", None)
        if not db_service or not hasattr(db_service, "get_session"):
            logger.warning("[MemoryV2] Jargon import deferred: db service unavailable")
            return 0
        imported = 0
        page_size = max(int(limit or 1000), 1)
        try:
            from ...infrastructure.persistence import Jargon
            from sqlmodel import desc, select

            def _load_jargons(offset: int):
                with db_service.get_session() as session:
                    statement = select(Jargon).order_by(desc(Jargon.updated_at)).offset(offset).limit(page_size)
                    return [Jargon.model_validate(item.model_dump()) for item in session.exec(statement).all()]

            offset = 0
            while True:
                rows = await asyncio.to_thread(_load_jargons, offset)
                for item in rows:
                    content = str(getattr(item, "content", "") or "").strip()
                    if not content:
                        continue
                    meaning = str(getattr(item, "meaning", "") or "").strip()
                    group_id = str(getattr(item, "group_id", "") or "GLOBAL")
                    dedup_key = f"jargon:{group_id}:{content.lower()}"
                    existing = await self.v2_store.get_by_dedup_key(dedup_key, include_inactive=True)
                    if existing is not None:
                        continue
                    status = "active" if bool(getattr(item, "is_jargon", False)) and bool(getattr(item, "is_complete", False)) and meaning else "review_pending"
                    memory_id = await self.write_service.write(
                        MemoryWriteRequest(
                            source="legacy_jargon",
                            kind="jargon",
                            session_id=group_id,
                            content=content,
                            summary=meaning or content,
                            importance=0.65,
                            confidence=0.75 if status == "active" else 0.55,
                            metadata={
                                "legacy_jargon_id": getattr(item, "id", None),
                                "raw_content": str(getattr(item, "raw_content", "") or content),
                                "meaning": meaning,
                                "count": int(getattr(item, "count", 1) or 1),
                                "review_status": status,
                            },
                            dedup_key=dedup_key,
                            source_ref=f"Jargon:{getattr(item, 'id', '')}",
                            visibility="auto_and_tool" if status == "active" else "maintenance_only",
                            status=status,
                        )
                    )
                    imported += int(bool(memory_id))
                offset += len(rows)
                if len(rows) < page_size:
                    break
            await self.v2_store.record_migration(version, status="applied", detail=f"imported={imported}")
        except Exception as exc:
            await self.v2_store.record_migration(version, status="failed", detail=str(exc)[:500])
            logger.warning(f"[MemoryV2] Jargon import degraded: {exc}")
        return imported

    async def import_legacy_expression_patterns(self, *, limit: int = 1000) -> int:
        version = "2_expression_pattern_import"
        if await self.v2_store.migration_applied(version):
            return 0
        db_service = getattr(self, "db_service", None)
        service = getattr(self, "expression_pattern_service", None)
        if not db_service or not hasattr(db_service, "get_session") or service is None:
            logger.warning("[MemoryV2] ExpressionPattern import deferred: dependency unavailable")
            return 0
        imported = 0
        page_size = max(int(limit or 1000), 1)
        try:
            from ...infrastructure.persistence import ExpressionPattern
            from sqlmodel import desc, select

            def _load_patterns(offset: int):
                with db_service.get_session() as session:
                    statement = (
                        select(ExpressionPattern)
                        .order_by(desc(ExpressionPattern.last_active_time))
                        .offset(offset)
                        .limit(page_size)
                    )
                    return [ExpressionPattern.model_validate(item.model_dump()) for item in session.exec(statement).all()]

            offset = 0
            while True:
                rows = await asyncio.to_thread(_load_patterns, offset)
                for item in rows:
                    expression = str(getattr(item, "expression", "") or "").strip()
                    situation = str(getattr(item, "situation", "") or "").strip()
                    if not expression or not situation:
                        continue
                    group_id = str(getattr(item, "group_id", "") or "")
                    shared_scope = str(getattr(item, "shared_scope", "") or "")
                    dedup_key = service.build_dedup_key(group_id, situation, expression, shared_scope)
                    existing = await self.v2_store.get_by_dedup_key(dedup_key, include_inactive=True)
                    if existing is not None:
                        continue
                    memory_id = await service.write_pattern(
                        group_id,
                        {
                            "expression": expression,
                            "situation": situation,
                            "style": str(getattr(item, "style", "") or ""),
                            "content_samples": json.loads(getattr(item, "content_list", "[]") or "[]"),
                            "count": int(getattr(item, "count", 1) or 1),
                            "think_level": int(getattr(item, "think_level", 0) or 0),
                            "review_status": str(getattr(item, "review_status", "pending") or "pending"),
                            "review_reason": str(getattr(item, "review_reason", "") or ""),
                            "review_suggestion": str(getattr(item, "review_suggestion", "") or ""),
                            "weight": float(getattr(item, "weight", 1.0) or 1.0),
                            "shared_scope": shared_scope,
                            "legacy_pattern_id": getattr(item, "id", None),
                            "source_ref": f"ExpressionPattern:{getattr(item, 'id', '')}",
                            "summary": expression,
                        },
                        source="legacy_expression_pattern",
                    )
                    imported += int(bool(memory_id))
                offset += len(rows)
                if len(rows) < page_size:
                    break
            await self.v2_store.record_migration(version, status="applied", detail=f"imported={imported}")
        except Exception as exc:
            await self.v2_store.record_migration(version, status="failed", detail=str(exc)[:500])
            logger.warning(f"[MemoryV2] ExpressionPattern import degraded: {exc}")
        return imported

    async def get_recent_memories(self, session_id: str, hours: int = 24) -> list:
        if not await self._ensure_faiss_initialized():
            return []

        recent_memories: list[str] = []
        cutoff_time = time.time() - (hours * 3600)
        try:
            columns = [row[1] for row in await self._run_documents_query("PRAGMA table_info(documents)", db_path=self.db_path)]
            if not columns:
                logger.warning("[Memory] documents table has no columns yet; skip recent memory lookup.")
                return []

            text_col = "page_content" if "page_content" in columns else ("content" if "content" in columns else "text")
            rows = await self._run_documents_query(
                f"""
                SELECT {text_col}
                FROM documents
                WHERE json_extract(metadata, '$.session_id') = ?
                  AND json_extract(metadata, '$.create_time') >= ?
                """,
                (session_id, cutoff_time),
                db_path=self.db_path,
            )
            for row in rows:
                if row and row[0]:
                    if self._is_cognitive_feedback_content(str(row[0])):
                        continue
                    recent_memories.append(row[0])
        except Exception as exc:
            logger.error(f"[Memory] get recent memories failed: {exc}")

        return recent_memories

    async def prune_low_importance(self, threshold: float = 0.2) -> int:
        return await self.maintenance_service.prune_low_importance(threshold=threshold)

    @staticmethod
    def _compute_text_similarity(text_a: str, text_b: str) -> float:
        def ngrams(text, n=2):
            return set(text[i:i + n] for i in range(len(text) - n + 1))

        a = ngrams(text_a)
        b = ngrams(text_b)
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    async def store_topic_results(self, topic_results: list, session_id: str, persona_id: str = None):
        for topic_result in topic_results:
            summary = str(topic_result.get("summary", "") or "").strip()
            if not summary or summary == "topic too short":
                continue

            normalized_summary = " ".join(summary.split()).lower()
            summary_digest = hashlib.sha256(normalized_summary.encode("utf-8")).hexdigest()[:24]
            topic_dedup_key = f"topic:{session_id}:{summary_digest}"
            existing_exact = await self.v2_store.get_by_dedup_key(topic_dedup_key, include_inactive=True)
            if existing_exact is not None:
                continue

            existing_results = await self.retrieval_service.retrieve(
                MemoryQuery(
                    query=summary,
                    session_id=str(session_id or ""),
                    persona_id=str(persona_id or ""),
                    layers=["memory", "topic", "event"],
                    top_k=1,
                    metadata={"visibility_mode": "tool"},
                )
            )
            merged = False
            if existing_results:
                existing_doc = existing_results[0]
                existing_text = existing_doc.content
                if self._compute_text_similarity(summary, existing_text) > 0.85:
                    merged_summary = f"{existing_text}\nSupplement: {summary}"
                    if len(merged_summary) > len(existing_text) * 2:
                        merged_summary = merged_summary[: len(existing_text) * 2] + "..."
                    new_id = await self.write_service.write(
                        MemoryWriteRequest(
                            source="topic_summarizer",
                            kind=existing_doc.kind or "topic",
                            session_id=str(session_id or ""),
                            persona_id=str(persona_id or ""),
                            content=merged_summary,
                            summary=merged_summary[:240],
                            tags=list(existing_doc.tags or []),
                            importance=0.85,
                            confidence=max(existing_doc.confidence, 0.6),
                            metadata={
                                "merged_from": [existing_doc.id],
                                "topic_result": dict(topic_result or {}),
                                "valid_until": time.time() + (30 * 86400),
                            },
                            dedup_key=(
                                f"topic_merged:{session_id}:"
                                f"{hashlib.sha256(merged_summary.encode('utf-8')).hexdigest()[:24]}"
                            ),
                            source_ref="summarizer.topic_merge",
                        )
                    )
                    if new_id:
                        await self.maintenance_service.mark_merged([existing_doc.id], superseded_by=new_id)
                    logger.info(f"[MemoryEngine] merged similar topic memory: {summary[:20]}...")
                    merged = True

            if not merged:
                importance = float(topic_result.get("importance", 0.4) or 0.4)
                try:
                    confidence = float(topic_result.get("confidence", 0.6) or 0.6)
                except (TypeError, ValueError):
                    confidence = 0.6
                confidence = max(0.45, min(0.75, confidence))
                await self.write_service.write(
                    MemoryWriteRequest(
                        source="topic_summarizer",
                        kind="topic",
                        session_id=str(session_id or ""),
                        persona_id=str(persona_id or ""),
                        content=str(summary or ""),
                        summary=str(summary or "")[:240],
                        tags=[str(item) for item in topic_result.get("topic_keywords", []) or []],
                        importance=importance,
                        confidence=confidence,
                        metadata={
                            "topic_result": dict(topic_result or {}),
                            "valid_until": time.time() + (14 * 86400),
                            "topic_scope": "session",
                        },
                        dedup_key=topic_dedup_key,
                        source_ref="summarizer.topic",
                    )
                )
