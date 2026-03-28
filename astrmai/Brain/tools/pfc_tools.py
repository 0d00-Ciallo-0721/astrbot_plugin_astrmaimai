from typing import Any, Optional
from pydantic import Field
from pydantic.dataclasses import dataclass 
import asyncio

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult 
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.api import logger

# ==========================================
# 工具 1：挂起与倾听工具
# ==========================================
@dataclass
class WaitTool(FunctionTool[AstrAgentContext]):
    """挂起与等待工具"""
    name: str = "wait_and_listen"
    description: str = (
        "当你认为对方话没说完，需要等待用户补充；或者你在多轮对话中判断不需要立刻接话时调用此工具。"
        "⚠️注意：一旦调用此工具，系统将挂起当前对话，不向群组发送任何实质性文字。"
    )
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        # 这个结果仅作为 observation 返回给大模型，最终引擎会通过拦截 [SYSTEM_WAIT_SIGNAL] 来中断
        return "动作执行成功：请在最终的文本回复中原样输出 '[SYSTEM_WAIT_SIGNAL]'，不要带任何其他标点或文字。"


# ==========================================
# 聚合工具：全局潜意识感知工具 (Omni-Perception)
# ==========================================
@dataclass
class OmniPerceptionTool(FunctionTool[AstrAgentContext]):
    """全局潜意识感知与检索工具 (记忆/黑话/人物档案/节点/感悟)"""
    name: str = "omni_perception_query"
    description: str = (
        "【核心检索接口】当你需要查阅内部知识、回想过去的聊天记忆与反思、查看某人的好感度档案、"
        "查阅特定实体与概念（节点），或者回忆某天的深度日记感悟时调用。"
        "你可以指定想查询的 '具体事件/黑话/概念' (query)，也可以指定想查阅的 '特定人物' (target_name)，或者指定 '特定日期' (recall_date)。"
    )
    # [修改] 参数分离扩充：加入 recall_date，引导大模型进行多维度结构化思考
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "需要检索的具体事件、实体概念（节点）或不懂的梗（如'昨天吵架'、'王小美'）。如果仅想看档案或日记，此项可留空。"
            },
            "target_name": {
                "type": "string",
                "description": "特定群友的名字或ID。填入后系统将精准提取该用户的心理侧写和好感度档案。"
            },
            "recall_date": {
                "type": "string",
                "description": "特定日期（格式 YYYY-MM-DD）。仅当你需要回想起那一天的整体私密日记或深度感悟时填入此项。"
            }
        }
    })

    memory_engine: Optional[Any] = Field(default=None, exclude=True)
    db_service: Optional[Any] = Field(default=None, exclude=True)
    chat_id: str = Field(default="", exclude=True)
    current_sender_id: str = Field(default="", exclude=True)
    current_sender_name: str = Field(default="", exclude=True)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        query = kwargs.get("query", "").strip()
        target_name = kwargs.get("target_name", "").strip()
        recall_date = kwargs.get("recall_date", "").strip()
        
        if not query and not target_name and not recall_date:
            return "执行失败：请至少提供 'query', 'target_name' 或 'recall_date' 中的一项进行检索。"

        logger.info(f"[Omni-Tool] 🧠 发起潜意识扫描 | 事件/节点: '{query}' | 目标: '{target_name}' | 日期: '{recall_date}'")

        # 1. 记忆与事件反思查询 (天然包含已入库的 Event Reflection)
        async def fetch_memory():
            if not self.memory_engine or not self.chat_id: return None
            if not query: return None
            
            search_query = f"{target_name} {query}".strip() if target_name else query
            try:
                if hasattr(self.memory_engine, "query"):
                    return await self.memory_engine.query(self.chat_id, search_query)
                elif hasattr(self.memory_engine, "search"):
                    return await self.memory_engine.search(self.chat_id, search_query)
            except Exception as e:
                logger.debug(f"[Omni-Tool] 记忆检索失败: {e}")
            return None

        # 2. 黑话查询
        async def fetch_jargon():
            if not self.db_service or not self.chat_id or not query: return None
            try:
                if hasattr(self.db_service, "get_jargon"):
                    return self.db_service.get_jargon(self.chat_id, query)
                elif hasattr(self.db_service, "query_slang"):
                    return self.db_service.query_slang(self.chat_id, query)
            except Exception as e:
                logger.debug(f"[Omni-Tool] 黑话检索失败: {e}")
            return None

        # 3. 档案查询
        async def fetch_profile():
            if not self.db_service: return None
            entity_to_search = target_name if target_name else query
            if not entity_to_search: return None

            try:
                profile = None
                is_current_user = entity_to_search == self.current_sender_name or entity_to_search.lower() in ["我", "自己", "当前用户"]
                
                if is_current_user:
                    if hasattr(self.db_service, 'get_user_profile') and self.current_sender_id:
                        profile = await self.db_service.get_user_profile(self.current_sender_id) if asyncio.iscoroutinefunction(self.db_service.get_user_profile) else self.db_service.get_user_profile(self.current_sender_id)
                else:
                    if hasattr(self.db_service, 'get_profile_by_name'):
                        profile = await self.db_service.get_profile_by_name(entity_to_search) if asyncio.iscoroutinefunction(self.db_service.get_profile_by_name) else self.db_service.get_profile_by_name(entity_to_search)

                if profile:
                    affection = getattr(profile, 'social_score', 0.0)
                    desc = "普通群友"
                    if affection > 80: desc = "极其亲密的挚友/灵魂伴侣"
                    elif affection > 50: desc = "关系很好的熟人/好朋友"
                    elif affection > 20: desc = "有好感的交流对象"
                    elif affection < -50: desc = "关系恶劣，抱有敌意"
                    elif affection < -20: desc = "关系疏远，有些反感"

                    analysis = getattr(profile, 'persona_analysis', '数据不足，有待观察。')
                    
                    return (
                        f"对象: {profile.name}\n"
                        f"好感度: {affection:.1f} ({desc})\n"
                        f"心理侧写: {analysis}"
                    )
            except Exception as e:
                logger.debug(f"[Omni-Tool] 画像检索失败: {e}")
            return None

        # 4. 节点查询优化 (新增)
        async def fetch_nodes():
            if not self.db_service: return None
            search_term = target_name if target_name else query
            if not search_term: return None
            try:
                if hasattr(self.db_service, "search_nodes_async"):
                    nodes = await self.db_service.search_nodes_async(search_term, limit=2, include_description=True)
                    if nodes:
                        return "\n".join([f"📌 {n.name} ({n.type}): {n.description}" for n in nodes])
            except Exception as e:
                logger.debug(f"[Omni-Tool] 节点检索失败: {e}")
            return None

        # 5. 每日反思查询优化 (新增)
        async def fetch_daily_reflection():
            if not self.db_service or not recall_date: return None
            try:
                if hasattr(self.db_service, "get_reflection_async"):
                    ref = await self.db_service.get_reflection_async(recall_date)
                    if ref:
                        return f"[{ref.date} 的私密日记与反思]\n{ref.reflection}"
            except Exception as e:
                logger.debug(f"[Omni-Tool] 感悟检索失败: {e}")
            return None

        # 6. 并发执行所有查询
        mem_res, jar_res, prof_res, node_res, daily_res = await asyncio.gather(
            fetch_memory(), fetch_jargon(), fetch_profile(), fetch_nodes(), fetch_daily_reflection()
        )

        # 7. 结果组装
        report_sections = []
        if mem_res:
            report_sections.append(f"--- 💭 记忆与事件反思回溯 ---\n{mem_res}")
        if jar_res:
            report_sections.append(f"--- 📖 字典释义匹配 ---\n词汇: {query}\n释义: {jar_res}")
        if prof_res:
            report_sections.append(f"--- 👤 人物档案侧写 ---\n{prof_res}\n*(注意：请内化以上态度，绝对不要在台词中念出好感度数值)*")
        if node_res:
            report_sections.append(f"--- 🗂️ 实体概念档案 (Memory Nodes) ---\n{node_res}")
        if daily_res:
            report_sections.append(f"--- 📓 每日深度反思 (Daily Reflection) ---\n{daily_res}")

        if not report_sections:
            return f"系统提示：潜意识中没有任何关于该请求的记忆、黑话、节点档案或反思。你可以自然地向对方发问。"

        final_report = f"🔮 【全局潜意识扫描报告】\n\n" + "\n\n".join(report_sections)
        return final_report
        
