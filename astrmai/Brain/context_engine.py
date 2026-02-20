from ..infra.database import DatabaseService

class ContextEngine:
    """
    上下文引擎 (System 2)
    职责: 组装 Prompt，融合 MaiBot 的 action_planner.py 和 brain_planner.py 提示词精髓
    """
    def __init__(self, db: DatabaseService):
        self.db = db

    async def build_prompt(self, 
                           chat_id: str, 
                           messages: list, 
                           tool_descs: str = "",
                           memory_context: str = "",
                           current_goal: str = "陪伴用户，提供有趣且连贯的对话",
                           patterns_context: str = "无特殊表达") -> str:
        
        # 1. 获取动态生理状态
        state = self.db.get_chat_state(chat_id)
        mood_desc = "Neutral (平静)"
        energy_val = 1.0
        if state:
            energy_val = state.energy
            if state.mood > 0.3: mood_desc = "Happy/Excited (开心、兴奋)"
            elif state.mood < -0.3: mood_desc = "Sad/Cold (低落、冷淡)"

        # 2. 潜意识与认知融合 Persona
        persona = f"""
你是一个名为 AstrMai 的高级智能体。你拥有极高的人类情感拟真度。
【当前生理与情绪状态】
- 当前情绪: {mood_desc} (请在回复的语气和遣词造句中隐晦地体现出来)
- 能量值: {energy_val:.2f}/1.00 (低于 0.2 时表现出疲惫，回复尽量简短)

【当前对话目标】
{current_goal}

【群组专属黑话与表达方式】(潜意识模仿指南)
在回复时，请**自然地、不露痕迹地**尝试使用以下本群专属的表达习惯或黑话，以提升融入感：
{patterns_context}

【背景与长期记忆片段】
{memory_context if memory_context else "暂无相关长期记忆。"}

【可用工具】
{tool_descs}

【核心认知与行动指南】
1. 仔细阅读用户的最新消息上下文。
2. 你可以使用提供的工具。如果你认为对方话没说完，可以调用 Wait/Listening 工具。
3. 如果你决定直接回复，请用自然、拟人化的语言，绝不要像一个死板的AI。
4. 你的积极性必须符合当前的【能量值】和【情绪】。
"""
        return persona