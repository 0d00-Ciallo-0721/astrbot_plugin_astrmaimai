### 📄 core/mind_scheduler.py
import asyncio
import time
import random
from typing import Dict
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

from ..datamodels import SensoryInput, ChatState
from ..config import HeartflowConfig
from .state_manager import StateManager
from .impulse_engine import ImpulseEngine
from .reply_engine import ReplyEngine
from .mood_manager import MoodManager
from .memory_glands import MemoryGlands # [新增]
from .evolution_cortex import EvolutionCortex
from ..utils.prompt_builder import PromptBuilder

class MindScheduler:
    """
    (v2.0) 神经中枢调度器
    职责：
    1. 感官路由 (Sensory Routing) -> Accumulation / Background
    2. 思考循环管理 (Thinking Loop)
    3. 协调器官 (Impulse, Reply, Memory)
    """
    def __init__(self, 
                 context: Context, 
                 config: HeartflowConfig, 
                 state_manager: StateManager,
                 prompt_builder: PromptBuilder,
                 mood_manager: MoodManager,
                 reply_engine: ReplyEngine): # 注入依赖
        self.context = context
        self.config = config
        self.state_manager = state_manager
        self.mood_manager = mood_manager
        self.reply_engine = reply_engine
        self.prompt_builder = prompt_builder
        
        # [修改] 初始化 2.0 器官
        self.memory = MemoryGlands(context)
        # 启动异步初始化 (不阻塞启动流程)
        if self.config.enable_memory_glands:
            asyncio.create_task(self.memory.initialize())
            
        self.evolution = None # P4 阶段接入 EvolutionCortex
        
        # [修改] 初始化大脑 (注入 memory)
        self.impulse = ImpulseEngine(
            context, config, prompt_builder, self.memory, self.evolution
        )
        
        # 运行中的循环任务: map[session_id, asyncio.Task]
        self.active_loops: Dict[str, asyncio.Task] = {}

    async def on_message(self, event: AstrMessageEvent):
        """主入口：处理感官信号"""
        # 1. 基础过滤
        if len(event.message_str) <= self.config.filter_short_length and not self.config.use_native_vision:
            return

        # 2. 封装感官输入
        session_id = event.unified_msg_origin
        sensory_input = SensoryInput.from_event(event)
        
        # 3. 快速情绪反应 (Fast Path)
        chat_state = await self.state_manager.get_chat_state(session_id)
        # 简单更新最后访问时间
        chat_state.last_access_time = time.time()
        
        # 4. 核心调度
        await self.dispatch(session_id, sensory_input, chat_state)

    async def dispatch(self, session_id: str, input: SensoryInput, state: ChatState):
        """双池调度算法"""
        if state.lock.locked():
            logger.debug(f"MindScheduler: Busy, buffering message from {input.sender_name}")
            state.background_buffer.append(input)
        else:
            state.accumulation_pool.append(input)
            if session_id not in self.active_loops or self.active_loops[session_id].done():
                self.active_loops[session_id] = asyncio.create_task(
                    self._run_thinking_loop(session_id, state)
                )

    async def _run_thinking_loop(self, session_id: str, state: ChatState):
        """思考循环 (The ReAct Loop)"""
        async with state.lock: # 获取锁
            try:
                # 1. [Accumulation] 动态等待
                wait_time = 0.5 if state.is_in_window_mode else self.config.min_reply_interval
                await asyncio.sleep(wait_time)
                
                if not state.accumulation_pool: return

                # 2. [Cognition] 调用冲动引擎
                current_inputs = list(state.accumulation_pool)
                state.accumulation_pool.clear()
                
                decision = await self.impulse.think(session_id, state, current_inputs)
                
                logger.info(f"🧠 [MindScheduler] Action: {decision.action} | Thought: {decision.thought}")

                # 3. [State Update] 应用状态变更
                if decision.state_diff:
                    await self.state_manager.apply_state_diff(session_id, decision.state_diff)

                # 4. [Action] 执行动作
                if decision.action == "REPLY":
                    state.is_in_window_mode = True 
                    state.window_remaining = self.config.active_window_count
                    
                    trigger_event = current_inputs[-1].raw_event
                    reply_content = await self.reply_engine.handle_reply(trigger_event, decision)
                    
                    # [新增] 记忆固化 (Memory Consolidation)
                    if reply_content and self.config.enable_memory_glands:
                        user_text = " ".join([s.text for s in current_inputs])
                        asyncio.create_task(
                            self.memory.store_interaction(session_id, user_text, reply_content)
                        )
                    
                elif decision.action == "WAIT":
                    wait_sec = decision.params.get("wait_seconds", 2)
                    await asyncio.sleep(wait_sec)
                    
                elif decision.action == "COMPLETE_TALK":
                    state.is_in_window_mode = False

                # 5. [Recursion] 处理背景池
                if state.background_buffer:
                    state.accumulation_pool.extend(state.background_buffer)
                    state.background_buffer.clear()
                    self.active_loops[session_id] = asyncio.create_task(
                        self._run_thinking_loop(session_id, state)
                    )

            except Exception as e:
                logger.error(f"MindScheduler Loop Error: {e}", exc_info=True)