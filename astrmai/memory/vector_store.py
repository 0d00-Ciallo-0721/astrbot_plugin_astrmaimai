import time
from typing import List, Dict, Any, Optional
from astrbot.api import logger
from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
from .utils import SearchResult, TextProcessor

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
            faiss_results = await self.faiss_db.retrieve(
                query=processed_query,
                k=k,
                fetch_k=fetch_k,
                rerank=False,
                metadata_filters=metadata_filters if metadata_filters else None,
            )
        except Exception as e:
            logger.error(f"[VectorStore] Faiss 原生检索异常: {e}")
            return []

        out = []
        for result in faiss_results:
            doc_data = result.data
            out.append(SearchResult(
                doc_id=doc_data["id"],
                score=result.similarity, # 原生引擎已归一化
                content=doc_data["text"],
                metadata=doc_data["metadata"],
                source="vector"
            ))
            
        return out