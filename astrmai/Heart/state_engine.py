# astrmai/Heart/state_engine.py
import time
import datetime
import asyncio
from typing import Dict, Optional, List 
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
    def __init__(self, persistence: PersistenceManager, gateway: GlobalModelGateway, config=None):
        self.persistence = persistence
        self.gateway = gateway
        self.config = config if config else gateway.config
        
        # 内存态活跃数据
        self.chat_states: Dict[str, ChatState] = {}
        self.user_profiles: Dict[str, UserProfile] = {}
        # 初始化情绪管理器
        self.mood_manager = MoodManager(gateway, self.config)        
        # 并发防击穿锁
        self._lock = asyncio.Lock()
        
        # [新增] 引入事件总线
        from ..infra.event_bus import EventBus
        self.event_bus = EventBus()

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
            # 接入 Config
            state.energy = min(1.0, state.energy + self.config.energy.daily_recovery)
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
        """基于最新消息更新情绪状态 (Mood Dynamics)"""
        state = await self.get_state(chat_id)
        
        tag, new_value = await self.mood_manager.analyze_text_mood(text, state.mood)
        
        state.mood = new_value
        await self.persistence.save_chat_state(chat_id, state) # 修复原版 self.db 调用报错
        
        return tag, new_value
    
    async def consume_energy(self, chat_id: str, amount: float = None):
        # 接入 Config 默认消耗
        if amount is None:
            amount = self.config.energy.cost_per_reply
            
        state = await self.get_state(chat_id)
        old_energy = state.energy
        
        state.energy = max(0.0, old_energy - amount)
        state.total_replies += 1
        state.last_reply_time = time.time()
        state.is_dirty = True
        logger.debug(f"[{chat_id}] 🔋 能量结算: {old_energy:.2f} -> {state.energy:.2f}")

    # [新增] 社交好感度闭环
    async def update_social_score_from_fact(self, user_id: str, impact_score: float):
        """[New] 基于交互事实的动态好感度闭环"""
        if not user_id: return
        
        # 修复原版 self.db.get_user_profile 调用
        profile = await self.get_user_profile(user_id)
        if not profile:
            profile = UserProfile(user_id=user_id, name="Unknown")
            
        old_score = profile.social_score
        
        # 更新分数
        profile.social_score += impact_score
        # 限制范围 -100 到 100
        profile.social_score = max(-100.0, min(100.0, profile.social_score))
        
        profile.last_seen = time.time()
        profile.is_dirty = True # 依赖周期落盘
        
        logger.info(f"[Social] 🤝 用户 {profile.name}({user_id}) 好感度变更: {old_score:.1f} -> {profile.social_score:.1f} (Δ{impact_score})")

    def get_active_states(self) -> List[ChatState]:
        """[Phase 6] 获取当前内存中活跃的所有群状态"""
        return list(self.chat_states.values())

    def get_active_profiles(self) -> List[UserProfile]:
        """[Phase 6] 获取当前内存中活跃的所有用户画像"""
        return list(self.user_profiles.values())

    def apply_natural_decay(self, state: ChatState):
        """
        [Phase 6] 自然状态衰减 (Metabolism)
        """
        now = time.time()
        minutes_silent = 999
        if state.last_reply_time != 0:
            minutes_silent = (now - state.last_reply_time) / 60
        
        # 2. 精力恢复 (Energy Recovery) 接入 Config
        if minutes_silent > self.config.energy.recovery_silence_min and state.energy < 0.8:
            state.energy = min(0.8, state.energy + 0.1)
            state.is_dirty = True
            logger.debug(f"[{state.chat_id}] 🌙 自然代谢: 精力恢复 -> {state.energy:.2f}")

        # 3. 情绪平复 (Mood Decay) 接入 Config
        if now - state.last_passive_decay_time > self.config.mood.decay_interval:
            state.last_passive_decay_time = now
            decay_rate = self.config.mood.decay_rate 
            
            if state.mood > 0:
                state.mood = max(0.0, state.mood - decay_rate)
            elif state.mood < 0:
                state.mood = min(0.0, state.mood + decay_rate)
            
            state.is_dirty = True
            logger.debug(f"[{state.chat_id}] 🌙 自然代谢: 情绪平复 -> {state.mood:.2f}")


    async def calculate_and_update_affection(self, user_id: str, group_id: str, mood_tag: str, intensity: float = 1.0):
        """
        [新增] 基于 System 1 解析出的情绪标签，动态计算并更新用户的好感度(Affection)。
        """
        async with self._lock:
            # 懒加载获取/初始化 UserProfile
            if user_id not in self.user_profiles:
                # 兼容旧逻辑，如果没有持久化获取方法，先初始化一个内存态对象
                self.user_profiles[user_id] = UserProfile(user_id=user_id)
            
            profile = self.user_profiles[user_id]
            
            # 定义情绪对好感度的影响权重 (可根据自学习模块的逻辑精调)
            affection_deltas = {
                "happy": 2.0,
                "excited": 3.0,
                "playful": 1.5,
                "calm": 0.5,
                "sad": -1.0,
                "angry": -3.0,
                "anxious": -1.0
            }
            
            delta = affection_deltas.get(mood_tag, 0.0) * intensity
            
            # 应用变化并限制在 -100 到 100 之间
            old_score = profile.social_score
            profile.social_score = max(-100.0, min(100.0, profile.social_score + delta))
            
            if old_score != profile.social_score:
                profile.is_dirty = True
                logger.debug(f"[StateEngine] 💗 好感度更新: 用户 {user_id} 在群 {group_id} 的好感度 {old_score:.1f} -> {profile.social_score:.1f} (Δ{delta:.1f})")
                
                # 触发好感度变更事件广播，通知 Brain 或后续的 ContextInjector 刷新系统提示词
                self.event_bus.trigger_affection_change()            