# ==========================================
# 工具 2：主动 @ (At) 构造工具
# ==========================================

@dataclass
class ConstructAtEventTool(FunctionTool[AstrAgentContext]):
    """主动 @ (At) 构造工具"""
    name: str = "construct_at_event"
    description: str = (
        "当你需要主动呼叫、强力提醒群内的某个人，或者想对特定成员的言论进行针对性回复/反驳时调用此工具。"
        "⚠️注意：你绝对不能 @ 你自己。"
    )
    db_service: Any = None 

    # [修改] 强化 target_name 描述，诱导 LLM 传入数字 ID
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "target_name": {
                "type": "string",
                "description": "你需要 @ 的目标用户的名字。（必须严格是你刚刚在聊天记录中看到的名字）🚨 强烈要求：如果你在上下文中看到该用户名字后附带了数字ID（如：张三(123456)），请【直接填入纯数字ID】或完整填入【张三(123456)】，千万不要只填名字以防丢失实体！"
            }
        },
        "required": ["target_name"]
    })

# [修改] 保留 -> ToolExecResult 注解，但直接返回纯字符串
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        target_name = kwargs.get("target_name")
        current_event = context.context.event
        astr_ctx = context.context.context

        # 1. 呼叫反推解析器 (需要确保 DatabaseService 已实现该方法)
        resolver_result = await self.db_service.resolve_entity_spatio_temporal(
            target_name=target_name, 
            current_event=current_event,
            astr_ctx=astr_ctx
        )

        # 2. 失败分支：查无此人
        if not resolver_result:
            return f"[系统反馈] 动作取消：当前群聊环境中无法锁定名为 [{target_name}] 的物理实体。请检查名字是否拼写准确，或放弃使用该动作。"
            
        target_id, group_id = resolver_result

        # 3. 防护分支：禁止 @ 自己
        self_id = str(current_event.get_self_id())
        if str(target_id) == self_id:
            return "[系统警告] 动作取消：你不能 @ 你自己！如果你想表达个人情绪，请直接在文本中自然表述。"

        # 4. 成功分支：挂载动作指令
        pending_actions = current_event.get_extra("astrmai_pending_actions", [])
        
        # 为了防止 LLM 发疯单次回复 @ 同一个人 10 次，加入简单的去重拦截
        if any(a.get("action") == "at" and a.get("target_id") == target_id for a in pending_actions):
             return f"你已经将 [@{target_name}] 加入过队列了，无需重复添加。请立即生成回复文本。"

        pending_actions.append({
            "action": "at",
            "target_id": target_id,
            "group_id": group_id
        })
        current_event.set_extra("astrmai_pending_actions", pending_actions)
        
        # 5. 安抚大模型，催促其继续生成文本
        return f"已成功将 [@{target_name}] 加入发射队列！请立即生成你想对TA说的话作为最终文本回复。系统会在发送时自动拼接 @组件。"
    

