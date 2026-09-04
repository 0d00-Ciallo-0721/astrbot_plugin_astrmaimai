from __future__ import annotations

import asyncio
import inspect
import time

from astrbot.api import logger

from ..infrastructure.context_economy import PromptTemplateId


class DiaryService:
    def __init__(self, persistence, memory_engine, config, call_background_lane, semaphore, prompt_registry=None, background_task_budget=None):
        self.persistence = persistence
        self.memory_engine = memory_engine
        self.config = config
        self._call_background_lane = call_background_lane
        self._bg_semaphore = semaphore
        self.prompt_registry = prompt_registry
        self.background_task_budget = background_task_budget
        self._completed_by_date: dict[str, set[str]] = {}
        self._checkpoint_by_date: dict[str, dict[str, object]] = {}
        self._checkpoint_load_failures: set[str] = set()

    async def _load_checkpoint(self, diary_date: str) -> dict[str, object]:
        self._checkpoint_load_failures.discard(diary_date)
        try:
            setattr(self.persistence, "_diary_checkpoint_load_failed", False)
        except Exception:
            pass
        loader = getattr(self.persistence, "load_diary_checkpoint_async", None)
        if not callable(loader):
            loader = getattr(self.persistence, "load_diary_checkpoint", None)
        if not callable(loader):
            # Lightweight/legacy persistence adapters intentionally retain an
            # in-memory fallback for tests and non-production hosts.  The
            # production PersistenceSchema always exposes the SQLite loader;
            # when it does, failures are marked degraded above rather than
            # falling back silently.
            return dict(self._checkpoint_by_date.get(diary_date, {}) or {})
        try:
            result = loader(diary_date)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            self._checkpoint_load_failures.add(diary_date)
            logger.warning("[Life] diary checkpoint load failed for %s: %s", diary_date, exc)
            return {}
        if bool(getattr(self.persistence, "_diary_checkpoint_load_failed", False)):
            self._checkpoint_load_failures.add(diary_date)
        checkpoint = dict(result or {}) if isinstance(result, dict) else {}
        self._checkpoint_by_date[diary_date] = checkpoint
        return checkpoint

    async def _save_checkpoint(self, diary_date: str, checkpoint: dict[str, object]) -> bool:
        normalized = dict(checkpoint or {})
        saver = getattr(self.persistence, "save_diary_checkpoint_async", None)
        if not callable(saver):
            saver = getattr(self.persistence, "save_diary_checkpoint", None)
        if not callable(saver):
            self._checkpoint_by_date[diary_date] = normalized
            return True
        try:
            result = saver(diary_date, normalized)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            logger.warning("[Life] diary checkpoint persist failed for %s: %s", diary_date, exc)
            return False
        if result is False:
            logger.warning("[Life] diary checkpoint persist returned false for %s", diary_date)
            return False
        self._checkpoint_by_date[diary_date] = normalized
        return True

    async def run_once(
        self,
        active_states,
        diary_date: str = "",
        *,
        max_chats: int | None = None,
        max_duration_sec: float | None = None,
        per_chat_timeout_sec: float | None = None,
    ) -> dict:
        async with self._bg_semaphore:
            diary_date = str(diary_date or time.strftime("%Y-%m-%d", time.localtime()))
            completed = self._completed_by_date.setdefault(diary_date, set())
            self._completed_by_date = {diary_date: completed}
            checkpoint = await self._load_checkpoint(diary_date)
            completed.update(str(item) for item in (checkpoint.get("completed_chat_ids", []) or []) if str(item))
            active_list = list(active_states or [])
            if diary_date in self._checkpoint_load_failures:
                # A failed checkpoint read is a persistence boundary failure;
                # do not read persona state or start provider work from an
                # ambiguous empty checkpoint.
                deferred_chat_ids = list(
                    dict.fromkeys(
                        str(getattr(item, "chat_id", "") or "").strip()
                        for item in active_list
                        if str(getattr(item, "chat_id", "") or "").strip()
                    )
                )
                return {
                    "date": diary_date,
                    "attempted": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "failed_chat_ids": [],
                    "deferred_chat_ids": deferred_chat_ids,
                    "checkpoint_persisted": 0,
                    "checkpoint_persist_failed": 0,
                    "succeeded_in_memory": 0,
                    "checkpoint_load_failed": True,
                    "diagnostics_status": "degraded",
                    "checkpoint": dict(checkpoint),
                }
            configured_batch = getattr(getattr(self.config, "proactive", None), "diary_batch_size", None)
            try:
                batch_limit = max(1, int(max_chats if max_chats is not None else configured_batch or 16))
            except (TypeError, ValueError):
                batch_limit = 16
            configured_duration = getattr(getattr(self.config, "proactive", None), "diary_batch_timeout_sec", None)
            try:
                duration_limit = max(0.1, float(max_duration_sec if max_duration_sec is not None else configured_duration or 240.0))
            except (TypeError, ValueError):
                duration_limit = 240.0
            configured_chat_timeout = getattr(
                getattr(self.config, "proactive", None),
                "diary_chat_timeout_sec",
                None,
            )
            try:
                chat_timeout = max(
                    0.1,
                    float(
                        per_chat_timeout_sec
                        if per_chat_timeout_sec is not None
                        else configured_chat_timeout or min(120.0, duration_limit)
                    ),
                )
            except (TypeError, ValueError):
                chat_timeout = min(120.0, duration_limit)
            batch_started = time.monotonic()
            persona_id = getattr(self.config.persona, "persona_id", "") or "global"
            if hasattr(self.persistence, "load_persona_cache_async"):
                cache = await self.persistence.load_persona_cache_async()
            else:
                cache = await asyncio.to_thread(self.persistence.load_persona_cache)
            persona_data = cache.get(persona_id, {})
            summary = persona_data.get("summary", "")
            persona_injection = f"\n[你的核心人设]: {summary}\n" if summary else ""
            report = {
                "date": diary_date,
                "attempted": 0,
                "succeeded": 0,
                "failed": 0,
                "failed_chat_ids": [],
                "deferred_chat_ids": [],
                "checkpoint_persisted": 0,
                "checkpoint_persist_failed": 0,
                "succeeded_in_memory": 0,
                "checkpoint_load_failed": False,
                "checkpoint": dict(checkpoint),
            }

            try:
                start_cursor = max(0, int(checkpoint.get("cursor", 0) or 0))
            except (TypeError, ValueError):
                start_cursor = 0
            if active_list:
                start_cursor %= len(active_list)
            ordered_items = (
                list(enumerate(active_list))[start_cursor:]
                + list(enumerate(active_list))[:start_cursor]
            )
            async def _process_chat(chat_id: str) -> None:
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

            for order_index, (index, state) in enumerate(ordered_items):
                if report["attempted"] >= batch_limit or time.monotonic() - batch_started >= duration_limit:
                    report["deferred_chat_ids"].extend(
                        str(getattr(item, "chat_id", "") or "").strip()
                        for _, item in ordered_items[order_index:]
                        if str(getattr(item, "chat_id", "") or "").strip()
                        and str(getattr(item, "chat_id", "") or "").strip() not in completed
                    )
                    break
                group_id = getattr(state, "chat_id", None)
                chat_id = str(group_id or "").strip()
                if not chat_id or chat_id in completed:
                    continue
                report["attempted"] += 1
                try:
                    remaining = max(0.1, duration_limit - (time.monotonic() - batch_started))
                    await asyncio.wait_for(_process_chat(chat_id), timeout=min(chat_timeout, remaining))
                    completed.add(chat_id)
                    report["succeeded_in_memory"] += 1
                    checkpoint_persisted = await self._save_checkpoint(
                        diary_date,
                        {
                            "date": diary_date,
                            "completed_chat_ids": sorted(completed),
                            "cursor": (index + 1) % len(active_list) if active_list else 0,
                            "updated_at": time.time(),
                        },
                    )
                    if checkpoint_persisted:
                        report["succeeded"] += 1
                        report["checkpoint_persisted"] += 1
                    else:
                        completed.discard(chat_id)
                        report["checkpoint_persist_failed"] += 1
                        report["deferred_chat_ids"].append(chat_id)
                        logger.warning("[Life] diary deferred for %s: checkpoint persistence failed", chat_id)
                except asyncio.TimeoutError:
                    report["deferred_chat_ids"].append(chat_id)
                    logger.warning("[Life] diary deferred for %s: per-chat deadline exceeded", chat_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    report["failed"] += 1
                    report["failed_chat_ids"].append(chat_id)
                    logger.warning(f"[Life] diary degraded for {chat_id}: {exc}")
            report["checkpoint"] = dict(self._checkpoint_by_date.get(diary_date, checkpoint) or {})
            return report

    def should_run(self, last_diary_date: str, now_ts: float) -> bool:
        current = time.localtime(now_ts)
        current_hour = current.tm_hour
        current_date = time.strftime("%Y-%m-%d", current)
        # ponytail: daily diary runs in the early-morning low-traffic window.
        return 3 <= current_hour < 5 and last_diary_date != current_date


__all__ = ["DiaryService"]
