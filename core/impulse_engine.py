### 📄 core/impulse_engine.py
# heartflow/core/impulse_engine.py
# (HeartCore 2.0 - The ReAct Brain)

import json
import asyncio
from astrbot.api import logger
from astrbot.api.star import Context

from ..datamodels import ImpulseDecision, ChatState
from ..utils.prompt_builder import PromptBuilder
from .memory_glands import MemoryGlands
from .evolution_cortex import EvolutionCortex
from .goals import GoalStateMachine
from ..utils.api_utils import APIUtils # 复用 v4.14 的 API 工具

class ImpulseEngine:
    """
    冲动引擎 (ImpulseEngine)
    职责：运行 ReAct 思考循环，管理目标状态机
    """
    def __init__(self, 
                 context: Context, 
                 config,
                 prompt_builder: PromptBuilder,
                 memory_glands: MemoryGlands, 
                 evolution_cortex: EvolutionCortex):
        self.context = context
        self.config = config
        self.prompt_builder = prompt_builder
        self.memory = memory_glands
        self.evolution = evolution_cortex
        self.api_utils = APIUtils(context) # 复用 API 工具

    async def think(self, 
                    session_id: str, 
                    chat_state: ChatState,
                    context_messages: list) -> ImpulseDecision:
        """
        ReAct 核心思考接口
        """
        # 1. 准备数据
        # (P3) 记忆检索
        retrieved_memory = await self.memory.active_retrieve(session_id, context_messages)
        # (P4) 人格突变
        persona_mutation = await self.evolution.get_mutation_state(session_id)
        # 目标状态
        goals_desc = self._get_goals_desc(chat_state)

        # 2. 构建 Prompt
        prompt_messages = self.prompt_builder.build_impulse_prompt(
            context_messages=context_messages,
            persona_mutation=persona_mutation,
            retrieved_memory=retrieved_memory,
            current_goals=goals_desc
        )

        # 3. LLM 决策调用
        # 优先使用配置的 judge_provider (小模型)，如果没有则使用默认
        provider_id = self.config.judge_provider_names[0] if self.config.judge_provider_names else None
        
        try:
            # 调用 LLM (要求 JSON)
            response_data = await self.api_utils.chat_json(
                prompt_messages, 
                provider_id=provider_id,
                retries=self.config.judge_max_retries
            )
            
            # 4. 解析决策
            if not response_data:
                raise ValueError("Empty response from LLM")

            decision = ImpulseDecision(
                action=response_data.get("action", "REPLY"),
                thought=response_data.get("thought", "I should reply."),
                goals_update=response_data.get("goals_update", []),
                params=response_data.get("params", {})
            )
            
            # 5. 更新目标状态机 (Side Effect)
            self._apply_goal_updates(chat_state, decision.goals_update)
            
            return decision

        except Exception as e:
            logger.error(f"ImpulseEngine Think Error: {e}", exc_info=True)
            # 降级策略：默认回复
            return ImpulseDecision(action="REPLY", thought="System error, fallback to reply.")

    def _get_goals_desc(self, state: ChatState) -> str:
        """从 ChatState 中恢复 GoalStateMachine 并获取描述"""
        # 这里是一个临时的适配逻辑，实际上 GoalStateMachine 应该挂载在 state 上
        # 简化处理：每次重建或从 state.current_goals 读取
        gsm = GoalStateMachine()
        gsm.goals = state.current_goals
        return gsm.get_goals_description()

    def _apply_goal_updates(self, state: ChatState, updates: list):
        """更新 ChatState 中的目标"""
        if not updates:
            return
        gsm = GoalStateMachine()
        gsm.goals = state.current_goals
        gsm.update_goals(updates)
        state.current_goals = gsm.goals # 回写