### 📄 services/memory/vector_retriever.py
from typing import Any, List, Dict
from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
from .text_processor import TextProcessor

class VectorRetriever:
    def __init__(self, faiss_db: FaissVecDB, text_processor: TextProcessor):
        self.faiss_db = faiss_db
        self.text_processor = text_processor

    async def add_document(self, content: str, metadata: Dict[str, Any]) -> int:
        # metadata 必须包含: session_id, create_time 等
        return await self.faiss_db.insert(content=content, metadata=metadata)

    async def search(self, query: str, k: int = 10, filters: Dict = None):
        # 预处理查询
        tokens = self.text_processor.tokenize(query)
        processed_query = " ".join(tokens) if tokens else query
        
        results = await self.faiss_db.retrieve(
            query=processed_query,
            k=k,
            metadata_filters=filters
        )
        return results # 返回 AstrBot 的 Result 对象列表

    async def delete_document(self, doc_id: int):
        # 注意：这里简化处理，实际需要通过 uuid 删除
        # HeartCore 暂时不通过 ID 删除，而是依赖过期淘汰
        pass