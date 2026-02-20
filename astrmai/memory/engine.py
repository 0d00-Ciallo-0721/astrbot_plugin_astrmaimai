import aiosqlite
import os
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB

from .bm25 import BM25Retriever
from .vector_store import VectorRetriever
from .retriever import HybridRetriever

class MemoryEngine:
    """
    统一记忆引擎 (Infrastructure Layer)
    Reference: LivingMemory/core/managers/memory_engine.py
    """
    def __init__(self):
        # 路径配置
        self.data_path = get_astrbot_data_path() / "plugin_data" / "astrmai" / "memory"
        if not os.path.exists(self.data_path):
            os.makedirs(self.data_path)
            
        self.db_path = str(self.data_path / "memory.db")
        
        # 初始化组件占位
        self.vec_db = None
        self.retriever = None

    async def initialize(self):
        """初始化所有子系统"""
        # 1. 确保 documents 表存在 (供 BM25 和元数据使用)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            )
            """)
            await db.commit()
            
        # 2. 初始化向量库 (AstrBot Native)
        # 注意: FaissVecDB 需要 path 指向存储目录
        self.vec_db = FaissVecDB(
            path=str(self.data_path),
            embedding_dim=768 # 假设使用 standard embedding
        )
        # FaissVecDB 通常会自动加载，如果需要显式 load:
        # await self.vec_db.load() 
        
        # 3. 初始化检索器
        bm25 = BM25Retriever(self.db_path)
        await bm25.initialize()
        
        vec_retriever = VectorRetriever(self.vec_db)
        
        self.retriever = HybridRetriever(bm25, vec_retriever)
        logger.info("[AstrMai] Memory Engine Initialized (Hybrid RAG Ready)")

    async def add_memory(self, content: str, session_id: str):
        if not self.retriever: return
        await self.retriever.add_memory(content, {
            "session_id": session_id,
            "importance": 0.8 # 默认重要性
        })

    async def recall(self, query: str, session_id: str) -> str:
        """RAG 回调接口"""
        if not self.retriever: return ""
        
        results = await self.retriever.search(query, k=3)
        # 过滤 session (简单过滤)
        # 实际应在 VectorRetriever 层面过滤，这里做二次筛选
        valid_results = [r for r in results if r.metadata.get("session_id") == session_id]
        
        if not valid_results: return ""
        
        context = "\n".join([f"- {r.content}" for r in valid_results])
        return f"相关记忆:\n{context}"