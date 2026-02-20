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
        基于最新消息更新情绪状态 (Mood Dynamics)
        Reference: MoodManager.analyze_text_mood
        """
        state = await self.get_state(chat_id)
        current_mood = state.mood
        
        # 1. 简单规则拦截 (Heuristic) - 命中直接短路，降低 System 1 压力
        negative_words = ["烦", "滚", "傻", "笨", "闭嘴"]
        positive_words = ["贴贴", "谢谢", "好棒", "开心", "厉害"]
        
        if any(w in text for w in negative_words):
            delta = -0.1
        elif any(w in text for w in positive_words):
            delta = 0.1
        else:
            # 2. LLM 分析 (System 1) 仅在文本中立时做深层判断
            prompt = f"""
            分析以下文本对聊天情绪的影响。当前情绪: {current_mood:.2f} (-1.0悲伤 ~ 1.0开心)。
            文本: "{text}"
            请严格返回JSON格式: {{"mood_delta": float, "reason": "string"}}
            mood_delta的允许范围是: -0.2 到 +0.2。如果文本是中性的，返回 0.0。
            """
            try:
                result = await self.gateway.call_judge(prompt)
                delta = result.get("mood_delta", 0.0)
                # 确保 delta 在合法范围内防暴走
                delta = max(-0.2, min(0.2, float(delta)))
            except Exception as e:
                logger.warning(f"[Heart] Mood Update LLM Failed: {e}")
                delta = 0.0

        # 更新状态
        state.mood = max(-1.0, min(1.0, current_mood + delta))
        self.db.save_chat_state(state) # 持久化
        
        if delta != 0.0:
            logger.debug(f"[Heart] 💓 情绪波动: {current_mood:.2f} -> {state.mood:.2f} (Delta: {delta:.2f})")

    async def consume_energy(self, chat_id: str, amount: float = 0.05):
        """
        消耗精力，每次处理消息默认消耗 5% (0.05)
        """
        state = await self.get_state(chat_id)
        old_energy = state.energy
        
        # 扣除能量并限制下限绝对值为 0.0
        state.energy = max(0.0, old_energy - amount)
        
        # 更新总回复数与最后活跃时间戳
        state.total_replies += 1
        state.last_reply_time = time.time()
        
        self.db.save_chat_state(state)
        logger.debug(f"[{chat_id}] 🔋 能量消耗结算: {old_energy:.2f} -> {state.energy:.2f}")

    async def recover_energy_passive(self, chat_id: str):
        """被动恢复精力"""
        state = await self.get_state(chat_id)
        if state.energy < 1.0:
            state.energy = min(1.0, state.energy + 0.05)
            # 不频繁写库，仅在必要时