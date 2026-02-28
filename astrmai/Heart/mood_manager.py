import json
from typing import Tuple  # <--- 新增这一行
from astrbot.api import logger
from ..infra.gateway import GlobalModelGateway

class MoodManager:
    """
    情绪管理器 (System 1)
    职责: 调用 LLM 分析文本对机器人的情绪影响，输出情绪标签与数值变化。
    Reference: HeartFlow/core/mood_manager.py
    """
    def __init__(self, gateway: GlobalModelGateway, config=None):
        self.gateway = gateway
        self.config = config if config else gateway.config
        
        self.emotion_mapping = {}
        
        # [修改] 将配置中的 List 动态解析为字典，兼容中英文冒号
        if hasattr(self.config, 'mood') and hasattr(self.config.mood, 'emotion_mapping'):
            mapping_list = self.config.mood.emotion_mapping
            for item in mapping_list:
                if ":" in item:
                    k, v = item.split(":", 1)
                    self.emotion_mapping[k.strip()] = v.strip()
                elif "：" in item: # 兼容全角冒号
                    k, v = item.split("：", 1)
                    self.emotion_mapping[k.strip()] = v.strip()

        # 兜底默认值
        if not self.emotion_mapping:
            self.emotion_mapping = {
                "happy": "积极、开心、感谢",
                "sad": "悲伤、遗憾、道歉",
                "angry": "生气、抱怨、攻击",
                "neutral": "平静、客观、陈述",
                "curious": "好奇、提问、困惑",
                "surprise": "惊讶、意外"
            }
    async def analyze_mood(self, text: str, current_mood: float, user_affection: float = 0.0) -> Tuple[str, float]:
        """
        [修改] 分析情绪时，引入当前用户的好感度 (Affection) 维度，模拟更真实的复合心理状态。
        """
        if not text or len(text) < 2:
            return "neutral", current_mood

        # 构造 Prompt
        mapping_desc = "\n".join([f"- {k}: {v}" for k, v in self.emotion_mapping.items()])
        prompt = f"""
你的任务是分析[待分析文本]，并评估它对“我”（AI助手）的情绪影响。

[我的当前情绪]
{current_mood:.2f} (范围 -1.0[极度沮丧] ~ 1.0[极度开心]，0.0 为平静)

[当前用户对我的好感度]
{user_affection:.2f} (范围 -100 ~ 100。正数代表关系亲密，负数代表关系恶劣)
*提示：对于高好感度用户，你可以表现得更包容、更易开心；对于低好感度用户，你的情绪更容易受负面影响。*

[可用情绪标签]
{mapping_desc}

[待分析文本]
"{text}"

[任务]
请基于当前情绪和用户好感度，综合分析这段文本会让我产生什么感觉？
返回一个 JSON，包含：
1. "mood_tag": (字符串) 从可用标签中选一个最匹配的。
2. "mood_value": (浮点数) 计算新的情绪值 (-1.0 到 1.0)。
   - 夸奖/感谢/有趣 -> 升高
   - 辱骂/指责/悲伤 -> 降低
   - 平常对话 -> 向 0.0 缓慢回归 (衰减)

严格返回 JSON: {{"mood_tag": "string", "mood_value": float}}
"""
        try:
            # 使用 System 1 快速模型
            result = await self.gateway.call_judge(prompt, system_prompt="你是一个具备丰富情感和社交包容度的专业分析师。")
            
            # 安全解析 JSON
            import json
            import re
            json_str = result
            match = re.search(r'\{.*\}', result, re.DOTALL)
            if match:
                json_str = match.group(0)
            
            data = json.loads(json_str)
            mood_tag = data.get("mood_tag", "neutral")
            mood_value = float(data.get("mood_value", current_mood))
            
            # 限幅
            mood_value = max(-1.0, min(1.0, mood_value))
            return mood_tag, mood_value
            
        except Exception as e:
            logger.debug(f"[MoodManager] 情绪分析失败 (降级到当前状态): {e}")
            return "neutral", current_mood