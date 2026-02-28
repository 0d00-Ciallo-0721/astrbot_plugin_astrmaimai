# astrmai/Brain/planner.py
from typing import List
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger

from ..infra.gateway import GlobalModelGateway
from .context_engine import ContextEngine
from .executor import ConcurrentExecutor
from .tools.pfc_tools import WaitTool, FetchKnowledgeTool
from .reply_engine import ReplyEngine

# 引入依赖类型
from ..memory.engine import MemoryEngine
from ..evolution.processor import EvolutionManager

class Planner:
    """
    认知总控 (System 2)
    职责: 统筹编排 System 2。将聚合的消息与环境状态拼装，定义原生工具栈，然后下发给 Executor 驱动智能体循环。
    """
    def __init__(self, 
                 context, 
                 gateway: GlobalModelGateway, 
                 context_engine: ContextEngine, 
                 reply_engine: ReplyEngine,
                 memory_engine: MemoryEngine,      # [新增] 注入记忆引擎
                 evolution_manager: EvolutionManager # [新增] 注入进化管理器
                 ):
        self.gateway = gateway
        self.context_engine = context_engine
        self.memory_engine = memory_engine
        self.evolution_manager = evolution_manager
        self.executor = ConcurrentExecutor(context, gateway, reply_engine)

    async def plan_and_execute(self, event: AstrMessageEvent, event_messages: List[AstrMessageEvent]):
        """
        [修改] 重构后的核心入口：跳过脆弱的手工 ReAct，全面基于 AstrBot Agent。
        引入目标驱动 (Goal-Driven) 与 黑话查询工具 (QueryJargonTool)。
        """
        chat_id = event.unified_msg_origin
        
        # 1. 消息重组：将防抖队列中积压的消息合并为单个用户 Prompt
        prompt_content = "\n".join([f"{m.get_sender_name()}: {m.message_str}" for m in event_messages])
        
        import asyncio
        # 2. 预加载上下文数据 (Slang 模式)
        slang_context = await asyncio.to_thread(self.evolution_manager.get_active_patterns, chat_id) 
        
        # [新增] 2.5 目标驱动分析：动态计算当前的短期对话目标 (IntelligentChatService 逻辑降维)
        current_goal = ""
        if hasattr(self.evolution_manager, 'analyze_and_get_goal'):
            current_goal = await self.evolution_manager.analyze_and_get_goal(chat_id, prompt_content)
        
        # 3. 潜意识与动态状态注入 (System Prompt 构建)
        system_prompt = await self.context_engine.build_prompt(
            chat_id=chat_id, 
            event_messages=event_messages,
            slang_patterns=slang_context,
            current_goal=current_goal # [新增] 注入阶段目标
        )
        
        # 4. 装配前额叶基建工具 (PFC Actions)
        from .tools.pfc_tools import WaitTool, FetchKnowledgeTool, QueryJargonTool
        
        # 初始化工具并注入所需的服务与上下文
        tools = [
            WaitTool(),
            FetchKnowledgeTool(memory_engine=self.memory_engine, chat_id=chat_id),
            # [新增] 挂载黑话查询工具
            QueryJargonTool(db_service=self.context_engine.db, chat_id=chat_id)
        ]
        
        # 5. 组装工具描述 (为了传递给 context_engine 如果需要的话，或者直接给 Executor)
        tool_descs = "\n".join([f"- {t.name}: {t.description}" for t in tools])
        
        # 如果 context_engine 需要最新的 tool_descs，可以在 build_prompt 后替换，或在上方提前生成
        
        # 6. 下发给 Executor 驱动智能体循环
        await self.executor.execute(
            event=event,
            system_prompt=system_prompt,
            user_prompt=prompt_content,
            tools=tools
        )