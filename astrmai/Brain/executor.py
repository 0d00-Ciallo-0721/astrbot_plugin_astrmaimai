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
    def __init__(self, context, gateway: GlobalModelGateway, reply_engine: ReplyEngine, config=None):
        self.context = context
        self.gateway = gateway
        
        self.reply_engine = reply_engine
        self.config = config if config else gateway.config

    async def execute(self, event: AstrMessageEvent, prompt: str, system_prompt: str, tools: List[Any]):
        chat_id = event.unified_msg_origin
        
        # [修改点] 获取 Agent 原生模型备用池
        models = self.gateway.get_agent_models()
        if not models:
            logger.error(f"[{chat_id}] Agent 模型未配置且无备用池，无法执行动作。")
            return

        tool_set = ToolSet(tools)
        
        # 接入 Config
        max_steps = self.config.agent.max_steps
        timeout = self.config.agent.timeout
        fallback_text = self.config.reply.fallback_text
        
        logger.info(f"[{chat_id}] 🧠 Brain 启动原生 Agent Loop (Max Steps: {max_steps})...")

        last_error = ""
        success = False

        # [修改点] 模型池弹性轮询重试
        for provider_id in models:
            try:
                # === [核心新增] 生命周期加锁：向事件总线广播当前进入了“最终回复生成阶段” ===
                setattr(event, '_is_final_reply_phase', True)
                
                # 调用 AstrBot 协议中提供的原生 Agent (集成工具调用和多轮反思)
                llm_resp = await self.context.tool_loop_agent(
                    event=event,
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    tools=tool_set,
                    max_steps=max_steps,
                    tool_call_timeout=timeout
                )
            except Exception as e:
                last_error = str(e)
                logger.warning(f"[{chat_id}] ⚠️ Agent 模型 {provider_id} 调用异常，尝试切换备用模型: {e}")
                continue
            finally:
                # === [核心新增] 生命周期解锁：无论执行成功还是崩溃，必须卸载标记 ===
                setattr(event, '_is_final_reply_phase', False)

            try:
                reply_text = getattr(llm_resp, 'completion_text', "")

                # 生成失败直接抛错触发 continue 轮询
                if not reply_text:
                    raise ValueError(f"模型 {provider_id} 生成的回复文本为空")

                # 处理特定工具触发的中断信号
                if "[SYSTEM_WAIT_SIGNAL]" in reply_text:
                    logger.info(f"[{chat_id}] 💤 Brain 决定挂起并倾听后续消息 (Wait/Listening)。")
                    success = True
                    break

                # 直接发送，剥离 Reply Checker (节约 Token 并加速响应)
                if reply_text:
                    # 最终执行回复
                    await self.reply_engine.handle_reply(event, reply_text, chat_id)
                    success = True
                    break
                    
            except Exception as e:
                last_error = str(e)
                logger.warning(f"[{chat_id}] ⚠️ Agent 模型 {provider_id} 处理回复异常，尝试切换备用模型: {e}")
                continue
                
        # [修改点] 循环穷尽仍未成功，进行最终系统兜底
        if not success:
            logger.error(f"[{chat_id}] ❌ 所有 Agent 模型池耗尽，最终异常: {last_error}")
            await event.send(event.plain_result(fallback_text))