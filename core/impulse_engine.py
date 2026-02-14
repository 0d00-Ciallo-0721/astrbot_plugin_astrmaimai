### 📄 core/impulse_engine.py
import time
import asyncio
from typing import Dict, Any, List
from astrbot.api import logger
from astrbot.api.star import Context

from ..datamodels import ImpulseDecision, ChatState, SensoryInput
from ..config import HeartflowConfig
from ..utils.prompt_builder import PromptBuilder
from ..services.llm_helper import LLMHelper
from .goals import GoalStateMachine
# 注意：MemoryGlands 和 EvolutionCortex 将在 Phase 3/4 接入，此处预留接口或接收 None

class ImpulseEngine:
    """
    (v2.0) 冲动引擎 (The ReAct Brain)
    职责：
    1. 接收感知输入 (Context)
    2. 执行 ReAct 思考循环 (Think)
    3. 输出决策与状态变更 (Decide)
    """
    def __init__(self, 
                 context: Context, 
                 config: HeartflowConfig,
                 prompt_builder: PromptBuilder,
                 memory_glands=None, 
                 evolution_cortex=None):
        self.context = context
        self.config = config
        self.prompt_builder = prompt_builder
        self.llm_helper = LLMHelper(context)
        self.memory = memory_glands
        self.evolution = evolution_cortex

    async def think(self, 
                    session_id: str, 
                    chat_state: ChatState,
                    context_inputs: List[SensoryInput]) -> ImpulseDecision:
        """
        核心思考接口
        """
        # 1. 准备上下文数据
        # (P3/P4 接入点)
        retrieved_memory = ""
        if self.memory:
            # 转换 SensoryInput 为 text list 供检索
            msgs = [{"role": "user", "content": s.text} for s in context_inputs]
            retrieved_memory = await self.memory.active_retrieve(session_id, msgs)
            
        persona_mutation = ""
        if self.evolution:
            persona_mutation = await self.evolution.get_mutation_state(session_id)

        # 恢复目标状态机
        gsm = GoalStateMachine(chat_state.current_goals)
        current_goals_desc = gsm.get_goals_description()

        # 2. 构建 Prompt (使用 utils/prompt_builder.py 中的新方法)
        # 将 SensoryInput 列表转换为 LLM 消息格式 (带时间戳)
        history_msgs = self.prompt_builder._build_time_aware_history(context_inputs)
        
        prompt_messages = self.prompt_builder.build_impulse_prompt(
            context_messages=history_msgs,
            persona_mutation=persona_mutation,
            retrieved_memory=retrieved_memory,
            current_goals=current_goals_desc
        )

        # 3. LLM 决策调用
        # 优先使用配置的 judge_provider
        provider_id = self.config.judge_provider_names[0] if self.config.judge_provider_names else None
        
        decision_data = await self.llm_helper.chat_json(
            prompt_messages, 
            provider_id=provider_id,
            retries=self.config.judge_max_retries
        )

        # 4. 解析决策与计算状态变更 (State收敛)
        action = decision_data.get("action", "REPLY") # 默认回复
        thought = decision_data.get("thought", "I should reply.")
        goals_update = decision_data.get("goals_update", [])
        params = decision_data.get("params", {})

        state_diff = {}
        
        # 计算精力与心情变更 (副作用剥离)
        if action == "REPLY":
            # 扣除精力
            new_energy = max(0.0, chat_state.energy - 0.05) # 示例数值
            state_diff["energy"] = new_energy
            state_diff["last_reply_time"] = time.time()
            state_diff["total_replies"] = chat_state.total_replies + 1
            
            # 如果有目标更新，应用到状态机并保存
            if goals_update:
                new_goals = gsm.update_goals(goals_update)
                state_diff["current_goals"] = new_goals

        elif action == "IGNORE":
            # 恢复少量精力
            new_energy = min(1.0, chat_state.energy + 0.01)
            state_diff["energy"] = new_energy

        elif action == "COMPLETE_TALK":
            # 清空目标
            gsm.update_goals([{"action": "clear"}])
            state_diff["current_goals"] = gsm.goals

        return ImpulseDecision(
            action=action,
            thought=thought,
            goals_update=goals_update,
            state_diff=state_diff,
            params=params
        )