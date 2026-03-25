# astrmai/Brain/planner.py
from typing import List
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger
import asyncio
from ..infra.gateway import GlobalModelGateway
from .context_engine import ContextEngine
from .executor import ConcurrentExecutor
from .reply_engine import ReplyEngine

# [修改] 统一导入所有系统原生工具与新增的 4 个拟人化微操工具
from .tools.pfc_tools import (
    WaitTool, 
    OmniPerceptionTool, 
    ConstructAtEventTool, 
    ProactivePokeTool, 
    ProactiveMemeTool,
    MemeResonanceTool,        # 🎭 复读/保持队形
    TopicHijackTool,          # 🥱 岔开话题
    SpaceTransitionTool,      # 🤫 悄悄话转私聊
    RegretAndWithdrawTool     # 🛑 手滑撤回找补
)

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
                 state_engine=None,
                 prompt_refiner=None  # [新增] 接收注入的 Refiner
                 ):
        self.gateway = gateway
        self.context_engine = context_engine
        self.memory_engine = memory_engine
        self.evolution_manager = evolution_manager
        self.state_engine = state_engine  
        self.reply_engine = reply_engine 
        self.prompt_refiner = prompt_refiner # [新增] 挂载 Refiner
        
        self.executor = ConcurrentExecutor(context, gateway, reply_engine, evolution_manager, config=gateway.config)
        
    async def plan_and_execute(self, event: AstrMessageEvent, event_messages: List[AstrMessageEvent]):
        """
        [修改] 在发送给大模型前显式调用 Refiner 进行渲染，实现 100% 的确定性执行
        增加 4 个新增拟人化工具的挂载
        [修改] 阶段三：主脑负载编排，提取直通图片 URL 并注入多模态旁白，传递给 Executor。
        """
        chat_id = event.unified_msg_origin
        user_id = event.get_sender_id() 
        sender_name = event.get_sender_name() or "群友/用户"

        retrieve_keys = event.get_extra("retrieve_keys", [])
        if not isinstance(retrieve_keys, list):
            retrieve_keys = []
            
        is_all_mode = "ALL" in retrieve_keys
        is_fast_mode = "CORE_ONLY" in retrieve_keys
        
        if is_all_mode and len(event_messages) > 3:
            event_messages = event_messages[-3:]
            
        window_lines = []
        for m in event_messages:
            sender_name = m.get_sender_name() or "群友/用户"
            rich_text = m.get_extra("astrmai_rich_text", m.message_str)
            window_lines.append(f"[{sender_name}] 说: {rich_text}")
        prompt_content = "\n".join(window_lines)
        
        import asyncio
        if is_fast_mode:
            slang_context = ""
        else:
            slang_context = await asyncio.to_thread(self.evolution_manager.get_active_patterns, chat_id) 
            
        sys1_thought = event.get_extra("sys1_thought", "")
        
        ctx = getattr(self.context_engine, 'context', None)
        
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
                ProactivePokeTool(db_service=self.context_engine.db),
                ProactiveMemeTool(emotion_mapping=self.reply_engine.config.reply.emotion_mapping),
                MemeResonanceTool(),
                TopicHijackTool(),
                SpaceTransitionTool(),
                RegretAndWithdrawTool()
            ]
            if ctx:
                if is_fast_mode:
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

        # === [新增修复逻辑: 基于信标的源会话语境溯源拉取] ===
        # 如果当前是私聊会话
        if not event.get_group_id():
            shared_dict = getattr(ctx, "shared_dict", {})
            jumps = shared_dict.get("astrmai_space_jumps", {})
            sender_id = str(user_id)
            
            if sender_id in jumps:
                jump_info = jumps[sender_id]
                import time
                
                # 信标有效期 10 分钟
                if time.time() - jump_info["timestamp"] < 600:
                    source_group_id = jump_info.get("group_id")
                    group_context_str = ""
                    
                    # 借鉴底层穿透代码，逆向抓取跳出前的群聊历史
                    if source_group_id and ctx:
                        try:
                            conv_mgr = ctx.conversation_manager
                            # 构造群聊的 UID
                            uid = f"default:GroupMessage:{source_group_id}"
                            curr_cid = await conv_mgr.get_curr_conversation_id(uid)
                            conversation = await conv_mgr.get_conversation(uid, curr_cid)
                            
                            import json
                            # 解析该群的 JSON 历史记录
                            history = json.loads(conversation.history) if conversation and conversation.history else []
                            
                            recent_msgs = []
                            # 提取最近的 5 条群聊上下文
                            for msg in history[-5:]:
                                role = msg.get("role", "")
                                # 提取文本节点
                                text_parts = [
                                    item.get("text", "") 
                                    for item in (msg.get("content") or []) 
                                    if isinstance(item, dict) and item.get("type") == "text"
                                ]
                                content = " ".join(text_parts) if text_parts else ""
                                if content:
                                    # 简易区分用户与自身
                                    speaker = "群友" if role == "user" else "你"
                                    recent_msgs.append(f"[{speaker}]: {content}")
                            
                            if recent_msgs:
                                group_context_str = "\n".join(recent_msgs)
                        except Exception as e:
                            logger.error(f"🤫 [Planner] 溯源群聊历史失败: {e}")

                    # 构建跨界记忆注入包
                    sys_inject = (
                        f"\n\n>>> [!!! 极其重要的跨界前置记忆 !!!] <<<\n"
                        f"几分钟前，你刚刚在群聊 (群号:{source_group_id}) 中与大家互动，随后跳出来主动给当前用户发了一句私聊：\n"
                        f"【你的悄悄话原文】：{jump_info['private_message']}\n"
                    )
                    
                    if group_context_str:
                        sys_inject += f"\n【跳转前的群聊事件回顾 (参考)】：\n{group_context_str}\n"
                        
                    sys_inject += (
                        f"\n用户现在的回复绝对是对你上述行为的回应！请结合群里的前置话题和你的悄悄话，"
                        f"以私下交流的自然感、亲密感继续往下聊！\n"
                        f">>> [记忆读取完毕] <<<"
                    )
                    
                    system_prompt += sys_inject
                    logger.info(f"🤫 [Planner] 已触发跨界语境补偿，成功抓取群聊历史并注入到 {sender_id} 的私聊思考中。")
                
                # 阅后即焚，清理信标
                del jumps[sender_id]
        # ===============================================

        if is_all_mode:
            user_message = event.message_str
            system_prompt += f"\n\n>>> [当前任务核心] 用户刚才发送了消息：“{user_message}”，你必须且只能基于此消息进行回复！ <<<"

        # 🟢 [核心瘦身] 彻底删除了重复的 [物理动作规范] 和 [工具输出约束]，依靠 ContextEngine 中更系统的指南即可。
            
        if is_fast_mode:
            system_prompt += "\n\n>>> [极速穿透模式] 你被强唤醒！请立刻、简短、直接地响应最新呼唤，忽略不必要的长篇大论。 <<<"
        
        # 🟢 [核心重构] 显式调用 Refiner 进行字符串渲染，直接闭环获得处理后的 Prompt
        final_system_prompt, final_prompt = await self.prompt_refiner.refine_prompt(
            event=event, 
            system_prompt=system_prompt, 
            prompt=prompt_content, 
            context=ctx
        )

        # ==========================================
        # [新增] 阶段三：主脑负载编排 (主脑视觉直通车)
        # ==========================================
        direct_vision_urls = event.get_extra("direct_vision_urls", [])
        if direct_vision_urls:
            final_prompt += "\n(导演旁白：用户递给了你几张照片，请结合画面内容进行回应。)"
            logger.info(f"[{chat_id}] 👁️ 已编排主脑直通车负载，携带 {len(direct_vision_urls)} 张图片进入执行器。")

        await self.executor.execute(
            event=event,
            system_prompt=final_system_prompt,
            prompt=final_prompt,
            tools=tools,
            direct_vision_urls=direct_vision_urls # [修改] 传递直通车 URL 到第四阶段
        )