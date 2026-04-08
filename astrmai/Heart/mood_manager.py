import ast
import json
import re
from typing import Tuple

from astrbot.api import logger

from ..infra.gateway import GlobalModelGateway
from ..infra.lane_manager import LaneKey


MOOD_SYSTEM_PROMPT = """
你是 AstrMai 的情绪分析器。
请只根据当前文本、当前情绪值和好感度，返回一个 JSON：
{"mood_tag": "happy|sad|angry|neutral|curious|surprise", "mood_value": float}
不要输出任何额外解释。
"""


class MoodManager:
    """
    情绪管理器 (System 1)
    职责: 调用 LLM 分析文本对机器人的情绪影响，输出情绪标签与数值变化。
    """

    def __init__(self, gateway: GlobalModelGateway, config=None):
        self.gateway = gateway
        self.config = config if config else gateway.config
        self.emotion_mapping = {}

        if hasattr(self.config, "reply") and hasattr(self.config.reply, "emotion_mapping"):
            mapping_list = self.config.reply.emotion_mapping
            for item in mapping_list:
                if ":" in item:
                    k, v = item.split(":", 1)
                    self.emotion_mapping[k.strip()] = v.strip()
                elif "：" in item:
                    k, v = item.split("：", 1)
                    self.emotion_mapping[k.strip()] = v.strip()

        if not self.emotion_mapping:
            self.emotion_mapping = {
                "happy": "积极、开心、感谢",
                "sad": "悲伤、遗憾、道歉",
                "angry": "生气、抱怨、攻击",
                "neutral": "平静、客观、陈述",
                "curious": "好奇、提问、困惑",
                "surprise": "惊讶、意外",
            }

    async def analyze_mood(self, text: str, current_mood: float, user_affection: float = 0.0, chat_id: str = "") -> Tuple[str, float]:
        if not text or len(text) < 2:
            return "neutral", current_mood

        mapping_desc = ", ".join(f"{k}={v}" for k, v in self.emotion_mapping.items())
        prompt = (
            f"当前情绪值: {current_mood:.2f}\n"
            f"当前用户好感度: {user_affection:.2f}\n"
            f"可用情绪标签: {mapping_desc}\n"
            f"待分析文本: {text}\n"
            "请只返回 JSON。"
        )

        try:
            if chat_id and getattr(self.gateway, "lane_manager", None):
                result = await self.gateway.chat_in_lane(
                    lane_key=LaneKey(subsystem="sys1", task_family="mood", scope_id=chat_id),
                    base_origin=chat_id,
                    prompt=prompt,
                    system_prompt=MOOD_SYSTEM_PROMPT,
                    models=getattr(self.config.provider, "task_models", []),
                    is_json=True,
                    use_fallback=False,
                )
            else:
                result = await self.gateway.call_mood_task(prompt, system_prompt=MOOD_SYSTEM_PROMPT)

            if isinstance(result, dict):
                data = result
            else:
                raw_str = str(result).strip()
                clean_str = re.sub(r"```(?:json)?", "", raw_str, flags=re.IGNORECASE).strip()
                data = {}
                parsed_successfully = False

                match = re.search(r"(\{.*\}|\[.*\])", clean_str, re.DOTALL)
                if match:
                    json_str = match.group(1)
                    try:
                        parsed_data = json.loads(json_str)
                        if isinstance(parsed_data, list) and parsed_data and isinstance(parsed_data[0], dict):
                            data = parsed_data[0]
                        elif isinstance(parsed_data, dict):
                            data = parsed_data
                        if data:
                            parsed_successfully = True
                    except json.JSONDecodeError as e:
                        logger.debug(f"[MoodManager] 标准 JSON 解析失败，尝试 AST 容错解析: {e}")
                        try:
                            eval_data = ast.literal_eval(json_str)
                            if isinstance(eval_data, list) and eval_data and isinstance(eval_data[0], dict):
                                data = eval_data[0]
                            elif isinstance(eval_data, dict):
                                data = eval_data
                            if data:
                                parsed_successfully = True
                        except Exception:
                            pass

                if not parsed_successfully or ("mood_tag" not in data and "mood_value" not in data):
                    logger.debug(f"[MoodManager] 结构化解析失败，尝试正则提取: {clean_str[:50]}...")
                    tag_match = re.search(
                        r'(?:"|\')?mood_tag(?:"|\')?\s*[:：=]\s*(?:"|\')?([a-zA-Z0-9_]+)(?:"|\')?',
                        clean_str,
                        re.IGNORECASE,
                    )
                    if tag_match:
                        data["mood_tag"] = tag_match.group(1).lower()

                    val_match = re.search(
                        r'(?:"|\')?mood_value(?:"|\')?\s*[:：=]\s*([-+]?\d*\.?\d+)',
                        clean_str,
                        re.IGNORECASE,
                    )
                    if val_match:
                        try:
                            data["mood_value"] = float(val_match.group(1))
                        except ValueError:
                            pass

            mood_tag = data.get("mood_tag", "neutral")
            mood_value = float(data.get("mood_value", current_mood))
            mood_value = max(-1.0, min(1.0, mood_value))
            return mood_tag, mood_value

        except Exception as e:
            logger.warning(f"[MoodManager] LLM 情绪分析失败，触发本地降级算法。原因: {e}")
            fallback_text = text.lower()
            if any(w in fallback_text for w in ["哈哈", "嘿嘿", "贴贴", "喜欢", "好棒", "谢谢", "开心", "做饭", "喵", "来啦"]):
                return "happy", min(1.0, current_mood + 0.1)
            if any(w in fallback_text for w in ["滚", "气死", "烦", "闭嘴", "傻", "笨", "打你", "饿"]):
                return "angry", max(-1.0, current_mood - 0.2)
            if any(w in fallback_text for w in ["呜呜", "难过", "惨", "抱歉", "对不起", "唉", "叹气"]):
                return "sad", max(-1.0, current_mood - 0.1)
            if any(w in fallback_text for w in ["?", "？", "啊", "怎么会", "啥", "什么"]):
                return "surprise", current_mood
            return "neutral", current_mood

    async def analyze_text_mood(self, text: str, current_mood: float, user_affection: float = 0.0, chat_id: str = "") -> Tuple[str, float]:
        return await self.analyze_mood(text, current_mood, user_affection=user_affection, chat_id=chat_id)
