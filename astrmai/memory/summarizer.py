import asyncio
import json
from astrbot.api import logger
from astrbot.core.agent.message import TextPart

class ChatHistorySummarizer:
    """
    历史摘要清道夫 (System 2 / Memory Lifecycle)
    定期在后台扫描超长的历史对话，调用模型进行话题总结，并压入长期记忆库。
    """
    def __init__(self, context, gateway, engine):
        self.context = context
        self.gateway = gateway
        self.engine = engine
        
        self._running = False
        self._periodic_task = None
        self.check_interval = 3600  # 每小时检查一次
        self.msg_threshold = 30     # 当对话记录超过 30 条时触发压缩

    async def start(self):
        """启动后台定期检查循环"""
        if self._running:
            return
        self._running = True
        self._periodic_task = asyncio.create_task(self._periodic_check_loop())
        logger.info(f"[Memory Summarizer] ♻️ 已启动后台记忆清道夫循环 (Interval: {self.check_interval}s)")

    async def stop(self):
        """停止后台定期检查循环"""
        self._running = False
        if self._periodic_task:
            self._periodic_task.cancel()
            self._periodic_task = None
        logger.info("[Memory Summarizer] 🛑 已停止后台记忆清道夫循环")

    async def _periodic_check_loop(self):
        try:
            while self._running:
                await self.process()
                await asyncio.sleep(self.check_interval)
        except asyncio.CancelledError:
            logger.info("[Memory Summarizer] 后台循环被取消。")
        except Exception as e:
            logger.error(f"[Memory Summarizer] 循环严重异常: {e}")

    async def process(self):
        """核心处理逻辑：扫描并压缩过长对话"""
        conv_mgr = self.context.conversation_manager
        
        # 遍历系统中的所有对话列表
        conversations = await conv_mgr.get_conversations(unified_msg_origin=None, platform_id=None)
        
        for conv in conversations:
            if not conv.history or len(conv.history) < self.msg_threshold:
                continue
                
            session_id = conv.id
            logger.info(f"[Memory Summarizer] 发现长对话 (Session: {session_id}), 长度: {len(conv.history)}，开始压缩提取...")
            
            # 提取前 N 条消息文本
            messages_block = ""
            for idx, msg_dict in enumerate(conv.history[:self.msg_threshold]):
                role = msg_dict.get("role", "unknown")
                content = ""
                # 解析 AstrBot 的 message part 格式
                for part in msg_dict.get("content", []):
                    if isinstance(part, TextPart) or (isinstance(part, dict) and part.get("type") == "text"):
                        content += getattr(part, 'text', part.get('text', ''))
                messages_block += f"[{idx}] {role}: {content}\n"

            # 构造摘要 Prompt
            prompt = f"""
            你是一个记忆压缩器。以下是一段近期的对话记录：
            {messages_block}
            
            请提取出这段对话中**最重要、最具有长期记忆价值的事实、偏好或重要事件**。
            请将提取出的记忆转化为独立的陈述句形式。如果没有重要信息，则返回空列表。
            严格返回 JSON 格式: {{"memories": ["陈述句1", "陈述句2"]}}
            """
            
            try:
                result = await self.gateway.call_judge(prompt)
                memories = result.get("memories", [])
                
                # 入库
                for memory_text in memories:
                    if memory_text.strip():
                        await self.engine.add_memory(
                            content=memory_text.strip(),
                            session_id=str(session_id)
                        )
                        logger.debug(f"[Memory Summarizer] 💾 已入库长期记忆: {memory_text}")
                
                # ⚠ 危险操作：在真实部署中，压缩成功后应使用 conv_mgr 截断历史列表
                # 这里仅作为提取不删除历史的保守实现
                
            except Exception as e:
                logger.error(f"[Memory Summarizer] 压缩对话时发生错误: {e}")