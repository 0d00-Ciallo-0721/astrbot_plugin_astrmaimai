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
    """纯内存态并发上下文，全局共享序列池"""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    accumulation_pool: List[Any] = field(default_factory=list)
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

    def _is_image_only(self, event: AstrMessageEvent) -> bool:
        """判断是否为纯图片消息"""
        has_img = bool(event.get_extra("extracted_image_urls"))
        has_text = bool(event.message_str and event.message_str.strip())
        return has_img and not has_text

    def _check_continuous_images(self, pool: List[AstrMessageEvent]) -> int:
        """计算末尾连续图片消息的数量"""
        count = 0
        for e in reversed(pool):
            if self._is_image_only(e):
                count += 1
            else:
                break
        return count

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
            # 所有人全部入池，取消所有权(owner_id)判定
            session.accumulation_pool.append(event)
            event.set_extra("astrmai_timestamp", time.time())

            if session.is_evaluating:
                logger.debug(f"[{chat_id}] 🧠 Busy: 追加消息 -> 累积池")
                return
            
            session.is_evaluating = True

        logger.info(f"[{chat_id}] 👁️ 注意力聚焦，开启多用户并发聚合池!")
        asyncio.create_task(self._debounce_and_judge(chat_id, session, self_id))

    def _format_and_filter_messages(self, events: List[AstrMessageEvent]):
        """斗图过滤与同源消息折叠"""
        if not events: return "", []
        
        filtered_events = []
        continuous_img_count = 0
        
        # 1. 斗图过滤阶段：连续 >= 3 图片时，保留第一个，抛弃后续
        for e in events:
            if self._is_image_only(e):
                continuous_img_count += 1
                if continuous_img_count >= 3:
                    continue # 直接过滤丢弃
            else:
                continuous_img_count = 0
            filtered_events.append(e)

        # 2. 同源聚合阶段
        grouped_texts = []
        curr_sender = None
        curr_msgs = []
        
        for e in filtered_events:
            sender = e.get_sender_name()
            content = e.message_str.strip() if e.message_str.strip() else "[图片]"
            
            if sender != curr_sender:
                if curr_sender is not None:
                    grouped_texts.append(f"{curr_sender}：{'，'.join(curr_msgs)}")
                curr_sender = sender
                curr_msgs = [content]
            else:
                curr_msgs.append(content)
                
        if curr_sender is not None:
            grouped_texts.append(f"{curr_sender}：{'，'.join(curr_msgs)}")

        return "\n".join(grouped_texts), filtered_events

    async def _debounce_and_judge(self, chat_id: str, session: SessionContext, self_id: str):
        """[修改] _debounce_and_judge (捕获多端时序并熔断)"""
        try:
            logger.debug(f"[{chat_id}] ⏱️ 开启聚合滑动窗口...")
            no_msg_start_time = time.time()
            last_pool_len = 0
            debounce_window = getattr(self.config.attention, 'debounce_window', 2.0)
            
            while True:
                current_pool_len = len(session.accumulation_pool)
                
                # 硬性结束事件 1：池内总数 >= 15
                if current_pool_len >= 15:
                    logger.debug(f"[{chat_id}] 触发容量限制 (>=15)，立即熔断。")
                    break
                    
                # 硬性结束事件 2：检测到连续图片 >= 3
                if self._check_continuous_images(session.accumulation_pool) >= 3:
                    logger.debug(f"[{chat_id}] 触发斗图防爆 (连续图片>=3)，立即熔断。")
                    break

                if current_pool_len > last_pool_len:
                    no_msg_start_time = time.time()
                    last_pool_len = current_pool_len
                    ts = session.accumulation_pool[-1].get_extra("astrmai_timestamp")
                    if ts: no_msg_start_time = ts
                
                # 软结束事件：静默时间 > 2s
                if time.time() - no_msg_start_time > debounce_window:
                    break
                await asyncio.sleep(0.3) # 缩短轮询间隔，提升熔断响应速度

            async with session.lock:
                events_to_process = list(session.accumulation_pool)
                session.accumulation_pool.clear()
                
            if not events_to_process:
                return

            # 执行聚合与过滤
            combined_text, final_events = self._format_and_filter_messages(events_to_process)
            
            if not final_events: return

            # 暴露第一条原始消息作为拉取历史记录的节点 (anchor_event)
            anchor_event = final_events[0]
            main_event = final_events[-1] 
            main_event.set_extra("astrmai_anchor_event", anchor_event)

            logger.info(f"[{chat_id}] 📦 窗口闭合。过滤后留存 {len(final_events)} 条消息。\n聚合内容:\n{combined_text}")
            
            is_wakeup = self.sensors.is_wakeup_signal(main_event, self_id)
            
            # 纯粹的真实调用，不做任何 mock 拦截
            plan = await self.judge.evaluate(chat_id, combined_text, is_wakeup)
            
            # 将 System 1 的直觉 (thought) 挂载到主事件上，传递给 System 2
            main_event.set_extra("sys1_thought", plan.thought)

            if plan.action in ["REPLY", "WAIT"]:
                if self.sys2_process:
                    await self.sys2_process(main_event, final_events)

        except Exception as e:
            logger.exception(f"Attention Aggregation Error: {e}")
        finally:
            async with session.lock:
                session.is_evaluating = False
            logger.debug(f"[{chat_id}] 🔓 注意力评估状态已释放。")