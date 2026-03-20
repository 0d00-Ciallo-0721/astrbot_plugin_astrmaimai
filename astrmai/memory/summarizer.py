import asyncio
from typing import Optional
from astrbot.api import logger
from .processor import MemoryProcessor

class ChatHistorySummarizer:
    """
    历史摘要清道夫 (System 2 / Memory Lifecycle)
    阶段二重构：废弃旧版扁平陈述句提取，接入 Cognitive Processor 实现高密度知识提取。
    """
    def __init__(self, context, gateway, engine, config=None):
        self.context = context
        self.gateway = gateway
        self.engine = engine
        self.config = config if config else gateway.config
        
        self._running = False
        self._periodic_task = None
        
        # 从配置读取阈值
        self.check_interval = getattr(self.config.memory, 'cleanup_interval', 3600)
        self.msg_threshold = getattr(self.config.memory, 'summary_threshold', 30)
        
        # 挂载认知处理器
        self.processor = MemoryProcessor(gateway)

    async def start(self):
        """启动后台定期检查循环"""
        if self._running:
            return
        self._running = True
        self._periodic_task = asyncio.create_task(self._periodic_check_loop())
        logger.info(f"[Memory Summarizer] ♻️ 已启动结构化记忆清道夫循环 (Interval: {self.check_interval}s)")

    async def stop(self):
        """停止后台定期检查循环"""
        self._running = False
        if self._periodic_task and not self._periodic_task.done():
            self._periodic_task.cancel()

    async def _periodic_check_loop(self):
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
                # 注：实际的扫描逻辑可在此结合 AstrBot/数据库 的 get_messages 进行批量处理
                # 此处保留循环框架，等待与阶段四的 Event Hook 结合实现即时/延时摘要
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Memory Summarizer] 后台循环异常: {e}")

    async def summarize_session(self, session_id: str, chat_history_text: str, persona_id: Optional[str] = None):
        """
        核心记忆提炼流水线
        调用时机：当特定会话消息积累达到阈值时触发
        """
        if not chat_history_text.strip():
            return
            
        logger.debug(f"[Memory Summarizer] 🧠 正在对 Session {session_id} 进行多维认知降维...")
        
        # 1. 调用认知大脑进行结构化解析
        memory_data = await self.processor.process_conversation(chat_history_text)
        
        # 2. 空转检测：如果没有任何有价值的事实，或者完全是系统默认回复，直接抛弃
        if not memory_data["key_facts"] and memory_data["summary"] == "对话记录":
            logger.debug(f"[Memory Summarizer] Session {session_id} 未提取到有效事实，跳过。")
            return
            
        importance = memory_data["importance"]
        
        # 3. 极速遗忘机制：重要性过低的内容不占用数据库和后续召回算力
        if importance < 0.2:
            logger.debug(f"[Memory Summarizer] 提取内容重要度过低 (importance={importance})，触发即时遗忘。")
            return

        # 4. 富文本组装：将多维数据渲染为对 System 2 的 Prompt 友好的易读格式
        content_lines = [f"【摘要】{memory_data['summary']}"]
        if memory_data["key_facts"]:
            content_lines.append("【核心事实】\n- " + "\n- ".join(memory_data["key_facts"]))
        if memory_data["topics"]:
            content_lines.append(f"【话题标签】{', '.join(memory_data['topics'])}")
            
        final_content = "\n".join(content_lines)

        # 5. 压入统一底层引擎
        try:
            # 在阶段一重构中，engine.add_memory 接收 importance
            await self.engine.add_memory(
                content=final_content,
                session_id=str(session_id),
                persona_id=persona_id,
                importance=importance
            )
            logger.info(f"[Memory Summarizer] 💾 已入库立体记忆 (Sentiment: {memory_data['sentiment']}, Importance: {importance})")
        except Exception as e:
            logger.error(f"[Memory Summarizer] 记忆入库失败: {e}", exc_info=True)