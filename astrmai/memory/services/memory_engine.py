import aiosqlite
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


@dataclass(slots=True)
class CognitiveFeedbackSignal:
    source: str
    chat_id: str
    summary: str
    guidance: str
    tags: list[str] = field(default_factory=list)
    timestamp: float = 0.0
    importance: float = 0.5


class MemoryEngine:
    """Refactored memory engine with lazy vector bootstrap and stable facade methods."""

    def __init__(self, context, gateway, embedding_models: list = None, config=None):
        self.context = context
        self.gateway = gateway
        self.config = config if config else gateway.config
        if hasattr(self.config, "provider") and getattr(self.config.provider, "embedding_models", None):
            self.embedding_models = self.config.provider.embedding_models
        else:
            self.embedding_models = embedding_models or []

        self.data_path = Path(get_astrbot_data_path()) / "plugin_data" / "astrmai" / "memory"
        os.makedirs(self.data_path, exist_ok=True)
        self.db_path = str(self.data_path / "docs.db")

        self.faiss_db = None
        self.vec_retriever = None
        self.bm25_retriever = None
        self.retriever = None
        self.summarizer = None

        self._is_ready = False
        self._init_failures = 0
        self._next_retry_time = 0.0
        self._learning_event_history = []
        self._cognitive_feedback_cache: dict[str, list[CognitiveFeedbackSignal]] = {}
        self._disabled_cognitive_feedback_keys: set[tuple[str, str, str, str]] = set()

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

    async def _run_documents_query(self, query: str, params: tuple = ()) -> list:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(query, params)
            return await cursor.fetchall()

    async def _execute_documents_write(self, query: str, params: tuple = ()) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(query, params)
            await db.commit()
            return cursor.rowcount

    async def _search_memories(self, query: str, *, top_k: int, session_id: str = None, persona_id: str = None):
        if not await self._ensure_faiss_initialized():
            return []
        return await self.retriever.search(query, k=top_k, session_id=session_id, persona_id=persona_id)

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

        try:
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

        if not self.bm25_retriever:
            self.bm25_retriever = BM25Retriever(self.db_path)
            await self.bm25_retriever.initialize()

        self.vec_retriever = VectorRetriever(self.faiss_db, self.config)
        self.retriever = HybridRetriever(self.bm25_retriever, self.vec_retriever, config=self.config)
        self._is_ready = True
        self._init_failures = 0
        logger.info("[AstrMai] hybrid memory engine ready (BM25 + FaissVecDB).")
        return True

    async def add_memory(self, content: str, session_id: str, persona_id: str = None, importance: float = 0.8):
        if not await self._ensure_faiss_initialized():
            return
        metadata = self._build_memory_metadata(session_id=session_id, persona_id=persona_id, importance=importance)
        await self.retriever.add_memory(content, metadata)

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
        return "\n".join(lines).strip()

    @classmethod
    def _parse_cognitive_feedback_content(
        cls,
        text: str,
        *,
        chat_id: str,
        timestamp: float = 0.0,
        importance: float = 0.5,
    ) -> CognitiveFeedbackSignal | None:
        content = str(text or "").strip()
        if not content.startswith("[cognitive_feedback:"):
            return None
        first_line, *rest = content.splitlines()
        source = first_line.removeprefix("[cognitive_feedback:").removesuffix("]").strip() or "unknown"
        summary = ""
        guidance = ""
        tags: list[str] = []
        for line in rest:
            if line.startswith("summary:"):
                summary = line.split(":", 1)[1].strip()
            elif line.startswith("guidance:"):
                guidance = line.split(":", 1)[1].strip()
            elif line.startswith("tags:"):
                tags = cls._normalize_feedback_tags(line.split(":", 1)[1].split(","))
        if not summary and not guidance:
            return None
        return CognitiveFeedbackSignal(
            source=source,
            chat_id=chat_id,
            summary=summary,
            guidance=guidance,
            tags=tags,
            timestamp=float(timestamp or 0.0),
            importance=float(importance or 0.5),
        )

    def _remember_cognitive_feedback(self, signal: CognitiveFeedbackSignal) -> None:
        items = [*self._cognitive_feedback_cache.get(signal.chat_id, []), signal]
        self._cognitive_feedback_cache[signal.chat_id] = items[-32:]

    @staticmethod
    def _cognitive_feedback_key(signal: CognitiveFeedbackSignal) -> tuple[str, str, str, str]:
        return (
            str(signal.chat_id or ""),
            str(signal.source or ""),
            str(signal.summary or ""),
            str(signal.guidance or ""),
        )

    def disable_cognitive_feedback(self, signal: CognitiveFeedbackSignal) -> None:
        self._disabled_cognitive_feedback_keys.add(self._cognitive_feedback_key(signal))

    async def record_cognitive_feedback(
        self,
        session_id: str,
        source: str,
        summary: str,
        guidance: str = "",
        tags: list[str] | None = None,
        importance: float = 0.5,
    ) -> None:
        chat_id = str(session_id or "").strip()
        clean_summary = str(summary or "").strip()
        clean_guidance = str(guidance or "").strip()
        if not chat_id or not (clean_summary or clean_guidance):
            return
        clean_source = str(source or "unknown").strip().lower() or "unknown"
        clean_tags = self._normalize_feedback_tags(tags)
        now = time.time()
        signal = CognitiveFeedbackSignal(
            source=clean_source,
            chat_id=chat_id,
            summary=clean_summary[:500],
            guidance=clean_guidance[:500],
            tags=clean_tags,
            timestamp=now,
            importance=float(importance or 0.5),
        )
        self._remember_cognitive_feedback(signal)

        if not await self._ensure_faiss_initialized():
            return
        content = self._format_cognitive_feedback_content(
            source=signal.source,
            summary=signal.summary,
            guidance=signal.guidance,
            tags=signal.tags,
        )
        metadata = self._build_memory_metadata(
            session_id=chat_id,
            importance=signal.importance,
            feedback_source=signal.source,
            cognitive_feedback=True,
            tags=json.dumps(signal.tags, ensure_ascii=False),
        )
        await self.retriever.add_memory(content, metadata)

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
            columns = [row[1] for row in await self._run_documents_query("PRAGMA table_info(documents)")]
            if columns:
                text_col = "page_content" if "page_content" in columns else ("content" if "content" in columns else "text")
                where = [
                    "json_extract(metadata, '$.session_id') = ?",
                    f"{text_col} LIKE '[cognitive_feedback:%'",
                ]
                params: list[Any] = [chat_id]
                if max_age_seconds is not None:
                    where.append("json_extract(metadata, '$.create_time') >= ?")
                    params.append(now - max_age_seconds)
                rows = await self._run_documents_query(
                    f"""
                    SELECT {text_col}, metadata
                    FROM documents
                    WHERE {' AND '.join(where)}
                    ORDER BY COALESCE(json_extract(metadata, '$.create_time'), 0) DESC
                    LIMIT ?
                    """,
                    (*params, max(limit * 4, limit)),
                )
                for text, metadata_raw in rows:
                    timestamp = 0.0
                    importance = 0.5
                    try:
                        metadata = json.loads(metadata_raw or "{}") if isinstance(metadata_raw, str) else {}
                        timestamp = float(metadata.get("create_time") or 0.0)
                        importance = float(metadata.get("importance") or 0.5)
                    except Exception:
                        pass
                    parsed = self._parse_cognitive_feedback_content(
                        str(text or ""),
                        chat_id=chat_id,
                        timestamp=timestamp,
                        importance=importance,
                    )
                    if not parsed:
                        continue
                    if source_filter and parsed.source not in source_filter:
                        continue
                    signals.append(parsed)
        except Exception as exc:
            logger.debug(f"[MemoryEngine] cognitive feedback lookup degraded: {exc}")

        unique: list[CognitiveFeedbackSignal] = []
        seen: set[tuple[str, str, str]] = set()
        for item in sorted(signals, key=lambda signal: signal.timestamp, reverse=True):
            if self._cognitive_feedback_key(item) in self._disabled_cognitive_feedback_keys:
                continue
            key = (item.source, item.summary, item.guidance)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
            if len(unique) >= limit:
                break
        return unique

    async def clear_persona_lore(self, persona_id: str = None) -> int:
        if not await self._ensure_faiss_initialized():
            return 0
        try:
            query = "DELETE FROM documents WHERE json_extract(metadata, '$.session_id') = '__self_lore__'"
            params: list[Any] = []
            if persona_id:
                query += " AND json_extract(metadata, '$.persona_id') = ?"
                params.append(persona_id)
            deleted = await self._execute_documents_write(query, tuple(params))
            logger.info(f"[MemoryEngine] persona lore cleared: {deleted} rows (persona={persona_id})")
            return deleted
        except Exception as exc:
            logger.error(f"[MemoryEngine] clear persona lore failed: {exc}")
            return 0

    async def add_persona_lore(self, content: str, persona_id: str = None):
        if not await self._ensure_faiss_initialized():
            return
        from ...conversation.execution.text_segmenter import TextSegmenter

        chunks = TextSegmenter.semantic_chunk(content, max_chunk_size=800)
        logger.info(f"[MemoryEngine] storing {len(chunks)} persona lore chunks.")
        for index, chunk in enumerate(chunks):
            metadata = self._build_memory_metadata(
                session_id="__self_lore__",
                persona_id=persona_id,
                importance=1.0,
                chunk_index=index,
            )
            await self.retriever.add_memory(chunk, metadata)

    async def recall_persona_lore(self, query: str, persona_id: str = None, top_k: int = 3) -> str:
        if not await self._ensure_faiss_initialized():
            return "(persona lore offline)"

        results = await self._search_memories(
            query,
            top_k=top_k,
            session_id="__self_lore__",
            persona_id=persona_id,
        )
        valid_results = self._filter_search_results(results, min_score=0.05)
        if not valid_results:
            return "(no relevant persona lore found)"
        return "\n".join(f"[fact] {item.content}" for item in valid_results)

    async def recall(self, query: str, session_id: str = None, persona_id: str = None, top_k: int = None) -> str:
        if not await self._ensure_faiss_initialized():
            return "(memory offline)"

        recall_top_k = top_k if top_k is not None else getattr(self.config.memory, "recall_top_k", 5)
        results = await self._search_memories(
            query,
            top_k=recall_top_k,
            session_id=session_id,
            persona_id=persona_id,
        )
        valid_results = self._filter_search_results(results, min_score=0.02)
        valid_results = [
            item
            for item in valid_results
            if not self._is_cognitive_feedback_content(getattr(item, "content", ""))
        ]
        if not valid_results:
            return f"No relevant memory found for '{query}'."

        retrieved_memory = self._format_bullet_memories(valid_results)
        logger.info(f"[Memory] recall matched {len(valid_results)} high-relevance fragments.")
        return (
            f"Relevant memory about '{query}':\n"
            f"{retrieved_memory}\n"
            "(use these memories naturally in the follow-up reply)"
        )

    async def start_background_tasks(self):
        from .summarizer import ChatHistorySummarizer

        self.summarizer = ChatHistorySummarizer(self.context, self.gateway, self, config=self.config)
        await self.summarizer.start()

    async def apply_daily_decay(self, decay_rate: float, days: int = 1) -> int:
        await self._ensure_faiss_initialized()
        decay_factor = (1 - decay_rate) ** days
        try:
            return await self._execute_documents_write(
                """
                UPDATE documents
                SET metadata = json_set(
                    metadata,
                    '$.importance',
                    MAX(0.01, ROUND(
                        COALESCE(json_extract(metadata, '$.importance'), 0.5) * ?, 4
                    ))
                )
                WHERE (json_extract(metadata, '$.importance') IS NOT NULL
                   OR metadata LIKE '%"importance"%')
                  AND COALESCE(json_extract(metadata, '$.session_id'), '') != '__self_lore__'
                """,
                (decay_factor,),
            )
        except Exception as exc:
            logger.error(f"[Memory] daily decay SQL failed: {exc}")
            return 0

    async def get_recent_memories(self, session_id: str, hours: int = 24) -> list:
        if not await self._ensure_faiss_initialized():
            return []

        recent_memories: list[str] = []
        cutoff_time = time.time() - (hours * 3600)
        try:
            columns = [row[1] for row in await self._run_documents_query("PRAGMA table_info(documents)")]
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
        if not await self._ensure_faiss_initialized():
            return 0
        try:
            deleted_rows = await self._execute_documents_write(
                """
                DELETE FROM documents
                WHERE json_extract(metadata, '$.importance') IS NOT NULL
                  AND CAST(json_extract(metadata, '$.importance') AS REAL) < ?
                """,
                (threshold,),
            )
            if deleted_rows > 0:
                logger.info(
                    f"[MemoryEngine] pruned {deleted_rows} low-importance memory rows below threshold {threshold}."
                )
            return deleted_rows
        except Exception as exc:
            logger.error(f"[MemoryEngine] prune low importance failed: {exc}")
            return 0

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
        if not await self._ensure_faiss_initialized():
            return

        for topic_result in topic_results:
            summary = topic_result.get("summary", "")
            if not summary or summary == "topic too short":
                continue

            existing_results = await self.retriever.search(summary, k=1, session_id=session_id, persona_id=persona_id)
            merged = False
            if existing_results:
                existing_doc = existing_results[0]
                existing_text = existing_doc.content
                if self._compute_text_similarity(summary, existing_text) > 0.85:
                    merged_summary = f"{existing_text}\nSupplement: {summary}"
                    if len(merged_summary) > len(existing_text) * 2:
                        merged_summary = merged_summary[: len(existing_text) * 2] + "..."
                    await self.add_memory(merged_summary, session_id=session_id, persona_id=persona_id, importance=0.85)
                    logger.info(f"[MemoryEngine] merged similar topic memory: {summary[:20]}...")
                    merged = True

            if not merged:
                importance = float(topic_result.get("importance", 0.4) or 0.4)
                await self.add_memory(summary, session_id=session_id, persona_id=persona_id, importance=importance)
