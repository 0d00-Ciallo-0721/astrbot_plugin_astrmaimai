# astrmai/Brain/persona_summarizer.py
import hashlib
import asyncio
import json
from typing import Dict, Any, Tuple
from astrbot.api import logger
from ..infra.persistence import PersistenceManager
from ..infra.gateway import GlobalModelGateway

class PersonaSummarizer:
    """
    人设摘要/压缩管理器 (System 2)
    职责: 将冗长的 System Prompt 压缩为高密度的核心特征与风格指南，减少 Token 消耗。
    """
    def __init__(self, persistence: PersistenceManager, gateway: GlobalModelGateway):
        self.persistence = persistence
        self.gateway = gateway
        # 加载持久化缓存
        self.cache = self.persistence.load_persona_cache()
        # 运行时任务锁
        self.pending_tasks: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def _compute_hash(self, text: str) -> str:
        """计算人设内容的 Hash 值，用于缓存 Key"""
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    async def get_summary(self, original_prompt: str) -> Tuple[str, str]:
        """
        获取人设摘要。
        Returns: (summarized_persona, style_guide)
        """
        if not original_prompt or len(original_prompt) < 300:
            # 如果人设很短，直接返回原始值，不做压缩
            return original_prompt, "保持原始风格"

        # 1. 计算 Hash Key
        cache_key = self._compute_hash(original_prompt)

        # 2. 查缓存 (Fast Path)
        if cache_key in self.cache:
            data = self.cache[cache_key]
            return data.get("summary", original_prompt), data.get("style", "")

        # 3. 缓存未命中，发起压缩任务 (Locking Path)
        async with self._lock:
            # 双重检查
            if cache_key in self.cache:
                data = self.cache[cache_key]
                return data.get("summary", original_prompt), data.get("style", "")
            
            # 检查是否有正在进行的任务
            if cache_key in self.pending_tasks:
                task = self.pending_tasks[cache_key]
            else:
                task = asyncio.create_task(self._summarize_remote(original_prompt))
                self.pending_tasks[cache_key] = task

        try:
            # 等待任务完成
            summary, style = await task
            
            # 更新缓存
            self.cache[cache_key] = {
                "summary": summary,
                "style": style,
                "timestamp": __import__("time").time()
            }
            self.persistence.save_persona_cache(self.cache)
            return summary, style
            
        except Exception as e:
            logger.error(f"[PersonaSummarizer] 压缩任务失败: {e}")
            return original_prompt, "保持原始风格" # 降级：使用原始 Prompt
        finally:
            # 清理任务记录
            async with self._lock:
                self.pending_tasks.pop(cache_key, None)

    async def _summarize_remote(self, original_prompt: str) -> Tuple[str, str]:
        """调用 Sys1 (Judge) 模型进行压缩"""
        logger.info(f"[PersonaSummarizer] 🔨 正在压缩人设 (Len: {len(original_prompt)})...")
        
        prompt = f"""
你的任务是将以下[原始人设]压缩为高密度的核心特征，以便让AI在极低Token消耗下仍能完美扮演。

[原始人设]
{original_prompt}

[压缩要求]
1. **summarized_persona**: 提取核心身份、性格关键词、说话习惯。去除冗余描述。
2. **style_guide**: 提取具体的回复格式要求（如：不加句号、喜欢用波浪号、傲娇语气等）。

请严格返回 JSON 格式:
{{
    "summarized_persona": "string (200字以内)",
    "style_guide": "string (简短的风格指导)"
}}
"""
        try:
            # 使用 Gateway 的 call_judge (Sys1) 进行低成本压缩
            result = await self.gateway.call_judge(prompt, system_prompt="你是一个资深的角色扮演专家。")
            summary = result.get("summarized_persona", original_prompt)
            style = result.get("style_guide", "")
            return summary, style
        except Exception as e:
            logger.warning(f"[PersonaSummarizer] LLM 调用失败: {e}")
            return original_prompt, ""