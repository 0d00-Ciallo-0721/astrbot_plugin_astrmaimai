# astrmai/Brain/executor.py
from typing import Any, List
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.tool import ToolSet
from ..infra.gateway import GlobalModelGateway
from .reply_checker import ReplyChecker
from .reply_engine import ReplyEngine 

class ConcurrentExecutor:
    """
    智能体执行器 (System 2)
    使用 AstrBot 原生 tool_loop_agent 替代原有手写 Action Loop。
    """
    def __init__(self, context, gateway: GlobalModelGateway, reply_engine: ReplyEngine, config=None):
        self.context = context
        self.gateway = gateway
        self.reply_checker = ReplyChecker(gateway)
        self.reply_engine = reply_engine
        self.config = config if config else gateway.config

    async def execute(self, event: AstrMessageEvent, prompt: str, system_prompt: str, tools: List[Any]):
        chat_id = event.unified_msg_origin
        sys2_id = self.gateway.sys2_id
        
        if not sys2_id:
            logger.error(f"[{chat_id}] System 2 Provider ID 未配置，无法执行动作。")
            return

        tool_set = ToolSet(tools)
        
        # 接入 Config
        max_steps = self.config.agent.max_steps
        timeout = self.config.agent.timeout
        fallback_text = self.config.reply.fallback_text
        
        logger.info(f"[{chat_id}] 🧠 Brain 启动原生 Agent Loop (Max Steps: {max_steps})...")

        try:
            # 调用 AstrBot 协议中提供的原生 Agent (集成工具调用和多轮反思)
            # 注意：system_prompt 由 ContextEngine 动态构建，已包含 Memory/State/Persona
            llm_resp = await self.context.tool_loop_agent(
                event=event,
                chat_provider_id=sys2_id,
                prompt=prompt,
                system_prompt=system_prompt,
                tools=tool_set,
                max_steps=max_steps,
                tool_call_timeout=timeout
            )

            reply_text = llm_resp.completion_text

            # 处理特定工具触发的中断信号
            if "[SYSTEM_WAIT_SIGNAL]" in reply_text:
                logger.info(f"[{chat_id}] 💤 Brain 决定挂起并倾听后续消息 (Wait/Listening)。")
                return

            # 发送前的反思校验 (Reply Checker)
            if reply_text:
                is_suitable, reason = await self.reply_checker.check(reply_text, chat_id)
                if not is_suitable:
                    logger.warning(f"[{chat_id}] ⚠️ 触发降级机制：回复未通过安全审判。")
                    # 降级策略：可以是沉默，或者发送一个通用表情
                    reply_text = fallback_text
                    
                # 最终执行回复 (交给 ReplyEngine 处理分段、表情包等)
                # ReplyEngine.handle_reply 负责最终的 send 操作
                await self.reply_engine.handle_reply(event, reply_text, chat_id)
                
        except Exception as e:
            logger.error(f"[{chat_id}] ❌ Agent Loop 执行严重异常: {e}")
            # 仅在 Debug 模式下发送错误详情，否则发送通用错误 (接入 Config)
            await event.send(event.plain_result(fallback_text))