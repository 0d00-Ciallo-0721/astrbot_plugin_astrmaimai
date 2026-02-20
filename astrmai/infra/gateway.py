import json
import re
from typing import Optional, List, Dict, Any
from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.core.agent.message import UserMessageSegment, TextPart

# 尝试导入 json_repair，如果未安装则回退到基础正则解析 (借鉴 MaiBot 容错设计)
try:
    from json_repair import repair_json
except ImportError:
    repair_json = None

class GlobalModelGateway:
    """
    统一模型网关 (Infrastructure Layer)
    基于 AstrBot v4.12 API 进行降维封装
    """
    def __init__(self, context: Context, config: dict):
        self.context = context
        self.sys1_id = config.get("system1_provider_id")
        self.sys2_id = config.get("system2_provider_id")

    def _extract_json(self, text: str) -> str:
        """粗略提取 Markdown 中的 JSON 块"""
        text = text.strip()
        match = re.search(r"```(?:json)?(.*?)```", text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return text

    async def call_judge(self, prompt: str, system_prompt: str = "") -> Dict[str, Any]:
        """
        System 1 调用 (Judge): 快速、低成本、强制返回 JSON
        借鉴 MaiBot 的 json_fix 兜底逻辑
        """
        if not self.sys1_id:
            logger.warning("[AstrMai] System 1 Provider ID not configured!")
            return {}

        full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt

        try:
            # 调用 AstrBot 原生 LLM 接口
            resp = await self.context.llm_generate(
                chat_provider_id=self.sys1_id,
                prompt=full_prompt
            )
            
            content = resp.completion_text
            if not content:
                raise ValueError("Empty response from System 1")

            raw_json_str = self._extract_json(content)

            # 解析容错
            try:
                return json.loads(raw_json_str)
            except json.JSONDecodeError:
                if repair_json:
                    return json.loads(repair_json(raw_json_str))
                else:
                    logger.error("[AstrMai] JSON Decode Failed and json_repair not installed.")
                    return {}

        except Exception as e:
            logger.error(f"[AstrMai] ❌ System 1 (Judge) Error: {e}")
            return {}

    async def call_planner(self, prompt: str) -> str:
        """
        System 2 调用 (Brain): 高算力推理
        后续阶段将升级为 tool_loop_agent
        """
        if not self.sys2_id:
            logger.error("[AstrMai] System 2 Brain Missing (Check Config)")
            return ""

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.sys2_id,
                prompt=prompt
            )
            return resp.completion_text
        except Exception as e:
            logger.error(f"[AstrMai] ❌ System 2 (Planner) Error: {e}")
            return ""