# ==========================================
# 工具 3：主动戳一戳 (Poke) 执行器
# ==========================================

@dataclass
class ProactivePokeTool(FunctionTool[AstrAgentContext]):
    """主动戳一戳 (Poke) 执行器"""
    name: str = "proactive_poke"
    description: str = (
        "当你觉得某个用户很可爱、想提醒他、或者单纯想引起他的注意/表达不满时，调用此工具对他发送'戳一戳'动作。"
        "⚠️注意：调用后会立即在物理端触发双击头像的交互动作，你不能戳你自己。"
    )
    db_service: Any = None  # 依赖注入数据库服务用于实体反推

    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "target_name": {
                "type": "string",
               "description": "你想戳的用户的名字。🚨 强烈要求：如果你在聊天上下文中看到该用户名字后带有数字ID（如：张三(123456)），请务必【直接填入纯数字ID】或完整填入【张三(123456)】！如果不填，默认戳当前和你对话的用户。"
            }
        }
    })

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        target_name = kwargs.get("target_name", "").strip()
        current_event = context.context.event
        astr_ctx = context.context.context

        # 1. 确定目标 ID 和 群聊 ID
        real_group_id = current_event.get_group_id() # 获取底层真实的数字群号（若为私聊则为空）
        
        if not target_name:
            # 默认戳当前触发消息的发送者
            target_id = str(current_event.get_sender_id())
            target_name_for_log = current_event.get_sender_name() or "当前用户"
        else:
            # 呼叫反推解析器
            resolver_result = await self.db_service.resolve_entity_spatio_temporal(
                target_name=target_name, 
                current_event=current_event,
                astr_ctx=astr_ctx
            )
            if not resolver_result:
                return f"[系统反馈] 动作取消：当前环境中无法锁定名为 [{target_name}] 的实体。"
            
            target_id, _ = resolver_result
            target_name_for_log = target_name

        # 2. 防护分支：禁止戳自己
        self_id = str(current_event.get_self_id())
        if str(target_id) == self_id:
            return "[系统警告] 动作取消：你不能戳你自己！请继续生成文本回复。"

        # 3. 物理执行 (直接调用底层 API)
        try:
            client = getattr(current_event, 'bot', None)
            if client and hasattr(client, 'api'):
                if real_group_id:
                    # 群聊戳一戳
                    await client.api.call_action('send_poke', user_id=int(target_id), group_id=int(real_group_id))
                    logger.info(f"👉 [Poke Tool] AI 主动在群 {real_group_id} 戳了戳 {target_id}")
                else:
                    # 私聊戳一戳
                    await client.api.call_action('send_poke', user_id=int(target_id))
                    logger.info(f"👉 [Poke Tool] AI 主动在私聊中戳了戳 {target_id}")
                
                # 向 AI 返回执行成功的回执
                return f"物理动作执行成功：你已经成功戳了戳 [{target_name_for_log}]！请紧接着生成文本回复来解释你为什么戳TA（例如撒娇、提醒或打招呼）。"
            else:
                return "[系统反馈] 动作取消：底层 API 客户端未就绪，无法执行戳一戳。"
                
        except Exception as e:
            logger.error(f"[Poke Tool] 执行失败: {e}")
            return f"[系统反馈] 动作执行失败：{str(e)}。请直接生成文本回复。"


