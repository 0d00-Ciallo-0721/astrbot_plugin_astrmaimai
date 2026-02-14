### 📄 features/proactive_task.py
import asyncio
import time
from typing import TYPE_CHECKING
from astrbot.api import logger
from astrbot.api.star import Context

from ..config import HeartflowConfig
from ..core.state_manager import StateManager
from ..datamodels import SensoryInput

if TYPE_CHECKING:
    from ..core.mind_scheduler import MindScheduler

class ProactiveTask:
    """
    (v2.0) 生物节律控制器
    职责：
    1. 状态自然衰减 (Energy/Mood Decay)
    2. 空闲检测 -> 触发 'Boredom' 冲动
    """

    def __init__(self, 
                 context: Context, 
                 config: HeartflowConfig, 
                 state_manager: StateManager,
                 scheduler: "MindScheduler"
                 ):
        self.context = context
        self.config = config
        self.state_manager = state_manager
        self.scheduler = scheduler
        self._is_running = False

    async def run(self):
        """启动后台循环"""
        self._is_running = True
        logger.info("💓 [BioRhythm] Proactive task started.")
        
        while self._is_running:
            try:
                await asyncio.sleep(60) # 1分钟心跳
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"BioRhythm Tick Error: {e}")

    async def _tick(self):
        """
        心跳逻辑
        """
        now = time.time()
        states = self.state_manager.get_all_states_unsafe() # 获取引用
        
        for chat_id, state in list(states.items()):
            # 1. 状态衰减 (Emotion/Energy Decay)
            # 简单线性衰减，向平静值(0.0)回归
            if abs(state.mood) > 0.1:
                state.mood *= 0.95 # 缓慢回归平静
                state.is_dirty = True
                
            # 精力恢复
            if state.energy < self.config.default_energy:
                state.energy = min(self.config.default_energy, state.energy + self.config.energy_recovery_rate)
                state.is_dirty = True

            # 2. 主动发起 (Proactive Chat) / 无聊机制
            # 如果不是 Enable Heartflow，跳过
            if not self.config.enable_heartflow:
                continue
                
            # 检查是否空闲过久 (例如 2小时 ~ 7200秒)
            # 且当前不在思考中
            idle_time = now - state.last_reply_time
            if idle_time > 7200 and not state.lock.locked():
                # 概率触发 (避免所有群同时说话)
                import random
                if random.random() < 0.05: # 5% 概率每分钟
                    logger.info(f"💓 [BioRhythm] Boredom trigger for {chat_id}")
                    
                    # 构造“内部感官信号”
                    # 这不是来自用户的消息，而是来自内部的冲动
                    fake_input = SensoryInput(
                        text="(System: User has been silent for a while. You feel bored.)",
                        images=[],
                        sender_id="system",
                        sender_name="System",
                        group_id=chat_id,
                        raw_event=None # 内部事件无原始 Event
                    )
                    
                    # 注入调度器
                    await self.scheduler.dispatch(chat_id, fake_input, state)

    def cancel(self):
        self._is_running = False