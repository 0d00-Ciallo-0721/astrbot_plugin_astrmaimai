import aiosqlite
import os
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
from pathlib import Path

from .bm25 import BM25Retriever
from .vector_store import VectorRetriever
from .retriever import HybridRetriever

class MemoryEngine:
    """
    统一记忆引擎 (Infrastructure Layer)
    Reference: LivingMemory/core/managers/memory_engine.py
    """
    def __init__(self, context, gateway, embedding_provider_id: str = ""):
        self.context = context
        self.gateway = gateway
        self.embedding_provider_id = embedding_provider_id # 新增：接收配置 ID
        
        # 路径配置
        self.data_path = get_astrbot_data_path() / "plugin_data" / "astrmai" / "memory"
        if not os.path.exists(self.data_path):
            os.makedirs(self.data_path)
            
        self.db_path = str(self.data_path / "memory.db")
        
        # 初始化组件占位
        self.vec_retriever = None
        self.retriever = None
        self.summarizer = None # 记忆清道夫

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
            
        # 2. 初始化标准化的 Embedding 客户端 (包含兜底逻辑)
        emb_client = EmbeddingClient(self.context, self.embedding_provider_id)
        
        # 3. 初始化自定义向量检索器 (不再依赖原生 FaissVecDB)
        self.vec_retriever = VectorRetriever(
            data_path=str(self.data_path),
            embedding_client=emb_client
        )
        
        # 4. 初始化 BM25 检索器
        bm25 = BM25Retriever(self.db_path)
        await bm25.initialize()
        
        self.retriever = HybridRetriever(bm25, self.vec_retriever)
        logger.info("[AstrMai] Memory Engine Initialized (Standardized Embedding RAG Ready)")

    async def add_memory(self, content: str, session_id: str):
        if not self.retriever: return
        await self.retriever.add_memory(content, {
            "session_id": session_id,
            "importance": 0.8 # 默认重要性
        })

    async def recall(self, query: str, session_id: str) -> str:
        """RAG 回调接口: 将记忆组装为 System 2 友好的上下文"""
        if not self.retriever: 
            return "（记忆模块离线）"
        
        # 检索最相关的 5 条记忆片段
        results = await self.retriever.search(query, k=5)
        
        if not results:
            logger.debug(f"[Memory] 未找到关于 '{query}' 的相关记忆。")
            return f"你努力在记忆中搜索关于 '{query}' 的事情，但是什么也没想起来。"

        all_results = []
        for r in results:
            all_results.append(f"- {r.content}")

        retrieved_memory = "\n".join(all_results)
        logger.info(f"[Memory] 记忆闪回成功，耗时极短，共检索到 {len(results)} 条相关记忆。")
        
        return f"你突然回忆起了以下关于 '{query}' 的相关信息：\n{retrieved_memory}\n（请在后续的回复中，根据当前语境自然地参考这些记忆）"
    
    async def start_background_tasks(self):
        """启动后台记忆清道夫任务 (Task Scheduler)"""
        from .summarizer import ChatHistorySummarizer
        self.summarizer = ChatHistorySummarizer(self.context, self.gateway, self)
        await self.summarizer.start()