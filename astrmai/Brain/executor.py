# astrmai/Brain/executor.py
from typing import Any, List
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.tool import ToolSet
from ..infra.gateway import GlobalModelGateway
from .reply_engine import ReplyEngine 

class ConcurrentExecutor:
    """
    智能体执行器 (System 2)
    使用 AstrBot 原生 tool_loop_agent 替代原有手写 Action Loop。
    """
    def __init__(self, context, gateway: GlobalModelGateway, reply_engine: ReplyEngine, evolution_manager, config=None):
        self.context = context
        self.gateway = gateway
        self.reply_engine = reply_engine
        self.evolution_manager = evolution_manager  # 挂载进化管理器
        self.config = config if config else gateway.config
        
        
    # [修改] 在执行成功的两个分支内，调用 evolution_manager.process_bot_reply 闭环反馈
    async def execute(self, event: AstrMessageEvent, prompt: str, system_prompt: str, tools: List[Any] = None):
        """[修改] 执行最终规划动作并维持底层状态机握手"""
        chat_id = event.unified_msg_origin
        # 🟢 [新增] 提取安全的 Bot ID
        bot_id = str(event.get_self_id()) if hasattr(event, 'get_self_id') else "SELF_BOT"
        
        models = self.gateway.get_agent_models()
        if not models:
            logger.error(f"[{chat_id}] Agent 模型未配置且无备用池，无法执行动作。")
            return

        # 🟢 [动态配置] 极速模式下极大收缩智能体反射步数，使其仅能做必要动作并极速回绝深层循环
        is_fast_mode = event.get_extra("is_fast_mode", False)
        max_steps = 1 if is_fast_mode else self.config.agent.max_steps
        timeout = 15 if is_fast_mode else self.config.agent.timeout
        
        try:
            # 🟢 [核心修复 Bug 2] 严格先决控制 _is_final_reply_phase 标志，确保 memory hook 能够无缝匹配抓取
            event._is_final_reply_phase = True 
            
            if tools is None or len(tools) == 0:
                logger.debug(f"[{chat_id}] ⚡ 纯文本模式：降级为纯文本生成器，剥离 Agent 环境...")
                from astrbot.core.agent.message import SystemMessageSegment, TextPart
                contexts = [SystemMessageSegment(content=[TextPart(text=system_prompt)])]
                last_error = ""
                
                for provider_id in models:
                    try:
                        llm_resp = await self.context.llm_generate(
                            chat_provider_id=provider_id,
                            prompt=prompt,
                            contexts=contexts
                        )
                        reply_text = getattr(llm_resp, 'completion_text', "")
                        if not reply_text:
                            raise ValueError(f"模型 {provider_id} 生成的回复文本为空")
                            
                        await self.reply_engine.handle_reply(event, reply_text, chat_id)
                        
                        # 🟢 [核心修复 Bug 2] 主动记录真实回复文本，防止记忆被污染
                        if hasattr(self.evolution_manager, 'process_bot_reply'):
                            await self.evolution_manager.process_bot_reply(chat_id, bot_id, reply_text)
                            
                        return # 执行成功直接退出
                    except Exception as e:
                        last_error = str(e)
                        logger.warning(f"[{chat_id}] ⚠️ 纯文本模型 {provider_id} 调用异常，尝试切换备用: {e}")
                        continue
                        
                logger.error(f"[{chat_id}] ❌ 模型池耗尽: {last_error}")
            else:
                # 原有的 Tool Loop 逻辑
                tool_set = ToolSet(tools)
                for provider_id in models:
                    try:
                        llm_resp = await self.context.tool_loop_agent(
                            event=event,
                            chat_provider_id=provider_id,
                            prompt=prompt,
                            system_prompt=system_prompt,
                            tools=tool_set,
                            max_steps=max_steps,
                            tool_call_timeout=timeout
                        )
                        reply_text = getattr(llm_resp, 'completion_text', "")
                        if not reply_text:
                            raise ValueError(f"模型 {provider_id} 生成的回复为空")

                        if "[SYSTEM_WAIT_SIGNAL]" in reply_text:
                            logger.info(f"[{chat_id}] 💤 Brain 决定挂起并倾听后续消息 (Wait/Listening)。")
                            return

                        await self.reply_engine.handle_reply(event, reply_text, chat_id)
                        
                        # 🟢 [核心修复 Bug 2] 主动记录真实回复文本，防止记忆被污染
                        if hasattr(self.evolution_manager, 'process_bot_reply'):
                            await self.evolution_manager.process_bot_reply(chat_id, bot_id, reply_text)
                            
                        return # 执行成功直接退出
                    except Exception as e:
                        logger.warning(f"[{chat_id}] ⚠️ Agent 模型 {provider_id} 调用异常，尝试切换备用: {e}")
                        continue
        finally:
            # 🟢 绝对释放防线：通过 finally 块确保即使模型崩溃或超时，也绝不让标志位逃逸到下一个生命周期
            if hasattr(event, '_is_final_reply_phase'):
                delattr(event, '_is_final_reply_phase')