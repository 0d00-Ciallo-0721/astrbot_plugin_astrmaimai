from __future__ import annotations

import asyncio
import time
from typing import Dict

from .instant_memory_gate import InstantMemoryGate
from .session_memory_summarizer import SessionMemorySummarizer, uuid4_short
from ...shared.helpers.plugin_helpers import safe_create_task


class ChatHistorySummarizer(SessionMemorySummarizer):
    """
    Thin compatibility facade.

    Production runtime should prefer:
    - InstantMemoryGate
    - MemoryTurnPipeline
    - SessionMemorySummarizer

    This class remains only for legacy import compatibility and a small number
    of smoke-level tests.
    """

    def __init__(self, context, gateway, engine, config=None):
        super().__init__(context, gateway, engine, config=config)
        self._running = False
        self._periodic_task = None
        # Compat-only fallback for legacy unit tests. Runtime state authority
        # remains in MemoryTurnPipeline and must not read this field.
        self._compat_instant_llm_last_check: dict[str, float] = {}

    async def start(self):
        if self._running:
            return
        self._running = True
        self._periodic_task = safe_create_task(
            self._periodic_check_loop(),
            name="astrmai:memory:compat-summarizer",
        )
        register = getattr(getattr(self.engine, "owner_registry", None), "register", None)
        if callable(register):
            registry = self.engine.owner_registry
            register(
                self._periodic_task,
                task_family="memory.compat_summarizer",
                scope_id="GLOBAL",
                run_id=f"memory-compat-summarizer-{uuid4_short()}",
                owner="ChatHistorySummarizer",
                generation=getattr(registry, "generation", 0),
                cancel_status="cancelled",
            )

    async def stop(self):
        self._running = False
        if self._periodic_task and not self._periodic_task.done():
            self._periodic_task.cancel()
            await asyncio.gather(self._periodic_task, return_exceptions=True)

    async def describe_session_eligibility(self, chat_id: str) -> Dict:
        pipeline = getattr(self.engine, "memory_pipeline", None)
        if pipeline and hasattr(pipeline, "describe_session_eligibility"):
            return await pipeline.describe_session_eligibility(chat_id)
        threshold_messages = int(getattr(self.config.memory, "summary_threshold", 30) or 30) * 2
        return {
            "eligible": False,
            "candidate_present": False,
            "reason": "memory_pipeline_unavailable",
            "pending_messages": 0,
            "history_size": 0,
            "threshold_messages": threshold_messages,
            "cooldown_until": 0.0,
            "last_memory_run_at": 0.0,
            "last_update": 0.0,
        }

    async def run_once_for_session(self, chat_id: str) -> Dict:
        pipeline = getattr(self.engine, "memory_pipeline", None)
        if pipeline and hasattr(pipeline, "run_maintenance_for_session"):
            return await pipeline.run_maintenance_for_session(chat_id)
        return {"performed": False, "reason": "memory_pipeline_unavailable"}

    async def ingest_committed_turn(
        self,
        chat_id: str,
        user_text: str,
        assistant_text: str,
        *,
        source: str,
        is_proactive: bool = False,
    ) -> Dict:
        pipeline = getattr(self.engine, "memory_pipeline", None)
        if pipeline is None:
            return {"performed": False, "reason": "memory_pipeline_unavailable", "source": source}
        turn = pipeline.build_turn(
            chat_id=chat_id,
            user_text=user_text,
            assistant_text=assistant_text,
            source=source,
            is_proactive=is_proactive,
            persona_id="",
        )
        result = await pipeline.record_turn(turn)
        if not bool(result.get("performed")):
            result["source"] = source
            return result
        gate_result = await pipeline.process_instant_gate(turn)
        result["source"] = source
        result["instant_gate_hit"] = bool(getattr(gate_result, "hit", False))
        result["pending_messages"] = len((pipeline._session_history_buffer.get(chat_id) or {}).get("buffer", []) or [])
        return result

    async def _periodic_check_loop(self):
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
                pipeline = getattr(self.engine, "memory_pipeline", None)
                if pipeline and hasattr(pipeline, "_session_history_buffer"):
                    for chat_id in list(pipeline._session_history_buffer.keys()):
                        payload = await pipeline.describe_session_eligibility(chat_id)
                        if bool(payload.get("eligible")):
                            await pipeline.run_maintenance_for_session(chat_id)
                if hasattr(self.engine, "prune_low_importance"):
                    threshold = getattr(self.config.memory, "prune_threshold", 0.2) if self.config else 0.2
                    await self.engine.prune_low_importance(threshold=threshold)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("[ChatHistorySummarizer] _periodic_check_loop degraded", exc_info=True)
                break

    async def _try_instant_memorize(self, chat_id: str, user_msg: str, ai_msg: str):
        gate = self._compat_instant_gate()
        turn = self._compat_turn(chat_id, user_msg, ai_msg)
        result = await gate._try_instant_memorize(turn)
        if getattr(result, "hit", False):
            return result
        if await self._should_run_instant_llm_backfill(chat_id):
            return await gate.run_llm_backfill(turn)
        return result

    async def _should_run_instant_llm_backfill(self, chat_id: str) -> bool:
        pipeline = getattr(self.engine, "memory_pipeline", None)
        last_check = 0.0
        session_rounds = 0
        if pipeline is not None:
            last_check = float(getattr(pipeline, "_instant_llm_last_check", {}).get(chat_id, 0.0) or 0.0)
            session_rounds = len(((getattr(pipeline, "_session_history_buffer", {}) or {}).get(chat_id) or {}).get("buffer", []) or []) // 2
        else:
            last_check = float(self._compat_instant_llm_last_check.get(chat_id, 0.0) or 0.0)
        think_level = self._resolve_runtime_think_level()
        now = asyncio.get_running_loop().time()
        allowed = self._compat_instant_gate().should_run_llm_backfill(
            self._compat_turn(chat_id, "", ""),
            session_rounds=session_rounds,
            last_check=last_check,
            now=now,
        ) if think_level >= 0 else False
        if allowed and pipeline is not None:
            getattr(pipeline, "_instant_llm_last_check", {})[chat_id] = now
        elif allowed:
            self._compat_instant_llm_last_check[chat_id] = now
        return allowed

    async def _try_instant_memorize_with_llm(self, chat_id: str, user_msg: str, ai_msg: str):
        turn = self._compat_turn(chat_id, user_msg, ai_msg)
        return await self._compat_instant_gate()._run_llm_backfill_legacy(turn)

    async def _try_instant_memorize_with_llm_v2(self, chat_id: str, user_msg: str, ai_msg: str):
        turn = self._compat_turn(chat_id, user_msg, ai_msg)
        return await self._compat_instant_gate().run_llm_backfill(turn)

    def _compat_instant_gate(self):
        gate = getattr(self.engine, "instant_gate", None)
        if gate is None:
            gate = InstantMemoryGate(self.gateway, self.engine, config=self.config)
            self.engine.instant_gate = gate
        return gate

    def _compat_turn(self, chat_id: str, user_msg: str, ai_msg: str):
        from ..contracts.memory_query import CommittedMemoryTurn

        return CommittedMemoryTurn(
            turn_id=f"compat_{uuid4_short()}",
            chat_id=str(chat_id or ""),
            user_text=str(user_msg or ""),
            assistant_text=str(ai_msg or ""),
            source="compat_summarizer",
            is_proactive=False,
            think_level=self._resolve_runtime_think_level(),
            persona_id=str(getattr(getattr(self.config, "persona", None), "persona_id", "") or ""),
            committed_at=time.time(),
        )

    def _resolve_runtime_think_level(self) -> int:
        candidates = [
            getattr(getattr(self.gateway, "context", None), "event", None),
            getattr(self.gateway, "event", None),
            getattr(self.context, "event", None),
            getattr(self.context, "current_event", None),
        ]
        for event in candidates:
            if event is None:
                continue
            value = None
            if hasattr(event, "get_extra"):
                value = event.get_extra("astrmai_think_level", None)
            try:
                if value is not None:
                    return int(value or 0)
            except (TypeError, ValueError):
                continue
        return 0


__all__ = ["ChatHistorySummarizer"]
