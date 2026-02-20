import time
import random
import json
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrmai.infra.database import DatabaseService, ChatState, UserProfile
from astrmai.infra.gateway import GlobalModelGateway

class StateEngine:
    """
    状态引擎 (System 1)
    职责:
    1. 维护 ChatState (Energy, Mood)
    2. 维护 UserProfile (Social Score)
    3. 计算情绪变化 (Mood Dynamics)
    Reference: HeartCore/core/state_manager.py & mood_manager.py
    """
    def __init__(self, db: DatabaseService, gateway: GlobalModelGateway):
        self.db = db
        self.gateway = gateway
        self.runtime_states = {} # 简单内存缓存 {chat_id: ChatState}

    async def get_state(self, chat_id: str) -> ChatState:
        if chat_id in self.runtime_states:
            return self.runtime_states[chat_id]
        
        state = self.db.get_chat_state(chat_id)
        if not state:
            # 初始化新状态
            state = ChatState(chat_id=chat_id, energy=0.8, mood=0.0)
            self.db.save_chat_state(state)
        
        self.runtime_states[chat_id] = state
        return state

    async def update_mood(self, chat_id: str, text: str):
        """
        基于文本分析情绪变化
        Reference: MoodManager.analyze_text_mood
        """
        state = await self.get_state(chat_id)
        current_mood = state.mood
        
        # 1. 简单规则 (Heuristic)
        # TODO: 可以在这里添加基于关键词的快速规则

        # 2. LLM 分析 (System 1)
        prompt = f"""
        分析文本对“我”的情绪影响。当前情绪: {current_mood:.2f} (-1.0悲伤 ~ 1.0开心)。
        文本: "{text}"
        返回JSON: {{"mood_delta": float, "reason": "..."}}
        mood_delta范围: -0.2 ~ +0.2
        """
        try:
            result = await self.gateway.call_judge(prompt)
            delta = result.get("mood_delta", 0.0)
            
            # 更新状态
            state.mood = max(-1.0, min(1.0, current_mood + delta))
            self.db.save_chat_state(state) # 持久化
            
            logger.debug(f"[Heart] Mood Update: {current_mood:.2f} -> {state.mood:.2f} (Delta: {delta})")
        except Exception as e:
            logger.warning(f"[Heart] Mood Update Failed: {e}")

    async def consume_energy(self, chat_id: str, amount: float = 0.1):
        state = await self.get_state(chat_id)
        state.energy = max(0.0, state.energy - amount)
        state.total_replies += 1
        state.last_reply_time = time.time()
        self.db.save_chat_state(state)

    async def recover_energy_passive(self, chat_id: str):
        """被动恢复精力"""
        state = await self.get_state(chat_id)
        if state.energy < 1.0:
            state.energy = min(1.0, state.energy + 0.05)
            # 不频繁写库，仅在必要时