import aiosqlite
import os
import time
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
    重构版：加入延迟唤醒 (Lazy Load) 机制，彻底解决启动时的 Provider 生命期时序问题。
    """
    def __init__(self, context, gateway, embedding_provider_id: str = "", config=None):
        self.context = context
        self.gateway = gateway
        self.config = config if config else gateway.config
        
        if hasattr(self.config, 'provider') and getattr(self.config.provider, 'embedding_provider_id', None):
            self.embedding_provider_id = self.config.provider.embedding_provider_id
        else:
            self.embedding_provider_id = embedding_provider_id
            
        self.data_path = Path(get_astrbot_data_path()) / "plugin_data" / "astrmai" / "memory"
        if not os.path.exists(self.data_path):
            os.makedirs(self.data_path)
            
        self.db_path = str(self.data_path / "memory_bm25.db") 
        
        self.faiss_db = None
        self.vec_retriever = None
        self.bm25_retriever = None
        self.retriever = None
        self.summarizer = None
        
        # 核心标记：向量模型是否已被唤醒
        self._is_ready = False

    async def initialize(self):
        """初始化基础骨架 (跳过模型挂载，转移到运行时)"""
        # 仅初始化不需要模型的 BM25 辅库
        db_path_for_bm25 = str(Path(get_astrbot_data_path()) / "data.db")
        self.bm25_retriever = BM25Retriever(db_path_for_bm25)
        await self.bm25_retriever.initialize()
        
        logger.info("[AstrMai] 🧬 记忆模块骨架已装载，等待首次对话时唤醒向量引擎...")

    async def _ensure_faiss_initialized(self):
        """延迟唤醒机制：在真正需要时再拉取 Provider 实例"""
        if self._is_ready: 
            return True
            
        provider_instance = None
        if self.embedding_provider_id:
            pm = getattr(self.context, 'provider_manager', None)
            if pm:
                providers_dict = getattr(pm, 'providers', {})
                if isinstance(providers_dict, dict):
                    # 精确匹配 ID
                    provider_instance = providers_dict.get(self.embedding_provider_id)
                    # 容错匹配：如果字典的 key 不是 id，遍历查找
                    if not provider_instance:
                        for p in providers_dict.values():
                            if getattr(p, 'id', '') == self.embedding_provider_id:
                                provider_instance = p
                                break
                elif hasattr(pm, 'get_provider'):
                    provider_instance = pm.get_provider(self.embedding_provider_id)
                    
            if not provider_instance and hasattr(self.context, 'get_provider'):
                provider_instance = self.context.get_provider(self.embedding_provider_id)

        if not provider_instance:
            logger.error(f"[AstrMai] ❌ 记忆模块唤醒失败: 找不到 Embedding 模型 ID '{self.embedding_provider_id}'")
            return False

        try:
            self.faiss_db = FaissVecDB(
                str(self.data_path), 
                "astrmai_memory", 
                embedding_provider=provider_instance
            )
        except TypeError:
            self.faiss_db = FaissVecDB(
                namespace="astrmai_memory", 
                embedding_provider=provider_instance
            )

        # 执行原生启动并挂载混合检索器
        await self.faiss_db.init()
        self.vec_retriever = VectorRetriever(self.faiss_db, self.config)
        self.retriever = HybridRetriever(self.bm25_retriever, self.vec_retriever, config=self.config)
        
        self._is_ready = True
        logger.info("[AstrMai] 🧬 向量引擎已成功唤醒！(FaissVecDB Ready)")
        return True

    async def add_memory(self, content: str, session_id: str, persona_id: str = None, importance: float = 0.8):
        # 拦截校验：确保模型已挂载
        if not await self._ensure_faiss_initialized(): return
        
        metadata = {
            "session_id": session_id,
            "persona_id": persona_id,
            "importance": importance,
            "create_time": time.time(),
            "last_access_time": time.time()
        }
        await self.retriever.add_memory(content, metadata)

    async def recall(self, query: str, session_id: str = None, persona_id: str = None) -> str:
        # 拦截校验：确保模型已挂载
        if not await self._ensure_faiss_initialized(): 
            return "（记忆模块离线）"
        
        recall_top_k = getattr(self.config.memory, 'recall_top_k', 5)
        results = await self.retriever.search(query, k=recall_top_k, session_id=session_id, persona_id=persona_id)
        
        if not results:
            return f"你努力在记忆中搜索关于 '{query}' 的事情，但是什么也没想起来。"

        all_results = [f"- {r.content}" for r in results]
        retrieved_memory = "\n".join(all_results)
        logger.info(f"[Memory] 💡 记忆闪回成功，检索到 {len(results)} 条强相关片段。")
        
        return f"你突然回忆起了以下关于 '{query}' 的信息：\n{retrieved_memory}\n（请在后续的回复中自然地参考这些记忆）"
    
    async def start_background_tasks(self):
        from .summarizer import ChatHistorySummarizer
        self.summarizer = ChatHistorySummarizer(self.context, self.gateway, self, config=self.config)
        await self.summarizer.start()
        
    async def apply_daily_decay(self, decay_rate: float, days: int = 1) -> int:
        await self._ensure_faiss_initialized()
        db_path = str(Path(get_astrbot_data_path()) / "data.db")
        decay_factor = (1 - decay_rate) ** days
        try:
            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute("""
                    UPDATE documents
                    SET metadata = json_set(
                        metadata,
                        '$.importance',
                        MAX(0.01, ROUND(
                            COALESCE(json_extract(metadata, '$.importance'), 0.5) * ?, 4
                        ))
                    )
                    WHERE json_extract(metadata, '$.importance') IS NOT NULL
                       OR metadata LIKE '%"importance"%'
                """, (decay_factor,))
                await db.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error(f"[Memory] 物理衰减批量 SQL 执行失败: {e}")
            return 0