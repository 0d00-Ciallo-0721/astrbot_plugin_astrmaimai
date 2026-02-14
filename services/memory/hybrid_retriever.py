### 📄 services/memory/hybrid_retriever.py
import asyncio
import time
import math
from typing import List, Dict
from astrbot.api import logger
from .bm25_retriever import BM25Retriever
from .vector_retriever import VectorRetriever
from .rrf_fusion import RRFFusion, BM25Result, VectorResult, FusedResult

class HybridRetriever:
    def __init__(self, bm25: BM25Retriever, vector: VectorRetriever, rrf: RRFFusion):
        self.bm25 = bm25
        self.vector = vector
        self.rrf = rrf

    async def add_memory(self, content: str, metadata: Dict) -> int:
        # 1. 存入向量库，获取 ID
        doc_id = await self.vector.add_document(content, metadata)
        # 2. 存入 BM25 索引
        await self.bm25.add_document(doc_id, content)
        return doc_id

    async def search(self, query: str, k: int = 5, session_id: str = None) -> List[FusedResult]:
        # 1. 并行检索
        filters = {"session_id": session_id} if session_id else None
        
        # 定义任务
        async def run_bm25():
            raw_res = await self.bm25.search(query, k * 2)
            # 补全内容需要查 DB，这里为了性能简化，假设 fetch_content 在外部或后续做
            # 在 LivingMemory 原版中，这里会查 documents 表。
            # 为了简化 HeartCore，我们只返回 ID，内容由 MemoryEngine 统一获取
            return [BM25Result(doc_id=r[0], score=r[1], content="", metadata={}) for r in raw_res]

        async def run_vector():
            raw_res = await self.vector.search(query, k * 2, filters)
            return [VectorResult(
                doc_id=r.data['id'], 
                score=r.similarity, 
                content=r.data['text'], 
                metadata=r.data['metadata']
            ) for r in raw_res]

        res_bm25, res_vector = await asyncio.gather(run_bm25(), run_vector())

        # 2. RRF 融合
        fused = self.rrf.fuse(res_bm25, res_vector, k)
        
        # 3. 补全内容 (如果 BM25 命中了但 Vector 没命中，fused.content 为空)
        # 这一步将在 MemoryEngine 中处理，或者通过 VectorDB 反查
        return fused