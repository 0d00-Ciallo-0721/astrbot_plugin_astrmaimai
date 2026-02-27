import json
import re
from typing import Any, Dict, List
from astrbot.api import logger

class MemoryProcessor:
    """
    认知结构化大脑 (System 2 - Cognitive Processor)
    负责将非结构化的对话历史，通过 LLM 降维压缩为高密度的多维记忆数据模型。
    """
    def __init__(self, gateway):
        self.gateway = gateway
        # 极简且高效的内部提示词模板，确保稳定输出 JSON
        self.prompt_template = """你是一个专业的对话分析智能体和记忆提炼大脑。
请阅读以下对话历史，提取其中具有长期记忆价值的信息（如用户的身份、偏好习惯、重要事件、约定、情感倾向等）。

⚠️ 提取规则：
1. 忽略无意义的过渡性闲聊（如“你好”、“在吗”、“哈哈”）。
2. 如果对话完全没有记忆价值，请降低 importance 评分（如 0.1）。
3. 请将事实转化为客观的第三人称陈述句。
4. 你必须严格返回以下格式的 JSON 对象，不要包含任何 markdown 代码块或解释性文字。

JSON 格式要求：
{
    "summary": "这段对话的核心总结（50字以内）",
    "topics": ["话题1", "话题2"],
    "key_facts": ["事实1（例：用户表示自己是程序员）", "事实2"],
    "sentiment": "positive", // (positive / neutral / negative)
    "importance": 0.8 // (0.0到1.0的浮点数，越重要分数越高)
}

以下是对话内容：
{history}
"""

    async def process_conversation(self, chat_history_text: str) -> Dict[str, Any]:
        """将对话文本处理为结构化记忆"""
        if not chat_history_text or not chat_history_text.strip():
            return self._get_default_structured_data()
            
        prompt = self.prompt_template.format(history=chat_history_text)
        
        try:
            # 这里的 gateway.call_judge 是 astrmai 的 LLM 调用封装
            result = await self.gateway.call_judge(prompt)
            
            # 解析兼容：如果网关直接返回了 dict 则直接使用，否则解析 JSON 字符串
            if isinstance(result, dict) and "summary" in result:
                return self._validate_and_fill(result)
            
            result_str = str(result)
            parsed_json = self._parse_json(result_str)
            return self._validate_and_fill(parsed_json)
            
        except Exception as e:
            logger.error(f"[MemoryProcessor] 结构化处理失败: {e}", exc_info=True)
            return self._get_default_structured_data()

    def _parse_json(self, text: str) -> Dict[str, Any]:
        """健壮的 JSON 解析器（带 Markdown 修复和正则兜底）"""
        # 尝试剥离 Markdown 代码块
        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL | re.IGNORECASE)
        if match:
            text = match.group(1)
        else:
            # 正则兜底提取大括号内容
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                text = match.group(0)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"[MemoryProcessor] JSON 解析失败，原始文本: {text[:100]}...")
            return {}

    def _validate_and_fill(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """校验并补全默认字段，确保契约结构完整"""
        def ensure_list(val: Any) -> List[str]:
            if isinstance(val, list):
                return [str(i) for i in val]
            return [str(val)] if val else []

        sentiment = str(data.get("sentiment", "neutral")).lower()
        if sentiment not in ["positive", "neutral", "negative"]:
            sentiment = "neutral"

        try:
            importance = float(data.get("importance", 0.5))
            importance = max(0.0, min(1.0, importance))
        except (ValueError, TypeError):
            importance = 0.5

        return {
            "summary": str(data.get("summary", "对话记录")),
            "topics": ensure_list(data.get("topics")),
            "key_facts": ensure_list(data.get("key_facts")),
            "sentiment": sentiment,
            "importance": importance
        }

    def _get_default_structured_data(self) -> Dict[str, Any]:
        return {
            "summary": "对话记录",
            "topics": [],
            "key_facts": [],
            "sentiment": "neutral",
            "importance": 0.5
        }