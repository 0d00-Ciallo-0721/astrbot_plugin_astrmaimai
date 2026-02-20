from astrmai.infra.gateway import GlobalModelGateway
from .state_engine import StateEngine

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
        评估回复必要性
        """
        state = await self.state_engine.get_state(chat_id)
        
        # 1. 能量硬限制 (Low Energy -> Ignore unless forced)
        if state.energy < 0.1 and not is_force_wakeup:
            return False

        # 2. 强唤醒直接通过
        if is_force_wakeup:
            return True

        # 3. LLM 判决 (Small Model)
        # 构造精简 Prompt
        prompt = f"""
        Determine if the AI Assistant should reply to this message.
        Context: Group Chat.
        Current Mood: {state.mood:.2f}
        Message: "{message}"
        
        Rules:
        - Reply if addressed to AI or needs help.
        - Ignore casual chit-chat not directed at AI.
        
        Return JSON: {{"reply": bool, "reason": "str"}}
        """
        
        try:
            decision = await self.gateway.call_judge(prompt)
            return decision.get("reply", False)
        except Exception:
            # Fallback: 如果 Judge 挂了，保守策略是不回
            return False