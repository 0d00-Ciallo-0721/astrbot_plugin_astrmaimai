from __future__ import annotations

import asyncio
import time

from astrbot.api import logger

from ..infrastructure.context_economy import PromptTemplateId


class DiaryService:
    def __init__(self, persistence, memory_engine, config, call_background_lane, semaphore, prompt_registry=None):
        self.persistence = persistence
        self.memory_engine = memory_engine
        self.config = config
        self._call_background_lane = call_background_lane
        self._bg_semaphore = semaphore
        self.prompt_registry = prompt_registry
        self._completed_by_date: dict[str, set[str]] = {}

    async def run_once(self, active_states, diary_date: str = "") -> dict:
        async with self._bg_semaphore:
            diary_date = str(diary_date or time.strftime("%Y-%m-%d", time.localtime()))
            completed = self._completed_by_date.setdefault(diary_date, set())
            self._completed_by_date = {diary_date: completed}
            persona_id = getattr(self.config.persona, "persona_id", "") or "global"
            if hasattr(self.persistence, "load_persona_cache_async"):
                cache = await self.persistence.load_persona_cache_async()
            else:
                cache = await asyncio.to_thread(self.persistence.load_persona_cache)
            persona_data = cache.get(persona_id, {})
            summary = persona_data.get("summary", "")
            persona_injection = f"\n[你的核心人设]: {summary}\n" if summary else ""
            report = {"date": diary_date, "attempted": 0, "succeeded": 0, "failed": 0, "failed_chat_ids": []}

            for state in active_states:
                group_id = getattr(state, "chat_id", None)
                chat_id = str(group_id or "").strip()
                if not chat_id or chat_id in completed:
                    continue
                report["attempted"] += 1
                try:
                    if self.memory_engine and hasattr(self.memory_engine, "session_summarizer"):
                        extract = getattr(self.memory_engine.session_summarizer, "extract_and_summarize_history", None)
                        if extract:
                            await extract(chat_id, days=1)

                    recent_memories = []
                    if self.memory_engine and hasattr(self.memory_engine, "get_recent_memories"):
                        recent_memories = await self.memory_engine.get_recent_memories(chat_id, hours=24)
                    recent_text = "\n".join(str(item) for item in recent_memories[:12]) or "今天没有显著事件。"

                    if self.prompt_registry is not None:
                        envelope = self.prompt_registry.render_template(
                            PromptTemplateId.PROACTIVE_DIARY_SUMMARY,
                            {
                                "persona_summary": summary,
                                "persona_injection": persona_injection,
                                "chat_id": chat_id,
                                "recent_text": recent_text,
                            },
                        )
                        diary = await self._call_background_lane(
                            "diary",
                            chat_id,
                            envelope.prompt,
                            system_prompt=envelope.system_prompt,
                            template_envelope=envelope,
                        )
                    else:
                        diary = None

                    if diary and self.memory_engine and hasattr(self.memory_engine, "record_cognitive_feedback"):
                        await self.memory_engine.record_cognitive_feedback(
                            session_id=chat_id,
                            source="diary",
                            summary=f"Daily internal diary: {str(diary)[:240]}",
                            guidance="Use this diary only as quiet continuity; do not quote it or force old topics.",
                            tags=["diary"],
                            importance=0.45,
                        )
                    if diary and self.memory_engine and hasattr(self.memory_engine, "add_memory"):
                        await self.memory_engine.add_memory(
                            content=f"[内部日记] {diary}",
                            session_id=chat_id,
                            importance=0.45,
                        )
                    completed.add(chat_id)
                    report["succeeded"] += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    report["failed"] += 1
                    report["failed_chat_ids"].append(chat_id)
                    logger.warning(f"[Life] diary degraded for {chat_id}: {exc}")
            return report

    def should_run(self, last_diary_date: str, now_ts: float) -> bool:
        current = time.localtime(now_ts)
        current_hour = current.tm_hour
        current_date = time.strftime("%Y-%m-%d", current)
        # ponytail: daily diary runs in the early-morning low-traffic window.
        return 3 <= current_hour < 5 and last_diary_date != current_date


__all__ = ["DiaryService"]
