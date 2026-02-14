### 📄 core/mind_scheduler.py
import asyncio
import time
import random
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

# 引入 2.0 组件
from ..datamodels import SensoryInput, ChatState
from .state_manager import StateManager
from .impulse_engine import ImpulseEngine
from .reply_engine import ReplyEngine
from .memory_glands import MemoryGlands
from .evolution_cortex import EvolutionCortex
from .mood_manager import MoodManager
from ..utils.prompt_builder import PromptBuilder

class MindScheduler:
    """
    HeartCore 2.0 神经中枢 (MindScheduler)
    
    职责:
    1. 感官路由 (Sensory Routing)
    2. 双池注意力调度 (Dual-Pool Attention)
    3. 思考循环管理 (Thinking Loop Lifecycle)
    """
    
    def __init__(self, 
                 context: Context, 
                 config, 
                 state_manager: StateManager,
                 prompt_builder: PromptBuilder,
                 mood_manager: MoodManager,
                 reply_engine: ReplyEngine 
                 ):
        self.context = context
        self.config = config
        self.state_manager = state_manager
        self.mood_manager = mood_manager
        self.reply_engine = reply_engine
        self.prompt_builder = prompt_builder
        
        # --- 初始化 2.0 器官 ---
        self.memory = MemoryGlands(context)
        self.evolution = EvolutionCortex(context)
        
        # [修改] 正确初始化 ImpulseEngine，传入必要的组件
        self.impulse = ImpulseEngine(
            context, 
            config, 
            prompt_builder, 
            self.memory, 
            self.evolution
        )
        
        # 运行时状态
        self.active_loops = {} # session_id -> asyncio.Task

    async def on_message(self, event: AstrMessageEvent):
        """
        主入口：接收感官信号
        """
        # 1. 基础过滤 (Gate 0)
        msg_len = len(event.message_str)
        if msg_len <= self.config.filter_short_length and not self.config.use_native_vision:
            # 如果没有视觉能力且文本极短，忽略 (除非是 Poke 等特殊事件)
            return

        # 2. 构建感官输入包
        session_id = event.unified_msg_origin
        sensory_input = SensoryInput.from_event(event)
        
        # 3. 情绪杏仁核预处理 (快速情绪反应)
        # 每次收到消息都先更新情绪，不等待思考
        chat_state = await self.state_manager.get_chat_state(session_id)
        if msg_len > 2:
            # 异步触发情绪分析，不阻塞调度
            asyncio.create_task(self._fast_emotion_reaction(sensory_input.text, chat_state))

        # 4. 调度到大脑皮层
        await self.dispatch(session_id, sensory_input, chat_state)

    async def _fast_emotion_reaction(self, text: str, state: ChatState):
        """杏仁核快速反应：更新情绪值"""
        tag, val = await self.mood_manager.analyze_text_mood(text, state)
        state.mood = val
        # 简单的精力消耗逻辑 (被动接收消息消耗极少)
        state.energy = max(0.0, state.energy - 0.001)

    async def dispatch(self, session_id: str, input: SensoryInput, state: ChatState):
        """
        核心调度算法 (Dual-Pool Logic)
        """
        # --- Gate 1: 斗图/刷屏阻断 ---
        if input.images:
            state.consecutive_image_count += 1
            if state.consecutive_image_count > self.config.image_spam_limit:
                logger.debug(f"MindScheduler: 拦截连续图片 ({state.consecutive_image_count})")
                return
        else:
            state.consecutive_image_count = 0

        # --- Gate 2: 精力软过滤 (Soft Filter) ---
        if state.energy < 0.1 and not state.is_in_window_mode:
            # 精力耗尽且不在窗口期，概率忽略
            if random.random() > 0.1: 
                logger.debug("MindScheduler: 精力耗尽，忽略消息。")
                return

        # --- 双池路由 ---
        if state.lock.locked():
            # 大脑正在思考 (Busy) -> 放入 Background Buffer
            logger.debug(f"MindScheduler: 思考中，消息存入 Background Buffer ({len(state.background_buffer)})")
            state.background_buffer.append(input)
        else:
            # 大脑空闲 (Idle) -> 放入 Accumulation Pool 并启动 Loop
            logger.debug(f"MindScheduler: 空闲，消息存入 Accumulation Pool")
            state.accumulation_pool.append(input)
            
            # 启动或延续思考循环
            # 使用 create_task 确保非阻塞
            if session_id not in self.active_loops or self.active_loops[session_id].done():
                self.active_loops[session_id] = asyncio.create_task(
                    self.run_thinking_loop(session_id, state)
                )

    async def run_thinking_loop(self, session_id: str, state: ChatState):
        """
        Thinking Loop: 观察 -> 思考 -> 行动
        """
        async with state.lock:
            try:
                # 1. 拟人化延迟 (Accumulation Phase)
                # 等待一小会儿，让 accumulation_pool 收集可能的连发消息
                wait_time = max(0.5, self.config.min_reply_interval)
                if state.is_in_window_mode: 
                    wait_time = 0.5 # 窗口期响应更快
                await asyncio.sleep(wait_time)
                
                if not state.accumulation_pool: return

                # 2. 构建上下文 (提取消息内容)
                # 简单转换 accumulation_pool 为 LLM 消息格式
                context_messages = []
                for sensory in state.accumulation_pool:
                    # 暂时简单处理文本，P3 会引入更复杂的 Time-Aware Context
                    # 这里的 sensory 是 SensoryInput 对象
                    context_messages.append({"role": "user", "content": sensory.text})
                
                # 3. 🧠 冲动引擎决策 (Impulse Decision) - 正式启用！
                # 传入 session_id, state 和当前积累的消息上下文
                decision = await self.impulse.think(session_id, state, context_messages)
                
                logger.info(f"🧠 [MindScheduler] Decision: {decision.action} | Thought: {decision.thought}")
                
                # 4. 执行行动 (Action Execution)
                if decision.action == "REPLY":
                    # 准备回复，取最后一条消息作为触发事件 (用于 reply_engine 的上下文兼容)
                    latest_event = state.accumulation_pool[-1].raw_event if state.accumulation_pool else None
                    state.accumulation_pool.clear()
                    
                    if latest_event:
                        # 传入 decision 对象 (包含 thought)
                        await self.reply_engine.handle_reply(
                            latest_event,
                            decision,
                            is_poke_or_nickname=False # 这里由 decision 决定，不再由 Poke 强制
                        )
                    
                    # 更新状态
                    state.last_reply_time = time.time()
                    state.total_replies += 1
                    # 激活窗口模式
                    state.is_in_window_mode = True
                    state.window_remaining = self.config.active_window_count

                elif decision.action == "WAIT":
                    # P2: 实现了真正的 WAIT 动作
                    # 保持 accumulation_pool 不变，释放锁，挂起一段时间
                    wait_seconds = decision.params.get("wait_seconds", 3) if decision.params else 3
                    logger.info(f"⏳ [MindScheduler] Waiting for {wait_seconds}s...")
                    
                    # 注意：这里我们释放了锁 (with block 结束)，所以在这 wait_seconds 期间，
                    # 新消息会进入 dispatch 并可能重新触发 active_loops (如果 loop 已结束)。
                    # 但为了简单起见，我们在 Loop 内部等待是不行的，因为这占用了锁。
                    # 正确做法应该是：释放锁 -> sleep -> 重新获取锁 -> 继续 Loop。
                    # 但 MindScheduler 的设计是 task 结束锁就释放。
                    # 所以我们只需在这里 sleep，但这样锁一直被占用，新消息会进 background_buffer。
                    # 这符合逻辑：因为我在"思考/等待"，没空处理新消息。
                    await asyncio.sleep(wait_seconds)
                    
                    # 等待结束后，Loop 结束，锁释放。
                    # 如果有 background_buffer，会在 finally 块之后的下一次 dispatch 或 递归调用中处理？
                    # 不，下面的代码处理了 background_buffer。
                    # 如果 WAIT 期间有新消息进了 background_buffer，它们会被捞起。
                    # 如果没有新消息，accumulation_pool 还在，下一次 Loop 会再次处理这些消息？
                    # 为了防止死循环 (一直 WAIT)，ImpulseEngine 内部应该有状态机或计数器。
                    pass 

                elif decision.action == "COMPLETE_TALK":
                    # 结束对话：清空池子，关闭窗口
                    state.accumulation_pool.clear()
                    state.is_in_window_mode = False
                    state.window_remaining = 0
                    logger.info("🛑 [MindScheduler] Conversation completed.")

                elif decision.action == "IGNORE":
                    # 忽略：清空池子，降低窗口权重
                    state.accumulation_pool.clear()
                    if state.is_in_window_mode:
                        state.window_remaining -= 1
                        if state.window_remaining <= 0:
                            state.is_in_window_mode = False

                # 5. 处理 Background Buffer (背景池回捞)
                # 如果在回复/思考期间用户又发了消息，这些消息在 background_buffer 中
                if state.background_buffer:
                    logger.info(f"MindScheduler: 处理 Background Buffer ({len(state.background_buffer)})")
                    # 将背景池移动到聚焦池，准备下一轮循环
                    state.accumulation_pool.extend(state.background_buffer)
                    state.background_buffer.clear()
                    
                    # 递归启动下一轮循环
                    self.active_loops[session_id] = asyncio.create_task(
                        self.run_thinking_loop(session_id, state)
                    )

            except Exception as e:
                logger.error(f"MindScheduler Loop Error: {e}", exc_info=True)