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

try:
    from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
    HAS_FAISS = True
except ImportError:
    HAS_FAISS = False
    logger.warning("[AstrMai] ⚠️ 未检测到 faiss 依赖，高级向量记忆功能将被禁用，基础功能不受影响。")



class MemoryEngine:
    """
    统一记忆引擎 (Infrastructure Layer)
    重构版：加入延迟唤醒 (Lazy Load) 机制，彻底解决启动时的 Provider 生命期时序问题。
    """
    def __init__(self, context, gateway, embedding_models: list = None, config=None):
        self.context = context
        self.gateway = gateway
        self.config = config if config else gateway.config
        
        # 适配变量名从 embedding_provider_id 改为 embedding_models
        if hasattr(self.config, 'provider') and getattr(self.config.provider, 'embedding_models', None):
            self.embedding_models = self.config.provider.embedding_models
        else:
            self.embedding_models = embedding_models or []
            
        self.data_path = Path(get_astrbot_data_path()) / "plugin_data" / "astrmai" / "memory"
        if not os.path.exists(self.data_path):
            os.makedirs(self.data_path)
            
        self.db_path = str(self.data_path / "docs.db") 
        
        self.faiss_db = None
        self.vec_retriever = None
        self.bm25_retriever = None
        self.retriever = None
        self.summarizer = None
        
        self._is_ready = False
        
        # 🟢 [核心修复 Bug 4] 状态机熔断器相关属性
        self._init_failures = 0           
        self._next_retry_time = 0.0  
        
    async def initialize(self):
        """初始化基础骨架 (跳过模型挂载，转移到运行时)"""
        # 🔑 【修改点】BM25 引擎指向正确的独立插件数据库，而不是全局 data.db
        self.bm25_retriever = BM25Retriever(self.db_path)
        await self.bm25_retriever.initialize()
        
        logger.info("[AstrMai] 🧬 记忆模块骨架已装载，等待首次对话时唤醒向量引擎...")
    
    async def _ensure_faiss_initialized(self):
        """[彻底修复 Bug 4] 引入指数退避熔断器机制 (Circuit Breaker)，并适配向量化模型的按序遍历策略"""
        if self._is_ready: 
            return True
            
        now = time.time()
        # 熔断冷却期内，直接拒绝重试请求 (Fast-Fail)
        if now < self._next_retry_time:
            return False
            
        if not HAS_FAISS:
            logger.error("[AstrMai] ❌ 记忆模块唤醒失败: 环境不支持向量检索 (未安装 faiss)。将封印此功能24小时。")
            self._next_retry_time = now + 86400 
            return False
            
        provider_instance = None
        
        # 获取清洗后的模型列表
        clean_models = [m.strip() for m in self.embedding_models if m and m.strip()]
        unique_models = list(dict.fromkeys(clean_models))
        
        # 由于 FaissVecDB 初始化只能传入一个实例，此处遍历池子寻找第一个能够成功实例化并加载的提供商
        if unique_models:
            for model_id in unique_models:
                if hasattr(self.context, 'get_provider_by_id'):
                    provider_instance = self.context.get_provider_by_id(model_id)
                if not provider_instance and hasattr(self.context, 'get_provider'):
                    provider_instance = self.context.get_provider(model_id)
                
                if provider_instance:
                    break

        if not provider_instance:
            self._init_failures += 1
            backoff = min(3600, 30 * (2 ** (self._init_failures - 1)))
            self._next_retry_time = now + backoff
            models_str = ", ".join(unique_models) if unique_models else "未配置"
            logger.error(f"[AstrMai] ❌ 记忆模块唤醒失败: 找不到有效的 Embedding 模型 [{models_str}]。熔断保护 {backoff} 秒。")
            return False

        doc_store_path = str(self.data_path / "docs.db")
        index_store_path = str(self.data_path / "vectors.index")

        try:
            self.faiss_db = FaissVecDB(
                doc_store_path=doc_store_path,
                index_store_path=index_store_path,
                embedding_provider=provider_instance
            )
            await self.faiss_db.initialize()
            
        except Exception as e:
            self._init_failures += 1
            backoff = min(3600, 30 * (2 ** (self._init_failures - 1)))
            self._next_retry_time = now + backoff
            logger.error(f"[AstrMai] ❌ FaissVecDB 核心实例化失败: {e}。熔断保护 {backoff} 秒。", exc_info=True)
            return False

        # 🟢 [核心修复 1] 补全被遗漏的 BM25 延迟加载 (Lazy Load)
        # 确保即使没有经过 main 生命周期，只要唤醒引擎，BM25 也必须通电！
        if not self.bm25_retriever:
            self.bm25_retriever = BM25Retriever(self.db_path)
            await self.bm25_retriever.initialize()

        # 装载完成：现在注入的绝不可能是 None
        self.vec_retriever = VectorRetriever(self.faiss_db, self.config)
        self.retriever = HybridRetriever(self.bm25_retriever, self.vec_retriever, config=self.config)
        
        self._is_ready = True
        self._init_failures = 0  # 恢复健康状态后清零熔断计数
        logger.info("[AstrMai] 🧬 混合记忆引擎已成功双路唤醒 (BM25 & FaissVecDB Ready)")
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

    async def clear_persona_lore(self, persona_id: str = None) -> int:
        """[新增] Phase 8: 清除旧版原典，避免因更新设定导致新旧人格数据并在带来的精神分裂"""
        if not await self._ensure_faiss_initialized(): return 0
        try:
            async with aiosqlite.connect(self.db_path) as db:
                query = "DELETE FROM documents WHERE json_extract(metadata, '$.session_id') = '__self_lore__'"
                params = []
                if persona_id:
                    query += " AND json_extract(metadata, '$.persona_id') = ?"
                    params.append(persona_id)
                cursor = await db.execute(query, params)
                await db.commit()
                deleted = cursor.rowcount
                logger.info(f"[MemoryEngine] 🧹 成功清洗 {deleted} 条旧版原典碎片 (Persona: {persona_id})。")
                return deleted
        except Exception as e:
            logger.error(f"[MemoryEngine] 清洗旧版原典失败: {e}")
            return 0

    async def add_persona_lore(self, content: str, persona_id: str = None):
        """[新增] Phase 8: 将长文本原典进行语义切块并注入隔离区"""
        if not await self._ensure_faiss_initialized(): return
        
        from ..Brain.text_segmenter import TextSegmenter
        chunks = TextSegmenter.semantic_chunk(content, max_chunk_size=800)
        
        logger.info(f"[MemoryEngine] 📥 正在将原点设定切割为 {len(chunks)} 个高保真语义切片存入潜意识原典库...")
        for i, chunk in enumerate(chunks):
            metadata = {
                "session_id": "__self_lore__",
                "persona_id": persona_id,
                "chunk_index": i,
                "importance": 1.0,  # 配合衰减豁免实现永生
                "create_time": time.time(),
                "last_access_time": time.time()
            }
            await self.retriever.add_memory(chunk, metadata)

    async def recall_persona_lore(self, query: str, persona_id: str = None, top_k: int = 3) -> str:
        """[新增] Phase 8: 精准靶向原典隔离区的检索"""
        if not await self._ensure_faiss_initialized(): 
            return "（设定原典库离线）"
            
        results = await self.retriever.search(query, k=top_k, session_id="__self_lore__", persona_id=persona_id)
        
        valid_results = [r for r in results if getattr(r, 'score', 1.0) >= 0.05]
        if not valid_results:
            return "（潜意识原典库中未发现相关事实）"
            
        all_results = [f"[绝对事实]: {r.content}" for r in valid_results]
        return "\n".join(all_results)

    async def recall(self, query: str, session_id: str = None, persona_id: str = None) -> str:
        # 拦截校验：确保模型已挂载
        if not await self._ensure_faiss_initialized(): 
            return "（记忆模块离线）"
        
        recall_top_k = getattr(self.config.memory, 'recall_top_k', 5)
        results = await self.retriever.search(query, k=recall_top_k, session_id=session_id, persona_id=persona_id)
        
        # [修改] 增加低权重衰减防线，过滤掉因长期未调用导致 score 过低的残余记忆
        # 阈值设为 0.005（因采用RRF融合算法基础分较小，低于此值代表已高度衰减）
        valid_results = [r for r in results if getattr(r, 'score', 1.0) >= 0.02]
        
        if not valid_results:
            return f"你努力在记忆中搜索关于 '{query}' 的事情，但是什么也没想起来。"

        all_results = [f"- {r.content}" for r in valid_results]
        retrieved_memory = "\n".join(all_results)
        logger.info(f"[Memory] 💡 记忆闪回成功，检索到 {len(valid_results)} 条强相关片段。")
        
        return f"你突然回忆起了以下关于 '{query}' 的信息：\n{retrieved_memory}\n（请在后续的回复中自然地参考这些记忆）"
    
    async def start_background_tasks(self):
        from .summarizer import ChatHistorySummarizer
        self.summarizer = ChatHistorySummarizer(self.context, self.gateway, self, config=self.config)
        await self.summarizer.start()
        
    async def apply_daily_decay(self, decay_rate: float, days: int = 1) -> int:
        await self._ensure_faiss_initialized()
        decay_factor = (1 - decay_rate) ** days
        try:
            # 🔑 【修改点】物理衰减 SQL 连接指向正确的 docs.db (self.db_path)，阻止表查询失败
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    UPDATE documents
                    SET metadata = json_set(
                        metadata,
                        '$.importance',
                        MAX(0.01, ROUND(
                            COALESCE(json_extract(metadata, '$.importance'), 0.5) * ?, 4
                        ))
                    )
                    WHERE (json_extract(metadata, '$.importance') IS NOT NULL
                       OR metadata LIKE '%"importance"%')
                      AND COALESCE(json_extract(metadata, '$.session_id'), '') != '__self_lore__'
                """, (decay_factor,))
                await db.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error(f"[Memory] 物理衰减批量 SQL 执行失败: {e}")
            return 0

    async def get_recent_memories(self, session_id: str, hours: int = 24) -> list:
        """[修改] 获取最近 N 小时内该群产生的记忆切片 (动态探测避免无 text 列报错)"""
        if not await self._ensure_faiss_initialized(): 
            return []
            
        recent_memories = []
        cutoff_time = time.time() - (hours * 3600)
        
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # 动态获取表结构探测真实的文本列名
                cursor = await db.execute("PRAGMA table_info(documents)")
                columns = [row[1] for row in await cursor.fetchall()]
                
                # [修复核心] 拦截无表结构的情况，安全降级，防止拼接出非法的 SQL 语法
                if not columns:
                    logger.warning("[Memory] documents 表尚无结构，安全跳过近期记忆检索。")
                    return []
                
                # 兼容 AstrBot/LangChain 不断更迭的底层 schema
                text_col = "page_content" if "page_content" in columns else ("content" if "content" in columns else "text")

                # 动态拼接 SQL
                query = f"""
                    SELECT {text_col} 
                    FROM documents 
                    WHERE json_extract(metadata, '$.session_id') = ? 
                      AND json_extract(metadata, '$.create_time') >= ?
                """
                cursor = await db.execute(query, (session_id, cutoff_time))
                
                rows = await cursor.fetchall()
                for row in rows:
                    if row and row[0]:
                        recent_memories.append(row[0])
                        
        except Exception as e:
            logger.error(f"[Memory] 提取最近记忆片段时发生异常: {e}")
            
        return recent_memories

    async def prune_low_importance(self, threshold: float = 0.2) -> int:
        """[新增] Phase 7.2 记忆遗忘机制: 物理剪枝重要度极低的陈旧记忆"""
        if not await self._ensure_faiss_initialized():
            return 0
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # 提取 JSON 元数据中的 importance
                cursor = await db.execute("""
                    DELETE FROM documents
                    WHERE json_extract(metadata, '$.importance') IS NOT NULL
                      AND CAST(json_extract(metadata, '$.importance') AS REAL) < ?
                """, (threshold,))
                await db.commit()
                deleted_rows = cursor.rowcount
                if deleted_rows > 0:
                    logger.info(f"[MemoryEngine] 🗑️ 遗忘机制触发：永久清除 {deleted_rows} 条低重要度 (<{threshold}) 碎片记忆。")
                return deleted_rows
        except Exception as e:
            logger.error(f"[MemoryEngine] 物理剪枝失败: {e}")
            return 0