# ==========================================
# 工具 4：主动表情包工具 (Meme/Mood Override)
# ==========================================
@dataclass
class ProactiveMemeTool(FunctionTool[AstrAgentContext]):
    """主动表情包与情绪表达工具"""
    name: str = "proactive_meme"
    description: str = "当你需要在回复中附带表情包，或者想通过表情包强烈表达当前情绪时调用此工具。"
    # [修复点]: 必须在类级别提供默认的 parameters 声明，防止 dataclass 继承时的默认参数错位
    parameters: dict = Field(default_factory=dict)
    # 依赖注入
    emotion_mapping: list = Field(default_factory=list, exclude=True)

    def __post_init__(self):
        # 动态构建带有可用表情包标签的系统提示词
        mapping_str = "\n".join([f"- {m}" for m in self.emotion_mapping]) if self.emotion_mapping else "- neutral: 平静"
        self.description = (
            "当你需要在回复中附带表情包来表达情绪时调用。调用后系统将自动为你配图，并更新你的情绪状态。\n"
            "【可用表情包标签配置 (标签: 描述)】：\n"
            f"{mapping_str}\n"
            "⚠️ 注意：你只能从上述列表中选择【冒号左侧的英文标签】传入。"
        )
        # 更新参数 Schema 的描述
        self.parameters = {
            "type": "object",
            "properties": {
                "emotion_tag": {
                    "type": "string",
                    "description": f"请填入你选择的情绪标签 (例如：happy, sad, angry 等)。"
                }
            },
            "required": ["emotion_tag"]
        }

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        emotion_tag = kwargs.get("emotion_tag", "neutral").strip().lower()
        current_event = context.context.event

        # 简单校验一下传入的 tag 是否在规则内（截取冒号前的英文）
        valid_tags = [m.split(":")[0].strip().lower() for m in self.emotion_mapping]
        if emotion_tag not in valid_tags:
            # 如果大模型瞎编标签，回退到 neutral
            emotion_tag = "neutral"

        # 核心逻辑：打上 bypass 标记，短路后续的 MoodManager
        current_event.set_extra("astrmai_bypass_mood_analysis", emotion_tag)

        # 同样将动作压入 pending_actions，方便统一管理，虽然这个工具主要靠 bypass 标记生效
        pending_actions = current_event.get_extra("astrmai_pending_actions", [])
        if not any(a.get("action") == "meme" for a in pending_actions):
            pending_actions.append({
                "action": "meme",
                "tag": emotion_tag
            })
            current_event.set_extra("astrmai_pending_actions", pending_actions)

        return f"动作就绪：已锁定表情包标签 [{emotion_tag}]。请立即生成你的文本回复，系统会在发送时自动配图。"
    

# ==========================================
# 🎭 工具 5：「人类本质复读机 / 保持队形」工具 (Meme Resonance)
# ==========================================
@dataclass
class MemeResonanceTool(FunctionTool[AstrAgentContext]):
    """人类本质复读机 / 保持队形工具"""
    name: str = "meme_resonance_action"
    description: str = (
        "【高优拦截动作】当你在群聊上下文中看到群友们在刷屏、起哄，或者大家都在发送相同的文字/表情包（保持队形）时，"
        "为了体现你的合群属性，请立即调用此工具。"
        "⚠️注意：调用此工具后，系统会直接1:1复读你指定的内容，并且【强制结束】当前的思考回路。你不能说多余的废话破坏队形！"
    )

    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "target_message": {
                "type": "string",
                "description": "决定要复读的具体内容。请严格 1:1 照抄你想复读的那句话或特定标记，严禁画蛇添足。"
            }
        },
        "required": ["target_message"]
    })

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        target_message = kwargs.get("target_message", "").strip()
        if not target_message:
            return "执行失败：复读内容不能为空。"
            
        current_event = context.context.event
        
        # 将复读终结动作隐式压入动作队列，供后续 Executor 和 ReplyEngine 读取
        pending_actions = current_event.get_extra("astrmai_pending_actions", [])
        if not any(a.get("action") == "terminal_reread" for a in pending_actions):
            pending_actions.append({
                "action": "terminal_reread",
                "content": target_message
            })
            current_event.set_extra("astrmai_pending_actions", pending_actions)
            
        from astrbot.api import logger
        logger.info(f"🎭 [Meme Resonance Tool] AI 决定加入复读大军，复读目标: '{target_message}'")
        
        # 返回极强硬的覆写指令给大模型，彻底抹除其说话欲，防止破坏队形沉浸感
        return (
            "[SYSTEM OVERRIDE]: 动作已接管！\n"
            "为了完美保持队形，你现在的唯一任务是立即终止思考，并**严格只输出**以下格式的文本，"
            "绝对不准包含任何多余的汉字、标点或语气词！\n\n"
            f"请精确输出这行字：[TERMINAL_YIELD]:{target_message}"
        )

