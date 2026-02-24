# astrmai/Heart/state_engine.py
import time
import datetime
import asyncio
from typing import Dict, Optional
from astrbot.api import logger
from ..infra.persistence import PersistenceManager
from ..infra.datamodels import ChatState, UserProfile
from ..infra.gateway import GlobalModelGateway
from .mood_manager import MoodManager

class StateEngine:
    """
    状态引擎 (System 1 - 异步化与脏数据懒加载重构版)
    职责:
    1. 维护 ChatState (Energy, Mood) 懒加载
    2. 维护 UserProfile (Social Score) 懒加载
    3. 管理多模态消息关联状态
    """
    def __init__(self, persistence: PersistenceManager, gateway: GlobalModelGateway):
        self.persistence = persistence
        self.gateway = gateway
        
        # 内存态活跃数据
        self.chat_states: Dict[str, ChatState] = {}
        self.user_profiles: Dict[str, UserProfile] = {}
        # [新增] 初始化情绪管理器
        self.mood_manager = MoodManager(gateway)        
        # 并发防击穿锁
        self._lock = asyncio.Lock()

    async def get_state(self, chat_id: str) -> ChatState:
        """异步懒加载获取状态"""
        async with self._lock:
            now = time.time()
            if chat_id in self.chat_states:
                state = self.chat_states[chat_id]
                state.last_access_time = now
                self._check_daily_reset(state)
                return state
            
            data = await self.persistence.load_chat_state(chat_id)
            if data:
                state = ChatState(**data)
            else:
                state = ChatState(chat_id=chat_id, energy=0.8, mood=0.0)
                state.last_reset_date = datetime.date.today().isoformat()
                
            # 补齐运行时动态属性
            state.lock = asyncio.Lock()
            state.last_access_time = now
            state.is_dirty = True 
            
            self.chat_states[chat_id] = state
            return state

    def _check_daily_reset(self, state: ChatState):
        today = datetime.date.today().isoformat()
        if state.last_reset_date != today:
            state.last_reset_date = today
            state.energy = min(1.0, state.energy + 0.2)
            state.mood = 0.0
            state.is_dirty = True

    async def get_user_profile(self, user_id: str) -> UserProfile:
        """异步懒加载用户画像"""
        async with self._lock:
            now = time.time()
            if user_id in self.user_profiles:
                profile = self.user_profiles[user_id]
                profile.last_access_time = now
                return profile
            
            data = await self.persistence.load_user_profile(user_id)
            if data:
                profile = UserProfile(**data)
            else:
                profile = UserProfile(user_id=user_id, name="未知用户")
                
            profile.last_access_time = now
            profile.is_dirty = True
            
            self.user_profiles[user_id] = profile
            return profile

    async def update_mood(self, chat_id: str, text: str):
        """
        基于最新消息更新情绪状态 (Mood Dynamics)
        """
        state = await self.get_state(chat_id)
        
        # 调用情绪管理器获取分析结果
        tag, new_value = await self.mood_manager.analyze_text_mood(text, state.mood)
        
        # 更新状态
        state.mood = new_value
        self.db.save_chat_state(state) # 持久化
        
        return tag, new_value
    async def consume_energy(self, chat_id: str, amount: float = 0.05):
        state = await self.get_state(chat_id)
        old_energy = state.energy
        
        state.energy = max(0.0, old_energy - amount)
        state.total_replies += 1
        state.last_reply_time = time.time()
        state.is_dirty = True
        logger.debug(f"[{chat_id}] 🔋 能量结算: {old_energy:.2f} -> {state.energy:.2f}")

    # [新增] 社交好感度闭环
    async def update_social_score_from_fact(self, user_id: str, impact_score: float):
        """
        [New] 基于交互事实的动态好感度闭环
        impact_score: 正数增加好感，负数扣除
        """
        if not user_id: return
        
        # 获取 UserProfile (利用 db service)
        profile = self.db.get_user_profile(user_id)
        if not profile:
            profile = UserProfile(user_id=user_id, name="Unknown")
            
        old_score = profile.social_score
        
        # 更新分数
        profile.social_score += impact_score
        # 限制范围 -100 到 100
        profile.social_score = max(-100.0, min(100.0, profile.social_score))
        
        profile.last_seen = time.time()
        self.db.save_user_profile(profile)
        
        logger.info(f"[Social] 🤝 用户 {profile.name}({user_id}) 好感度变更: {old_score:.1f} -> {profile.social_score:.1f} (Δ{impact_score})")