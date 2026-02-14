### 📄 core/memory_glands.py
import json
import time
from typing import List, Dict
from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB

from ..services.memory.memory_engine import MemoryEngine
from ..utils.api_utils import APIUtils

class MemoryGlands:
    """
    记忆腺体 (The Hippocampus)
    职责：
    1. 封装 MemoryEngine
    2. 执行 Active Retrieval (自我提问 -> 检索)
    3. 话题切片存储
    """
    
    def __init__(self, context: Context):
        self.context = context
        self.data_dir = context.plugin_data_dir # 需确保 main.py 传递正确路径或由此获取
        
        # 初始化底层引擎
        # HeartCore 复用 AstrBot 内置的 Faiss，但也需要独立的 Collection 隔离？
        # 为简化，直接使用 AstrBot 的 vec_db 接口，但在 metadata 加 tag
        # 注意：这里我们假设 context 提供了访问 vec_db 的能力，或者我们自己实例化一个
        # 为了稳妥，我们通过 AstrBot 的标准路径实例化一个独立的 FaissDB
        self.vec_db_path = f"{self.data_dir}/vector_db"
        self.faiss_db = FaissVecDB(self.vec_db_path, 768) # 维度需匹配模型
        
        self.engine = MemoryEngine(self.data_dir, self.faiss_db)
        self.api_utils = APIUtils(context)
        self.is_ready = False

    async def initialize(self):
        """异步初始化"""
        if self.is_ready: return
        await self.engine.initialize()
        # 需确保 Faiss 加载
        # await self.faiss_db.load() # 视 AstrBot 版本 API 而定
        self.is_ready = True
        logger.info("🦄 MemoryGlands initialized.")

    async def active_retrieve(self, session_id: str, context_messages: List[Dict]) -> str:
        """
        主动检索：决定是否需要查阅记忆，并返回结果字符串
        """
        if not self.is_ready: await self.initialize()
        
        # 1. 提取最近对话 (Last 3 rounds)
        recent_chat = context_messages[-6:]
        
        # 2. 让 LLM 判断是否需要检索 (Self-Questioning)
        # 使用简单的 Prompt (已汉化)
        check_prompt = [
            {"role": "system", "content": """
分析对话历史。用户是否提到了当前上下文中**未出现**的“过去事件”、“特定名称”或“具体细节”？
(即：如果不查阅记忆，是否无法完全理解用户在说什么？)

- 如果是 (YES)：请生成一个简短的**搜索查询语句 (Query)**。
- 如果否 (NO)：请直接输出 "NO"。

格式要求：仅输出查询语句或 "NO"，不要包含任何解释。
            """.strip()},
            {"role": "user", "content": str(recent_chat)}
        ]
        
        query = await self.api_utils.chat_simple(check_prompt)
        
        # 保持 "NO" 的判断逻辑不变
        if not query or "NO" in query.upper() or len(query) > 50:
            return "" # 无需检索
            
        logger.info(f"🦄 [Memory] Active Query: {query}")
        
        # 3. 执行检索
        memories = await self.engine.search(query, k=3, session_id=session_id)
        
        if not memories:
            return ""
            
        # 4. 格式化结果 (标题汉化)
        result_text = "[检索到的相关记忆]:\n"
        for i, mem in enumerate(memories):
            # 简单的相关性过滤
            if mem['score'] < 0.01: continue 
            result_text += f"{i+1}. {mem['content']} (置信度: {mem['score']:.2f})\n"
            
        return result_text

    async def store_short_term(self, session_id: str, text: str, role: str):
        """存储单条消息 (短期流)"""
        # 简单存入，用于测试。Phase 4 将升级为话题切片存储。
        meta = {
            "session_id": session_id,
            "role": role,
            "create_time": time.time(),
            "importance": 0.5
        }
        await self.engine.add_memory(text, meta)