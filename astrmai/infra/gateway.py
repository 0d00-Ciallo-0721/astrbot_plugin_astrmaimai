import json
from typing import Optional, List, Dict, Any
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.api.provider import ProviderRequest

class GlobalModelGateway:
    """
    统一模型网关 (Infrastructure Layer)
    负责路由 System 1 (Judge) 和 System 2 (Planner) 的请求，并处理重试与容错。
    Reference: HeartCore/utils/api_utils.py
    """
    def __init__(self, context: Context):
        self.context = context
        # 从配置获取 Provider ID
        self.sys1_id = context.get_config("system1_provider_id")
        self.sys2_id = context.get_config("system2_provider_id")

    @retry(
        stop=stop_after_attempt(3), 
        wait=wait_fixed(1),
        retry=retry_if_exception_type(json.JSONDecodeError)
    )
    async def call_judge(self, prompt: str, system_prompt: str = "") -> Dict[str, Any]:
        """
        System 1 调用: 快速、低成本、结构化输出
        用于: Judge, Analyzer
        """
        if not self.sys1_id:
            logger.warning("[AstrMai] System 1 Provider ID not configured.")
            return {}

        contexts = []
        if system_prompt:
            contexts.append({"role": "system", "content": system_prompt})

        try:
            # 使用 llm_generate 适配 v4.12
            resp = await self.context.llm_generate(
                chat_provider_id=self.sys1_id,
                prompt=prompt,
                contexts=contexts
            )
            
            content = resp.completion_text
            if not content:
                raise ValueError("Empty response from System 1")

            # 清洗 Markdown 标记
            content = content.strip()
            if content.startswith("```json"): 
                content = content[7:]
            if content.startswith("```"): 
                content = content[3:]
            if content.endswith("```"): 
                content = content[:-3]
            
            return json.loads(content.strip())

        except Exception as e:
            logger.error(f"[AstrMai] System 1 (Judge) Error: {e}")
            raise # 抛出给 tenacity 重试

    async def call_planner(self, messages: List[Dict], tools: List[Any] = None) -> str:
        """
        System 2 调用: 智能、高消耗、支持工具
        用于: Planner, Chat
        """
        if not self.sys2_id:
            return "⚠️ System 2 Brain Missing (Check Config)"

        # 如果需要工具调用，未来在这里集成 tool_loop_agent
        # Phase 1: 仅文本生成
        resp = await self.context.llm_generate(
            chat_provider_id=self.sys2_id,
            contexts=messages # 这里的 messages 结构需符合 AstrBot 标准
        )
        return resp.completion_text