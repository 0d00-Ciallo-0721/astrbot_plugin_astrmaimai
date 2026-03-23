# astrmai/Brain/planner.py
from typing import List
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger
import asyncio
from ..infra.gateway import GlobalModelGateway
from .context_engine import ContextEngine
from .executor import ConcurrentExecutor
from .reply_engine import ReplyEngine
from .tools.pfc_tools import WaitTool, OmniPerceptionTool, ConstructAtEventTool, ProactivePokeTool, ProactiveMemeTool

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
                 evolution_manager: EvolutionManager,
                 state_engine=None  # [新增] 接收 state_engine 用于底层用户状态查询
                 ):
        self.gateway = gateway
        self.context_engine = context_engine
        self.memory_engine = memory_engine
        self.evolution_manager = evolution_manager
        self.state_engine = state_engine  # [新增] 挂载到实例
        
        # 🟢 [核心修复]: 必须将 reply_engine 挂载到实例属性，否则后续 plan_and_execute 无法访问其配置
        self.reply_engine = reply_engine 
        
        # 🟢 [核心修改 Bug 2]: 将 evolution_manager 传入 Executor，以便其进行潜意识写入
        self.executor = ConcurrentExecutor(context, gateway, reply_engine, evolution_manager, config=gateway.config)
    
    async def plan_and_execute(self, event: AstrMessageEvent, event_messages: List[AstrMessageEvent]):
        """
        [修改] 动态上下文修剪、切分视界消息构造纯文本当前剧本格式。
        """
        chat_id = event.unified_msg_origin
        user_id = event.get_sender_id() 
        sender_name = event.get_sender_name() or "群友/用户"

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
            # ✨ 【修改此行】：优先读取 sys1 已经解析好的富文本剧本，兜底才用 message_str
            rich_text = m.get_extra("astrmai_rich_text", m.message_str)
            window_lines.append(f"[{sender_name}] 说: {rich_text}")
        prompt_content = "\n".join(window_lines)
        
        import asyncio
        # [修改] 极速模式下直接砍掉群组黑话与专属表达的检索
        if is_fast_mode:
            slang_context = ""
        else:
            slang_context = await asyncio.to_thread(self.evolution_manager.get_active_patterns, chat_id) 
            
        sys1_thought = event.get_extra("sys1_thought", "")
        
        ctx = getattr(self.context_engine, 'context', None)
        
        # 🟢 [核心修复 Bug 2] 极速模式不再剥夺 tools，仅剥夺 RAG，确保动作能力（表情包等）正常运转
        if is_all_mode:
            tools = None
            if ctx:
                if hasattr(ctx, "set"):
                    ctx.set("disable_rag_injection", True)
                elif hasattr(ctx, "shared_dict"):
                    ctx.shared_dict["disable_rag_injection"] = True
        else:
            tools = [
                WaitTool(),
                OmniPerceptionTool(
                    memory_engine=self.memory_engine,
                    db_service=self.context_engine.db,
                    chat_id=chat_id,
                    current_sender_id=str(user_id) if user_id is not None else "",
                    current_sender_name=sender_name
                ),
                ConstructAtEventTool(db_service=self.context_engine.db),
                # === [新增] 挂载主动戳一戳工具 ===
                ProactivePokeTool(db_service=self.context_engine.db),
                # === [新增] 挂载主动表情包工具，并注入 Config 中的映射规则 ===
                ProactiveMemeTool(emotion_mapping=self.reply_engine.config.reply.emotion_mapping)
            ]
            if ctx:
                if is_fast_mode:
                    # 极速模式也禁用 RAG 减轻上下文包袱
                    if hasattr(ctx, "set"):
                        ctx.set("disable_rag_injection", True)
                    elif hasattr(ctx, "shared_dict"):
                        ctx.shared_dict["disable_rag_injection"] = True
                else:
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

        if not is_all_mode and not is_fast_mode:
             system_prompt += "\n\n>>> [物理动作规范] 你现在拥有了在群聊中执行物理动作的能力（如 @群友）。如果你决定使用工具执行动作，你依然必须在工具执行成功后输出最终的文本回复来解释你的行为。不要在执行完动作后就突然沉默！ <<<"
             
             # [安全加固] 在 system_prompt 尾部强化输出约束，防止非标准 JSON 导致 Executor 崩溃
             if tools:
                 system_prompt += "\n\n>>> [工具输出约束] 若你决定调用上述工具，你的输出 MUST 严格遵守 JSON 格式规范。不要包含任何除 JSON 之外的聊天解释或代码块修饰符！ <<<"
            
        # [新增] 极速模式强化约束
        if is_fast_mode:
            system_prompt += "\n\n>>> [极速穿透模式] 你被强唤醒！请立刻、简短、直接地响应最新呼唤，忽略不必要的长篇大论。 <<<"
        
        await self.executor.execute(
            event=event,
            system_prompt=system_prompt,
            prompt=prompt_content,
            tools=tools
        )