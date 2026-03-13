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
    设计原则: Anti-Bloat (结构化标签), Dynamic Injection (动态注入), Native Chinese (中文原声), 纯剧本模式 (Script Mode)
    (注：用户画像已从被动注入改为主动 Tool Call 召回)
    """
    def __init__(self, db: DatabaseService, persona_summarizer: PersonaSummarizer, config=None, context=None):
        self.db = db
        self.summarizer = persona_summarizer
        self.config = config if config else self.summarizer.gateway.config
        self.context = context if context else self.summarizer.gateway.context
    
    async def build_prompt(self, 
                           chat_id: str, 
                           event_messages: List[AstrMessageEvent],
                           retrieve_keys: List[str] = None,
                           slang_patterns: str = "",
                           tool_descs: str = "",
                           sys1_thought: str = "") -> str: 
        
        if retrieve_keys is None:
            retrieve_keys = []
            
        is_fast_mode = "CORE_ONLY" in retrieve_keys
            
        valid_keys = []
        if hasattr(self, "filter_retrieve_keys"):
            valid_keys = self.filter_retrieve_keys(retrieve_keys)
        else:
            valid_keys = retrieve_keys

        # 1. 获取基础状态数据 (情绪、精力)
        state = self.db.get_chat_state(chat_id)
        
        # 2. 调用 Summarizer 获取人格切片数据
        target_persona_id = getattr(self.config.persona, 'persona_id', "")
        raw_prompt = getattr(self.config.persona, 'prompt', "")

        persona_data = await self.summarizer.get_summary(
            original_prompt=raw_prompt,
            persona_id=target_persona_id,
            session_id=chat_id
        )
        
        if isinstance(persona_data, dict):
            persona_summary = persona_data.get("summary", "")
            style_guide = persona_data.get("style", "保持自然对话风格，偶尔使用网络颜文字，被夸奖时会嘴硬")
            shards = persona_data.get("shards", {})
            raw_persona = persona_data.get("raw", "")
            is_full_ready = persona_data.get("is_full_ready", True)
        else:
            persona_summary = persona_data[0] if isinstance(persona_data, tuple) else str(persona_data)
            style_guide = persona_data[1] if isinstance(persona_data, tuple) and len(persona_data) > 1 else "保持自然对话风格，偶尔使用网络颜文字，被夸奖时会嘴硬"
            shards = {}
            raw_persona = persona_summary
            is_full_ready = True
            
        if not is_full_ready:
            valid_keys = []
            if retrieve_keys and not is_fast_mode:
                persona_summary += "\n(你正在努力回忆更深层的记忆细节，但暂时只能想起个大概。)"

        # 3. 按需组装区块
        role_block = ""
        style_block = ""
        
        if "ALL" in valid_keys:
            role_block = f"{raw_persona}"
            style_guide = "" 
        elif is_fast_mode:
            role_block = f"{persona_summary}"
        else:
            role_block = f"{persona_summary}"
            if valid_keys:
                recollections = []
                for k in valid_keys:
                    if k in shards and shards[k] and shards[k] != "无":
                        recollections.append(f"- {k}: {shards[k]}")
                if recollections:
                    role_block += "\n\n临时回忆加载：\n" + "\n".join(recollections)
        
        if style_guide:
            style_block = f"对话风格：\n{style_guide}\n"

        state_block = self._build_state_block(state)
            
        slang_block = ""
        if slang_patterns and not is_fast_mode:
            slang_block = f"群组专属表达模式与黑话参考:\n{slang_patterns}\n"
            
        subconscious_block = ""
        if sys1_thought:
            subconscious_block = f"大脑潜意识：\"{sys1_thought}\"\n指南：这是你最真实的直觉。在接下来的回复中，请自然地顺应、延伸或掩饰这种情绪。绝对禁止像复读机一样直接说出你的内心戏。\n"

        # 4. [核心] 组装最终 Prompt (移除 User Block)
        prompt = f"""核心人格设定：
{role_block}

{style_block}
{state_block}

{slang_block}
当前你看到的消息：
<CURRENT_MESSAGES>

{subconscious_block}
<CHAT_HISTORY>

<RAG_MEMORY>

