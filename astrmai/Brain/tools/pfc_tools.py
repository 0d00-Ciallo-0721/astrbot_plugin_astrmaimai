from typing import Any, Optional
from pydantic import Field
from pydantic.dataclasses import dataclass 

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
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