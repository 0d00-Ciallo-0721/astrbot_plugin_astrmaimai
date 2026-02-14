# heartflow/core/mood_manager.py
# (v13.0 新增 - 智能情感系统)
# (v13.0: 本模块取代并升级了 meme_engine/meme_emotion_engine.py)

import json
from typing import List, Tuple
from astrbot.api import logger
from astrbot.api.star import Context

# (v12.0) 导入 HeartCore 模块
from ..config import HeartflowConfig
from ..datamodels import ChatState
from ..utils.api_utils import elastic_json_chat # (v11.0) 使用弹性 JSON API

class MoodManager:
    """
    (新) v13.0 智能情感管理器
    职责：
    1. 作为唯一的情绪分析中心 (取代 meme_emotion_engine.py)。
    2. 调用 LLM 对文本进行详细分析。
    3. 返回“情绪标签 (tag)”（用于表情包）和“情绪精确值 (value)”（用于状态）。
    """

    def __init__(self, 
                 context: Context, 
                 config: HeartflowConfig
                 ):
        self.context = context
        self.config = config
        
        # (v13.0) 情绪分析模型列表
        self.providers_to_try = []
        if self.config.emotion_model_provider_name:
            self.providers_to_try.append(self.config.emotion_model_provider_name)
        if self.config.general_pool:
            self.providers_to_try.extend(self.config.general_pool)
        if self.config.judge_provider_names: # 回退到大脑模型
            self.providers_to_try.extend(self.config.judge_provider_names)

        if not self.providers_to_try:
            logger.warning("💖 情感系统：未配置任何可用的情绪分析模型。")

    def _build_mood_prompt(self, text: str, current_mood: float) -> str:
        """
        (新) v13.0 构建情绪分析V2.0提示词
        要求 LLM 返回标签和精确值
        """
        
        # (v13.0) 复用 config.py 中已有的表情包映射
        emotion_mapping_str = self.config.emotion_mapping_string
        if not emotion_mapping_str:
             emotion_mapping_str = "- happy: 积极、开心的场景\n- sad: 悲伤、遗憾的场景\n- angry: 生气、抱怨的场景"

        return f"""
你的任务是分析[待分析文本]，并评估它对“我”的情绪影响。

[我的当前情绪]
{current_mood:.2f} (范围从 -1.0[极度沮丧] 到 1.0[极度开心]，0.0 为中性)

[可用情绪标签]
{emotion_mapping_str}
- none: 情绪平淡、中性或无对应

[待分析文本]
{text}

[任务]
请基于[我的当前情绪]，分析[待分析文本]会如何改变“我”的情绪。
返回一个 JSON，包含两个键：
1. "mood_tag": (字符串) 从[可用情绪标签]中选择一个最匹配的标签。
2. "mood_value": (浮点数) 计算一个新的情绪值 (必须在 -1.0 到 1.0 之间)。
   - 如果文本是积极的，新值应高于当前情绪。
   - 如果文本是消极的，新值应低于当前情绪。
   - 如果文本是中性的，新值应向 0.0 靠近 (例如，从 0.8 变为 0.7，或从 -0.5 变为 -0.4)。

请严格按照以下JSON格式回复：
{{
    "mood_tag": "...",
    "mood_value": ...
}}
"""

    async def analyze_text_mood(self, text: str, chat_state: ChatState) -> Tuple[str, float]:
        """
        (新) v13.0 核心情绪分析
        调用 LLM，返回 (情绪标签, 新的情绪值)
        """
        
        current_mood_float = chat_state.mood
        
        # 1. 检查前置条件
        if not self.providers_to_try:
            return "none", current_mood_float # 返回原始值
            
        if not text or len(text.strip()) < 3: # 文本太短
            logger.debug("情感系统：文本过短，跳过分析。")
            return "none", current_mood_float # 返回原始值

        # 2. 构建 Prompt
        prompt = self._build_mood_prompt(text, current_mood_float)

        # 3. (v11.0) 调用弹性 JSON API
        data = await elastic_json_chat(
            self.context,
            self.providers_to_try,
            prompt,
            max_retries=self.config.judge_max_retries,
            system_prompt="你是一个专业的情绪分析师。"
        )

        # 4. (v13.0) 解析结果
        if (data and 
            isinstance(data.get("mood_tag"), str) and 
            isinstance(data.get("mood_value"), (float, int))):
            
            new_tag = data.get("mood_tag").strip().lower()
            new_value = float(data.get("mood_value"))
            
            # 5. (v13.0) 数据校验
            if new_tag not in self.config.emotion_mapping and new_tag != "none":
                logger.warning(f"情感系统：LLM 返回了无效的 mood_tag '{new_tag}'，已重置为 'none'")
                new_tag = "none"
            
            # 保证情绪值在安全范围内
            new_value = max(-1.0, min(1.0, new_value))
            
            logger.info(f"💖 情感系统：分析成功。情绪 {current_mood_float:.2f} -> {new_value:.2f} | 标签: {new_tag}")
            return new_tag, new_value
            
        else:
            logger.warning(f"情感系统：LLM 返回了无效的 JSON 结构: {data}")
            # (v13.0) 失败时，执行“中性衰减”作为回退
            decayed_mood = current_mood_float
            if decayed_mood > 0:
                decayed_mood = max(0.0, decayed_mood - self.config.mood_decay)
            elif decayed_mood < 0:
                decayed_mood = min(0.0, decayed_mood + self.config.mood_decay)
            
            return "none", decayed_mood