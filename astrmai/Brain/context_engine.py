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
                           retrieve_keys: List[str] = None,
                           slang_patterns: str = "",
                           tool_descs: str = "",
                           sys1_thought: str = "") -> str: 
        """[修改] 动态编织 Prompt，集成按需组装人格、潜意识直觉驱动、状态注入、记忆与社交上下文"""
        if retrieve_keys is None:
            retrieve_keys = []
            
        # 经过第二阶段新增的模糊匹配容错器
        valid_keys = []
        if hasattr(self, "filter_retrieve_keys"):
            valid_keys = self.filter_retrieve_keys(retrieve_keys)
        else:
            valid_keys = retrieve_keys

        # 1. 获取基础状态数据
        state = self.db.get_chat_state(chat_id)
        
        # 获取当前发言者的 Profile
        user_profile = None
        if event_messages:
            last_msg = event_messages[-1]
            sender_id = last_msg.get_sender_id()
            if hasattr(self.db, 'get_user_profile'):
                user_profile = self.db.get_user_profile(sender_id)
        
        # 2. 调用 Summarizer 获取人格切片数据
        # [修改] 获取配置中的 ID 并传递给 Summarizer，实现 ID 绑定逻辑
        target_persona_id = getattr(self.config.persona, 'persona_id', "")
        # 获取原始 Prompt (System 1/2 Prompt 配置通常在 config.persona.prompt 或 context 中)
        raw_prompt = getattr(self.config.persona, 'prompt', "")

        # 此时已经传入了配置的 Prompt，并且无需考虑以前的方法签名
        persona_data = await self.summarizer.get_summary(
            original_prompt=raw_prompt,
            persona_id=target_persona_id,
            session_id=chat_id
        )
        
        # 安全解析字典结构 (兼容第一阶段修改后的格式)
        if isinstance(persona_data, dict):
            persona_summary = persona_data.get("summary", "")
            style_guide = persona_data.get("style", "保持自然对话风格")
            shards = persona_data.get("shards", {})
            raw_persona = persona_data.get("raw", "")
            is_full_ready = persona_data.get("is_full_ready", True)
        else:
            # 兼容极早期版本的 Tuple 返回值
            persona_summary = persona_data[0] if isinstance(persona_data, tuple) else str(persona_data)
            style_guide = persona_data[1] if isinstance(persona_data, tuple) and len(persona_data) > 1 else ""
            shards = {}
            raw_persona = persona_summary
            is_full_ready = True
            
        # 降级防御：如果后台切片还没生成完毕，强制清空 Keys 走基础摘要
        if not is_full_ready:
            valid_keys = []
            if retrieve_keys:
                persona_summary += "\n(你正在努力回忆更深层的记忆细节，但暂时只能想起个大概。)"

        # 3. 按需组装 Role 区块
        role_block = ""
        if "ALL" in valid_keys:
            # Path C: 完整降临模式 (使用 5000 字巨量人设)
            role_block = f"[Role]\n{raw_persona}"
        else:
            # Path A (轻量) / Path B (精准增强)
            role_block = f"[Role]\n{persona_summary}"
            if valid_keys:
                recollections = []
                for k in valid_keys:
                    if k in shards and shards[k] and shards[k] != "无":
                        recollections.append(f"- {k}: {shards[k]}")
                if recollections:
                    role_block += "\n\n## 临时回忆加载\n" + "\n".join(recollections)

        # 4. 构建各个信息块
        state_block = self._build_state_block(state)
        user_block = self._build_user_block(user_profile)
        
        slang_block = ""
        if slang_patterns:
            slang_block = f"[Group Patterns]\n群组专属表达模式与黑话参考:\n{slang_patterns}"
            
        # [修改] 替换原来的 goal_block 为 subconscious_block
        subconscious_block = ""
        if sys1_thought:
            subconscious_block = f"[Subconscious / 潜意识]\n在看到用户的消息时，你脑海中闪过的第一反应（内心戏）是：\n\"{sys1_thought}\"\n\n💡 指南：\n这是你最真实的直觉。在接下来的回复中，请自然地顺应、延伸或掩饰这种情绪。绝对禁止像复读机一样直接说出你的内心戏。"

        # 5. 组装最终 Prompt
        prompt = f"""
{role_block}

[Style Guide]
{style_guide}

{state_block}
{user_block}
{slang_block}
{subconscious_block}

[Tools]
{tool_descs}

[Instruction]
1. 模拟该人设的意识，完全沉浸在角色中，结合[User Info]中的好感度和关系动态调整对用户的态度。
2. 如果遇到不懂的词汇，可以调用 'query_jargon' 工具查询；缺少背景信息请调用 'fetch_knowledge' 工具检索。
3. 回复必须严格遵循[Style Guide]中的语气和格式要求。
4. 必须使用中文回复，除非用户主动使用其他语言。
5. 你的回复长度和积极性应受当前[State] (Mood/Energy) 的动态影响，并自然地流露潜意识的情绪，但绝对不要复述原话。
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
    


# [新增] 具体位置：类 ContextEngine 内部的顶部（作为内部类）
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
                
                # 1. 严格直接匹配
                if key_strip in cls.ALLOWED_KEYS:
                    valid_keys.add(key_strip)
                    continue
                    
                # 2. 中文映射直接匹配
                if key_strip in cls.CN_TO_EN_MAP:
                    valid_keys.add(cls.CN_TO_EN_MAP[key_strip])
                    continue
                    
                # 3. 英文模糊匹配 (对抗单词拼写错误)
                en_matches = difflib.get_close_matches(key_strip, cls.ALLOWED_KEYS, n=1, cutoff=0.6)
                if en_matches:
                    valid_keys.add(en_matches[0])
                    continue
                    
                # 4. 中文模糊匹配 (对抗语义词汇偏差)
                cn_matches = difflib.get_close_matches(key_strip, cls.CN_TO_EN_MAP.keys(), n=1, cutoff=0.6)
                if cn_matches:
                    valid_keys.add(cls.CN_TO_EN_MAP[cn_matches[0]])
                    
            return list(valid_keys)

    # [新增] 具体位置：类 ContextEngine 中，作为类方法
    def filter_retrieve_keys(self, raw_keys: List[str]) -> List[str]:
        """
        [新增] 拦截 Judge 的输出，如果发现异常 Key，过一遍映射表和模糊匹配。
        如果有效 Key 为空，则降级为 []，在组装时将仅使用 Summary。
        """
        if not raw_keys:
            return []
            
        valid_keys = self.FuzzyKeyMatcher.match(raw_keys)
        
        if len(valid_keys) != len(raw_keys) or set(valid_keys) != set(raw_keys):
            logger.warning(f"[ContextEngine] 🔍 模糊过滤容错器触发: 原始 Keys {raw_keys} -> 有效 Keys {valid_keys}")
            
        return valid_keys    