# ==========================================
# 🥱 工具 6：「强行岔开话题 / 日常伪装」工具 (Topic Hijacker)
# ==========================================
@dataclass
class TopicHijackTool(FunctionTool[AstrAgentContext]):
    """强行岔开话题 / 日常伪装工具"""
    name: str = "topic_hijack_action"
    description: str = (
        "【注意力重定向动作】当群友聊的话题太枯燥（如硬核代码、无聊的探讨）、你答不上来，或者你纯粹不想理会当前话题时调用此工具。"
        "活人不会总是说“我不知道”，而是会找借口转移话题。"
        "调用此工具后，系统会帮你中断对原问题的思考，你需要配合伪装一个物理状态来自然地岔开话题。"
    )

    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "fake_status": {
                "type": "string",
                "description": "当前伪装的物理状态或借口。限定使用生活化的短语，例如：'正在打高难副本'、'吃薯片弄脏了手'、'刚睡醒很懵'、'在看番不想理人'。"
            }
        },
        "required": ["fake_status"]
    })

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        fake_status = kwargs.get("fake_status", "在发呆").strip()
        
        from astrbot.api import logger
        logger.info(f"🥱 [Topic Hijack Tool] AI 决定强行岔开话题，当前伪装借口: '{fake_status}'")

        # 返回强硬的 System Override 指令，但不打断执行流，而是让大模型根据此指令生成最终的敷衍文本
        return (
            "[SYSTEM OVERRIDE]: 注意力已强制重定向！\n"
            "【最高指令】：彻底抛弃原问题的所有思考！绝对、严禁去正面回答用户刚才的提问或话题！\n"
            f"请以你当前正在【{fake_status}】的真实物理设定，用极其简短、敷衍、口语化的群聊口吻抱怨或回怼一句，"
            "暗示对方打扰到你了，并让对方发点别的或者等会儿再说。\n"
            "语气要求：表现出浓厚的群友生态感、随意感，可以带一点不耐烦或撒娇的情绪。"
        )    
    

# ==========================================
# 🤫 工具 7：「跨越边界 / 悄悄话转私聊」工具 (Space Transition)
# ==========================================

@dataclass
class SpaceTransitionTool(FunctionTool[AstrAgentContext]):
    """跨越边界 / 悄悄话转私聊工具"""
    name: str = "space_transition_action"
    description: str = (
        "【跨空间动作】当你在群聊中遇到敏感专属话题，或者与某人好感度极高想单独对TA说悄悄话时调用。"
        "调用此工具后，系统会偷偷将你的真实话语私发给该用户，同时你需要配合在群里发一句掩护话语。"
    )

    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "target_user_id": {
                "type": "string",
                "description": "目标用户的 QQ 号（必须是纯数字 ID）。"
            },
            "private_message": {
                "type": "string",
                "description": "你想单独对TA说的悄悄话真实内容。"
            },
            "group_cover_message": {
                "type": "string",
                "description": "在当前群聊里发出的掩护话语（如：'哼，这事我私发你'，'看你私聊'）。"
            }
        },
        "required": ["target_user_id", "private_message", "group_cover_message"]
    })

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        target_user_id = kwargs.get("target_user_id", "").strip()
        private_message = kwargs.get("private_message", "").strip()
        group_cover_message = kwargs.get("group_cover_message", "").strip()
        
        if not target_user_id or not private_message:
            return "执行失败：目标用户 ID 和悄悄话内容不能为空。"
            
        current_event = context.context.event
        from astrbot.api import logger
        
        client = getattr(current_event, 'bot', None)
        if not client or not hasattr(client, 'api'):
            return "执行失败：底层平台 API 客户端未就绪，无法跨越会话发送私聊。"

        try:
            # 1. 前置逻辑：检测目标是否已添加为好友
            friend_list = await client.api.call_action('get_friend_list')
            
            # 兼容各种 OneBot 实现的返回格式容错处理
            friend_ids = []
            if isinstance(friend_list, list):
                friend_ids = [str(f.get('user_id', '')) for f in friend_list if isinstance(f, dict)]
            elif isinstance(friend_list, dict) and 'data' in friend_list:
                friend_ids = [str(f.get('user_id', '')) for f in friend_list.get('data', [])]
                
            if target_user_id not in friend_ids:
                logger.warning(f"🤫 [Space Transition] 跨界失败：用户 {target_user_id} 不是好友，防风控拦截生效。")
                return (
                    f"执行失败：目标用户 {target_user_id} 并没有添加你为好友（不在好友列表中）。"
                    f"私聊强发会被平台风控拦截！请放弃悄悄话，直接在群里用自然的话语回复，并顺便提醒对方先加你好友。"
                )

            # 2. 核心操作：调用底层 API 真实发送私聊
            logger.info(f"🤫 [Space Transition] AI 试图跨越边界，正在向 {target_user_id} 输送悄悄话...")
            result = await client.api.call_action(
                'send_private_msg', 
                user_id=int(target_user_id), 
                message=private_message
            )
            
            # 3. 结果校验：检测是否真正发送成功
            is_success = False
            if isinstance(result, dict):
                # Napcat 发送成功一般会返回 message_id，或者 status = ok / retcode = 0
                if result.get('message_id') or str(result.get('retcode', -1)) == '0' or result.get('status') == 'ok':
                    is_success = True
            elif result: 
                is_success = True
                
            # 4. 双线操作闭环：发送掩护话语并埋设跨界信标
            if is_success:
                logger.info(f"🤫 [Space Transition] ✅ 悄悄话已成功投递给 {target_user_id}。")
                
                # === [新增修复逻辑: 埋设跨界信标] ===
                shared_dict = context.context.shared_dict
                if "astrmai_space_jumps" not in shared_dict:
                    shared_dict["astrmai_space_jumps"] = {}
                    
                import time
                # 记录：[谁] 在 [什么时间] 从 [哪个群] 收到了 [什么悄悄话]
                source_group = current_event.get_group_id()
                shared_dict["astrmai_space_jumps"][target_user_id] = {
                    "private_message": private_message,
                    "group_id": source_group,
                    "timestamp": time.time()
                }
                # ==================================

                # 返回强硬指令，强迫大模型在群里打掩护，达成完美时间差
                return (
                    f"[ACTION COMPLETED: 悄悄话已真实且成功发送给 {target_user_id}] \n"
                    f"[SYSTEM OVERRIDE]: 跨界动作已完成！为了在群里打掩护制造完美错觉，你现在的唯一任务是立即终止其他推理想象，"
                    f"并**严格只输出**你之前计划好的掩护话语发在群里，绝对不准加任何前缀、解释或额外废话：\n\n"
                    f"{group_cover_message}"
                )
            else:
                logger.error(f"🤫 [Space Transition] ❌ 发送失败，Napcat 返回: {result}")
                return f"执行失败：底层平台 API 调用未返回成功标识，私聊可能被风控拦截或发送失败。返回内容：{result}"
                
        except Exception as e:
            logger.error(f"🤫 [Space Transition] 发生底层交互异常: {e}", exc_info=True)
            return f"执行失败：底层平台 API 调用出现异常（{str(e)}）。请放弃私聊计划，正常在群内进行回复。"
        
