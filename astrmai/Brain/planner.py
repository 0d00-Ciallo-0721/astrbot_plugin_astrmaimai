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
        重构后的核心入口：跳过脆弱的手工 ReAct，全面基于 AstrBot Agent。
        """
        chat_id = event.unified_msg_origin
        
        # 1. 消息重组：将防抖队列中积压的消息合并为单个用户 Prompt
        # 简单拼接，后续可升级为剧本模式
        prompt_content = "\n".join([f"{m.get_sender_name()}: {m.message_str}" for m in event_messages])
        
        # 2. [新增] 预加载上下文数据 (Memory & Slang)
        # 异步并行获取以降低延迟
        # 注意：这里做一次初步检索，Tool Loop 中 Agent 还可以再次调用 FetchKnowledgeTool
        import asyncio
        memory_task = self.memory_engine.recall(prompt_content, session_id=chat_id)
        slang_task = asyncio.to_thread(self.evolution_manager.get_active_patterns, chat_id) # 假设这是同步方法，用 to_thread 包装
        
        memory_context, slang_context = await asyncio.gather(memory_task, slang_task)
        
        # 3. 潜意识与动态状态注入 (System Prompt 构建)
        # 将获取到的 Context 传入
        system_prompt = await self.context_engine.build_prompt(
            chat_id=chat_id, 
            event_messages=event_messages,
            memory_context=memory_context,
            slang_patterns=slang_context
        )
        
        # 4. 装配前额叶基建工具 (PFC Actions)
        # 可以在这里动态添加更多工具
        pfc_tools = [
            WaitTool(),
            FetchKnowledgeTool()
        ]
        
        # 5. 移交并发执行器引爆思考闭环
        await self.executor.execute(
            event=event,
            prompt=prompt_content,
            system_prompt=system_prompt,
            tools=pfc_tools
        )