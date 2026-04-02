# astrmai/Brain/planner.py
import random
from typing import List, Optional
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger
import asyncio
from ..infra.gateway import GlobalModelGateway
from .context_engine import ContextEngine
from .executor import ConcurrentExecutor
from .reply_engine import ReplyEngine
from .goal_manager import GoalManager
from .action_modifier import ActionModifier
from .expression_selector import ExpressionSelector  # Phase 6.1
# 统一导入所有系统原生工具与新增的 4 个拟人化微操工具
from .tools.pfc_tools import (
    WaitTool, 
    OmniPerceptionTool, 
    ConstructAtEventTool, 
    ProactivePokeTool, 
    ProactiveMemeTool,
    MemeResonanceTool,        # 🎭 复读/保持队形
    TopicHijackTool,          # 🥱 岔开话题
    SpaceTransitionTool,      # 🤫 悄悄话转私聊
    RegretAndWithdrawTool,    # 🛑 手滑撤回找补
    MessageReactionTool,      # ✨ 贴表情回应工具
    ProactiveLikeTool,        # 👍 狂点赞工具
    SelfLoreQueryTool         # 📜 [Phase 8] 原典潜意识防幻觉查阅工具
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
                 memory_engine: 'MemoryEngine',
                 evolution_manager: 'EvolutionManager',
                 state_engine=None,
                 prompt_refiner=None,
                 sys3_router=None
                 ):
        self.gateway = gateway
        self.context_engine = context_engine
        self.memory_engine = memory_engine
        self.evolution_manager = evolution_manager
        self.state_engine = state_engine  
        self.reply_engine = reply_engine 
        self.prompt_refiner = prompt_refiner 
        self.sys3_router = sys3_router
        self.context = context
        
        # Phase 1: 多目标管理器
        self.goal_manager = GoalManager(gateway, config=gateway.config)
        # Phase 5: 动态动作修改器
        self.action_modifier = ActionModifier(config=gateway.config)
        # Phase 6.1: 表达习惯选择器
        self.expression_selector = ExpressionSelector(
            db=context_engine.db,
            gateway=gateway,
            config=gateway.config
        )
        self.executor = ConcurrentExecutor(context, gateway, reply_engine, evolution_manager, config=gateway.config)
        
    async def plan_and_execute(self, event: AstrMessageEvent, event_messages: List[AstrMessageEvent]):
        """
        在发送给大模型前显式调用 Refiner 进行渲染，实现 100% 的确定性执行
        阶段三：主脑负载编排，提取直通图片 URL 并注入多模态旁白，传递给 Executor。
        """
        chat_id = event.unified_msg_origin
        user_id = event.get_sender_id() 
        sender_name = event.get_sender_name() or "群友/用户"

        retrieve_keys = event.get_extra("retrieve_keys", [])
        if not isinstance(retrieve_keys, list):
            retrieve_keys = []
            
        # ==========================================
        # 🟢 同步底层引擎的 is_fast_mode 标识
        # 确保 ContextEngine 和 Refiner 接收到 "CORE_ONLY" 指令并使用压缩人格
        # ==========================================
        if event.get_extra("is_fast_mode", False) and "CORE_ONLY" not in retrieve_keys:
            retrieve_keys.append("CORE_ONLY")
            
        is_all_mode = "ALL" in retrieve_keys
        is_fast_mode = "CORE_ONLY" in retrieve_keys
        
        # [Sys3新增] 读取 Sys1 透传的裁决动作
        judge_action = event.get_extra("judge_action", "REPLY")
        is_tool_call_mode = (judge_action == "TOOL_CALL") and (self.sys3_router is not None)
        
        if is_all_mode and len(event_messages) > 3:
            event_messages = event_messages[-3:]
            
        window_lines = []
        for m in event_messages:
            sender_name = m.get_sender_name() or "群友/用户"
            rich_text = m.get_extra("astrmai_rich_text", m.message_str)
            window_lines.append(f"[{sender_name}] 说: {rich_text}")
        prompt_content = "\n".join(window_lines)
        
        if is_fast_mode:
            slang_context = ""
        else:
            slang_context = await asyncio.to_thread(self.evolution_manager.get_active_patterns, chat_id) 
            
        sys1_thought = event.get_extra("sys1_thought", "")
        
        ctx = getattr(self.context_engine, 'context', None)
        
        # Phase 1: 更新多目标并获取目标上下文
        planner_reasoning = ""  # Phase 6.2B: 每轮决策的意图说明
        if not is_fast_mode:
            window_text = "\n".join(window_lines) if window_lines else ""
            goal_text = await self.goal_manager.analyze_and_update(chat_id, window_text)
            logger.debug(f"[{chat_id}] 🎯 当前主目标: {goal_text}")
            # reasoning 初始化为当前目标文本，后续 Executor 可覆写
            planner_reasoning = goal_text
        
        # [修改] 重构 tools 选择分支
        if is_all_mode:
            tools = None
            if ctx:
                if hasattr(ctx, "set"):
                    ctx.set("disable_rag_injection", True)
                elif hasattr(ctx, "shared_dict"):
                    ctx.shared_dict["disable_rag_injection"] = True
                    
        elif is_tool_call_mode:
            # ── [Sys3新增] TOOL_CALL 模式：加载 Sys3 SubAgent 轻量索引 ──
            sys3_light_tools = (await self.sys3_router.get_light_tools_for_planner()).tools
            
            # 保留核心 PFC 拟人工具，去除纯情感/聊天微操工具，专注执行任务
            target_persona_id = getattr(self.gateway.config.persona, 'persona_id', "") if hasattr(self.gateway.config, 'persona') else ""
            task_mode_pfc_tools = [
                WaitTool(),
                OmniPerceptionTool(
                    memory_engine=self.memory_engine,
                    db_service=self.context_engine.db,
                    chat_id=chat_id,
                    current_sender_id=str(user_id) if user_id is not None else "",
                    current_sender_name=sender_name
                ),
                SelfLoreQueryTool(memory_engine=self.memory_engine, persona_id=target_persona_id)
            ]
            
            tools = task_mode_pfc_tools + sys3_light_tools
            
            # TOOL_CALL 模式下关闭 RAG 注入以减少干扰
            if ctx:
                if hasattr(ctx, "set"):
                    ctx.set("disable_rag_injection", True)
                elif hasattr(ctx, "shared_dict"):
                    ctx.shared_dict["disable_rag_injection"] = True
            logger.info(f"[{chat_id}] 🔧 [TOOL_CALL 模式] 加载 Sys3 SubAgent 索引，工具总数: {len(tools)}")
            
        else:
            # ── 原有纯聊天模式 ──
            target_persona_id = getattr(self.gateway.config.persona, 'persona_id', "") if hasattr(self.gateway.config, 'persona') else ""
            tools = [
                WaitTool(),
                SelfLoreQueryTool(memory_engine=self.memory_engine, persona_id=target_persona_id),
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
                RegretAndWithdrawTool(),
                MessageReactionTool(),                                  
                ProactiveLikeTool(db_service=self.context_engine.db)    
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

            # Phase 5: 动态工具过滤 (四维关系驱动)
            state = None
            profile = None
            relationship_vec = None
            if self.state_engine:
                try:
                    state = await self.state_engine.get_state(chat_id)
                except Exception:
                    pass
                if user_id:
                    try:
                        profile = await self.state_engine.get_user_profile(str(user_id))
                    except Exception:
                        pass
                    if hasattr(self.state_engine, 'relationship_engine'):
                        relationship_vec = self.state_engine.relationship_engine.get_or_create(str(user_id))
            tools = self.action_modifier.modify_tools(tools, state=state, profile=profile, relationship_vec=relationship_vec)

        # Phase 1: 注入多目标上下文
        goals_context = self.goal_manager.get_goals_context(chat_id)
        
        tool_descs = "\n".join([f"- {t.name}: {t.description}" for t in tools]) if tools else "无可用工具"
        
        # Phase 6.1: 表达习惯选择 (ExpressionSelector)
        expression_habits = ""
        if not is_fast_mode:
            recent_text = "\n".join(window_lines[-3:]) if window_lines else ""
            expression_habits = await self.expression_selector.select(
                chat_id=chat_id,
                context_text=recent_text,
                think_level=0  # 默认快速模式，零 LLM
            )

        # Phase 6.2C: 黑话注入
        jargon_explanation = ""
        if not is_fast_mode:
            try:
                jargon_list = await self.context_engine.db.persistence.load_jargon_list(
                    chat_id, limit=8
                ) if hasattr(self.context_engine.db, 'persistence') else []
                if jargon_list:
                    jargon_lines = [
                        f"{j.get('text', '')} → {j.get('meaning', '...')} "
                        f"(场景: {j.get('situation', '?')})"
                        for j in jargon_list
                        if j.get('meaning') and j.get('text')
                    ]
                    if jargon_lines:
                        jargon_explanation = "\n".join(jargon_lines)
            except Exception as e:
                logger.debug(f"[Planner] 黑话加载失败: {e}")

        system_prompt = await self.context_engine.build_prompt(
            chat_id=chat_id, 
            event_messages=event_messages,
            retrieve_keys=retrieve_keys,
            slang_patterns=slang_context,
            tool_descs=tool_descs,
            sys1_thought=sys1_thought,
            goals_context=goals_context,
            expression_habits=expression_habits,    # Phase 6.1
            planner_reasoning=planner_reasoning,    # Phase 6.2B
            jargon_explanation=jargon_explanation,  # Phase 6.2C
        )

        # === [基于信标的源会话语境溯源拉取] ===
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
                    
                    if source_group_id and ctx:
                        try:
                            conv_mgr = ctx.conversation_manager
                            uid = f"default:GroupMessage:{source_group_id}"
                            curr_cid = await conv_mgr.get_curr_conversation_id(uid)
                            conversation = await conv_mgr.get_conversation(uid, curr_cid)
                            
                            import json
                            history = json.loads(conversation.history) if conversation and conversation.history else []
                            
                            recent_msgs = []
                            for msg in history[-5:]:
                                role = msg.get("role", "")
                                text_parts = [
                                    item.get("text", "") 
                                    for item in (msg.get("content") or []) 
                                    if isinstance(item, dict) and item.get("type") == "text"
                                ]
                                content = " ".join(text_parts) if text_parts else ""
                                if content:
                                    speaker = "群友" if role == "user" else "你"
                                    recent_msgs.append(f"[{speaker}]: {content}")
                            
                            if recent_msgs:
                                group_context_str = "\n".join(recent_msgs)
                        except Exception as e:
                            logger.error(f"🤫 [Planner] 溯源群聊历史失败: {e}")

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
                
                del jumps[sender_id]
        # ===============================================

        # [Sys3新增] 在 final_prompt 渲染前，如果处于工具模式，追加委派提示词
        if is_tool_call_mode:
            system_prompt += (
                "\n\n>>> [工作委派模式] <<<\n"
                "用户提出了明确的执行性需求。\n"
                "你现在的首要任务是：调用上方列出的对应子智能体工具来完成任务，而不是直接用文字假装执行。\n"
                "子智能体会替你真正执行任务并返回结果，你再将结果用你的语气告诉用户。\n"
                ">>> [委派说明结束] <<<"
            )

        if is_all_mode:
            user_message = event.message_str
            system_prompt += f"\n\n>>> [当前任务核心] 用户刚才发送了消息：“{user_message}”，你必须且只能基于此消息进行回复！ <<<"
            
        if is_fast_mode:
            system_prompt += "\n\n>>> [极速穿透模式] 你被强唤醒！请立刻、简短、直接地响应最新呼唤，忽略不必要的长篇大论。 <<<"
        
        final_system_prompt, final_prompt = await self.prompt_refiner.refine_prompt(
            event=event, 
            system_prompt=system_prompt, 
            prompt=prompt_content, 
            context=ctx
        )

        direct_vision_urls = event.get_extra("direct_vision_urls", [])
        if direct_vision_urls:
            final_prompt += "\n(导演旁白：用户递给了你几张照片，请结合画面内容进行回应。)"
            logger.info(f"[{chat_id}] 👁️ 已编排主脑直通车负载，携带 {len(direct_vision_urls)} 张图片进入执行器。")

        reply_text = await self.executor.execute(
            event=event,
            system_prompt=final_system_prompt,
            prompt=final_prompt,
            tools=tools,
            direct_vision_urls=direct_vision_urls 
        )

        # ==========================================
        # Phase 1: Follow-up 连续发言判定
        # ==========================================
        if reply_text and not is_fast_mode and not is_all_mode and not is_tool_call_mode:
            follow_reason = await self._should_follow_up(chat_id, reply_text)
            if follow_reason:
                logger.info(f"[{chat_id}] 💬 触发连续发言: {follow_reason}")
                follow_prompt = (
                    f"(导演旁白: 你刚刚说了 \"{reply_text[:100]}\"。"
                    f"现在你想补充一句——{follow_reason}。"
                    f"请生成一条极其简短的追加消息，像真人追发第二条那样自然。"
                    f"严禁重复你刚才说过的话！)"
                )
                await asyncio.sleep(random.uniform(1.0, 3.5))  # 模拟打字延迟
                await self.executor.execute(
                    event=event,
                    system_prompt=final_system_prompt,
                    prompt=follow_prompt,
                    tools=None  # 追加消息不使用工具
                )

    # ==========================================
    # Phase 1: Follow-up 连续发言决策器
    # ==========================================

    async def _should_follow_up(self, chat_id: str, last_reply: str) -> Optional[str]:
        """
        混合模式 Follow-up 决策器:
        第一层: 算法预筛 (零成本快速排除 ~85% 的情况)
        第二层: 极短 LLM 精判 (仅对通过预筛的 ~15% 情况调用)
        """
        # ===== 算法预筛层 (零 Token) =====
        
        # 精力不足时不追加
        if self.state_engine:
            state = self.state_engine.get_state(chat_id)
            if state and state.energy < 0.3:
                return None

        clean_reply = last_reply.strip()
        # 回复太短不追（已经够简洁了）
        if len(clean_reply) < 15:
            return None
            
        # 以问号结尾 = 已经在追问了，不再追
        if clean_reply.endswith("？") or clean_reply.endswith("?"):
            return None

        # 概率门控: 约 20% 概率才进入 LLM 精判（大幅降低调用频率）
        if random.random() > 0.20:
            return None

        # ===== LLM 精判层 (极短 Prompt) =====
        # 只截取回复前 100 字，prompt 总计 ~60 字
        prompt = f"""你刚回复:"{clean_reply[:100]}"
需要紧接着追发第二句吗？(补充/追问/表情/吐槽)
JSON: {{"follow": true/false, "reason": "原因"}}"""

        try:
            import re, json
            result = await self.gateway.call_data_process_task(prompt, is_json=True)
            data = result if isinstance(result, dict) else {}
            if not isinstance(data, dict):
                match = re.search(r'\{.*?\}', str(result), re.DOTALL)
                if match:
                    data = json.loads(match.group(0))
            if data.get("follow") or data.get("should_follow"):
                return data.get("reason", "补充细节")
        except Exception as e:
            logger.debug(f"[Planner] Follow-up 判定异常: {e}")
        return None