import asyncio
import time
import math
from typing import List, Dict, Any, Optional
from astrbot.api import logger

from .bm25 import BM25Retriever
from .vector_store import VectorRetriever
from .utils import RRFFusion, SearchResult

class HybridRetriever:
    """
    混合检索器 (System 2 Memory)
    升级：透传 session_id 和 persona_id 隔离条件。
    """
    def __init__(self, bm25: BM25Retriever, vector: VectorRetriever, config=None):
        self.bm25 = bm25
        self.vector = vector
        self.rrf = RRFFusion()
        self.config = config

    async def add_memory(self, content: str, metadata: Dict[str, Any]) -> int:
        # 1. 存入向量库获取统一主键 doc_id
        doc_id = await self.vector.add_document(content, metadata)
        
        # 2. 将关联 ID 存入 BM25 辅库实现双路绑定
        await self.bm25.add_document(doc_id, content)
        
        return doc_id

    async def search(self, query: str, k: int = 10, session_id: Optional[str] = None, persona_id: Optional[str] = None) -> List[SearchResult]:
        # 并行发起带有过滤参数的检索
        results = await asyncio.gather(
            self.bm25.search(query, k=k*2, session_id=session_id, persona_id=persona_id),
            self.vector.search(query, k=k*2, session_id=session_id, persona_id=persona_id),
            return_exceptions=True
        )
        
        bm25_res = results[0] if not isinstance(results[0], Exception) else []
        vec_res = results[1] if not isinstance(results[1], Exception) else []
        
        if isinstance(results[0], Exception):
            logger.error(f"[Hybrid] BM25 查询异常: {results[0]}")
        if isinstance(results[1], Exception):
            logger.error(f"[Hybrid] Vector 查询异常: {results[1]}")
            
        if not bm25_res and not vec_res:
            return []
            
        # RRF 融合
        fused = self.rrf.fuse(bm25_res, vec_res, top_k=k)
        
        # 时间衰减加权
        final_results = self._apply_weighting(fused)
        return final_results

    def _apply_weighting(self, results: List[SearchResult]) -> List[SearchResult]:
        now = time.time()
        time_decay_rate = getattr(self.config.memory, 'time_decay_rate', 0.01) if self.config else 0.01
        
        for r in results:
            meta = r.metadata
            if isinstance(meta, str):
                import json
                try:
                    meta = json.loads(meta)
                except:
                    meta = {}
                    
            create_time = meta.get("create_time", now)
            importance = meta.get("importance", 0.5)
            
            # 物理衰减公式: score * importance * e^(-lambda * days)
            days_old = (now - create_time) / 86400
            decay = math.exp(-time_decay_rate * days_old)
            
            r.score = r.score * importance * decay
            r.metadata = meta
            
        # 重排
        results.sort(key=lambda x: x.score, reverse=True)
        return results