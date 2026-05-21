import ast
import json
import re
from typing import Any, Tuple

from astrbot.api import logger

from ...infrastructure.gateway import GlobalModelGateway
from ...infrastructure.runtime.lane_manager import LaneKey


MOOD_SYSTEM_PROMPT = """
You are AstrMai's mood analyzer.
Read the current user message and return only JSON:
{"mood_tag": "happy|sad|angry|neutral|curious|surprise", "mood_value": float}

Rules:
- Choose the dominant felt affect toward the bot in this turn.
- Sarcasm, passive aggression, or mock praise are negative, not happy.
- Mixed affect should not be flattened into happy if clear hurt, complaint, or tension is present.
- Use neutral only for genuinely plain or procedural text.
- Keep mood_value in [-1.0, 1.0].
- Output JSON only.
"""


class MoodManager:
    """
    Emotion analyzer for chat state updates.
    """

    VALID_TAGS = {"happy", "sad", "angry", "neutral", "curious", "surprise"}

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
                "happy": "positive, glad, relieved, affectionate",
                "sad": "hurt, low, apologetic, disappointed",
                "angry": "annoyed, hostile, blaming, rejecting",
                "neutral": "plain, procedural, emotionally weak",
                "curious": "wondering, asking, probing",
                "surprise": "unexpected, startled, sudden turn",
            }

    @staticmethod
    def _extract_lane_text_result(result: Any) -> Any:
        if result is None:
            return None
        for field in ("parsed_json", "text", "raw_completion", "completion_text", "response_text", "content"):
            value = getattr(result, field, None)
            if value:
                return value
        if isinstance(result, dict):
            for field in ("parsed_json", "text", "raw_completion", "completion_text", "response_text", "content"):
                value = result.get(field)
                if value:
                    return value
        return None

    def _parse_result_payload(self, result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return dict(result)
        raw_str = str(result or "").strip()
        if not raw_str:
            return {}
        clean_str = re.sub(r"```(?:json)?", "", raw_str, flags=re.IGNORECASE).strip()
        data: dict[str, Any] = {}
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
            except json.JSONDecodeError as exc:
                logger.debug(f"[MoodManager] standard JSON parse failed, trying AST fallback: {exc}")
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
            logger.debug(f"[MoodManager] structured parse failed, trying regex extraction: {clean_str[:80]}...")
            tag_match = re.search(
                r'(?:"|\')?mood_tag(?:"|\')?\s*[:：]\s*(?:"|\')?([a-zA-Z0-9_]+)(?:"|\')?',
                clean_str,
                re.IGNORECASE,
            )
            if tag_match:
                data["mood_tag"] = tag_match.group(1).lower()

            val_match = re.search(
                r'(?:"|\')?mood_value(?:"|\')?\s*[:：]\s*([-+]?\d*\.?\d+)',
                clean_str,
                re.IGNORECASE,
            )
            if val_match:
                try:
                    data["mood_value"] = float(val_match.group(1))
                except ValueError:
                    pass
        return data

    def _normalize_result(self, data: dict[str, Any], current_mood: float) -> tuple[str, float] | None:
        if not isinstance(data, dict):
            return None
        mood_tag = str(data.get("mood_tag", "") or "").strip().lower()
        if not mood_tag and "mood_value" not in data:
            return None
        if mood_tag not in self.VALID_TAGS:
            mood_tag = "neutral"
        try:
            mood_value = float(data.get("mood_value", current_mood))
        except (TypeError, ValueError):
            mood_value = float(current_mood)
        mood_value = max(-1.0, min(1.0, mood_value))
        return mood_tag or "neutral", mood_value

    @staticmethod
    def _fallback_analyze_local(text: str, current_mood: float) -> tuple[str, float]:
        fallback_text = str(text or "").lower()
        positive_hits = sum(
            token in fallback_text
            for token in ["哈哈", "谢谢", "喜欢", "开心", "贴贴", "好棒", "爱你", "抱抱", "辛苦了", "支持你", "太好了"]
        )
        sad_hits = sum(
            token in fallback_text
            for token in ["难过", "委屈", "失落", "对不起", "抱歉", "呜呜", "唉", "心累", "好惨", "低落"]
        )
        angry_hits = sum(
            token in fallback_text
            for token in ["闭嘴", "烦死", "滚", "气死", "讨厌", "废物", "搞砸", "有病", "受够", "离谱", "破防"]
        )
        sarcasm_hits = sum(
            token in fallback_text
            for token in ["可真行", "真棒", "谢谢你啊", "厉害了", "还真是", "真有你的", "真贴心"]
        )
        question_hits = sum(
            token in fallback_text
            for token in ["?", "？", "怎么", "什么", "为何", "为什么", "咋", "吗"]
        )
        procedural_intent_hits = sum(
            token in fallback_text
            for token in ["帮我", "查一下", "查一查", "告诉我", "记得吗", "天气", "几点", "怎么做", "安排", "提醒我"]
        )

        if sarcasm_hits and (angry_hits or sad_hits or any(token in fallback_text for token in ["搞砸", "又来", "还敢", "出事"])):
            return "angry", max(-1.0, current_mood - 0.18)
        if angry_hits >= 1 and angry_hits >= sad_hits:
            return "angry", max(-1.0, current_mood - 0.2)
        if sad_hits >= 1 and positive_hits == 0:
            return "sad", max(-1.0, current_mood - 0.12)
        if sad_hits >= 1 and positive_hits >= 1:
            return "sad", max(-1.0, current_mood - 0.06)
        if positive_hits >= 1:
            return "happy", min(1.0, current_mood + 0.1)
        if question_hits >= 1:
            if procedural_intent_hits >= 1:
                return "neutral", current_mood
            return "curious", current_mood
        return "neutral", current_mood

    async def analyze_mood(self, text: str, current_mood: float, user_affection: float = 0.0, chat_id: str = "") -> Tuple[str, float]:
        if not text or len(text) < 2:
            return "neutral", current_mood

        mapping_desc = ", ".join(f"{k}={v}" for k, v in self.emotion_mapping.items())
        prompt = (
            f"Current mood value: {current_mood:.2f}\n"
            f"Current user affection: {user_affection:.2f}\n"
            f"Available mood tags: {mapping_desc}\n"
            f"Text to analyze: {text}\n"
            "Return JSON only."
        )

        try:
            if chat_id and getattr(self.gateway, "lane_manager", None):
                llm_result = await self.gateway.chat_in_lane_result(
                    lane_key=LaneKey(subsystem="sys1", task_family="mood", scope_id=chat_id),
                    base_origin=chat_id,
                    prompt=prompt,
                    system_prompt=MOOD_SYSTEM_PROMPT,
                    models=getattr(self.config.provider, "task_models", []),
                    is_json=True,
                    use_fallback=False,
                )
                result = llm_result.parsed_json or self._extract_lane_text_result(llm_result)
            else:
                result = await self.gateway.call_mood_task(prompt, system_prompt=MOOD_SYSTEM_PROMPT)

            data = self._parse_result_payload(result)
            normalized = self._normalize_result(data, current_mood)
            if normalized is not None:
                return normalized
            logger.warning("[MoodManager] empty or invalid mood payload, falling back to local heuristic")
            return self._fallback_analyze_local(text, current_mood)

        except Exception as exc:
            logger.warning(f"[MoodManager] LLM mood analysis failed, using local fallback. reason: {exc}")
            return self._fallback_analyze_local(text, current_mood)

    async def analyze_text_mood(self, text: str, current_mood: float, user_affection: float = 0.0, chat_id: str = "") -> Tuple[str, float]:
        return await self.analyze_mood(text, current_mood, user_affection=user_affection, chat_id=chat_id)
