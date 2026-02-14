### 📄 services/memory/memory_engine.py
import aiosqlite
import time
from pathlib import Path
from typing import List, Dict
from astrbot.api import logger
from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB

from .text_processor import TextProcessor
from .bm25_retriever import BM25Retriever
from .vector_retriever import VectorRetriever
from .hybrid_retriever import HybridRetriever
from .rrf_fusion import RRFFusion

class MemoryEngine:
    """记忆存储引擎：管理 SQLite 和 VectorDB"""
    
    def __init__(self, data_dir: str, faiss_db: FaissVecDB):
        self.data_dir = data_dir
        self.db_path = str(Path(data_dir) / "memory.db")
        self.faiss_db = faiss_db
        
        # 初始化组件
        self.processor = TextProcessor()
        self.rrf = RRFFusion()
        self.bm25 = BM25Retriever(self.db_path, self.processor)
        self.vector = VectorRetriever(faiss_db, self.processor)
        self.hybrid = HybridRetriever(self.bm25, self.vector, self.rrf)

    async def initialize(self):
        """初始化数据库"""
        await self.bm25.initialize()
        # 确保 documents 表存在 (LivingMemory 逻辑)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            )
            """)
            await db.commit()

    async def add_memory(self, content: str, metadata: Dict) -> int:
        return await self.hybrid.add_memory(content, metadata)

    async def search(self, query: str, k: int = 5, session_id: str = None) -> List[Dict]:
        """
        对外搜索接口：返回最终的字典列表
        """
        results = await self.hybrid.search(query, k, session_id)
        
        final_memories = []
        for res in results:
            content = res.content
            meta = res.metadata
            
            # 如果是 BM25 命中，内容可能为空，需要从 DB 补全
            if not content:
                content, meta = await self._fetch_doc(res.doc_id)
            
            if content:
                final_memories.append({
                    "content": content,
                    "score": res.rrf_score,
                    "metadata": meta
                })
        return final_memories

    async def _fetch_doc(self, doc_id: int):
        import json
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT text, metadata FROM documents WHERE id = ?", (doc_id,))
            row = await cursor.fetchone()
            if row:
                try:
                    meta = json.loads(row[1])
                except:
                    meta = {}
                return row[0], meta
        return None, {}