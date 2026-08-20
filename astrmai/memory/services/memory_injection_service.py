from __future__ import annotations

import uuid

from astrbot.api.event import AstrMessageEvent

from ...conversation.contracts.prompt_envelope import PromptEnvelope
from ...conversation.contracts.turn_context import MemoryInjectionDecision, ensure_turn_context, get_turn_context
import json

from astrbot.api import logger

from ..contracts.memory_query import MemoryInjectionBundle, MemoryInjectionTrace
from ..contracts.retrieval_trace import RetrievalTrace
from ...infrastructure.runtime.turn_call_ledger import begin_stage, finish_stage
from .memory_context_builder import MemoryContextBuilder
from .memory_query_builder import MemoryQueryBuilder
from .memory_retrieval_service import MemoryRetrievalService
from .actor_memory_scope import build_actor_memory_scope


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
        self.query_builder = MemoryQueryBuilder(config)

    def refresh_config(self, config) -> None:
        self.config = config
        max_items = int(getattr(getattr(config, "memory", None), "recall_top_k", 5) or 5) if config else 5
        self.context_builder.max_items = max(max_items, 1)
        self.query_builder.config = config

    @staticmethod
    def _preview(text: str, limit: int = 160) -> str:
        cleaned = " ".join(str(text or "").split())
        return cleaned if len(cleaned) <= limit else cleaned[: max(0, limit - 3)] + "..."

    @staticmethod
    def _build_trace_summary(query, trace_payload, trace, skip_reason):
        selected = trace_payload.get("selected")
        retrieved = trace_payload.get("retrieved")
        query_builder = trace_payload.get("query_builder")
        retrieval = trace_payload.get("retrieval")
        actor_scope_filter = trace_payload.get("actor_scope_filter")
        search_steps = [
            {key: value for key, value in step.items() if key != "query"}
            for step in list(trace_payload.get("search_steps") or [])
            if isinstance(step, dict)
        ]
        return {
            "tool": "auto_injection",
            "policy": str(getattr(query, "policy", "") or ""),
            "visibility_mode": str((getattr(query, "metadata", {}) or {}).get("visibility_mode") or ""),
            "rewritten_queries": [],
            "retrieve_keys": list(getattr(query, "retrieve_keys", []) or []),
            "search_steps": search_steps,
            "selected_ids": list(getattr(trace, "selected_ids", []) or []),
            "selected_count": int(getattr(trace, "selected_count", 0) or 0),
            "candidate_count": int(getattr(trace, "candidate_count", 0) or 0),
            "retrieved_count": len(retrieved) if isinstance(retrieved, list) else 0,
            "query_builder": dict(query_builder) if isinstance(query_builder, dict) else {},
            "retrieval": dict(retrieval) if isinstance(retrieval, dict) else {},
            "actor_scope_filter": (
                dict(actor_scope_filter)
                if isinstance(actor_scope_filter, dict)
                else {}
            ),
            "skip_reason": str(skip_reason or getattr(trace, "skip_reason", "") or ""),
            "error": str(trace_payload.get("error") or ""),
        }

    async def _persist_trace(self, event, query, trace, selected, skip_reason=""):
        engine = getattr(self.retrieval_service, "engine", None)
        db_service = getattr(engine, "db_service", None) if engine else None
        if not db_service or not hasattr(db_service, "save_retrieval_trace_async"):
            return
        trace_payload = dict((getattr(query, "metadata", {}) or {}).get("_trace", {}) or {})
        debug_enabled = bool((getattr(query, "metadata", {}) or {}).get("memory_retrieval_debug_trace_enabled", False))
        persisted_query = str(getattr(query, "query", "") or "") if debug_enabled else ""
        tool_calls = [{
            "tool": "auto_injection",
            "memory_ids": list(getattr(trace, "selected_ids", []) or []),
            "search_steps": list(trace_payload.get("search_steps") or []),
            "skip_reason": str(skip_reason or ""),
        }]
        if debug_enabled:
            tool_calls[0]["query"] = persisted_query
        try:
            trace_summary = self._build_trace_summary(query, trace_payload, trace, skip_reason)
            record = RetrievalTrace(
                trace_id=getattr(trace, "trace_id", "") or "",
                chat_id=str(getattr(event, "unified_msg_origin", "") or ""),
                sender_name=str(event.get_sender_name() or "") if hasattr(event, "get_sender_name") else "",
                query=persisted_query,
                planner_question=persisted_query,
                tool_calls=json.dumps(tool_calls, ensure_ascii=False),
                trace_summary=json.dumps(trace_summary, ensure_ascii=False),
                selected_memory_ids=json.dumps(list(getattr(trace, "selected_ids", []) or []), ensure_ascii=False),
                source_layers=json.dumps(list(getattr(trace, "layers", []) or []), ensure_ascii=False),
                confidence=max((float(getattr(item, "relevance_score", 0.0) or 0.0) for item in (selected or [])), default=0.0),
            )
            await db_service.save_retrieval_trace_async(record.to_orm_model())
        except Exception:
            logger.warning("[MemoryInjection] failed to persist retrieval trace", exc_info=True)

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
        stage_id = begin_stage(
            event,
            "memory.injection",
            metadata={
                "policy": policy,
                "retrieve_key_count": len(retrieve_keys),
                "fast_mode": bool(is_fast_mode),
                "rag_disabled": bool(disable_rag),
            },
        )
        query = None

        def remember_funnel(payload: dict) -> None:
            query_metadata = dict(getattr(query, "metadata", {}) or {})
            rewrite_trace = query_metadata.get("query_rewrite_trace")
            if isinstance(rewrite_trace, dict):
                payload = {**payload, "query_rewrite_trace": dict(rewrite_trace)}
            trace_payload = dict(query_metadata.get("_trace") or {})
            hybrid_observations = [
                dict(item)
                for item in list(trace_payload.get("hybrid_observations") or [])[-4:]
                if isinstance(item, dict)
            ]
            vector_fallback = trace_payload.get("vector_fallback")
            if hybrid_observations:
                payload = {**payload, "hybrid_observations": hybrid_observations}
            if isinstance(vector_fallback, dict) and vector_fallback:
                payload = {**payload, "vector_fallback": dict(vector_fallback)}
            if hasattr(event, "set_extra"):
                event.set_extra("astrmai_memory_funnel", dict(payload))

        def apply_actor_scope_decision() -> dict:
            query_metadata = dict(getattr(query, "metadata", {}) or {})
            trace_payload = dict(query_metadata.get("_trace") or {})
            actor_filter_payload = dict(trace_payload.get("actor_scope_filter") or {})
            decision.actor_whitelist = list(
                actor_filter_payload.get("allowed_actor_ids") or []
            )
            decision.suppressed_candidate_ids = list(
                actor_filter_payload.get("suppressed_ids") or []
            )
            decision.suppressed_candidate_count = int(
                actor_filter_payload.get("suppressed_count", 0) or 0
            )
            return actor_filter_payload

        def skipped(reason: str) -> MemoryInjectionBundle:
            actor_filter_payload = apply_actor_scope_decision()
            trace.skip_reason = reason
            decision.skip_reason = reason
            decision.trace_id = trace.trace_id
            if reason == "think_level_0":
                decision.policy = "none"
                trace.policy = "none"
            ensure_turn_context(event).memory = decision
            if hasattr(event, "set_extra"):
                event.set_extra("astrmai_memory_injection_trace", trace)
            funnel = {
                "status": "skipped",
                "policy": trace.policy,
                "skip_reason": reason,
                "candidate_count": 0,
                "selected_count": 0,
                "rendered_chars": 0,
                "actor_whitelist_count": len(decision.actor_whitelist),
                "actor_suppressed_count": decision.suppressed_candidate_count,
                "actor_candidate_count_before_filter": int(
                    actor_filter_payload.get("before_count", 0) or 0
                ),
            }
            remember_funnel(funnel)
            finish_stage(
                event,
                stage_id,
                status="skipped",
                reason=reason,
                metadata={
                    "candidate_count": 0,
                    "selected_count": 0,
                    "rendered_chars": 0,
                    "actor_whitelist_count": funnel["actor_whitelist_count"],
                    "actor_suppressed_count": funnel["actor_suppressed_count"],
                    "actor_candidate_count_before_filter": funnel[
                        "actor_candidate_count_before_filter"
                    ],
                },
            )
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
        actor_memory_scope = build_actor_memory_scope(event)
        try:
            query = self.query_builder.build(
                event=event,
                raw_query=current_query,
                prompt_envelope=prompt_envelope,
                session_id=chat_id,
                persona_id=persona_id,
                sender_id=str(event.get_sender_id() or "") if hasattr(event, "get_sender_id") else "",
                top_k=int(getattr(getattr(self.config, "memory", None), "recall_top_k", 5) or 5),
                policy=policy,
                think_level=think_level,
                retrieve_keys=list(retrieve_keys),
                allow_stale=policy == "deep" or (think_level is not None and think_level >= 3),
                metadata={
                    "visibility_mode": "auto",
                    "actor_memory_scope": actor_memory_scope.as_dict(),
                },
            )
            query.exclude_kinds = list(
                dict.fromkeys(
                    [
                        *(query.exclude_kinds or []),
                        "expression_pattern",
                        "jargon",
                    ]
                )
            )
            candidates = await self.retrieval_service.retrieve(query)
        except Exception as exc:
            remember_funnel(
                {
                    "status": "error",
                    "policy": policy,
                    "error_kind": exc.__class__.__name__,
                    "candidate_count": 0,
                    "selected_count": 0,
                }
            )
            finish_stage(
                event,
                stage_id,
                status="error",
                reason=exc.__class__.__name__,
            )
            raise
        trace.candidate_count = len(candidates)
        if not candidates:
            return skipped("no_result")

        try:
            selected = self.context_builder.select(candidates, max_items=query.top_k)
            rendered, guidance = self.context_builder.render_prompt_block(selected, max_items=query.top_k)
        except Exception as exc:
            remember_funnel(
                {
                    "status": "error",
                    "policy": policy,
                    "error_kind": exc.__class__.__name__,
                    "candidate_count": len(candidates),
                    "selected_count": 0,
                }
            )
            finish_stage(
                event,
                stage_id,
                status="error",
                reason=exc.__class__.__name__,
                metadata={"candidate_count": len(candidates)},
            )
            raise
        trace.injected = True
        trace.source = "memory_v2"
        trace.layers = list(dict.fromkeys(item.kind for item in selected if item.kind))
        trace.selected_count = len(selected)
        trace.selected_ids = [item.id for item in selected]
        debug_trace_enabled = bool(query.metadata.get("memory_retrieval_debug_trace_enabled", False))
        trace.summary_preview = self._preview(rendered) if debug_trace_enabled else ""
        decision.source = trace.source
        decision.layers = list(trace.layers)
        decision.selected_ids = list(trace.selected_ids)
        decision.trace_id = trace.trace_id
        decision.injected = True
        decision.summary_preview = trace.summary_preview
        ensure_turn_context(event).memory = decision
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_memory_injection_trace", trace)
        trace_payload = dict((query.metadata or {}).get("_trace", {}) or {})
        retrieval_payload = dict(trace_payload.get("retrieval") or {})
        apply_actor_scope_decision()
        search_steps = [
            step for step in list(trace_payload.get("search_steps") or [])
            if isinstance(step, dict)
        ]
        matched_by = sorted(
            {
                str(source)
                for item in selected
                for source in list((getattr(item, "metadata", {}) or {}).get("matched_by") or [])
                if str(source).strip()
            }
        )
        funnel = {
            "status": "injected",
            "policy": policy,
            "candidate_limit": int((query.metadata or {}).get("candidate_limit", 0) or 0),
            "injection_top_k": int(query.top_k or 0),
            "search_step_count": len(search_steps),
            "candidate_count": int(retrieval_payload.get("candidate_count", len(candidates)) or 0),
            "selected_count": len(selected),
            "rendered_chars": len(rendered or ""),
            "matched_source_count": len(matched_by),
            "degraded_component_count": len(list(trace_payload.get("degraded_components") or [])),
            "actor_whitelist_count": len(decision.actor_whitelist),
            "actor_suppressed_count": decision.suppressed_candidate_count,
        }
        remember_funnel(funnel)
        finish_stage(
            event,
            stage_id,
            metadata={
                "candidate_limit": funnel["candidate_limit"],
                "injection_top_k": funnel["injection_top_k"],
                "candidate_count": funnel["candidate_count"],
                "selected_count": funnel["selected_count"],
                "rendered_chars": funnel["rendered_chars"],
                "matched_source_count": funnel["matched_source_count"],
                "degraded_component_count": funnel["degraded_component_count"],
                "actor_whitelist_count": funnel["actor_whitelist_count"],
                "actor_suppressed_count": funnel["actor_suppressed_count"],
            },
        )
        await self._persist_trace(event=event, query=query, trace=trace, selected=selected)
        return MemoryInjectionBundle(
            rendered_prompt_block=rendered,
            items=selected,
            guidance=guidance,
            trace=trace,
        )


__all__ = ["MemoryInjectionService"]
