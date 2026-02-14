### 📄 services/llm_helper.py
# (Refactored from utils/api_utils.py)
import json
from astrbot.api.star import Context
from astrbot.api import logger

class LLMHelper:
    def __init__(self, context: Context):
        self.context = context

    async def chat(self, prompt: list, provider_id: str = None) -> str:
        """基础对话"""
        try:
            resp = await self.context.llm_chat(
                prompt=prompt, # AstrBot prompt list
                chat_provider_id=provider_id
            )
            return resp.completion_text if resp else ""
        except Exception as e:
            logger.error(f"LLM Chat Error: {e}")
            return ""

    async def chat_json(self, prompt: list, provider_id: str = None, retries: int = 2) -> dict:
        """
        请求 JSON 响应 (带重试和解析)
        """
        # 强制 System Prompt 要求 JSON
        json_instr = {"role": "system", "content": "You MUST respond with valid JSON only. Do not wrap in markdown blocks."}
        full_prompt = [json_instr] + prompt
        
        for _ in range(retries):
            text = await self.chat(full_prompt, provider_id)
            if not text: continue
            
            # 清洗 Markdown
            clean_text = text.replace("```json", "").replace("```", "").strip()
            
            try:
                return json.loads(clean_text)
            except json.JSONDecodeError:
                logger.warning(f"LLM JSON Decode Failed: {clean_text[:20]}...")
                continue
                
        return {} # Failed