[Tools]
{tool_descs}

【系统规则：如何行动与说话 (必读)】
1. 沉浸式回复：你的普通文本回复将直接作为你的“台词”发送到群里。请完全沉浸在角色中说话，**绝对不要**在文本里加上 `[我] 说:` 或 `Bot:` 这样的前缀！
2. 工具调用机制：你可以像普通人思考一样调用工具。**调用工具属于你的底层“物理动作”或“隐秘神经活动”**，群友绝对看不到。
3. 主动社交感知 (防失忆协议)：你现在无法直接预知你与群友的好感度！在剧本中遇到任何人发言，只要你不确定你们的羁绊等级，【必须优先调用 `query_person_profile` 工具】查阅档案！严禁自行脑补关系！
4. 严禁把动作当台词：请直接触发工具的标准 API 调用！**千万不要**在文本回复中说出“我要调用工具”等打破第四面墙的话！

回复要求：
1. 模拟该人设的意识，完全沉浸在剧本中，像真人一样在群聊里接话。
2. 你的回复长度和积极性应受当前心情/精力的动态影响。
3. 必须使用中文回复。
"""
        return prompt.strip()

    def _build_state_block(self, state: Optional[ChatState]) -> str:
        if not state:
            return "[当前心情: 平静 (情绪 0.00) | 精力: 1.00]"
        
        mood_val = state.mood
        mood_tag = "平静"
        if mood_val > 0.3: mood_tag = "开心/兴奋"
        elif mood_val > 0.8: mood_tag = "狂喜"
        elif mood_val < -0.3: mood_tag = "低落/冷淡"
        elif mood_val < -0.8: mood_tag = "愤怒/极度悲伤"
        
        return f"[当前心情: {mood_tag} (情绪 {mood_val:.2f}) | 精力: {state.energy:.2f}]"


    def _build_slang_block(self, patterns: str) -> str:
        if not patterns:
            return ""
        return f"\n[Speaking Patterns]\n{patterns}"
    
    class FuzzyKeyMatcher:
        ALLOWED_KEYS = {"logic_style", "speech_style", "world_view", "timeline", "relations", "skills", "values", "secrets", "ALL"}
        CN_TO_EN_MAP = {
            "性格逻辑": "logic_style",
            "语言风格": "speech_style",
            "世界观": "world_view",
            "生平经历": "timeline",
            "人际关系": "relations",
            "技能能力": "skills",
            "价值观": "values",
            "深层秘密": "secrets",
            "完整降临": "ALL",
            "全部": "ALL",
            "所有": "ALL"
        }

        @classmethod
        def match(cls, raw_keys: List[str]) -> List[str]:
            import difflib
            valid_keys = set()
            if not isinstance(raw_keys, list):
                return []
                
            for key in raw_keys:
                if not isinstance(key, str): continue
                key_strip = key.strip()
                if not key_strip: continue
                
                if key_strip in cls.ALLOWED_KEYS:
                    valid_keys.add(key_strip)
                    continue
                    
                if key_strip in cls.CN_TO_EN_MAP:
                    valid_keys.add(cls.CN_TO_EN_MAP[key_strip])
                    continue
                    
                en_matches = difflib.get_close_matches(key_strip, cls.ALLOWED_KEYS, n=1, cutoff=0.6)
                if en_matches:
                    valid_keys.add(en_matches[0])
                    continue
                    
                cn_matches = difflib.get_close_matches(key_strip, cls.CN_TO_EN_MAP.keys(), n=1, cutoff=0.6)
                if cn_matches:
                    valid_keys.add(cls.CN_TO_EN_MAP[cn_matches[0]])
                    
            return list(valid_keys)

    def filter_retrieve_keys(self, raw_keys: List[str]) -> List[str]:
        if not raw_keys:
            return []
            
        valid_keys = self.FuzzyKeyMatcher.match(raw_keys)
        
        if len(valid_keys) != len(raw_keys) or set(valid_keys) != set(raw_keys):
            logger.warning(f"[ContextEngine] 🔍 模糊过滤容错器触发: 原始 Keys {raw_keys} -> 有效 Keys {valid_keys}")
            
        return valid_keys