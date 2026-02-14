import json
import re
from typing import Dict, Any, Optional
from astrbot.api import logger
from astrbot.api.star import Context

class LLMHelper:
    """
    (v2.0) LLM 调用助手
    职责：封装底层 API 调用，提供稳定的 JSON 解析和重试机制
    """
    def __init__(self, context: Context):
        self.context = context

    async def chat(self, prompt: list, provider_id: str = None) -> str:
        """
        基础对话接口
        """
        try:
            resp = await self.context.llm_chat(
                prompt=prompt, 
                chat_provider_id=provider_id
            )
            return resp.completion_text if resp else ""
        except Exception as e:
            logger.error(f"LLMHelper Chat Error: {e}")
            return ""

    async def chat_json(self, prompt: list, provider_id: str = None, retries: int = 2) -> Dict[str, Any]:
        """
        请求 JSON 响应 (带重试、正则提取和Markdown清洗)
        """
        # 1. 强制 System Prompt 要求 JSON
        json_instr = {
            "role": "system", 
            "content": "You MUST respond with valid JSON only. Do not wrap in markdown blocks like ```json."
        }
        # 将 JSON 指令插入到 Prompt 最前方 (或者 System Prompt 之后)
        full_prompt = [json_instr] + prompt
        
        for attempt in range(retries + 1):
            text = await self.chat(full_prompt, provider_id)
            if not text: continue
            
            # 2. 清洗 Markdown 标记
            clean_text = text.replace("```json", "").replace("```", "").strip()
            
            # 3. 尝试直接解析
            try:
                return json.loads(clean_text)
            except json.JSONDecodeError:
                pass
                
            # 4. 如果失败，尝试正则提取第一个 { ... }
            try:
                match = re.search(r"\{.*\}", clean_text, re.DOTALL)
                if match:
                    json_str = match.group(0)
                    return json.loads(json_str)
            except (json.JSONDecodeError, AttributeError):
                logger.warning(f"LLMHelper: JSON Decode Failed (Try {attempt+1}/{retries+1}): {clean_text[:50]}...")
                continue
                
        # 重试耗尽
        return {}