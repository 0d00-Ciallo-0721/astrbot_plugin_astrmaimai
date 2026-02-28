import time
from typing import List, Dict, Any, Optional
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from ..infra.database import DatabaseService
from ..infra.datamodels import ChatState, UserProfile
from .persona_summarizer import PersonaSummarizer

class ContextEngine:
    """
    上下文引擎 (System 2: Cognition Core)
    职责: 动态编织 Prompt，集成人设压缩、状态注入、记忆回溯与黑话植入。
    设计原则: Anti-Bloat (结构化标签), Dynamic Injection (动态注入), Native Chinese (中文原声)
    """
    def __init__(self, db: DatabaseService, persona_summarizer: PersonaSummarizer, config=None, context=None):
        self.db = db
        self.summarizer = persona_summarizer
        # 通过依赖链反向获取 config 和 context，避免修改 main.py 的实例化签名
        self.config = config if config else self.summarizer.gateway.config
        self.context = context if context else self.summarizer.gateway.context

    async def build_prompt(self, 
                           chat_id: str, 
                           event_messages: List[AstrMessageEvent],
                           slang_patterns: str = "",
                           tool_descs: str = "",
                           current_goal: str = "") -> str: 
        """[修改] 动态编织 Prompt，集成目标驱动、状态注入、记忆与社交上下文"""
        # 1. 获取基础状态数据
        state = self.db.get_chat_state(chat_id)
        
        # 获取当前发言者的 Profile (取最后一条消息的发送者)
        user_profile = None
        if event_messages:
            last_msg = event_messages[-1]
            # 兼容旧代码，如果没有从 state_engine 直接获取的方法，尝试从 db 或运行时缓存获取
            # 这里假定已经可以通过 user_id 获取
            sender_id = last_msg.get_sender_id()
            if hasattr(self.db, 'get_user_profile'):
                user_profile = self.db.get_user_profile(sender_id)
        
        # 2. 调用 Summarizer 获取压缩人设 (传入好感度以动态调整风格)
        affection_score = getattr(user_profile, 'social_score', 0.0) if user_profile else 0.0
        persona_summary, style_guide = await self.summarizer.get_summary(
            self.config.persona.prompt, 
            user_affection=affection_score
        )
        
        # 3. 构建各个信息块
        state_block = self._build_state_block(state)
        user_block = self._build_user_block(user_profile)
        
        slang_block = ""
        if slang_patterns:
            slang_block = f"[Group Patterns]\n群组专属表达模式与黑话参考:\n{slang_patterns}"
            
        goal_block = ""
        if current_goal:
            goal_block = f"[Current Goal]\n当前对话阶段的隐式目标是：「{current_goal}」。\n请自然地推进对话朝着这个方向发展，避免机械地提及目标。"

        # 4. 组装最终 Prompt
        prompt = f"""
[Role]
{persona_summary}

[Style Guide]
{style_guide}

{state_block}
{user_block}
{slang_block}
{goal_block}

[Tools]
{tool_descs}

[Instruction]
1. 模拟该人设的意识，完全沉浸在角色中，结合[User Info]中的好感度和关系动态调整对用户的态度。
2. 如果遇到不懂的词汇，可以调用 'query_jargon' 工具查询；缺少背景信息请调用 'fetch_knowledge' 工具检索。
3. 回复必须严格遵循[Style Guide]中的语气和格式要求。
4. 必须使用中文回复，除非用户主动使用其他语言。
5. 你的回复长度和积极性应受当前[State] (Mood/Energy) 的动态影响，并服务于[Current Goal]。
"""
        return prompt.strip()

    def _build_state_block(self, state: Optional[ChatState]) -> str:
        """构建生理状态标签"""
        if not state:
            return "[State: 平静 (Mood: 0.0) | Energy: 1.0]"
        
        # 情绪数值化映射为中文标签
        mood_val = state.mood
        mood_tag = "平静"
        if mood_val > 0.3: mood_tag = "开心/兴奋"
        elif mood_val > 0.8: mood_tag = "狂喜"
        elif mood_val < -0.3: mood_tag = "低落/冷淡"
        elif mood_val < -0.8: mood_tag = "愤怒/极度悲伤"
        
        return f"[State: {mood_tag} (Mood: {mood_val:.2f}) | Energy: {state.energy:.2f}]"

    def _build_user_block(self, profile: Optional[UserProfile]) -> str:
        """[修改] 构建包含好感度、互动倾向和心理画像的动态社交上下文"""
        if not profile:
            return "[User Info: 未知的新用户，保持礼貌与观察]"
            
        # 根据好感度划分社交距离
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
            
        block = f"[User Info]\n当前互动用户: {profile.name} (好感度: {affection:.1f} - {relation_desc})\n"
        
        # 注入心理侧写
        if hasattr(profile, 'persona_analysis') and profile.persona_analysis:
            block += f"该用户的心理侧写与行为习惯: {profile.persona_analysis}\n"
            
        # 动态社交指南
        if affection > 50:
            block += "💡 社交指南: 对方好感度很高，请使用更亲昵、自然、无防备的语气，可以适度撒娇或开玩笑。\n"
        elif affection < -20:
            block += "💡 社交指南: 对方好感度较低，请保持距离感，使用客气、简短甚至带点冷淡的语气。\n"
            
        return block

    def _build_slang_block(self, patterns: str) -> str:
        """动态构建潜意识/黑话板块"""
        if not patterns:
            return ""
        return f"\n[Speaking Patterns]\n{patterns}"