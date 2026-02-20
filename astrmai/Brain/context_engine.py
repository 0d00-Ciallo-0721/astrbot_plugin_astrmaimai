import time
from astrbot.api import logger
from astrmai.infra.database import DatabaseService, ChatState

class ContextEngine:
    """
    上下文引擎 (System 2)
    职责: 组装 Prompt (Persona + Memory + Context + Tools)
    """
    def __init__(self, db: DatabaseService):
        self.db = db

    async def build_prompt(self, 
                           chat_id: str, 
                           messages: list, 
                           tool_descs: str = "",
                           memory_context: str = "") -> str:
        
        # 1. 获取状态 (Energy/Mood)
        state = self.db.get_chat_state(chat_id)
        mood_desc = "Neutral"
        if state and state.mood > 0.3: mood_desc = "Happy/Excited"
        elif state and state.mood < -0.3: mood_desc = "Sad/Cold"

        # 2. 基础 Persona
        persona = f"""
You are an AI assistant named AstrMai.
Current Mood: {mood_desc} (Affects your tone).
Context:
{memory_context}

Available Tools:
{tool_descs}

Instructions:
1. Analyze the user's message.
2. Decide if you need to use a tool or just reply.
3. If using a tool, output a JSON block.
4. If replying, just speak naturally.
"""
        return persona