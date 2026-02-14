# heartflow/core/state_manager.py
# (v4.13 Refactored - Async, Lazy Load & Cache)
import time
import datetime
import asyncio
from typing import Dict, List, Optional
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..datamodels import ChatState, BrainActionPlan, UserProfile, LastMessageMetadata
from ..config import HeartflowConfig
from ..persistence import PersistenceManager

class StateManager:
    """
    (v4.13) 状态管理器 - 重构版
    职责：
    1. 管理 ChatState 和 UserProfile 的生命周期 (Load/Cache/Save)。
    2. 提供异步的获取接口，实现懒加载。
    3. 维护内存中的“脏数据”标记，供 MaintenanceTask 定时回写。
    """

    def __init__(self, config: HeartflowConfig, persistence: PersistenceManager):
        self.config = config
        self.persistence = persistence
        
        # 内存缓存 (只存储当前活跃的数据)
        # Key: chat_id / user_id
        self.chat_states: Dict[str, ChatState] = {}
        self.user_profiles: Dict[str, UserProfile] = {}
        
        # 并发控制锁 (防止同一ID同时触发多次DB读取)
        self._lock = asyncio.Lock()

    # =================================================================
    # 1. ChatState 管理 (异步懒加载)
    # =================================================================

    async def get_chat_state(self, chat_id: str) -> ChatState:
        """
        获取群聊状态 (核心入口)
        策略: Cache -> DB -> Create New
        """
        async with self._lock:
            now = time.time()
            
            # A. 命中内存缓存
            if chat_id in self.chat_states:
                state = self.chat_states[chat_id]
                state.last_access_time = now
                self._check_daily_reset(state, chat_id)
                return state
            
            # B. 未命中，查数据库
            data = await self.persistence.load_chat_state(chat_id)
            
            if data:
                # 反序列化并初始化运行时字段
                state = ChatState(**data)
                # 补充运行时对象
                state.last_msg_info = LastMessageMetadata()
                state.lock = asyncio.Lock()
                state.last_access_time = now
                
                self.chat_states[chat_id] = state
                logger.debug(f"State loaded from DB: {chat_id}")
                self._check_daily_reset(state, chat_id)
                return state
            
            # C. 数据库无记录，新建状态
            new_state = ChatState(energy=self.config.energy_initial)
            new_state.last_reset_date = datetime.date.today().isoformat()
            new_state.last_access_time = now
            new_state.is_dirty = True # 标记为需写入
            
            self.chat_states[chat_id] = new_state
            logger.info(f"New State created: {chat_id}")
            return new_state
        
    def _init_runtime_fields(self, state: ChatState):
        """初始化运行时字段 (Lock, Buffer 等无法序列化的对象)"""
        if not hasattr(state, 'lock') or state.lock is None:
            state.lock = asyncio.Lock()
        if not hasattr(state, 'accumulation_pool') or state.accumulation_pool is None:
            state.accumulation_pool = []
        if not hasattr(state, 'background_buffer') or state.background_buffer is None:
            state.background_buffer = []
        if not hasattr(state, 'last_msg_info') or state.last_msg_info is None:
            state.last_msg_info = LastMessageMetadata()
        
        # 重置计数器
        state.message_counter = 0
        state.poke_spam_count = 0
        state.poke_spam_senders = []

    def _check_daily_reset(self, state: ChatState, chat_id: str):
        """检查并执行每日重置逻辑"""
        today = datetime.date.today().isoformat()
        if state.last_reset_date != today:
            state.last_reset_date = today
            # 每日回复精力 +0.2，心情归零
            state.energy = min(1.0, state.energy + 0.2)
            state.mood = 0.0
            state.is_dirty = True # 标记脏数据
            logger.debug(f"执行每日状态重置: {chat_id[:10]}... | E:{state.energy:.2f}")

    def get_all_states_unsafe(self) -> Dict[str, ChatState]:
        """获取当前内存中的所有状态 (仅供定时任务使用)"""
        return self.chat_states


    # =================================================================
    # 2. UserProfile 管理 (异步懒加载)
    # =================================================================

    async def get_user_profile(self, user_id: str) -> UserProfile:
        """
        异步获取用户画像。
        流程: 内存 -> DB -> 新建
        """
        async with self._lock:
            now = time.time()
            
            # A. 内存
            if user_id in self.user_profiles:
                profile = self.user_profiles[user_id]
                profile.last_access_time = now
                return profile
            
            # B. 数据库
            data = await self.persistence.load_user_profile(user_id)
            if data:
                profile = UserProfile(**data)
                profile.last_access_time = now
                self.user_profiles[user_id] = profile
                return profile
            
            # C. 新建
            new_profile = UserProfile(user_id=user_id, name="未知用户")
            new_profile.identity = self.config.default_user_identity
            new_profile.last_access_time = now
            
            new_profile.is_dirty = True
            
            self.user_profiles[user_id] = new_profile
            return new_profile
            
    def get_all_user_profiles_unsafe(self) -> Dict[str, UserProfile]:
        """获取当前内存中的所有画像 (仅供定时任务使用)"""
        return self.user_profiles

    def update_user_profile(self, event: AstrMessageEvent):
        """更新用户活跃时间与昵称 (仅在内存存在时)"""
        # 注意：这里不执行异步加载，只更新已存在的。
        # 调用方应确保在处理消息前已调用 await get_user_profile 加载数据
        sender_id = event.get_sender_id()
        if sender_id and sender_id in self.user_profiles:
            profile = self.user_profiles[sender_id]
            
            current_name = event.get_sender_name()
            if current_name and current_name != profile.name:
                profile.name = current_name
                profile.is_dirty = True # Name 改变
            
            profile.last_seen = time.time()
            profile.last_access_time = time.time()
            profile.is_dirty = True # LastSeen 改变 (Phase 1 定义为持久化字段)

    def update_user_profile(self, event: AstrMessageEvent):
        """更新用户活跃时间与昵称 (仅在内存存在时)"""
        # 注意：这里不执行异步加载，只更新已存在的。
        # 调用方应确保在处理消息前已调用 await get_user_profile 加载数据
        sender_id = event.get_sender_id()
        if sender_id and sender_id in self.user_profiles:
            profile = self.user_profiles[sender_id]
            
            # 1. [修复] 增加画像生成计数器
            profile.message_count_for_profiling += 1
            
            # 2. [修复] 更新群聊足迹 (用于画像生成的历史记录回溯)
            chat_id = event.unified_msg_origin
            if chat_id:
                if chat_id not in profile.group_footprints:
                    profile.group_footprints[chat_id] = {
                        "last_active_time": 0.0, 
                        "message_weight": 0
                    }
                
                # 更新足迹数据
                fp = profile.group_footprints[chat_id]
                fp["last_active_time"] = time.time()
                fp["message_weight"] += 1

            # 3. 更新基础信息
            current_name = event.get_sender_name()
            if current_name and current_name != profile.name:
                profile.name = current_name
                profile.is_dirty = True # Name 改变
            
            profile.last_seen = time.time()
            profile.last_access_time = time.time()
            profile.is_dirty = True # 标记为脏数据，等待写入 DB
            

    # =================================================================
    # 3. 状态更新逻辑 (业务逻辑)
    # =================================================================

    def get_all_states(self) -> Dict[str, ChatState]:
        """
        获取当前*缓存中*的所有状态
        注意：不再是全量数据，仅包含活跃群聊。
        主要供 ProactiveTask 使用。
        """
        return self.chat_states
    
    def get_all_user_profiles(self) -> Dict[str, UserProfile]:
        """获取所有 *已加载到内存* 的画像"""
        return self.user_profiles

    # 标记 Dirty 的辅助方法
    def mark_chat_dirty(self, chat_id: str):
        if chat_id in self.chat_states:
            self.chat_states[chat_id].is_dirty = True
            self.chat_states[chat_id].last_access_time = time.time()


    def _apply_passive_decay(self, chat_id: str):
        """被动精力恢复 (定时任务调用)"""
        if chat_id in self.chat_states:
            state = self.chat_states[chat_id]
            minutes_silent = 999
            if state.last_reply_time != 0:
                minutes_silent = (time.time() - state.last_reply_time) / 60
            
            if 60 < minutes_silent < 999:
                if state.energy < 0.8:
                    state.energy = min(0.8, state.energy + 0.1)
                    state.is_dirty = True
                    # 定时任务也会刷新 access_time，防止刚恢复就被淘汰
                    # 但也可以不刷新，让不活跃的群被淘汰出内存
                    # state.last_access_time = time.time()

    def _update_active_state(self, event: AstrMessageEvent, plan: BrainActionPlan, is_poke_or_nickname: bool):
        """
        更新主动回复相关的运行时状态
        注意：此方法假设 state 已在 MessageHandler 中加载到内存
        """
        chat_id = event.unified_msg_origin
        if chat_id in self.chat_states:
            state = self.chat_states[chat_id]
            state.last_reply_time = time.time()
            state.total_replies += 1
            state.total_messages += 1
            state.consecutive_reply_count += 1
            state.judgment_mode = "single"
            state.message_counter = 0
            state.last_access_time = time.time()
            # 注意：这里只更新了运行时字段(Runtime)，不需要 set dirty
            # 除非业务逻辑认为 total_replies 需要严格持久化，这里按 Phase 1 设计归为 Runtime

    def _update_passive_state(self, event: AstrMessageEvent, plan: BrainActionPlan, batch_size: int = 1):
        """更新被动状态 (精力恢复)"""
        chat_id = event.unified_msg_origin
        if chat_id in self.chat_states:
            state = self.chat_states[chat_id]
            state.total_messages += batch_size
            
            old_energy = state.energy
            state.energy = min(1.0, state.energy + (self.config.energy_recovery_rate * batch_size))
            state.consecutive_reply_count = 0
            state.last_access_time = time.time()
            
            if old_energy != state.energy:
                state.is_dirty = True # Energy 改变，需持久化

    def _consume_energy_for_proactive_reply(self, chat_id: str):
        """主动发起话题消耗精力"""
        if chat_id in self.chat_states:
            state = self.chat_states[chat_id]
            state.last_reply_time = time.time()
            state.total_replies += 1
            state.total_messages += 1
            
            state.energy = max(0.1, state.energy - self.config.energy_decay_rate)
            state.judgment_mode = "single"
            state.message_counter = 0
            state.last_access_time = time.time()
            state.is_dirty = True # Energy 改变

    # =================================================================
    # 4. 其他辅助
    # =================================================================

    def update_social_score_from_fact(self, user_id: str, impact_score: float):
        """社交事实更新分数"""
        if not self.config.enable_user_profiles or not user_id: return
        
        # 这是一个异步操作的同步入口，如果 profile 不在内存，可能无法更新
        # 建议改为 async，或者只更新内存中已存在的
        if user_id in self.user_profiles:
            profile = self.user_profiles[user_id]
            profile.social_score += impact_score
            profile.is_dirty = True
            profile.last_access_time = time.time()
            logger.info(f"💖 社交事实：用户 {user_id} 好感度变更 {impact_score:+.1f} -> {profile.social_score:.1f}")

    def reset_chat_state(self, chat_id: str):
        """重置群聊状态"""
        if chat_id in self.chat_states:
            del self.chat_states[chat_id]
            # 同时也应该从 DB 删除，需要调用 persistence
            # 这里暂时只清除内存，下次 get 会新建
            # 若需彻底重置，需增加 persistence.delete_chat_state
            logger.info(f"心流状态已重置 (内存): {chat_id}")
            return True
        return False
    
    def get_chat_state_readonly(self, chat_id: str) -> Optional[ChatState]:
        """同步读取 (仅缓存)"""
        if chat_id in self.chat_states:
            self.chat_states[chat_id].last_access_time = time.time()
            return self.chat_states[chat_id]
        return None    
    


    async def apply_state_diff(self, session_id: str, diff: Dict[str, Any]):
        """
        (v2.0) 应用状态变更差分
        供 ImpulseEngine 调用，统一更新状态
        """
        if not diff: return
        
        state = await self.get_chat_state(session_id)
        
        # 批量更新属性
        for key, value in diff.items():
            if hasattr(state, key):
                setattr(state, key, value)
        
        state.is_dirty = True
        logger.debug(f"State updated for {session_id}: {diff.keys()}")    