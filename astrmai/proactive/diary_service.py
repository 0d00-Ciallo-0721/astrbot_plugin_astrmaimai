from __future__ import annotations

import time

from astrbot.api import logger


class DiaryService:
    def __init__(self, persistence, memory_engine, config, call_background_lane, semaphore):
        self.persistence = persistence
        self.memory_engine = memory_engine
        self.config = config
        self._call_background_lane = call_background_lane
        self._bg_semaphore = semaphore

    async def run_once(self, active_states):
        async with self._bg_semaphore:
            persona_id = getattr(self.config.persona, "persona_id", "") or "global"
            cache = self.persistence.load_persona_cache()
            persona_data = cache.get(persona_id, {})
            summary = persona_data.get("summary", "")
            persona_injection = f"\n[你的核心人设]: {summary}\n" if summary else ""

            for state in active_states:
                group_id = getattr(state, "chat_id", None)
                if not group_id:
                    continue
                if self.memory_engine and hasattr(self.memory_engine, "summarizer"):
                    extract = getattr(self.memory_engine.summarizer, "extract_and_summarize_history", None)
                    if extract:
                        await extract(group_id, days=1)

                recent_memories = []
                if self.memory_engine and hasattr(self.memory_engine, "get_recent_memories"):
                    recent_memories = await self.memory_engine.get_recent_memories(group_id, hours=24)
                recent_text = "\n".join(str(item) for item in recent_memories[:12]) or "今天没有显著事件。"
                prompt = f"""{persona_injection}
请根据以下群聊最近24小时记忆，写一段仅供内部记录的简短日记摘要。
群聊ID: {group_id}
最近记忆:
{recent_text}

要求：
- 100字以内
- 偏总结，不要像聊天回复
- 直接输出正文
"""
                diary = await self._call_background_lane("diary", str(group_id), prompt)
                if diary and self.memory_engine and hasattr(self.memory_engine, "record_cognitive_feedback"):
                    try:
                        await self.memory_engine.record_cognitive_feedback(
                            session_id=str(group_id),
                            source="diary",
                            summary=f"Daily internal diary: {str(diary)[:240]}",
                            guidance="Use this diary only as quiet continuity; do not quote it or force old topics.",
                            tags=["diary"],
                            importance=0.45,
                        )
                    except Exception as exc:
                        logger.debug(f"[Life] diary feedback degraded: {exc}")
                if diary and self.memory_engine and hasattr(self.memory_engine, "add_memory"):
                    try:
                        await self.memory_engine.add_memory(
                            content=f"[内部日记] {diary}",
                            session_id=str(group_id),
                            importance=0.45,
                        )
                    except Exception as exc:
                        logger.debug(f"[Life] diary write-back degraded: {exc}")

    def should_run(self, last_diary_date: str, now_ts: float) -> bool:
        current = time.localtime(now_ts)
        current_hour = current.tm_hour
        current_date = time.strftime("%Y-%m-%d", current)
        return 3 <= current_hour < 4 and last_diary_date != current_date


__all__ = ["DiaryService"]
