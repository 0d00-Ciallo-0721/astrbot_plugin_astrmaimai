from typing import Any, Optional
from pydantic import Field
from pydantic.dataclasses import dataclass 

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
    """全局潜意识感知与检索工具 (记忆/黑话/人物画像)"""
    name: str = "omni_perception_query"
    description: str = (
        "【核心检索接口】当你需要查阅任何内部知识时调用此工具。"
        "无论是回想过去发生的事、查询不懂的梗/黑话，还是想了解某个群友的羁绊好感度和心理侧写，都只需将关键词传入此工具。"
        "系统会同时扫描你的记忆库、字典库和人物档案库，并返回一份综合报告。"
    )
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "需要检索的关键词。可以是事件片段（如'昨天吵架'）、未知的黑话/梗（如'Ciallo'）、或者是特定群友的名字（如'张三'）。"
            }
        },
        "required": ["query"]
    })

    # 依赖注入集合 (整合了三个工具所需的所有依赖)
    memory_engine: Optional[Any] = Field(default=None, exclude=True)
    db_service: Optional[Any] = Field(default=None, exclude=True)
    chat_id: str = Field(default="", exclude=True)
    current_sender_id: str = Field(default="", exclude=True)
    current_sender_name: str = Field(default="", exclude=True)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        query = kwargs.get("query", "").strip()
        if not query:
            return "执行失败：未提供需要检索的关键词。"

        logger.info(f"[Omni-Tool] 🧠 发起全局潜意识扫描，关键词: '{query}'")

        # 1. 定义三个并发的查询子任务
        async def fetch_memory():
            if not self.memory_engine or not self.chat_id: return None
            try:
                if hasattr(self.memory_engine, "query"):
                    return await self.memory_engine.query(self.chat_id, query)
                elif hasattr(self.memory_engine, "search"):
                    return await self.memory_engine.search(self.chat_id, query)
            except Exception as e:
                logger.debug(f"[Omni-Tool] 记忆检索失败: {e}")
            return None

        async def fetch_jargon():
            if not self.db_service or not self.chat_id: return None
            try:
                if hasattr(self.db_service, "get_jargon"):
                    return self.db_service.get_jargon(self.chat_id, query)
                elif hasattr(self.db_service, "query_slang"):
                    return self.db_service.query_slang(self.chat_id, query)
            except Exception as e:
                logger.debug(f"[Omni-Tool] 黑话检索失败: {e}")
            return None

        async def fetch_profile():
            if not self.db_service: return None
            try:
                profile = None
                # 特殊逻辑：如果搜的就是当前说话的人，用精确 ID 查
                if query == self.current_sender_name or query.lower() in ["我", "自己", "刚刚说话的人"]:
                    if hasattr(self.db_service, 'get_user_profile') and self.current_sender_id:
                        profile = await self.db_service.get_user_profile(self.current_sender_id) if asyncio.iscoroutinefunction(self.db_service.get_user_profile) else self.db_service.get_user_profile(self.current_sender_id)
                else:
                    if hasattr(self.db_service, 'get_profile_by_name'):
                        profile = await self.db_service.get_profile_by_name(query) if asyncio.iscoroutinefunction(self.db_service.get_profile_by_name) else self.db_service.get_profile_by_name(query)

                if profile:
                    affection = getattr(profile, 'social_score', 0.0)
                    desc = "普通群友"
                    if affection > 80: desc = "极其亲密的挚友/灵魂伴侣"
                    elif affection > 50: desc = "关系很好的熟人/好朋友"
                    elif affection > 20: desc = "有好感的交流对象"
                    elif affection < -50: desc = "关系恶劣，抱有敌意"
                    elif affection < -20: desc = "关系疏远，有些反感"

                    analysis = getattr(profile, 'persona_analysis', '数据不足，有待观察。')
                    
                    report = (
                        f"对象: {profile.name}\n"
                        f"好感度: {affection:.1f} ({desc})\n"
                        f"心理侧写: {analysis}"
                    )
                    return report
            except Exception as e:
                logger.debug(f"[Omni-Tool] 画像检索失败: {e}")
            return None

        # 2. 并发执行所有查询 (极大提高效率)
        mem_res, jar_res, prof_res = await asyncio.gather(
            fetch_memory(), fetch_jargon(), fetch_profile()
        )

        # 3. 结果组装与适配适配 (Formatting Adaptation)
        report_sections = []

        if mem_res:
            report_sections.append(f"--- 💭 记忆回溯片段 ---\n{mem_res}")
        
        if jar_res:
            report_sections.append(f"--- 📖 字典释义匹配 ---\n词汇: {query}\n释义: {jar_res}")
            
        if prof_res:
            report_sections.append(f"--- 👤 人物档案侧写 ---\n{prof_res}\n*(注意：请内化以上好感度态度，绝对不要在台词中念出好感度数值)*")

        # 4. 终极判断
        if not report_sections:
            return f"系统提示：潜意识中没有任何关于 '{query}' 的记忆、黑话或人物档案。你可以自然地向对方发问或表示不清楚。"

        final_report = f"🔮 【全局潜意识扫描报告】\n关键词: '{query}'\n\n" + "\n\n".join(report_sections)
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