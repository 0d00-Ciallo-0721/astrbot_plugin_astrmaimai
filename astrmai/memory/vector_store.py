import time
from typing import List, Dict, Any
from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
from .utils import SearchResult, TextProcessor

class VectorRetriever:
    """
    向量检索器 (Faiss Wrapper)
    Reference: LivingMemory/core/retrieval/vector_retriever.py
    """
    def __init__(self, faiss_db: FaissVecDB):
        self.db = faiss_db
        self.processor = TextProcessor()

    async def add_document(self, content: str, metadata: Dict[str, Any]) -> int:
        """返回 document id (int)"""
        # 补全元数据
        if "create_time" not in metadata:
            metadata["create_time"] = time.time()
            
        doc_id = await self.db.insert(content=content, metadata=metadata)
        return doc_id

    async def search(self, query: str, k: int = 20) -> List[SearchResult]:
        # 预处理查询
        tokens = self.processor.tokenize(query)
        processed_query = " ".join(tokens) if tokens else query
        
        # 检索
        # fetch_k = k * 2
        results = await self.db.retrieve(query=processed_query, k=k, fetch_k=k*2)
        
        out = []
        for r in results:
            data = r.data
            out.append(SearchResult(
                doc_id=data["id"],
                score=r.similarity, # 已归一化 0-1
                content=data["text"],
                metadata=data["metadata"],
                source="vector"
            ))
        return out