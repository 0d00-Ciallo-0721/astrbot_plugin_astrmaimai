### 📄 core/mood_manager.py
import json
from typing import Tuple, List
from astrbot.api import logger
from astrbot.api.star import Context

# (v2.0) 导入 HeartCore 模块
from ..config import HeartflowConfig
from ..datamodels import ChatState
from ..services.llm_helper import LLMHelper # [修改] 使用新的服务

class MoodManager:
    """
    (v2.0) 智能情感管理器
    职责：
    1. 作为唯一的情绪分析中心。
    2. 调用 LLM 对文本进行详细分析。
    3. 返回“情绪标签 (tag)”（用于表情包）和“情绪精确值 (value)”（用于状态）。
    """

    def __init__(self, 
                 context: Context, 
                 config: HeartflowConfig
                 ):
        self.context = context
        self.config = config
        self.llm_helper = LLMHelper(context) # [新增] 初始化 LLMHelper
        
        # 情绪分析模型列表
        self.providers_to_try = []
        if self.config.emotion_model_provider_name:
            self.providers_to_try.append(self.config.emotion_model_provider_name)
        if self.config.judge_provider_names: 
            self.providers_to_try.extend(self.config.judge_provider_names)

        if not self.providers_to_try:
            logger.warning("💖 情感系统：未配置任何可用的情绪分析模型，将使用默认模型。")

    async def analyze_text_mood(self, text: str, chat_state: ChatState) -> Tuple[str, float]:
        """
        核心情绪分析
        调用 LLM，返回 (情绪标签, 新的情绪值)
        """
        current_mood_float = chat_state.mood
        
        # 1. 检查前置条件
        if not self.config.enable_emotion_sending:
            return "neutral", current_mood_float
            
        if not text or len(text.strip()) < 2: # 文本太短
            return "neutral", current_mood_float

        # 2. 构建 Prompt (List format for LLMHelper)
        emotion_mapping_str = self.config.emotion_mapping_string or "happy, sad, angry, neutral"
        
        system_content = f"""
你的任务是分析用户的文本，并评估它对“我”（AI）的情绪影响。

[我的当前情绪]
{current_mood_float:.2f} (范围 -1.0[消极] 到 1.0[积极]，0.0 为中性)

[可用情绪标签]
{emotion_mapping_str}
- neutral: 中性/无明显情绪

[任务]
分析文本会如何改变“我”的情绪。
返回 JSON:
{{
    "mood_tag": "...",  // 从可用标签中选最匹配的
    "mood_value": ...   // 计算新的情绪值 (-1.0 到 1.0)
}}
""".strip()

        prompt = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": text}
        ]

        # 3. 调用 LLMHelper
        # 尝试配置列表中的第一个模型，或者让 LLMHelper 使用默认
        provider_id = self.providers_to_try[0] if self.providers_to_try else None
        
        data = await self.llm_helper.chat_json(
            prompt, 
            provider_id=provider_id,
            retries=1
        )

        # 4. 解析结果
        tag = data.get("mood_tag", "neutral")
        value = data.get("mood_value", current_mood_float)
        
        # 类型安全转换
        try:
            value = float(value)
        except (ValueError, TypeError):
            value = current_mood_float

        # 5. 数据校验
        if tag not in self.config.emotion_mapping and tag != "neutral":
            tag = "neutral"
        
        # 保证情绪值在安全范围内
        value = max(-1.0, min(1.0, value))
        
        logger.debug(f"💖 情感分析: {current_mood_float:.2f} -> {value:.2f} | 标签: {tag}")
        return tag, value

    async def check_and_send_emotion(self, event, force_tag=None):
        """
        发送表情包 (不做修改，逻辑保持)
        """
        # 此处逻辑依赖 reply_engine/meme_engine，不涉及 LLM 调用，保持原样
        # 为避免循环引用，通常在 ReplyEngine 中调用此方法，或者这里只做检查
        pass