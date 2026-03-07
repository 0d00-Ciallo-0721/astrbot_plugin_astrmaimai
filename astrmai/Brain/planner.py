# astrmai/Brain/planner.py
from typing import List
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger
import asyncio
from ..infra.gateway import GlobalModelGateway
from .context_engine import ContextEngine
from .executor import ConcurrentExecutor
from .tools.pfc_tools import WaitTool, FetchKnowledgeTool
from .reply_engine import ReplyEngine
from .tools.pfc_tools import WaitTool, FetchKnowledgeTool, QueryJargonTool
        
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
        [修改] 动态上下文修剪、按需注入路由与沉浸模式屏蔽
        """
        chat_id = event.unified_msg_origin
        
        # 1. 提取检索 Keys (从 System 1 在 event 或 meta 中携带的标签中获取)
        retrieve_keys = event.get_extra("retrieve_keys", [])
        if not isinstance(retrieve_keys, list):
            retrieve_keys = []
            
        is_all_mode = "ALL" in retrieve_keys
        
        # 2. 上下文修剪 (感官剥夺模式：处于 ALL 完整降临时，抛弃冗长历史)
        if is_all_mode and len(event_messages) > 3:
            event_messages = event_messages[-3:]
            
        prompt_content = "\n".join([f"{m.get_sender_name()}: {m.message_str}" for m in event_messages])
        
        import asyncio
        # 3. 预加载上下文数据
        slang_context = await asyncio.to_thread(self.evolution_manager.get_active_patterns, chat_id) 
        
        current_goal = ""
        if hasattr(self.evolution_manager, 'analyze_and_get_goal'):
            current_goal = await self.evolution_manager.analyze_and_get_goal(chat_id, prompt_content)
        
        # 4. 装配工具与 RAG 屏蔽 (实现记忆关闭开关)
        from .tools.pfc_tools import WaitTool, FetchKnowledgeTool, QueryJargonTool
        
        ctx = getattr(self.context_engine, 'context', None)
        if is_all_mode:
            # 沉浸模式：清空工具，强制底层拦截 RAG
            tools = []
            if ctx:
                if hasattr(ctx, "set"):
                    ctx.set("disable_rag_injection", True)
                elif hasattr(ctx, "shared_dict"):
                    ctx.shared_dict["disable_rag_injection"] = True
        else:
            # 常规模式：挂载工具
            tools = [
                WaitTool(),
                FetchKnowledgeTool(memory_engine=self.memory_engine, chat_id=chat_id),
                QueryJargonTool(db_service=self.context_engine.db, chat_id=chat_id)
            ]
            if ctx:
                if hasattr(ctx, "set"):
                    ctx.set("disable_rag_injection", False)
                elif hasattr(ctx, "shared_dict"):
                    ctx.shared_dict["disable_rag_injection"] = False
                    
        tool_descs = "\n".join([f"- {t.name}: {t.description}" for t in tools]) if tools else "无可用工具"
        
        # 5. 构建 System Prompt
        system_prompt = await self.context_engine.build_prompt(
            chat_id=chat_id, 
            event_messages=event_messages,
            retrieve_keys=retrieve_keys,
            slang_patterns=slang_context,
            tool_descs=tool_descs,
            current_goal=current_goal
        )
        
        # 6. 沉浸模式强制收束：使用极致的强调语法防止跑题
        if is_all_mode:
            user_message = event.message_str
            system_prompt += f"\n\n>>> [当前任务核心] 用户刚才发送了消息：“{user_message}”，你必须且只能基于此消息进行回复！ <<<"
        
        # 7. 下发给 Executor (修复: 将 user_prompt 修正为正确的传参 prompt)
        await self.executor.execute(
            event=event,
            system_prompt=system_prompt,
            prompt=prompt_content,
            tools=tools
        )