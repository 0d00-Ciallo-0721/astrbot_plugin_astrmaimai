# astrmai/Heart/attention.py
import asyncio
import time
from typing import List, Dict, Any
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger

from .state_engine import StateEngine
from .judge import Judge
from .sensors import PreFilters

class AttentionGate:
    """
    注意力门控 (System 1: Dual-Pool Locked Version)
    职责: 双池路由，滑动窗口聚合，防止高并发打断。
    """
    def __init__(self, state_engine: StateEngine, judge: Judge, sensors: PreFilters, system2_callback, config=None):
        self.state_engine = state_engine
        self.judge = judge
        self.sensors = sensors
        self.sys2_process = system2_callback 
        self.config = config if config else state_engine.config


    async def process_event(self, event: AstrMessageEvent):
        chat_id = event.unified_msg_origin
        sender_id = event.get_sender_id()
        self_id = event.get_self_id()
        is_cmd = await self.sensors.is_command(msg_str)

        if is_cmd:
            # 【完善】给事件打上标签，供后续 Subconscious 识别
            setattr(event, "is_command_trigger", True)
            logger.info(f"[AstrMai-Sensor] 🛡️ 识别到指令: {msg_str[:10]}... 已标记并拦截。")
            return # 彻底拦截，不进入 System 2


        # 1. 预过滤与指令隔离
        should_process = await self.sensors.should_process_message(event)
        if not should_process or event.get_extra("astrmai_is_command"):
            return

        chat_state = await self.state_engine.get_state(chat_id)
        
        # 更新 LastMessageMetadata (图片等)
        extracted_images = event.get_extra("extracted_image_urls") or []
        if extracted_images:
            await self.state_engine.persistence.add_last_message_meta(
                chat_id, sender_id, True, extracted_images
            )

        # 2. 唤醒与快速判决
        is_wakeup = self.sensors.is_wakeup_signal(event, self_id)
        plan = await self.judge.evaluate(chat_id, event.message_str, is_wakeup)

        # =================================================================
        # 3. 双池路由与并发锁 (The Core Attention Logic)
        # =================================================================
        
        # --- 场景 A: 大脑正在思考 (锁已被占用) ---
        if chat_state.lock.locked():
            if chat_state.owner_id == sender_id:
                # User A (发起者) 继续补充消息，放入焦点池
                chat_state.accumulation_pool.append(event)
                event.set_extra("astrmai_timestamp", time.time())
                logger.debug(f"[{chat_id}] 🧠 Busy: Owner 追加消息 -> 累积池")
            else:
                # User B/C 的消息，放入背景池
                chat_state.background_buffer.append(event)
            return

        # --- 场景 B: 大脑空闲 ---
        if plan.action == "REPLY" or plan.action == "WAIT":
            logger.info(f"[{chat_id}] 👁️ 注意力聚焦! Owner: {sender_id}")
            await chat_state.lock.acquire() # 上锁！
            
            try:
                chat_state.owner_id = sender_id
                chat_state.accumulation_pool.append(event)
                # 启动防抖聚合计时器
                chat_state.wakeup_timer = asyncio.create_task(self._wait_and_process(chat_id, chat_state))
            except Exception as e:
                logger.error(f"启动唤醒任务失败，释放锁: {e}")
                if chat_state.lock.locked():
                    chat_state.lock.release()
                chat_state.owner_id = None
        else:
            # 被 IGNORE 的消息，作为环境上下文放入背景池 (接入 Config)
            chat_state.background_buffer.append(event)
            if len(chat_state.background_buffer) > self.config.attention.bg_pool_size:
                chat_state.background_buffer.pop(0)

    async def _wait_and_process(self, chat_id: str, state: Any):
        """
        滑动窗口聚合：等待 User A 说完。
        """
        try:
            logger.debug(f"[{chat_id}] ⏱️ 开启聚合滑动窗口...")
            no_msg_start_time = time.time()
            last_pool_len = 0
            debounce_window = self.config.attention.debounce_window
            
            # 动态防抖循环
            while True:
                current_pool_len = len(state.accumulation_pool)
                if current_pool_len > last_pool_len:
                    # 发现新消息，重置静默时间
                    no_msg_start_time = time.time()
                    last_pool_len = current_pool_len
                    ts = state.accumulation_pool[-1].get_extra("astrmai_timestamp")
                    if ts: no_msg_start_time = ts
                
                # 如果超过防抖窗口没有新消息，跳出循环 (接入 Config)
                if time.time() - no_msg_start_time > debounce_window:
                    break
                await asyncio.sleep(0.5)

            # 聚合结算
            events_to_process = list(state.accumulation_pool)
            state.accumulation_pool.clear()
            
            if events_to_process:
                logger.info(f"[{chat_id}] 📦 聚合结束，将 {len(events_to_process)} 条消息打包送入 System 2。")
                # 选出主事件作为载体上抛
                main_event = events_to_process[-1]
                
                # 如果有注入的系统级回调，则执行
                if self.sys2_process:
                    await self.sys2_process(main_event, events_to_process)
                    
        except Exception as e:
            logger.error(f"Attention Aggregation Error: {e}")
            
        finally:
            # 无论发生什么，释放注意力锁
            state.owner_id = None
            if state.lock.locked():
                state.lock.release()
                logger.debug(f"[{chat_id}] 🔓 注意力锁释放。")