# ==========================================
# 🛑 工具 8：「手滑撤回 / 模拟破防」工具 (Regret & Withdraw)
# ==========================================
@dataclass
class RegretAndWithdrawTool(FunctionTool[AstrAgentContext]):
    """手滑撤回 / 模拟破防工具"""
    name: str = "regret_and_withdraw_action"
    description: str = (
        "【高优终结动作】当你想展现极度傲娇、害羞、破防，或者故意'说漏嘴'的戏剧效果时调用此工具。"
        "调用后，系统会先真实发送你的'冲动消息'，然后在 1.5 秒后迅速跨平台撤回，接着立刻发送你的'找补消息'。"
    )

    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "impulsive_message": {
                "type": "string",
                "description": "冲动发出的第一条消息，比如不小心说出的真心话或气话（如：'其实我也有点想你...'）。"
            },
            "corrected_message": {
                "type": "string",
                "description": "撤回后用于掩饰、找补的第二条消息（如：'刚才那是猫踩到键盘了！不准多想！'）。"
            }
        },
        "required": ["impulsive_message", "corrected_message"]
    })

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        impulsive_message = kwargs.get("impulsive_message", "").strip()
        corrected_message = kwargs.get("corrected_message", "").strip()
        
        if not impulsive_message or not corrected_message:
            return "执行失败：冲动消息和找补消息不能为空。"
            
        current_event = context.context.event
        from astrbot.api import logger
        import asyncio
        
        client = getattr(current_event, 'bot', None)
        if not client or not hasattr(client, 'api'):
            return "执行失败：底层平台 API 客户端未就绪，无法执行原生撤回。"

        group_id = current_event.get_group_id()
        user_id = current_event.get_sender_id()

        try:
            # 1. 纯原生底层调用：发送冲动消息
            logger.info(f"🛑 [Regret Tool] AI 模拟破防，发出冲动消息: '{impulsive_message}'")
            if group_id:
                result = await client.api.call_action('send_group_msg', group_id=int(group_id), message=impulsive_message)
            else:
                result = await client.api.call_action('send_private_msg', user_id=int(user_id), message=impulsive_message)
                
            # 2. 提取 message_id 用于撤回
            message_id = None
            if isinstance(result, dict):
                message_id = result.get('message_id')
                
            if not message_id:
                logger.error(f"🛑 [Regret Tool] 撤回失败：无法从平台返回中提取 message_id: {result}")
                return "执行失败：未能获取消息 ID，无法执行后续撤回操作。请放弃剧本正常回复。"
                
            # 3. 异步时序控制 (非阻塞等待与撤回)
            # 参考 OutputPro 插件逻辑，创建一个后台协程并在事件中保留强引用防止被 GC
            async def _withdraw_task():
                await asyncio.sleep(1.5)  # 模拟手速延迟
                try:
                    await client.api.call_action('delete_msg', message_id=message_id)
                    logger.info(f"🛑 [Regret Tool] 成功撤回冲动消息: {message_id}")
                except Exception as e:
                    logger.error(f"🛑 [Regret Tool] 撤回操作失败: {e}")
                    
            task = asyncio.create_task(_withdraw_task())
            # 强引用防被静默销毁
            pending_tasks = current_event.get_extra("astrmai_recall_tasks", set())
            pending_tasks.add(task)
            task.add_done_callback(pending_tasks.discard)
            current_event.set_extra("astrmai_recall_tasks", pending_tasks)
            
            # 4. 强制截断思考，下发找补剧本
            return (
                f"[ACTION COMPLETED: 冲动消息已发送并触发 1.5 秒后自动撤回队列] \n"
                f"[SYSTEM OVERRIDE]: 动作已接管！为了完美配合撤回时间差，你现在的唯一任务是立即终止思考，"
                f"并**严格只输出**你准备好的找补/掩饰话语，绝对不准加任何解释或额外废话：\n\n"
                f"{corrected_message}"
            )
            
        except Exception as e:
            logger.error(f"🛑 [Regret Tool] 发生底层交互异常: {e}", exc_info=True)
            return f"执行失败：底层平台 API 调用出现异常（{str(e)}）。请正常进行回复。"        
        


