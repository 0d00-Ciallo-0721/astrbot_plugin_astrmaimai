import asyncio
import time
from typing import List, Dict, Any
from dataclasses import dataclass, field
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger

from .state_engine import StateEngine
from .judge import Judge
from .sensors import PreFilters

@dataclass
class SessionContext:
    """纯内存态并发上下文，绝不参与数据库序列化"""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner_id: str = None
    accumulation_pool: List[Any] = field(default_factory=list)
    background_buffer: List[Any] = field(default_factory=list)
    is_evaluating: bool = False 

class AttentionGate:
    def __init__(self, state_engine: StateEngine, judge: Judge, sensors: PreFilters, system2_callback, config=None):
        self.state_engine = state_engine
        self.judge = judge
        self.sensors = sensors
        self.sys2_process = system2_callback 
        self.config = config if config else state_engine.config
        
        self.focus_pools: Dict[str, SessionContext] = {}
        self._pool_lock = asyncio.Lock()

    async def _get_or_create_session(self, chat_id: str) -> SessionContext:
        async with self._pool_lock:
            if chat_id not in self.focus_pools:
                self.focus_pools[chat_id] = SessionContext()
            return self.focus_pools[chat_id]

    async def process_event(self, event: AstrMessageEvent):
        msg_str = event.message_str
        chat_id = str(event.unified_msg_origin)
        sender_id = str(event.get_sender_id())
        self_id = str(event.get_self_id())
        
        is_cmd = await self.sensors.is_command(msg_str)
        if is_cmd:
            setattr(event, "is_command_trigger", True)
            logger.info(f"[AstrMai-Sensor] 🛡️ 识别到指令: {msg_str[:10]}... 已标记并拦截。")
            return 

        should_process = await self.sensors.should_process_message(event)
        if not should_process or event.get_extra("astrmai_is_command"):
            return

        chat_state = await self.state_engine.get_state(chat_id)
        
        extracted_images = event.get_extra("extracted_image_urls") or []
        if extracted_images:
            await self.state_engine.persistence.add_last_message_meta(
                chat_id, sender_id, True, extracted_images
            )

        session = await self._get_or_create_session(chat_id)

        async with session.lock:
            if session.owner_id is None:
                session.owner_id = sender_id

            if session.owner_id == sender_id:
                session.accumulation_pool.append(event)
                event.set_extra("astrmai_timestamp", time.time())
            else:
                session.background_buffer.append(event)
                return 

            if session.is_evaluating:
                logger.debug(f"[{chat_id}] 🧠 Busy: Owner 追加消息 -> 累积池")
                return
            
            session.is_evaluating = True

        logger.info(f"[{chat_id}] 👁️ 注意力聚焦! Owner: {sender_id}")
        asyncio.create_task(self._debounce_and_judge(chat_id, session, self_id))

    async def _debounce_and_judge(self, chat_id: str, session: SessionContext, self_id: str):
        try:
            logger.debug(f"[{chat_id}] ⏱️ 开启聚合滑动窗口...")
            no_msg_start_time = time.time()
            last_pool_len = 0
            debounce_window = getattr(self.config.attention, 'debounce_window', 2.0)
            
            while True:
                current_pool_len = len(session.accumulation_pool)
                if current_pool_len > last_pool_len:
                    no_msg_start_time = time.time()
                    last_pool_len = current_pool_len
                    ts = session.accumulation_pool[-1].get_extra("astrmai_timestamp")
                    if ts: no_msg_start_time = ts
                
                if time.time() - no_msg_start_time > debounce_window:
                    break
                await asyncio.sleep(0.5)

            async with session.lock:
                events_to_process = list(session.accumulation_pool)
                session.accumulation_pool.clear()
                
            if not events_to_process:
                return

            logger.info(f"[{chat_id}] 📦 聚合结束，将 {len(events_to_process)} 条消息打包进行 Judge 判决。")
            
            main_event = events_to_process[-1]
            combined_text = " \n ".join([e.message_str for e in events_to_process])
            is_wakeup = self.sensors.is_wakeup_signal(main_event, self_id)
            
            # 纯粹的真实调用，不做任何 mock 拦截
            plan = await self.judge.evaluate(chat_id, combined_text, is_wakeup)

            if plan.action in ["REPLY", "WAIT"]:
                if self.sys2_process:
                    await self.sys2_process(main_event, events_to_process)
            else:
                async with session.lock:
                    session.background_buffer.extend(events_to_process)
                    if len(session.background_buffer) > getattr(self.config.attention, 'bg_pool_size', 10):
                        session.background_buffer = session.background_buffer[-getattr(self.config.attention, 'bg_pool_size', 10):]

        except Exception as e:
            logger.exception(f"Attention Aggregation Error: {e}")
        finally:
            async with session.lock:
                session.owner_id = None
                session.is_evaluating = False
            logger.debug(f"[{chat_id}] 🔓 注意力评估状态已释放。")