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
                           memory_context: str = "",
                           slang_patterns: str = "",
                           tool_descs: str = "") -> str: 
        # 1. 获取基础状态数据
        # 注意: 如果 db_service 是同步的，这里直接调用；如果是异步的需 await
        # 假设 DatabaseService 提供了兼容的同步/异步接口，这里按 Phase 4 标准使用
        state = self.db.get_chat_state(chat_id)
        
        # 获取当前发言者的 Profile (取最后一条消息的发送者)
        sender_id = event_messages[-1].get_sender_id()
        sender_name = event_messages[-1].get_sender_name()
        user_profile = self.db.get_user_profile(sender_id)
        
        # 2. 获取并压缩人设 (Anti-Bloat & Anti-OOC) [接入 Config 与动态人设]
        raw_persona = "你是一个名为 AstrMai 的高级智能体，拥有丰富的情感和独特的个性..." 
        
        if self.config and self.context:
            persona_id = self.config.persona.persona_id
            if persona_id:
                try:
                    # 读取配置中的固定人设
                    persona_data = self.context.persona_manager.get_persona(persona_id)
                    if persona_data:
                        raw_persona = persona_data.system_prompt
                except Exception as e:
                    logger.warning(f"[ContextEngine] 无法加载配置的人设 {persona_id}: {e}")
            else:
                try:
                    # 没有绑定时，使用当前会话的使用的人格即默认人设
                    default_persona = self.context.persona_manager.get_default_persona_v3(chat_id)
                    if default_persona:
                        raw_persona = default_persona.get("prompt", raw_persona)
                except Exception as e:
                    logger.warning(f"[ContextEngine] 无法加载默认人设: {e}")

        # 调用 Summarizer 获取压缩后的人设和风格指南
        persona_summary, style_guide = await self.summarizer.get_summary(raw_persona)

        # 3. 动态构建各结构化板块
        state_block = self._build_state_block(state)
        user_block = self._build_user_block(user_profile, sender_name)
        memory_block = self._build_memory_block(memory_context)
        slang_block = self._build_slang_block(slang_patterns)
        
        # 4. 组装最终 System Prompt (结构化/高密度/中文指令)
        prompt = f"""
[Role]
{persona_summary}

[Style Guide]
{style_guide}

{state_block}
{user_block}
{memory_block}
{slang_block}

[Tools]
{tool_descs}

[Instruction]
1. 模拟该人设的意识，完全沉浸在角色中。
2. 如果[Role]中缺少信息，请依赖[Memory Retrieval]或使用'fetch_knowledge'工具检索。
3. 回复必须严格遵循[Style Guide]中的语气和格式要求。
4. 必须使用中文回复，除非用户主动使用其他语言。
5. 你的回复长度和积极性应受当前[State] (Mood/Energy) 的动态影响。
"""
        return prompt.strip()
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

    def _build_user_block(self, profile: Optional[UserProfile], sender_name: str) -> str:
        """构建用户画像标签"""
        if not profile:
            return f"[User: {sender_name} | Relation: 陌生人]"
        
        identity = profile.identity if profile.identity else "群友"
        return f"[User: {profile.name} | Score: {profile.social_score:.1f} | Identity: {identity}]"

    def _build_memory_block(self, memory_context: str) -> str:
        """动态构建记忆板块"""
        if not memory_context:
            return ""
        return f"\n[Memory Retrieval]\n{memory_context}"

    def _build_slang_block(self, patterns: str) -> str:
        """动态构建潜意识/黑话板块"""
        if not patterns:
            return ""
        return f"\n[Speaking Patterns]\n{patterns}"