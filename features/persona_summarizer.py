### 📄 features/persona_summarizer.py
import json
import asyncio
from typing import TYPE_CHECKING, Dict, Any, Tuple
from astrbot.api import logger
from astrbot.api.star import Context

from ..config import HeartflowConfig
from ..persistence import PersistenceManager
from ..services.llm_helper import LLMHelper # [修改] 引入新助手

if TYPE_CHECKING:
    from ..utils.prompt_builder import PromptBuilder 

class PersonaSummarizer:
    """
    (v2.0) 人格摘要管理器
    职责：负责管理和生成人格摘要，并处理缓存
    """
    
    def __init__(self, 
                 context: Context, 
                 config: HeartflowConfig, 
                 persistence: PersistenceManager,
                 prompt_builder: "PromptBuilder"
                 ):
        self.context = context
        self.config = config
        self.persistence = persistence
        self.llm_helper = LLMHelper(context) # [新增]
        
        self.cache = self.persistence.load_persona_cache()
        self.pending_summaries: Dict[str, asyncio.Task[str]] = {} 
        self._lock = asyncio.Lock()

    async def get_or_create_summary(self, umo: str, persona_id: str, original_prompt: str) -> str:
        """获取或创建人格缓存"""
        try:
            # 1. 检查缓存
            cached_data = self.cache.get(persona_id)
            if (cached_data and 
                cached_data.get("summarized") and 
                cached_data.get("dynamic_style_guide") is not None):
                return cached_data.get("summarized")

            # 2. 检查或创建任务
            async with self._lock:
                pending_task = self.pending_summaries.get(persona_id)
                if not pending_task:
                    pending_task = asyncio.create_task(
                        self._internal_create_summary(umo, persona_id, original_prompt)
                    )
                    self.pending_summaries[persona_id] = pending_task
            
            # 3. 等待结果
            return await pending_task

        except Exception as e:
            logger.error(f"PersonaSummarizer Error: {e}")
            return original_prompt

    async def _internal_create_summary(self, umo: str, persona_key: str, original_prompt: str) -> str:
        """内部执行摘要生成"""
        try:
            if not original_prompt or len(original_prompt.strip()) < 50:
                return original_prompt
            
            logger.info(f"正在生成人格摘要 (Key: {persona_key})...")
            
            # 生成摘要
            summarized, style_guide = await self._summarize_system_prompt(original_prompt)
            
            # 缓存
            self.cache[persona_key] = {
                "original": original_prompt,
                "summarized": summarized,
                "dynamic_style_guide": style_guide
            }
            self.save_cache()
            return summarized
            
        except Exception as e:
            logger.error(f"生成摘要失败: {e}")
            return original_prompt
        finally:
            async with self._lock:
                self.pending_summaries.pop(persona_key, None)

    async def _summarize_system_prompt(self, original_prompt: str) -> Tuple[str, str]:
        """
        使用 LLM 对系统提示词进行总结
        """
        # 构建模型列表
        providers = []
        if self.config.summarize_provider_name:
            providers.append(self.config.summarize_provider_name)
        if self.config.judge_provider_names:
            providers.extend(self.config.judge_provider_names)
        
        # 构建 Prompt (List format)
        system_content = """
你的任务是分析用户的[原始角色设定]，并提取两项关键内容：
1. "summarized_persona": 核心人格摘要（100-200字）。
2. "dynamic_style_guide": 回复风格指南。必须包含对不同心情(mood)的语气指导，使用 {mood:.2f} 占位符。

请严格返回 JSON:
{
    "summarized_persona": "...",
    "dynamic_style_guide": "..."
}
""".strip()
        
        prompt = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": f"[原始角色设定]\n{original_prompt}"}
        ]

        # 尝试调用
        result_data = {}
        if not providers:
            # 尝试默认模型
            result_data = await self.llm_helper.chat_json(prompt, retries=1)
        else:
            # 简单的轮询重试
            for pid in providers:
                result_data = await self.llm_helper.chat_json(prompt, provider_id=pid, retries=1)
                if result_data: break
        
        summarized = result_data.get("summarized_persona", "")
        style_guide = result_data.get("dynamic_style_guide", "")
        
        if summarized and style_guide:
            return summarized, style_guide
        
        return original_prompt, ""

    def save_cache(self):
        self.persistence.save_persona_cache(self.cache)

    def get_cached_style_guide(self, persona_key: str) -> str:
        data = self.cache.get(persona_key)
        return data.get("dynamic_style_guide") if data else None

    def clear_cache(self) -> int:
        count = len(self.cache)
        self.cache.clear()
        self.save_cache()
        return count