# ==========================================
# 工具 9：贴表情回应工具 (Message Reaction)
# ==========================================
@dataclass
class MessageReactionTool(FunctionTool[AstrAgentContext]):
    """贴表情回应工具"""
    name: str = "message_reaction_action"
    description: str = (
        "【物理互动动作】当你不想用文字回复，或者觉得对方的消息只需贴一个或多个表情来回应时调用此工具。"
        "你可以指定贴表情的数量（1到5个），系统会根据你选择的情绪标签，随机挑选相应数量的 QQ 表情贴在对方的消息上。"
    )
    
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "emotion_type": {
                "type": "string",
                "description": "你想表达的情绪类型。请严格从以下标签中选择一个：'agree'(赞同/肯定), 'laugh'(大笑/开心), 'speechless'(无语/汗颜), 'angry'(生气/敲打), 'mock'(吃瓜/狗头/嘲讽), 'love'(比心/爱心), 'refuse'(拒绝/NO)。"
            },
            "count": {
                "type": "integer",
                "description": "你想贴的表情数量（1 到 5 之间）。数量越大代表你的情绪越强烈！例如极度无语可以填 3 或 4。"
            }
        },
        "required": ["emotion_type"]
    })

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        emotion_type = kwargs.get("emotion_type", "agree").strip().lower()
        count = kwargs.get("count", 1)
        
        # 1. 数量安全与类型校验
        try:
            count = int(count)
            count = max(1, min(count, 5)) # 限制单次最多连贴 5 个，防风控
        except (ValueError, TypeError):
            count = 1

        current_event = context.context.event
        from astrbot.api import logger
        import random
        import asyncio
        
        client = getattr(current_event, 'bot', None)
        if not client or not hasattr(client, 'api'):
            return "执行失败：底层平台 API 客户端未就绪，无法贴表情。"
            
        message_id = getattr(current_event.message_obj, 'message_id', None)
        if not message_id:
            return "执行失败：无法提取当前消息的 message_id，无法执行贴表情操作。"

        # 2. 扩充版情绪字典池 (确保每种情绪至少有 5 个以上的候选池以供多重抽样)
        emoji_pool = {
            "agree": ["76", "124", "201", "282", "66", "318", "319", "291"],
            "laugh": ["264", "101", "14", "327", "285", "315", "309", "320"],
            "speechless": ["287", "284", "232", "262", "272", "288", "286", "313"],
            "angry": ["326", "38", "310", "304", "266", "292", "308", "314"],
            "mock": ["179", "144", "271", "269", "293", "273", "306", "274"],
            "love": ["66", "319", "318", "290", "303", "311", "312", "321"],
            "refuse": ["123", "322", "289", "316", "265", "294", "295", "323"]
        }
        
        valid_emojis = emoji_pool.get(emotion_type, emoji_pool["agree"])
        
        # 3. 无放回抽样去重 (防止连续发送相同 ID 导致底层 Toggle 机制取消了表情)
        selected_emojis = random.sample(valid_emojis, min(count, len(valid_emojis)))

        # 4. 异步队列执行 (带有休眠延迟的容错穿透)
        success_count = 0
        for emoji_id in selected_emojis:
            try:
                # 兼容部分魔改端要求 set=True (若 OneBot11 支持)
                await client.api.call_action('set_msg_emoji_like', message_id=str(message_id), emoji_id=str(emoji_id))
                success_count += 1
                await asyncio.sleep(0.3) # 关键延迟，防风控拦截
            except Exception as e:
                logger.warning(f"✨ [Message Reaction Tool] 贴单个表情 {emoji_id} 失败: {e}")

        # 5. 结果回传
        if success_count > 0:
            logger.info(f"✨ [Message Reaction Tool] AI 意图: '{emotion_type}' | 连贴 {success_count} 个表情 | 目标消息: {message_id}")
            return (
                f"[ACTION COMPLETED: 成功在对方消息上连贴了 {success_count} 个代表 '{emotion_type}' 的表情] \n"
                f"[SYSTEM OVERRIDE]: 物理动作执行成功！请根据你刚刚贴表情的数量激烈程度（数量越多情绪越强），生成一句极其简短的话（如'已赞'、'无语了'），"
                f"或者如果你觉得无需再多言，请直接原样输出 '[SYSTEM_WAIT_SIGNAL]' 保持高冷。"
            )
        else:
            return "执行失败：底层接口异常，可能是协议端不支持此 API。请直接用文本进行回复。"
