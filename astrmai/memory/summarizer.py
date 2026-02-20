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
        
        try:
            conversations = await conv_mgr.get_conversations(unified_msg_origin=None, platform_id=None)
        except Exception as e:
            logger.error(f"[Memory Summarizer] 获取对话列表失败: {e}")
            return
        
        for conv in conversations:
            # [Fix] 安全获取 history 并解析 JSON
            raw_history = conv.history
            history_list = []
            
            if not raw_history:
                continue

            # 1. 如果是字符串，尝试解析 JSON
            if isinstance(raw_history, str):
                try:
                    history_list = json.loads(raw_history)
                except json.JSONDecodeError:
                    logger.warning(f"[Memory Summarizer] 无法解析历史记录 JSON: {str(raw_history)[:50]}...")
                    continue
            # 2. 如果已经是列表，直接使用
            elif isinstance(raw_history, list):
                history_list = raw_history
            else:
                continue

            if len(history_list) < self.msg_threshold:
                continue
                
            # [Fix] 既然 debug 显示属性叫 'cid'，那就直接用它
            session_id = getattr(conv, "cid", None)
            
            # 兜底：如果 cid 也没有，尝试构建
            if not session_id:
                if hasattr(conv, "platform_id") and hasattr(conv, "user_id"):
                    session_id = f"{conv.platform_id}:{conv.user_id}"
                else:
                    session_id = "unknown_session"
            logger.info(f"[Memory Summarizer] 发现长对话 (Session: {session_id}), 长度: {len(history_list)}，开始压缩提取...")
            
            # 提取前 N 条消息文本
            messages_block = ""
            for idx, msg_dict in enumerate(history_list[:self.msg_threshold]):
                # 防御性编程：确保 msg_dict 是字典
                if isinstance(msg_dict, str):
                    try:
                        msg_dict = json.loads(msg_dict)
                    except:
                        continue
                if not isinstance(msg_dict, dict):
                    continue

                role = msg_dict.get("role", "unknown")
                content = ""
                # 解析 AstrBot 的 message part 格式
                # 兼容 content 可能是字符串的情况 (非标准但可能存在)
                raw_content = msg_dict.get("content", [])
                if isinstance(raw_content, str):
                    content = raw_content
                elif isinstance(raw_content, list):
                    for part in raw_content:
                        if isinstance(part, TextPart) or (isinstance(part, dict) and part.get("type") == "text"):
                            content += getattr(part, 'text', part.get('text', ''))
                        # 处理旧版本可能存在的纯文本结构
                        elif isinstance(part, dict) and 'text' in part:
                            content += part['text']
                            
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
                # [Debug] 阶段 1: 调用 LLM
                logger.debug(f"[Memory Summarizer] 正在调用 System 1 进行压缩 (Prompt len: {len(prompt)})...")
                result = await self.gateway.call_judge(prompt)
                memories = result.get("memories", [])
                
                if not memories:
                    logger.debug("[Memory Summarizer] 未提取到有效记忆。")
                    return

                # [Debug] 阶段 2: 记忆入库
                logger.info(f"[Memory Summarizer] 提取到 {len(memories)} 条记忆，准备入库...")
                
                for memory_text in memories:
                    if memory_text.strip():
                        # 这里是可能抛出 "Database connection is not initialized" 的地方
                        await self.engine.add_memory(
                            content=memory_text.strip(),
                            session_id=str(session_id)
                        )
                        logger.debug(f"[Memory Summarizer] 💾 已入库长期记忆: {memory_text}")
                
            except Exception as e:
                # [Debug] 捕获异常并打印堆栈，帮助定位是 Gateway 还是 Engine 报错
                import traceback
                logger.error(f"[Memory Summarizer] 压缩对话时发生错误: {e}")
                logger.error(traceback.format_exc()) # 如果需要更详细堆栈可取消注释