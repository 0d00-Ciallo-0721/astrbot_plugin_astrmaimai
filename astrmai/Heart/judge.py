# astrmai/Heart/judge.py
from ..infra.gateway import GlobalModelGateway
from .state_engine import StateEngine
import time
import json
from astrbot.api import logger
from ..infra.datamodels import BrainActionPlan


class Judge:
    """
    判官 (System 1: Fused 3-State Version)
    职责: 决定 System 2 的初步动作倾向 (REPLY, WAIT, IGNORE)
    """
    def __init__(self, gateway: GlobalModelGateway, state_engine: StateEngine, config=None):
        self.gateway = gateway
        self.state_engine = state_engine
        self.config = config if config else gateway.config

    async def evaluate(self, chat_id: str, message: str, is_force_wakeup: bool) -> BrainActionPlan:
        """
        输出结构化的 BrainActionPlan，融合了 HeartFlow 的评分机制和 3 态决策。
        """
        start_time = time.perf_counter()
        state = await self.state_engine.get_state(chat_id)
        
        # 1. 能量硬限制 (接入 Config)
        if state.energy < self.config.energy.min_reply_threshold and not is_force_wakeup:
            logger.debug(f"[{chat_id}] Judge: 能量过低 ({state.energy:.2f})，抑制回复。")
            return BrainActionPlan(action="IGNORE", thought="能量耗尽", necessity=0)

        # 2. 强唤醒直接通过
        if is_force_wakeup:
            logger.debug(f"[{chat_id}] Judge: 强唤醒，最高优先级放行。")
            return BrainActionPlan(action="REPLY", thought="受到强唤醒", necessity=10, relevance=10)

        # 3. 关键词短路 (接入 Config 修复隐患 Bug)
        wakeup_words = self.config.system1.wakeup_words
        msg_lower = message.strip().lower()
        
        for kw in wakeup_words:
            if msg_lower.startswith(kw.lower()):
                logger.debug(f"[{chat_id}] Judge: 唤醒词 [{kw}] 命中首部，快速放行。")
                return BrainActionPlan(action="REPLY", thought=f"命中唤醒词 [{kw}]", necessity=9, relevance=10)

        # 4. LLM 三态判决 (REPLY / WAIT / IGNORE)
        prompt = f"""
        你是对话意图研判器。当前群聊情绪: {state.mood:.2f} (-1.0 到 1.0)。
        用户消息: "{message}"
        
        请评估这条消息，决定 AI 的动作：
        - REPLY: 包含明确问题，或话题直接相关，必须立刻回复。
        - WAIT: 话似乎没说完（例如只发了“那个..”或半截句子），需要等待。
        - IGNORE: 明显的闲聊、无意义刷屏且没@你，不需要理会。
        
        请严格返回 JSON: {{"action": "REPLY"|"WAIT"|"IGNORE", "relevance": int(1-10), "necessity": int(1-10)}}
        """
        
        plan = BrainActionPlan()
        try:
            result = await self.gateway.call_judge(prompt)
            plan.action = result.get("action", "IGNORE").upper()
            plan.relevance = int(result.get("relevance", 0))
            plan.necessity = int(result.get("necessity", 0))
            
            if plan.action not in ["REPLY", "WAIT", "IGNORE"]:
                plan.action = "IGNORE"
                
            elapsed = time.perf_counter() - start_time
            logger.debug(f"[{chat_id}] Judge 耗时 {elapsed:.2f}s | Action: {plan.action} | Nec: {plan.necessity}")
            
        except Exception as e:
            logger.warning(f"[{chat_id}] Judge LLM 失败，默认放行: {e}")
            plan.action = "REPLY" # 降级放行
            
        return plan