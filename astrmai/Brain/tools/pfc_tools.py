from typing import Any, Optional
from pydantic import Field
from pydantic.dataclasses import dataclass 

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool,ToolExecResult 
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
# 工具 2：长期记忆检索工具
# ==========================================
@dataclass
class FetchKnowledgeTool(FunctionTool[AstrAgentContext]):
    """长期记忆与知识检索工具"""
    name: str = "fetch_knowledge"
    description: str = (
        "需要调取长期记忆、知识库，或者当用户提到以前发生过的事情（如'之前那个...'、'你还记得...'）时调用此工具。"
        "传入关键词，系统会返回你脑海中相关的历史记忆片段。"
    )
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "需要检索的记忆关键词、事件短语或相关人名。"
            }
        },
        "required": ["query"]
    })

    # 依赖注入：使用 exclude=True 避免被序列化到 LLM
    memory_engine: Optional[Any] = Field(default=None, exclude=True)
    chat_id: str = Field(default="", exclude=True)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        query = kwargs.get("query", "").strip()
        if not query:
            return "执行失败：未提供需要检索的关键词。"
            
        if not self.memory_engine or not self.chat_id:
            return "系统提示：记忆引擎未挂载或上下文环境丢失，无法检索长期记忆。"

        try:
            # 适配你的 memory_engine 接口逻辑 (这里假设为 query 或 search 方法)
            result = None
            if hasattr(self.memory_engine, "query"):
                result = await self.memory_engine.query(self.chat_id, query)
            elif hasattr(self.memory_engine, "search"):
                result = await self.memory_engine.search(self.chat_id, query)
            else:
                return "系统提示：底层记忆引擎检索接口不可用。"

            if not result:
                return f"系统提示：脑海中没有找到关于 '{query}' 的相关记忆片段。你可以自然地告诉对方你不记得了。"
                
            return f"💭 【潜意识记忆回溯】\n检索关键词: {query}\n回想起了以下内容：\n{result}"
            
        except Exception as e:
            logger.error(f"[Tool] 检索记忆失败: {e}")
            return f"记忆检索过程发生模糊与底层异常: {e}"


# ==========================================
# 工具 3：黑话与专属梗查询工具
# ==========================================
@dataclass
class QueryJargonTool(FunctionTool[AstrAgentContext]):
    """黑话与专属梗查询工具"""
    name: str = "query_jargon"
    description: str = (
        "当群友使用了你不懂的缩写、黑话、奇怪的词汇、或者群内专有的梗时，调用此工具查阅你的潜意识字典。"
    )
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "word": {
                "type": "string",
                "description": "需要查询的未知词汇、黑话或缩写。"
            }
        },
        "required": ["word"]
    })

    # 依赖注入
    db_service: Optional[Any] = Field(default=None, exclude=True)
    chat_id: str = Field(default="", exclude=True)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        word = kwargs.get("word", "").strip()
        if not word:
            return "执行失败：未提供查询词汇。"
            
        if not self.db_service or not self.chat_id:
            return "系统提示：数据库服务未挂载，无法查阅潜意识字典。"

        try:
            # 适配你的 db_service 黑话查询接口
            definition = None
            if hasattr(self.db_service, "get_jargon"):
                definition = self.db_service.get_jargon(self.chat_id, word)
            elif hasattr(self.db_service, "query_slang"):
                definition = self.db_service.query_slang(self.chat_id, word)

            if not definition:
                return f"系统提示：潜意识字典中没有收录关于 '{word}' 的解释，这可能是一个新造词或者你不了解的梗。可以自然地向群友发问或掩饰过去。"
                
            return f"📖 【潜意识字典查询】\n词汇: {word}\n释义与历史用法: {definition}"
            
        except Exception as e:
            logger.error(f"[Tool] 查阅字典失败: {e}")
            return f"字典检索中断: {e}"


# ==========================================
# 工具 4：用户画像与羁绊查询工具
# ==========================================
@dataclass
class QueryPersonProfileTool(FunctionTool[AstrAgentContext]):
    """深度用户画像与社交羁绊查询工具"""
    name: str = "query_user_profile"
    description: str = (
        "当需要了解正在对话的用户的深层信息（如你们的羁绊程度、好感度、对方的心理侧写与行为习惯等）时调用。"
        "当你在群聊中遇到不确定的对象，或者涉及情感交流需要确认对方身份以调整语气时，务必使用此工具。"
    )
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "target_name": {
                "type": "string", 
                "description": "需要查询的目标用户名称（即剧本中 [xxx] 里的名字）。如果查询正在对话的人，直接传入其名字。"
            }
        },
        "required": ["target_name"]
    })

    db_service: Optional[Any] = Field(default=None, exclude=True)
    current_sender_id: str = Field(default="", exclude=True)
    current_sender_name: str = Field(default="", exclude=True)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        target_name = kwargs.get("target_name", "").strip()
        
        if not self.db_service:
            return "系统提示：数据库服务未挂载，无法感知用户画像。"

        try:
            profile = None
            
            if target_name == self.current_sender_name or not target_name:
                if hasattr(self.db_service, 'get_user_profile') and self.current_sender_id:
                    profile = self.db_service.get_user_profile(self.current_sender_id)
            else:
                if hasattr(self.db_service, 'get_profile_by_name'):
                    profile = self.db_service.get_profile_by_name(target_name)
                else:
                    return f"系统提示：当前环境无法直接通过名字 '{target_name}' 查询不在场的群友档案，请凭基础印象互动。"

            if not profile:
                return f"系统提示：潜意识中没有关于 '{target_name}' 的深度记录。对方可能是新互动的群友。社交指南: 保持礼貌与观察。"

            affection = getattr(profile, 'social_score', 0.0)
            relation_desc = "普通群友"
            if affection > 80:
                relation_desc = "极其亲密的挚友/灵魂伴侣"
            elif affection > 50:
                relation_desc = "关系很好的熟人/好朋友"
            elif affection > 20:
                relation_desc = "有好感的交流对象"
            elif affection < -50:
                relation_desc = "关系恶劣，抱有敌意"
            elif affection < -20:
                relation_desc = "关系疏远，有些反感"

            report = (
                f"📝 【潜意识系统回调：用户画像报告】\n"
                f"目标对象: {profile.name} (底层识别码匹配成功)\n"
                f"当前好感度评分: {affection:.1f}\n"
                f"羁绊等级: {relation_desc}\n"
            )
            
            if hasattr(profile, 'persona_analysis') and profile.persona_analysis:
                report += f"心理侧写与行为习惯: {profile.persona_analysis}\n"
            else:
                report += "心理侧写与行为习惯: 数据不足，有待进一步观察。\n"
                
            if affection > 50:
                report += "\n>>> 社交行动指南: 对方好感度很高，请使用更亲昵、自然、无防备的语气，可以适度撒娇或开玩笑。"
            elif affection < -20:
                report += "\n>>> 社交行动指南: 对方好感度较低，请保持距离感，使用客气、简短甚至带点冷淡的语气。"
            else:
                report += "\n>>> 社交行动指南: 保持正常的人际边界，顺其自然地交流。"

            report += "\n⚠️ 系统约束：请将上述指南内化为你的情绪与态度，【绝对禁止】在你的台词中复述好感度数值或指南内容！"
            
            return report

        except Exception as e:
            logger.error(f"[Tool] 查询用户画像失败: {e}")
            return f"潜意识阻断：查询过程中发生异常: {e}"
        

# ==========================================
# 工具 5：主动 @ (At) 构造工具
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
# 工具 6：主动戳一戳 (Poke) 执行器
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
# 工具 7：主动表情包工具 (Meme/Mood Override)
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