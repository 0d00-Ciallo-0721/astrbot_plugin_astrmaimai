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
        msg_str = event.message_str.strip()
        is_cmd = await self.sensors.is_command(msg_str)
        
        # =================================================================
        # 0. 安全网与预过滤 (The Firewall)
        # =================================================================
        # 异步调用强化后的预过滤器

        if is_cmd:
            # 【完善】给事件打上标签，供后续 Subconscious 识别
            setattr(event, "is_command_trigger", True)
            logger.info(f"[AstrMai-Sensor] 🛡️ 识别到指令: {msg_str[:10]}... 已标记并拦截。")
            return # 彻底拦截，不进入 System 2
                
        should_process = await self.sensors.should_process_message(event)
        
        # 如果判定为无需处理，或被强制打上了指令标记，立即执行短路阻断
        if not should_process or event.get_extra("astrmai_is_command"):
            return
        # 检测是否命中指令防火墙

        # =================================================================
        # 1. 唤醒检测与判官路由
        # =================================================================
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
            
        # 提取并清空当前聚合队列
        events = pool['queue'][:]
        pool['queue'].clear()
        
        # 合并消息内容 (防抖期间的多条消息视为同一上下文)
        merged_text = "\n".join([e.message_str for e in events])
        logger.info(f"[{chat_id}] 聚合了 {len(events)} 条消息, 准备进入 System 2。")
        
        # 选出最后一条事件作为对象载体，并将合并后的文本动态挂载
        main_event = events[-1]
        main_event.merged_text = merged_text 
        
        if self.sys2_process:
            await self.sys2_process(main_event)