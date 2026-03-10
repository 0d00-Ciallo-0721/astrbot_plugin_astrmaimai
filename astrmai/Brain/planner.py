# astrmai/Brain/planner.py
from typing import List
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger
import asyncio
from ..infra.gateway import GlobalModelGateway
from .context_engine import ContextEngine
from .executor import ConcurrentExecutor
from .reply_engine import ReplyEngine
from .tools.pfc_tools import WaitTool, FetchKnowledgeTool, QueryJargonTool
        
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
                 memory_engine: MemoryEngine,
                 evolution_manager: EvolutionManager
                 ):
        self.gateway = gateway
        self.context_engine = context_engine
        self.memory_engine = memory_engine
        self.evolution_manager = evolution_manager
        self.executor = ConcurrentExecutor(context, gateway, reply_engine)

    async def plan_and_execute(self, event: AstrMessageEvent, event_messages: List[AstrMessageEvent]):
        """
        [修改] 动态上下文修剪、切分视界消息构造纯文本当前剧本格式。
        """
        chat_id = event.unified_msg_origin
        
        retrieve_keys = event.get_extra("retrieve_keys", [])
        if not isinstance(retrieve_keys, list):
            retrieve_keys = []
            
        is_all_mode = "ALL" in retrieve_keys
        is_fast_mode = "CORE_ONLY" in retrieve_keys # [新增] 极速穿透模式标志
        
        if is_all_mode and len(event_messages) > 3:
            event_messages = event_messages[-3:]
            
        # [修改] 将滑动窗口内的“当前视界消息”彻底扁平化为纯台词格式
        window_lines = []
        for m in event_messages:
            sender_name = m.get_sender_name() or "群友/用户"
            window_lines.append(f"[{sender_name}] 说: {m.message_str}")
        prompt_content = "\n".join(window_lines)
        
        import asyncio
        # [修改] 极速模式下直接砍掉群组黑话与专属表达的检索
        if is_fast_mode:
            slang_context = ""
        else:
            slang_context = await asyncio.to_thread(self.evolution_manager.get_active_patterns, chat_id) 
            
        sys1_thought = event.get_extra("sys1_thought", "")
        
        ctx = getattr(self.context_engine, 'context', None)
        
        # [修改] 极速模式下不提供任何工具，强迫 AI 瞬间作答
        if is_all_mode or is_fast_mode:
            tools = []
            if ctx:
                if hasattr(ctx, "set"):
                    ctx.set("disable_rag_injection", True)
                elif hasattr(ctx, "shared_dict"):
                    ctx.shared_dict["disable_rag_injection"] = True
        else:
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
        
        system_prompt = await self.context_engine.build_prompt(
            chat_id=chat_id, 
            event_messages=event_messages,
            retrieve_keys=retrieve_keys,
            slang_patterns=slang_context,
            tool_descs=tool_descs,
            sys1_thought=sys1_thought 
        )
        
        if is_all_mode:
            user_message = event.message_str
            system_prompt += f"\n\n>>> [当前任务核心] 用户刚才发送了消息：“{user_message}”，你必须且只能基于此消息进行回复！ <<<"
            
        # [新增] 极速模式强化约束
        if is_fast_mode:
            system_prompt += "\n\n>>> [极速穿透模式] 你被强唤醒！请立刻、简短、直接地响应最新呼唤，忽略不必要的长篇大论。 <<<"
        
        await self.executor.execute(
            event=event,
            system_prompt=system_prompt,
            prompt=prompt_content,
            tools=tools
        )