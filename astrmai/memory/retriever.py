import asyncio
import time
import math
from typing import List, Dict, Any
from astrbot.api import logger

from .bm25 import BM25Retriever
from .vector_store import VectorRetriever
from .utils import RRFFusion, SearchResult

class HybridRetriever:
    """
    混合检索器 (System 2 Memory)
    结合 BM25 + Vector + RRF
    Reference: LivingMemory/core/retrieval/hybrid_retriever.py
    """
    def __init__(self, bm25: BM25Retriever, vector: VectorRetriever, config=None):
        self.bm25 = bm25
        self.vector = vector
        self.rrf = RRFFusion()
        self.config = config

    async def add_memory(self, content: str, metadata: Dict[str, Any]) -> int:
        # 1. 存入向量库 (主库)
        doc_id = await self.vector.add_document(content, metadata)
        
        # 2. 存入 BM25 索引 (辅库)
        await self.bm25.add_document(doc_id, content)
        
        return doc_id

    async def search(self, query: str, k: int = 10) -> List[SearchResult]:
        # 并行检索
        results = await asyncio.gather(
            self.bm25.search(query, k=k*2),
            self.vector.search(query, k=k*2),
            return_exceptions=True
        )
        
        bm25_res = results[0] if not isinstance(results[0], Exception) else []
        vec_res = results[1] if not isinstance(results[1], Exception) else []
        
        if not bm25_res and not vec_res:
            return []
            
        # RRF 融合
        fused = self.rrf.fuse(bm25_res, vec_res, top_k=k)
        
        # 时间衰减加权
        final_results = self._apply_weighting(fused)
        return final_results

    def _apply_weighting(self, results: List[SearchResult]) -> List[SearchResult]:
        now = time.time()
        
        # 接入 Config (时间衰减率)
        time_decay_rate = getattr(self.config.memory, 'time_decay_rate', 0.01) if self.config else 0.01
        
        for r in results:
            meta = r.metadata
            create_time = meta.get("create_time", now)
            importance = meta.get("importance", 0.5)
            
            # 衰减公式: score * importance * e^(-lambda * days)
            days_old = (now - create_time) / 86400
            decay = math.exp(-time_decay_rate * days_old)
            
            r.score = r.score * importance * decay
            
        # 重新排序
        results.sort(key=lambda x: x.score, reverse=True)
        return results