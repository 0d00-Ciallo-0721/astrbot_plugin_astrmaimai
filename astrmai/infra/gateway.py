# astrmai/infra/gateway.py
import json
import re
import asyncio
from typing import Dict, Any
from astrbot.api import logger
from astrbot.api.star import Context

try:
    from json_repair import repair_json
except ImportError:
    repair_json = None

class GlobalModelGateway:
    """
    统一模型网关 (重构版：增加弹性熔断与指数退避)
    """
    def __init__(self, context: Context, config: dict):
        self.context = context
        self.sys1_id = config.get("system1_provider_id")
        self.sys2_id = config.get("system2_provider_id")

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        match = re.search(r"```(?:json)?(.*?)```", text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return text

    async def call_judge(self, prompt: str, system_prompt: str = "", max_retries: int = 2) -> Dict[str, Any]:
        """
        System 1 (Judge) 弹性 JSON 接口
        包含指数退避策略与 JsonRepair 自动修复
        """
        if not self.sys1_id:
            logger.warning("[AstrMai-Gateway] System 1 Provider ID 未配置！")
            return {}

        contexts = [{"role": "system", "content": system_prompt}] if system_prompt else []
        last_error = ""

        for attempt in range(max_retries + 1):
            try:
                resp = await self.context.llm_generate(
                    chat_provider_id=self.sys1_id,
                    prompt=prompt,
                    contexts=contexts
                )
                
                content = resp.completion_text
                if not content or not content.strip():
                    raise ValueError("响应为空")

                raw_json_str = self._extract_json(content)

                try:
                    return json.loads(raw_json_str)
                except json.JSONDecodeError:
                    if repair_json:
                        repaired = repair_json(raw_json_str)
                        if repaired:
                            return json.loads(repaired)
                    raise ValueError(f"JSON 损坏且无法修复: {raw_json_str[:50]}...")

            except Exception as e:
                last_error = str(e)
                logger.warning(f"[AstrMai-Gateway] System 1 失败 (Try {attempt+1}/{max_retries+1}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(1.5 ** attempt) # 指数退避: 1s, 1.5s, 2.25s...
                
        logger.error(f"[AstrMai-Gateway] ❌ System 1 最终异常: {last_error}")
        return {}

    async def call_planner(self, prompt: str, max_retries: int = 2) -> str:
        """
        System 2 (Brain) 弹性纯文本接口
        """
        if not self.sys2_id:
            logger.error("[AstrMai-Gateway] System 2 Brain 未配置！")
            return ""

        last_error = ""
        for attempt in range(max_retries + 1):
            try:
                resp = await self.context.llm_generate(
                    chat_provider_id=self.sys2_id,
                    prompt=prompt
                )
                if resp and resp.completion_text:
                    return resp.completion_text.strip()
                raise ValueError("文本生成为空")
                
            except Exception as e:
                last_error = str(e)
                logger.warning(f"[AstrMai-Gateway] System 2 失败 (Try {attempt+1}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2.0 ** attempt) # 慢思考退避：1s, 2s, 4s...
                    
        logger.error(f"[AstrMai-Gateway] ❌ System 2 最终异常: {last_error}")
        return ""