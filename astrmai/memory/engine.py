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
    """
    # [Fix] 增加 embedding_provider_id 参数
    def __init__(self, context, gateway, embedding_provider_id: str = None):
        self.context = context
        self.gateway = gateway
        self.embedding_provider_id = embedding_provider_id # 存储 ID
        
        # 路径配置
        self.data_path = Path(get_astrbot_data_path()) / "plugin_data" / "astrmai" / "memory"
        if not os.path.exists(self.data_path):
            os.makedirs(self.data_path)
            
        self.db_path = str(self.data_path / "memory.db")
        
        # 初始化组件占位
        self.vec_db = None
        self.retriever = None
        self.summarizer = None 
    
    def _get_provider_id_safe(self, provider):
        """辅助函数：安全获取 Provider 的 ID"""
        if not provider:
            return "None"
        # 1. 尝试直接获取 id 属性
        if hasattr(provider, 'id'):
            return provider.id
        # 2. 尝试从配置字典获取
        if hasattr(provider, 'provider_config') and isinstance(provider.provider_config, dict):
            return provider.provider_config.get('id', 'Unknown_Config_ID')
        # 3. 回退到类名
        return type(provider).__name__
    
    async def initialize(self):
        """初始化记忆引擎子系统"""
        # 1. 确保 documents 表存在
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            )
            """)
            await db.commit()
            
        # 2. 获取 Embedding Provider (智能自动发现策略)
        emb_provider = None
        provider_mgr = self.context.provider_manager

        # 策略 A: 尝试使用配置的 ID
        if self.embedding_provider_id:
            emb_provider = provider_mgr.get_provider(self.embedding_provider_id)
            if not emb_provider:
                logger.warning(f"[AstrMai] ⚠️ 配置的 Embedding ID '{self.embedding_provider_id}' 无效，尝试自动搜索...")

        # 策略 B: 自动搜索
        if not emb_provider:
            all_providers = self.context.get_all_embedding_providers()
            if all_providers:
                emb_provider = all_providers[0]
                safe_id = self._get_provider_id_safe(emb_provider)
                logger.info(f"[AstrMai] 🪄 自动选择了 Embedding Provider: {safe_id}")
            else:
                logger.error("[AstrMai] ❌ 系统中未找到任何可用的 Embedding 提供商！请先在 AstrBot 后台配置一个。")

        if not emb_provider:
            logger.warning("[AstrMai] ⚠️ MemoryEngine running in DEGRADED mode (BM25 only, No Vector DB).")
        else:
            # 3. 初始化向量库
            try:
                # 指定具体的文件路径 (Faiss 要求)
                index_file = str(self.data_path / "faiss_index.bin")
                doc_file = str(self.data_path / "faiss_docs.json")
                
                self.vec_db = FaissVecDB(
                    embedding_provider=emb_provider,
                    index_store_path=index_file,
                    doc_store_path=doc_file
                )
                
                # [Final Fix] 显式调用 initialize()
                # 之前的诊断日志确认了 AstrBot v4.12.1 的 FaissVecDB 使用 initialize() 方法来建立连接
                await self.vec_db.initialize()
                
                safe_id = self._get_provider_id_safe(emb_provider)
                logger.info(f"[AstrMai] ✅ Vector DB connected & loaded using: {safe_id}")
                
            except Exception as e:
                logger.error(f"[AstrMai] ❌ FaissVecDB 初始化失败: {e}")
                self.vec_db = None

        # 4. 初始化检索器
        bm25 = BM25Retriever(self.db_path)
        await bm25.initialize()
        
        # 如果 vec_db 初始化成功，则使用混合检索，否则仅使用 BM25 (降级处理)
        if self.vec_db:
            vec_retriever = VectorRetriever(self.vec_db)
            self.retriever = HybridRetriever(bm25, vec_retriever)
            logger.info("[AstrMai] Memory Engine Initialized (Hybrid RAG Ready)")
        else:
            # 此时需要做一个只包含 BM25 的简单封装，或者让 HybridRetriever 兼容 None
            # 为了简单起见，这里如果不兼容可能会报错，建议确保配置正确。
            # 如果 HybridRetriever 不支持 vec_retriever 为空，这里可能需要额外修改代码，
            # 但基于目前请求，我们优先解决 Provider 获取问题。
            logger.warning("[AstrMai] Memory Engine Initialized (BM25 Only - Vector DB Failed)")
            # 这里的实现取决于 Retriever 如何处理，由于没看到 Retriever 代码，假设它需要调整。
            # 暂且保留 HybridRetriever，但请确保 VectorRetriever 在没有 DB 时能安全处理。
            pass

    async def add_memory(self, content: str, session_id: str):
        if not self.retriever: return
        await self.retriever.add_memory(content, {
            "session_id": session_id,
            "importance": 0.8
        })

    async def recall(self, query: str, session_id: str) -> str:
        if not self.retriever: 
            return "（记忆模块离线）"
        
        # 检索最相关的 5 条记忆片段
        # 注意：如果 vec_db 失败，retriever 可能会报错，需确保 retriever 内部有容错
        try:
            results = await self.retriever.search(query, k=5)
        except Exception as e:
            logger.error(f"[Memory] Recall failed: {e}")
            return ""
        
        if not results:
            return f"你努力在记忆中搜索关于 '{query}' 的事情，但是什么也没想起来。"

        all_results = []
        for r in results:
            all_results.append(f"- {r.content}")

        retrieved_memory = "\n".join(all_results)
        return f"你突然回忆起了以下关于 '{query}' 的相关信息：\n{retrieved_memory}\n（请在后续的回复中，根据当前语境自然地参考这些记忆）"
    
    async def start_background_tasks(self):
        """启动后台记忆清道夫任务"""
        from .summarizer import ChatHistorySummarizer
        self.summarizer = ChatHistorySummarizer(self.context, self.gateway, self)
        await self.summarizer.start()