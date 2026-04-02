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
from .relationship_engine import RelationshipEngine, RelationshipEvent

class StateEngine:
    """
    状态引擎 (System 1 - 异步化与脏数据懒加载重构版)
    职责:
    1. 维护 ChatState (Energy, Mood) 懒加载
    2. 维护 UserProfile (Social Score) 懒加载
    3. 管理多模态消息关联状态
    """
    def __init__(self, persistence: PersistenceManager, gateway: GlobalModelGateway, config=None, event_bus=None):  
        import threading 
        self.persistence = persistence
        self.gateway = gateway
        self.config = config if config else gateway.config
        
        # 内存态活跃数据
        self.chat_states: Dict[str, ChatState] = {}
        self.user_profiles: Dict[str, UserProfile] = {}
        self.mood_manager = MoodManager(gateway, self.config)        
        
        # 🟢 [彻底修复 Bug 1] 弃用 weakref.WeakValueDictionary()
        self._chat_locks = {}
        self._user_locks = {}
        
        self._last_cleanup_time = time.time()
        self._pool_lock_mutex = threading.Lock()
        
        self.event_bus = event_bus

        # Phase 5: 多维度关系引擎
        self.relationship_engine = RelationshipEngine(config=self.config)

        # 初始化内存微批处理计数器池与互斥锁
        self._message_counter_buffer = {}
        self._counter_lock = asyncio.Lock()

    # [修改] 函数位置：astrmai/Heart/state_engine.py -> StateEngine 类下
    def _get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        """彻底修复 Bug 1: 弱引用锁管理，自动回收 0 泄漏"""
        with self._pool_lock_mutex:
            lock = self._chat_locks.get(chat_id)
            if lock is None:
                lock = asyncio.Lock()
                self._chat_locks[chat_id] = lock
        return lock

    # [修改]
    def _get_user_lock(self, user_id: str) -> asyncio.Lock:
        """彻底修复 Bug 1: 弱引用锁管理，自动回收 0 泄漏"""
        with self._pool_lock_mutex:
            lock = self._user_locks.get(user_id)
            if lock is None:
                lock = asyncio.Lock()
                self._user_locks[user_id] = lock
        return lock

    # [修改] 函数位置：astrmai/Heart/state_engine.py -> StateEngine 类下
    async def _get_state_inner(self, chat_id: str) -> ChatState:
        """无并发锁的内部状态获取机制，必须仅在已获取锁的临界区内调用，防止 asyncio.Lock 不可重入死锁"""
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
            
        state.last_access_time = now
        state.is_dirty = True 
        
        self.chat_states[chat_id] = state
        return state

    # 位置: astrmai/Heart/state_engine.py
    # 状态: [修改] 
    async def get_state(self, chat_id: str) -> ChatState:
        """异步懒加载获取状态 (自带防并发穿透锁)"""
        async with self._get_chat_lock(chat_id):
            return await self._get_state_inner(chat_id)



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
        async with self._get_user_lock(user_id): # [修改] 切换细粒度锁
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
        """基于最新消息更新情绪状态 (消除 LLM 请求期间的并发脏读)"""
        # 1. 获取轻量级快照，在锁外进行耗时的 LLM 情绪运算（确保不会阻塞主线程并发状态机）
        current_state = await self.get_state(chat_id)
        snapshot_mood = current_state.mood
        
        tag, new_value = await self.mood_manager.analyze_text_mood(text, snapshot_mood)
        delta = new_value - snapshot_mood
        
        # 2. 运算结束后，下发增量给严格的锁内原子方法
        final_mood = await self.atomic_update_mood(chat_id, delta=delta)
        return tag, final_mood
    
    async def consume_energy(self, chat_id: str, amount: float = None):
        # 接入 Config 默认消耗
        if amount is None:
            amount = self.config.energy.cost_per_reply
            
        async with self._get_chat_lock(chat_id): 
            # 🟢 [核心修复 Bug 2] 绝不直接依赖 chat_states 字典快照，必须调用内部的懒加载方法从 DB 或内存安全拉取真实状态
            state = await self._get_state_inner(chat_id)

            old_energy = state.energy
            
            state.energy = max(0.0, old_energy - amount)
            state.total_replies += 1
            state.last_reply_time = time.time()
            state.is_dirty = True
            
            logger.debug(f"[{chat_id}] 🔋 能量结算: {old_energy:.2f} -> {state.energy:.2f}")

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

    async def calculate_and_update_affection(self, user_id: str, group_id: str, mood_tag: str, intensity: float = 1.0, message_text: str = ""):
        """
        [Phase 5 重写] 使用多维度关系引擎替代旧的简单加减法。
        纯算法驱动，零 LLM 消耗。
        """
        # 1. 懒加载用户画像
        profile = await self.get_user_profile(user_id)
        old_score = profile.social_score
        
        # 2. 纯算法交互类型检测
        event_type = RelationshipEvent.NORMAL_CHAT
        if message_text:
            event_type = self.relationship_engine.classify_interaction_type(message_text)
        
        # 3. 调用多维度关系引擎处理事件
        new_score = self.relationship_engine.process_event(
            user_id=user_id,
            event_type=event_type,
            intensity=intensity,
            mood_tag=mood_tag
        )
        
        # 4. 同步回写 social_score 到 UserProfile (向后兼容)
        async with self._get_user_lock(user_id):
            profile.social_score = new_score
            profile.last_seen = time.time()
            profile.is_dirty = True
        
        # 5. 事件总线广播
        if abs(new_score - old_score) > 0.1:
            logger.info(
                f"[StateEngine] 💗 好感度更新: 用户 {user_id} | "
                f"{old_score:.1f} → {new_score:.1f} (Δ{new_score - old_score:+.2f})"
            )
            if hasattr(self.event_bus, 'trigger_affection_change'):
                await self.event_bus.trigger_affection_change()
        else:
            vec = self.relationship_engine.get_or_create(user_id)
            logger.debug(
                f"[StateEngine] ⚖️ 好感度结算: 用户 {user_id} | "
                f"情绪: {mood_tag} | 事件: {event_type} | "
                f"trust:{vec.trust:.1f} fam:{vec.familiarity:.1f} "
                f"emo:{vec.emotion_bond:.1f} resp:{vec.respect:.1f} | "
                f"综合: {new_score:.1f}"
            )


            
    async def should_drop_by_energy(self, chat_id: str, msg_count: int) -> bool:
        """[新增] 中间组件 2 - 动态能量退避机制"""
        async with self._get_chat_lock(chat_id): # [修改] 切换细粒度锁
            if chat_id not in self.chat_states:
                return False
            state = self.chat_states[chat_id]
            current_energy = state.energy
            min_threshold = self.config.energy.min_reply_threshold
            
            # 如果精力大于一半则认为非常安全，不触发节流丢包
            if current_energy >= 0.5:
                return False
                
            import random
            if current_energy <= min_threshold:
                drop_prob = 1.0
            else:
                # 线性插值，越逼近 min_threshold，丢弃概率越高
                drop_prob = max(0.0, (0.5 - current_energy) / (0.5 - min_threshold))
                
            if random.random() < drop_prob:
                # 命中丢弃概率，执行回血并放弃处理
                recover_amount = msg_count * self.config.energy.cost_per_reply
                state.energy = min(1.0, state.energy + recover_amount)
                state.is_dirty = True
                logger.debug(f"[{chat_id}] 🔋 动态能量退避生效，命中丢弃概率({drop_prob:.2f})。恢复精力: +{recover_amount:.2f} -> {state.energy:.2f}")
                return True
                
            return False   

    # [新增] 函数位置：astrmai/Heart/state_engine.py -> StateEngine 类下
    async def increment_user_message_count(self, user_id: str):
        """
        🟢 [修改] 废弃此处的全局内存计数池，改为在 ReplyEngine 中仅对私聊进行计数。
        由于该方法仍然被外部旧版 main.py 调用，故保留空函数以防报错。
        """
        pass
    async def flush_message_counters(self):
        """
        🟢 [修改] 由于废弃了全局内存计数池，此处保持为空逻辑即可。
        """
        pass
    
    async def atomic_update_mood(self, chat_id: str, delta: float = 0.0, absolute_val: float = None) -> float:
        """
        [修改] 严格原子化 Read-Compute-Write 事务，彻底消除 TOCTOU 竞态条件
        """
        async with self._get_chat_lock(chat_id):
            # 🟢 [核心修复 Bug 3] 进入锁内后，绝不能轻信外面的快照引用，必须当场获取最新内存！
            state = await self._get_state_inner(chat_id)
            
            if absolute_val is not None:
                state.mood = max(-1.0, min(1.0, absolute_val))
            else:
                state.mood = max(-1.0, min(1.0, state.mood + delta))
                
            state.is_dirty = True
            
            # 兼容底层落盘
            if hasattr(self, 'persistence'):
                await self.persistence.save_chat_state(chat_id, state)
            elif hasattr(self, 'db'):
                await self.db.save_chat_state(chat_id, state)
                
            return state.mood
        
    async def consume_energy(self, chat_id: str, amount: float = None):
        # [新增] 拦截：私聊绝对专注，不消耗机器人精力
        if "FriendMessage" in chat_id:
            return

        # 接入 Config 默认消耗
        if amount is None:
            amount = self.config.energy.cost_per_reply
            
        async with self._get_chat_lock(chat_id): 
            # 🟢 [核心修复 Bug 2] 绝不直接依赖 chat_states 字典快照，必须调用内部的懒加载方法从 DB 或内存安全拉取真实状态
            state = await self._get_state_inner(chat_id)

            old_energy = state.energy
            
            state.energy = max(0.0, old_energy - amount)
            state.total_replies += 1
            state.last_reply_time = time.time()
            state.is_dirty = True
            
            logger.debug(f"[{chat_id}] 🔋 能量结算: {old_energy:.2f} -> {state.energy:.2f}")        