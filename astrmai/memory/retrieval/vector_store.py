import asyncio
import json
import time
from typing import List, Dict, Any, Optional
from astrbot.api import logger
from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
from ..utils import SearchResult, TextProcessor

class VectorRetriever:
    """
    向量密集检索器 (基于 AstrBot FaissVecDB 原生底座)
    完全重构：废弃脆弱的本地 bin 文件维护，全面接入平台提供的一致性存储。
    """
    def __init__(self, faiss_db: FaissVecDB, config=None):
        self.faiss_db = faiss_db
        self.processor = TextProcessor()
        self.config = config or {}
        # ID 映射缓存优化 (int_id -> uuid)
        self._id_cache: Dict[int, str] = {}
        self._cache_max_size = 1000
        self._failure_count = 0
        self._unavailable_until = 0.0

    def _timing_value(self, name: str, default: float) -> float:
        timing = getattr(self.config, "timing", None)
        if timing is None and isinstance(self.config, dict):
            timing = self.config.get("timing")
        value = getattr(timing, name, None) if timing is not None else None
        if value is None and isinstance(timing, dict):
            value = timing.get(name)
        try:
            return float(value if value is not None else default)
        except (TypeError, ValueError):
            return float(default)

    def _failure_threshold(self) -> int:
        return max(1, int(self._timing_value("faiss_failure_threshold", 3.0)))

    def _circuit_open(self) -> bool:
        return time.monotonic() < self._unavailable_until

    def _mark_failure(self, reason: str) -> None:
        self._failure_count += 1
        if self._failure_count >= self._failure_threshold():
            cooldown = max(5.0, self._timing_value("faiss_circuit_breaker_cooldown_sec", 30.0))
            self._unavailable_until = time.monotonic() + cooldown
            logger.warning(
                f"[VectorStore] Faiss circuit opened: failures={self._failure_count} "
                f"cooldown_sec={cooldown:.1f} reason={reason}"
            )

    def _mark_success(self) -> None:
        self._failure_count = 0
        self._unavailable_until = 0.0

    @staticmethod
    def _normalize_metadata(raw_metadata: Any) -> Dict[str, Any]:
        if isinstance(raw_metadata, dict):
            return dict(raw_metadata)
        if isinstance(raw_metadata, str):
            try:
                parsed = json.loads(raw_metadata)
            except (json.JSONDecodeError, TypeError, ValueError):
                logger.warning("[VectorStore] ignoring malformed JSON metadata from Faiss result")
                return {}
            if isinstance(parsed, dict):
                return parsed
            logger.warning("[VectorStore] ignoring non-object JSON metadata from Faiss result")
        return {}

    async def add_document(self, content: str, metadata: Dict[str, Any] = None) -> int:
        """存入文本，返回 document id (由 FaissVecDB 底层的 DocumentStorage 提供的主键)"""
        metadata = metadata or {}
        
        # 补充默认字段
        if "importance" not in metadata:
            metadata["importance"] = 0.5
        if "create_time" not in metadata:
            metadata["create_time"] = time.time()
        if "last_access_time" not in metadata:
            metadata["last_access_time"] = time.time()
            
        # 直接使用原生 faiss_db 的 insert
        doc_id = await self.faiss_db.insert(content=content, metadata=metadata)
        return doc_id

    async def search(self, query: str, k: int = 10, session_id: Optional[str] = None, persona_id: Optional[str] = None) -> List[SearchResult]:
        """执行向量相似度搜索"""
        if not query or not query.strip():
            return []
        if self._circuit_open():
            logger.warning("[VectorStore] Faiss circuit open; using lexical fallback")
            return []
            
        # 预处理查询
        tokens = self.processor.tokenize(query)
        processed_query = " ".join(tokens) if tokens else query

        # 构建元数据过滤器
        metadata_filters = {}
        if session_id is not None:
            metadata_filters["session_id"] = session_id
        if persona_id is not None:
            metadata_filters["persona_id"] = persona_id

        fetch_k = k * 2 if metadata_filters else k

        # 执行原生检索
        try:
            faiss_results = await asyncio.wait_for(
                self.faiss_db.retrieve(
                    query=processed_query,
                    k=k,
                    fetch_k=fetch_k,
                    rerank=False,
                    metadata_filters=metadata_filters if metadata_filters else None,
                ),
                timeout=max(0.5, self._timing_value("faiss_timeout_sec", 4.0)),
            )
            self._mark_success()
        except asyncio.TimeoutError:
            self._mark_failure("timeout")
            logger.warning("[VectorStore] Faiss search timed out; using lexical fallback")
            return []
        except Exception as e:
            self._mark_failure(type(e).__name__)
            logger.error(f"[VectorStore] Faiss 原生检索异常: {e}")
            return []

        out = []
        for result in faiss_results:
            doc_data = getattr(result, "data", {}) or {}
            doc_id = doc_data.get("id")
            content = doc_data.get("text")
            if doc_id is None or content is None:
                logger.warning(f"[VectorStore] skipping malformed Faiss result: {doc_data}")
                continue
            out.append(SearchResult(
                doc_id=doc_id,
                score=float(getattr(result, "similarity", 0.0) or 0.0),
                content=content,
                metadata=self._normalize_metadata(doc_data.get("metadata")),
                source="vector"
            ))
            
        return out
