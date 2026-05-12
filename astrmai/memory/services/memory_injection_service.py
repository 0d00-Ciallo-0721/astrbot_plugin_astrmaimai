from __future__ import annotations

import uuid

from astrbot.api.event import AstrMessageEvent

from ...conversation.contracts.prompt_envelope import PromptEnvelope
from ...conversation.contracts.turn_context import MemoryInjectionDecision, ensure_turn_context, get_turn_context
from ..contracts.memory_query import MemoryInjectionBundle, MemoryInjectionTrace, MemoryQuery
from .memory_context_builder import MemoryContextBuilder
from .memory_retrieval_service import MemoryRetrievalService


class MemoryInjectionService:
    MEMORY_INTENT_KEYWORDS = {
        "记得",
        "刚才",
        "之前",
        "上次",
        "回忆",
        "想起",
        "remember",
        "last time",
        "earlier",
        "before",
    }

    def __init__(self, retrieval_service: MemoryRetrievalService, config=None):
        self.retrieval_service = retrieval_service
        self.config = config
        max_items = int(getattr(getattr(config, "memory", None), "recall_top_k", 5) or 5) if config else 5
        self.context_builder = MemoryContextBuilder(max_items=max_items)

    @staticmethod
    def _preview(text: str, limit: int = 160) -> str:
        cleaned = " ".join(str(text or "").split())
        return cleaned if len(cleaned) <= limit else cleaned[: max(0, limit - 3)] + "..."

    @classmethod
    def has_memory_intent(cls, text: str) -> bool:
        lowered = str(text or "").lower()
        return any(keyword in str(text or "") or keyword in lowered for keyword in cls.MEMORY_INTENT_KEYWORDS)

    @staticmethod
    def _memory_policy_for_event(event: AstrMessageEvent) -> str:
        turn_context = get_turn_context(event)
        if turn_context is not None and str(turn_context.cognitive.memory_policy or "").strip():
            return str(turn_context.cognitive.memory_policy or "").strip()
        policy = event.get_extra("astrmai_cognitive_memory_policy", "light") if hasattr(event, "get_extra") else "light"
        return str(policy or "light").strip() or "light"

    @staticmethod
    def _think_level(event: AstrMessageEvent) -> int | None:
        value = event.get_extra("astrmai_think_level", None) if hasattr(event, "get_extra") else None
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _current_query(event: AstrMessageEvent, prompt: str, prompt_envelope: PromptEnvelope | None) -> str:
        if isinstance(prompt_envelope, PromptEnvelope):
            current_query = str(
                prompt_envelope.raw_user_text
                or prompt_envelope.focus_message_text
                or prompt_envelope.direct_context_text
                or prompt
                or getattr(event, "message_str", "")
                or ""
            ).strip()
        else:
            current_query = str(prompt or getattr(event, "message_str", "") or "").strip()
        prompt_text = str(prompt or "").strip()
        if prompt_text and prompt_text not in current_query:
            current_query = f"{current_query}\n{prompt_text}" if current_query else prompt_text
        return current_query

    async def build_bundle(
        self,
        *,
        event: AstrMessageEvent,
        prompt: str = "",
        prompt_envelope: PromptEnvelope | None = None,
        disable_rag: bool = False,
        is_fast_mode: bool = False,
        retrieve_keys: list[str] | None = None,
    ) -> MemoryInjectionBundle:
        retrieve_keys = retrieve_keys or []
        policy = self._memory_policy_for_event(event)
        trace = MemoryInjectionTrace(trace_id=f"memtrace_{uuid.uuid4().hex[:12]}", policy=policy)
        decision = MemoryInjectionDecision(policy=policy, retrieve_keys=list(retrieve_keys))

        def skipped(reason: str) -> MemoryInjectionBundle:
            trace.skip_reason = reason
            decision.skip_reason = reason
            decision.trace_id = trace.trace_id
            if reason == "think_level_0":
                decision.policy = "none"
                trace.policy = "none"
            ensure_turn_context(event).memory = decision
            if hasattr(event, "set_extra"):
                event.set_extra("astrmai_memory_injection_trace", trace)
            return MemoryInjectionBundle(trace=trace, skip_reason=reason)

        if hasattr(event, "get_extra") and event.get_extra("astrmai_lightweight_event", False):
            return skipped("lightweight_event")
        if isinstance(prompt_envelope, PromptEnvelope) and prompt_envelope.near_context_priority:
            return skipped("near_context_priority")
        if hasattr(event, "get_extra") and event.get_extra("astrmai_near_context_priority", False):
            return skipped("near_context_priority")

        current_query = self._current_query(event, prompt, prompt_envelope)
        if not current_query:
            return skipped("empty_query")

        think_level = self._think_level(event)
        if think_level is not None and think_level <= 0:
            return skipped("think_level_0")
        if think_level == 1 and not self.has_memory_intent(current_query):
            return skipped("think_level_1_no_memory_intent")
        if disable_rag or is_fast_mode:
            return skipped("disable_rag_injection" if disable_rag else "fast_mode")

        chat_id = str(getattr(event, "unified_msg_origin", "") or "")
        persona_id = str(getattr(getattr(self.config, "persona", None), "persona_id", "") or "")
        query = MemoryQuery(
            query=current_query,
            session_id=chat_id,
            persona_id=persona_id,
            sender_id=str(event.get_sender_id() or "") if hasattr(event, "get_sender_id") else "",
            top_k=int(getattr(getattr(self.config, "memory", None), "recall_top_k", 5) or 5),
            policy=policy,
            think_level=think_level,
            retrieve_keys=list(retrieve_keys),
            allow_stale=policy == "deep" or (think_level is not None and think_level >= 3),
            metadata={"visibility_mode": "auto"},
        )
        candidates = await self.retrieval_service.retrieve(query)
        trace.candidate_count = len(candidates)
        if not candidates:
            return skipped("no_result")

        selected = self.context_builder.select(candidates)
        trace.injected = True
        trace.source = "memory_v2"
        trace.layers = list(dict.fromkeys(item.kind for item in selected if item.kind))
        trace.selected_count = len(selected)
        trace.selected_ids = [item.id for item in selected]
        rendered, guidance = self.context_builder.render_prompt_block(selected)
        trace.summary_preview = self._preview(rendered)
        decision.source = trace.source
        decision.layers = list(trace.layers)
        decision.selected_ids = list(trace.selected_ids)
        decision.trace_id = trace.trace_id
        decision.injected = True
        decision.summary_preview = trace.summary_preview
        ensure_turn_context(event).memory = decision
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_memory_injection_trace", trace)
        return MemoryInjectionBundle(
            rendered_prompt_block=rendered,
            items=selected,
            guidance=guidance,
            trace=trace,
        )


__all__ = ["MemoryInjectionService"]
