import json
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
    async def analyze_text_mood(self, text: str, current_mood: float) -> tuple[str, float]:
        """
        核心情绪分析
        Returns: (mood_tag, new_mood_value)
        """
        if not text or len(text) < 2:
            return "neutral", current_mood

        # 构造 Prompt
        mapping_desc = "\n".join([f"- {k}: {v}" for k, v in self.emotion_mapping.items()])
        prompt = f"""
你的任务是分析[待分析文本]，并评估它对“我”（AI助手）的情绪影响。

[我的当前情绪]
{current_mood:.2f} (范围 -1.0[极度沮丧] ~ 1.0[极度开心]，0.0 为平静)

[可用情绪标签]
{mapping_desc}

[待分析文本]
"{text}"

[任务]
请基于当前情绪，分析这段文本会让我产生什么感觉？
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
            result = await self.gateway.call_judge(prompt, system_prompt="你是一个专业的情绪分析师。")
            
            new_tag = result.get("mood_tag", "neutral").lower()
            new_value = float(result.get("mood_value", current_mood))
            
            # 数据清洗与边界限制
            if new_tag not in self.emotion_mapping:
                new_tag = "neutral"
            new_value = max(-1.0, min(1.0, new_value))
            
            logger.debug(f"[Mood] 💓 情绪波动: {current_mood:.2f} -> {new_value:.2f} | 标签: {new_tag}")
            return new_tag, new_value

        except Exception as e:
            logger.warning(f"[Mood] ⚠️ 分析失败，执行自然衰减: {e}")
            # 失败时的自然衰减逻辑 (接入 Config)
            decayed = current_mood * self.config.mood.unknown_decay
            return "neutral", decayed