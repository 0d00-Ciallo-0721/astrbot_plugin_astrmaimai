import asyncio
import time
from typing import List
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger

# 引用接口，避免直接依赖实现类
from .state_engine import StateEngine
from .judge import Judge
from .sensors import PreFilters

class AttentionGate:
    """
    注意力门控 (System 1)
    职责: 消息聚合 (Debounce) 与 路由 (Focus vs Background)
    Reference: HeartCore/core/message_handler.py
    """
    def __init__(self, state_engine: StateEngine, judge: Judge, sensors: PreFilters, system2_callback):
        self.state = state_engine
        self.judge = judge
        self.sensors = sensors
        self.sys2_process = system2_callback # 回调函数，指向 System2.process

        # 运行时内存池 {chat_id: {'pool': [], 'timer': Task}}
        self.focus_pools = {} 
        self.background_pools = {}

    async def process_event(self, event: AstrMessageEvent):
        chat_id = event.unified_msg_origin
        sender_id = event.get_sender_id()
        self_id = event.get_self_id()

        # 0. 预过滤
        if self.sensors.is_noise(event):
            return

        # 1. 唤醒检测
        is_wakeup = self.sensors.is_wakeup_signal(event, self_id)

        # 2. 判官介入 (Judge)
        # 注意: 这里简化了逻辑，先判断是否值得回复，再决定是否进入聚合池
        # 实际 HeartCore 是先聚合再判断，这里为了响应速度，对单条消息预判
        should_reply = await self.judge.evaluate(chat_id, event.message_str, is_wakeup)

        if should_reply:
            # >>> 进入 Focus Pool (准备回复) >>>
            await self._add_to_focus(chat_id, event)
        else:
            # >>> 进入 Background Pool (背景噪音) >>>
            self._add_to_background(chat_id, event)

    async def _add_to_focus(self, chat_id: str, event: AstrMessageEvent):
        if chat_id not in self.focus_pools:
            self.focus_pools[chat_id] = {'queue': [], 'task': None}
        
        pool = self.focus_pools[chat_id]
        pool['queue'].append(event)
        
        # 防抖逻辑 (Debounce): 如果有任务在跑，重置计时？
        # 这里采用 HeartCore 的 _wait_and_process 逻辑
        if pool['task'] is None or pool['task'].done():
            pool['task'] = asyncio.create_task(self._wait_and_process(chat_id))

    def _add_to_background(self, chat_id: str, event: AstrMessageEvent):
        if chat_id not in self.background_pools:
            self.background_pools[chat_id] = []
        
        bg_pool = self.background_pools[chat_id]
        bg_pool.append(event)
        
        # 简单溢出清理
        if len(bg_pool) > 20:
            bg_pool.pop(0)

    async def _wait_and_process(self, chat_id: str):
        """
        等待消息聚合完成，然后打包发送给 System 2
        """
        logger.debug(f"[{chat_id}] Attention Window Open...")
        await asyncio.sleep(2.0) # 简单 2秒防抖
        
        pool = self.focus_pools.get(chat_id)
        if not pool or not pool['queue']:
            return

        messages = list(pool['queue'])
        pool['queue'].clear() # 清空队列
        
        logger.info(f"[{chat_id}] System 1 -> System 2 ({len(messages)} msgs)")
        
        # 调用 System 2 处理 (Fire and Forget or Await?)
        # 这里使用 await 确保顺序，但 main.py 中可能是 create_task
        await self.sys2_process(chat_id, messages)