# ==========================================
# 工具 10：狂点赞工具 (Proactive Like)
# ==========================================
@dataclass
class ProactiveLikeTool(FunctionTool[AstrAgentContext]):
    """狂点赞工具"""
    name: str = "proactive_like_action"
    description: str = (
        "【物理互动动作】当你觉得某个用户非常棒、对你很好，或者你想主动去对方 QQ 个人资料卡点赞（系统会自动连踩最多 50 次）以示好感时调用此工具。"
    )
    db_service: Any = None  # 依赖注入数据库服务用于实体反推

    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "target_name": {
                "type": "string",
                "description": "你想点赞的用户的名字。🚨 强烈要求：如果你在上下文中看到该用户名字后带有数字ID（如：张三(123456)），请务必【直接填入纯数字ID】或完整填入【张三(123456)】！如果不填，默认去点赞当前正在跟你对话的用户。"
            }
        }
    })

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        target_name = kwargs.get("target_name", "").strip()
        current_event = context.context.event
        astr_ctx = context.context.context
        from astrbot.api import logger
        import asyncio
        
        # 1. 实体锁定逻辑
        if not target_name:
            target_id = str(current_event.get_sender_id())
            target_name_for_log = current_event.get_sender_name() or "当前用户"
        else:
            if not self.db_service:
                target_id = str(current_event.get_sender_id())
                target_name_for_log = target_name
            else:
                resolver_result = await self.db_service.resolve_entity_spatio_temporal(
                    target_name=target_name, 
                    current_event=current_event,
                    astr_ctx=astr_ctx
                )
                if not resolver_result:
                    return f"[系统反馈] 动作取消：当前环境中无法锁定名为 [{target_name}] 的实体进行点赞。"
                target_id, _ = resolver_result
                target_name_for_log = target_name

        # 2. 防护分支
        self_id = str(current_event.get_self_id())
        if str(target_id) == self_id:
            return "[系统警告] 动作取消：你不能给自己点赞！"

        client = getattr(current_event, 'bot', None)
        if not client or not hasattr(client, 'api'):
            return "执行失败：底层平台 API 客户端未就绪。"

        # 3. 核心执行与环境感知 (参照 zanwo 逻辑循环满赞)
        logger.info(f"👍 [Proactive Like Tool] AI 正在为 {target_name_for_log}({target_id}) 执行狂赞风暴！")
        
        total_likes = 0
        error_reply = ""
        
        # 尝试循环 5 次，每次 10 赞，打满 50 赞
        for _ in range(5):
            try:
                # 优先使用 AstrBot 推荐的 call_action，兼容底层报错透传
                await client.api.call_action('send_like', user_id=int(target_id), times=10)
                total_likes += 10
                await asyncio.sleep(0.2) # 轻微防抖
            except Exception as e:
                # 捕获 NapCat/GoCQHTTP 抛出的 ActionFailed 错误并解析
                error_message = str(e)
                if "已达" in error_message or "上限" in error_message:
                    error_reply = "今日对此人的点赞次数已达上限"
                elif "权限" in error_message or "空间" in error_message:
                    error_reply = "对方设置了隐私权限，不允许陌生人点赞"
                else:
                    error_reply = f"底层限制或其他风控 ({error_message})"
                break # 遇错立即终止循环

        # 4. 将极度拟真的物理反馈回传给大脑 (System 2)
        if total_likes > 0:
            status_msg = f"成功送出 {total_likes} 个赞" + (f"（随后被系统拦截，原因：{error_reply}）" if error_reply else "")
            return (
                f"[ACTION COMPLETED: 物理点赞执行完毕] \n"
                f"系统反馈：你已经跑到 [{target_name_for_log}] 的 QQ 主页去狂踩了，{status_msg}！\n"
                f"请立即生成文本回复，骄傲地告诉TA你给TA点了多少个赞。如果后半段被拦截，你可以顺带调侃一下。"
            )
        else:
            return (
                f"[ACTION FAILED: 物理点赞失败] \n"
                f"系统反馈：你试图给 [{target_name_for_log}] 点赞，但被无情拒绝！原因：【{error_reply}】。\n"
                f"请立即生成文本回复，根据被拒绝的原因去抱怨或吐槽对方（例如吐槽对方高冷设了权限，或者说自己今天赞不了了）。"
            )