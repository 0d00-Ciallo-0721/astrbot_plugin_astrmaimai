from __future__ import annotations

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

    async def run_once(self, active_states):
        async with self._bg_semaphore:
            persona_id = getattr(self.config.persona, "persona_id", "") or "global"
            cache = self.persistence.load_persona_cache()
            persona_data = cache.get(persona_id, {})
            summary = persona_data.get("summary", "")
            persona_injection = f"\n[浣犵殑鏍稿績浜鸿]: {summary}\n" if summary else ""

            for state in active_states:
                group_id = getattr(state, "chat_id", None)
                if not group_id:
                    continue
                if self.memory_engine and hasattr(self.memory_engine, "session_summarizer"):
                    extract = getattr(self.memory_engine.session_summarizer, "extract_and_summarize_history", None)
                    if extract:
                        await extract(group_id, days=1)

                recent_memories = []
                if self.memory_engine and hasattr(self.memory_engine, "get_recent_memories"):
                    recent_memories = await self.memory_engine.get_recent_memories(group_id, hours=24)
                recent_text = "\n".join(str(item) for item in recent_memories[:12]) or "浠婂ぉ娌℃湁鏄捐憲浜嬩欢銆?"

                if self.prompt_registry is not None:
                    envelope = self.prompt_registry.render_template(
                        PromptTemplateId.PROACTIVE_DIARY_SUMMARY,
                        {
                            "persona_summary": summary,
                            "chat_id": str(group_id),
                            "recent_text": recent_text,
                        },
                    )
                    diary = await self._call_background_lane(
                        "diary",
                        str(group_id),
                        envelope.prompt,
                        system_prompt=envelope.system_prompt,
                        template_envelope=envelope,
                    )
                else:
                    prompt = f"""{persona_injection}
璇锋牴鎹互涓嬬兢鑱婃渶杩?4灏忔椂璁板繂锛屽啓涓€娈典粎渚涘唴閮ㄨ褰曠殑绠€鐭棩璁版憳瑕併€?缇よ亰ID: {group_id}
鏈€杩戣蹇?
{recent_text}

瑕佹眰锛?- 100瀛椾互鍐?- 鍋忔€荤粨锛屼笉瑕佸儚鑱婂ぉ鍥炲
- 鐩存帴杈撳嚭姝ｆ枃
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
                            content=f"[鍐呴儴鏃ヨ] {diary}",
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
