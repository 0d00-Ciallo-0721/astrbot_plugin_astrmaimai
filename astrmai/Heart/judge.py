from astrmai.infra.gateway import GlobalModelGateway
from .state_engine import StateEngine
import time
import json
from astrbot.api import logger
class Judge:
    """
    判官 (System 1)
    职责: 决定 System 2 是否介入
    Reference: BrainPlanner (relevance/necessity logic)
    """
    def __init__(self, gateway: GlobalModelGateway, state_engine: StateEngine):
        self.gateway = gateway
        self.state_engine = state_engine

    async def evaluate(self, chat_id: str, message: str, is_force_wakeup: bool) -> bool:
        """
        评估回复必要性 (融入 MaiBot 性能计算与关键词短路逻辑)
        """
        import time
        start_time = time.perf_counter()
        state = await self.state_engine.get_state(chat_id)
        
        # 1. 能量硬限制 (Low Energy -> Ignore unless forced)
        if state.energy < 0.1 and not is_force_wakeup:
            logger.debug(f"[{chat_id}] Judge: 能量过低 ({state.energy:.2f})，拒绝回复。")
            return False

        # 2. 强唤醒直接通过
        if is_force_wakeup:
            logger.debug(f"[{chat_id}] Judge: 强唤醒，直接放行。")
            return True

        # 3. 关键词快速匹配 (Short-circuit, 借鉴 action_modifier.py)
        # 匹配到高频交互词时，跳过 LLM 直接放行以节省算力
        quick_keywords = ["在吗", "帮我", "机器人", "bot"] 
        for kw in quick_keywords:
            if kw in message.lower():
                logger.debug(f"[{chat_id}] Judge: 关键词 [{kw}] 命中，快速放行。")
                return True

        # 4. LLM 判决 (Small Model，借鉴 lpmm_prompt.py 单一职责)
        prompt = f"""
        你是一个消息过滤器。当前群聊情绪值: {state.mood:.2f} (-1.0 到 1.0)。
        用户发送了消息: "{message}"
        
        请分析这条消息是否需要 AI 助手介入回复：
        1. 如果是明显的闲聊且没有@你，不需要回复。
        2. 如果包含问题或寻求帮助，必须回复。
        
        直接返回 JSON 格式: {{"should_reply": bool, "reason": "string"}}
        """
        try:
            result = await self.gateway.call_judge(prompt)
            should_reply = result.get("should_reply", False)
            reason = result.get("reason", "No reason provided")
            
            elapsed = time.perf_counter() - start_time
            logger.debug(f"[{chat_id}] Judge LLM 耗时: {elapsed:.3f}秒 | 判决: {should_reply} | 理由: {reason}")
            return should_reply
        except Exception as e:
            logger.error(f"[{chat_id}] Judge LLM 失败，默认放行: {e}")
            return True