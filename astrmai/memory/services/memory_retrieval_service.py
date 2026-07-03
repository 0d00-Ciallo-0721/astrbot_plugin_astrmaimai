from __future__ import annotations

import asyncio
import json
import time

from astrbot.api import logger

from ..contracts.memory_query import MemoryCandidate, MemoryQuery
from .memory_scoring import DEFAULT_MEMORY_SCORING, MemoryScoringConfig, rerank_candidates, scoring_from_config
from .expression_pattern_retrieval_policy import ExpressionPatternRetrievalPolicy
from .jargon_retrieval_policy import JargonRetrievalPolicy
from .v2_store import MemoryV2Store
from ...infrastructure.runtime.lane_manager import LaneKey


class MemoryRetrievalService:
    def __init__(self, store: MemoryV2Store, engine=None, scoring: MemoryScoringConfig | None = None):
        self.store = store
        self.engine = engine
        self.scoring = scoring or scoring_from_config(getattr(engine, "config", None)) or DEFAULT_MEMORY_SCORING
        self.jargon_policy = JargonRetrievalPolicy(store)
        self.expression_pattern_policy = ExpressionPatternRetrievalPolicy(store)

    @staticmethod
    def _resolved_session_id(query: MemoryQuery) -> str:
        layer_set = {str(item) for item in query.layers or [] if str(item).strip()}
        if query.include_persona_lore or "persona_lore" in layer_set:
            return "__self_lore__"
        return str(query.session_id or "")

    @staticmethod
    def _trace_bucket(query: MemoryQuery) -> dict:
        metadata = query.metadata if isinstance(query.metadata, dict) else {}
        trace = metadata.get("_trace")
        if not isinstance(trace, dict):
            trace = {}
            metadata["_trace"] = trace
            query.metadata = metadata
        return trace

    @staticmethod
    def _candidate_trace_payload(candidates, *, limit=8):
        payload = []
        for item in list(candidates or [])[:max(int(limit or 0), 0)]:
            payload.append({
                "id": str(getattr(item, "id", "") or ""),
                "kind": str(getattr(item, "kind", "") or ""),
                "status": str(getattr(item, "status", "") or ""),
                "visibility": str(getattr(item, "visibility", "") or ""),
                "relevance_score": round(float(getattr(item, "relevance_score", 0.0) or 0.0), 4),
                "importance": round(float(getattr(item, "importance", 0.0) or 0.0), 4),
                "confidence": round(float(getattr(item, "confidence", 0.0) or 0.0), 4),
                "score_breakdown": (getattr(item, "metadata", {}) or {}).get("_score_breakdown"),
                "summary_preview": str(getattr(item, "summary", "") or getattr(item, "content", "") or "")[:120],
            })
        return payload

    @staticmethod
    def _result_to_candidate(result, query: MemoryQuery) -> MemoryCandidate:
        metadata = getattr(result, "metadata", {}) or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        content = str(getattr(result, "content", "") or "")
        memory_id = str(metadata.get("canonical_id") or metadata.get("id") or f"idx_{abs(hash(content))}")
        return MemoryCandidate(
            id=memory_id,
            kind=str(metadata.get("kind") or ("persona_lore" if metadata.get("session_id") == "__self_lore__" else "memory")),
            source=str(metadata.get("source") or "hybrid_index"),
            summary=content[:240],
            content=content,
            session_id=str(metadata.get("session_id") or query.session_id or ""),
            persona_id=str(metadata.get("persona_id") or query.persona_id or ""),
            sender_id=str(metadata.get("sender_id") or ""),
            importance=float(metadata.get("importance") or 0.5),
            confidence=0.75,
            relevance_score=float(getattr(result, "score", 0.0) or 0.0),
            recency_score=1.0,
            status=str(metadata.get("status") or "active"),
            visibility=str(metadata.get("visibility") or "auto_and_tool"),
            created_at=float(metadata.get("create_time") or 0.0),
            updated_at=float(metadata.get("update_time") or 0.0),
            last_access_time=float(metadata.get("last_access_time") or 0.0),
            superseded_by=str(metadata.get("superseded_by") or ""),
            access_count=int(metadata.get("access_count") or 0),
            decay_score=float(metadata.get("decay_score") or 1.0),
            metadata_hydrated=False,
            metadata=metadata,
        )

    async def retrieve(self, query: MemoryQuery) -> list[MemoryCandidate]:
        if query.policy == "deep" or (query.think_level is not None and query.think_level >= 3):
            return await self.retrieve_deep(query)
        return await self._retrieve_queries(query, [query.query], top_k=query.top_k)

    async def retrieve_deep(self, query: MemoryQuery) -> list[MemoryCandidate]:
        queries = [query.query]
        try:
            queries = await self._rewrite_queries(query)
            candidate_pool_limit = max(
                int(query.top_k or 5) * max(int(self.scoring.deep_temporal_candidate_pool_factor or 4), 1),
                max(int(self.scoring.deep_temporal_candidate_pool_min or 20), 1),
            )
            candidates = await self._retrieve_queries(query, queries, top_k=candidate_pool_limit)
            candidates = await self._hydrate_candidate_metadata(candidates)
            try:
                candidates = rerank_candidates(candidates, config=self.scoring)
            except Exception as exc:
                logger.warning(f"[MemoryRetrievalService] temporal rerank degraded: {exc}")
            llm_window = max(int(self.scoring.deep_temporal_llm_window or 8), 1)
            temporal_top = candidates[:llm_window]
            temporal_tail = candidates[llm_window:]
            reranked = await self._rerank_candidates(query, temporal_top)
            candidates = list(reranked) + list(temporal_tail)
            guidance = await self._compress_guidance(query, candidates[: max(int(query.top_k or 5), 1)])
            if guidance:
                for item in candidates:
                    item.metadata.setdefault("deep_guidance", guidance)
            return candidates[: max(int(query.top_k or 5), 1)]
        except Exception as exc:
            logger.warning(f"[MemoryRetrievalService] deep retrieval degraded: {exc}")
            return await self._retrieve_queries(query, [query.query], top_k=query.top_k)

    async def _retrieve_queries(self, query: MemoryQuery, queries: list[str], *, top_k: int | None = None) -> list[MemoryCandidate]:
        candidates: list[MemoryCandidate] = []
        seen: set[str] = set()
        limit = max(int(top_k or query.top_k or 5), 1)
        for query_text in queries:
            # NOTE: include_feedback and retrieve_keys are intentionally NOT
            # copied — both are deprecated dead fields (see MemoryQuery
            # docstring).  Copying them would trigger the DeprecationWarning
            # on every scoped query creation.
            scoped_query = MemoryQuery(
                query=query_text,
                session_id=query.session_id,
                persona_id=query.persona_id,
                sender_id=query.sender_id,
                layers=list(query.layers or []),
                top_k=limit,
                policy=query.policy,
                think_level=query.think_level,
                intent=query.intent,
                time_window=query.time_window,
                exclude_kinds=list(query.exclude_kinds or []),
                include_persona_lore=query.include_persona_lore,
                exclude_ids=list(query.exclude_ids or []),
                allow_stale=query.allow_stale,
                metadata=dict(query.metadata or {}),
            )
            for item in await self._retrieve_once(scoped_query):
                if item.id in seen:
                    continue
                seen.add(item.id)
                candidates.append(item)
            if len(candidates) >= limit:
                break  # ponytail: M11 — stop early instead of wasting more queries
        return candidates[:limit]

    async def _retrieve_once(self, query: MemoryQuery) -> list[MemoryCandidate]:
        visibility_mode = str(query.metadata.get("visibility_mode") or "")
        query_layers = {str(item) for item in query.layers or [] if str(item).strip()}
        trace = self._trace_bucket(query)
        if query.intent == "jargon" or query_layers == {"jargon"}:
            trace.setdefault("search_steps", []).append({
                "query": str(query.query or ""),
                "layers": sorted(query_layers),
                "visibility_mode": visibility_mode,
                "allow_stale": bool(query.allow_stale),
            })
            results = await self.jargon_policy.search(
                query=query.query,
                session_id=query.session_id,
                persona_id=query.persona_id,
                top_k=query.top_k,
                exclude_ids=query.exclude_ids,
                allow_stale=query.allow_stale,
                visibility_mode=visibility_mode,
                trace=trace,
            )
            last_step = trace["search_steps"][-1]
            last_step["matched_terms"] = trace.pop("matched_terms", [])
            last_step["top_k_scores"] = trace.pop("top_k_scores", [])
            return results
        if query.intent == "expression_pattern" or query_layers == {"expression_pattern"}:
            trace.setdefault("search_steps", []).append({
                "query": str(query.query or ""),
                "layers": sorted(query_layers),
                "visibility_mode": visibility_mode,
                "allow_stale": bool(query.allow_stale),
            })
            return await self.expression_pattern_policy.search(
                query=query.query,
                session_id=query.session_id,
                top_k=query.top_k,
                shared_scope=str(query.metadata.get("shared_scope") or query.session_id or ""),
                think_level=query.think_level,
                exclude_ids=query.exclude_ids,
                allow_stale=query.allow_stale,
                visibility_mode=visibility_mode,
            )

        limit = max(int(query.top_k or 5), 1)
        resolved_session_id = self._resolved_session_id(query)
        canonical_task = self.store.search(
            query.query,
            session_id=resolved_session_id,
            persona_id=query.persona_id,
            layers=query.layers,
            top_k=limit,
            exclude_ids=query.exclude_ids,
            allow_stale=query.allow_stale,
            visibility_mode=visibility_mode,
        )
        hybrid_task = self._hybrid_search(query, visibility_mode)
        canonical_results, hybrid_results = await asyncio.gather(canonical_task, hybrid_task, return_exceptions=True)
        if isinstance(canonical_results, Exception):
            logger.warning(f"[MemoryRetrievalService] canonical search degraded: {canonical_results}")
            canonical_results = []
        if isinstance(hybrid_results, Exception):
            logger.warning(f"[MemoryRetrievalService] hybrid search degraded: {hybrid_results}")
            hybrid_results = []
        candidates = self._fuse_candidates(canonical_results, hybrid_results, query)
        exclude_kinds = {str(item) for item in query.exclude_kinds or [] if str(item).strip()}
        if exclude_kinds:
            candidates = [c for c in candidates if c.kind not in exclude_kinds]
        return candidates

    async def _hybrid_search(self, query: MemoryQuery, visibility_mode: str) -> list[MemoryCandidate]:
        if not self.engine or not hasattr(self.engine, "search_memories"):
            return []
        try:
            results = await self.engine.search_memories(
                query.query,
                top_k=max(int(query.top_k or 5), 1),
                session_id=self._resolved_session_id(query),
                persona_id=query.persona_id or None,
            )
        except Exception as exc:
            logger.debug(f"[MemoryRetrievalService] hybrid engine search failed: {exc}")
            return []

        excluded = {str(item) for item in query.exclude_ids or [] if str(item).strip()}
        layer_set = {str(item) for item in query.layers or [] if str(item).strip()}
        candidates: list[MemoryCandidate] = []
        pending_ids: list[str] = []
        hybrid_candidates: list[tuple[MemoryCandidate, str | None]] = []
        for result in results:
            candidate = self._result_to_candidate(result, query)
            canonical_id = str(candidate.metadata.get("canonical_id") or candidate.id or "")
            if canonical_id and not canonical_id.startswith("idx_"):
                pending_ids.append(canonical_id)
                hybrid_candidates.append((candidate, canonical_id))
            else:
                hybrid_candidates.append((candidate, None))
        canonical_map = {}
        if pending_ids and hasattr(self.store, "batch_get_by_ids"):
            try:
                canonical_map = await self.store.batch_get_by_ids(pending_ids, allow_stale=query.allow_stale)
            except Exception as exc:
                logger.debug(f"[MemoryRetrievalService] batch canonical hydrate failed: {exc}")
                canonical_map = {}
        for candidate, canonical_id in hybrid_candidates:
            if canonical_id:
                canonical = canonical_map.get(canonical_id)
                if not canonical:
                    continue
                canonical.relevance_score = max(float(candidate.relevance_score or 0.0), float(canonical.relevance_score or 0.0))
                candidate = canonical
            candidates.append(candidate)
        # Filter out excluded/deleted
        final_candidates = []
        for candidate in candidates:
            if candidate.id in excluded:
                continue
            if candidate.status in {"deleted", "merged", "deprecated", "review_pending", "rejected", "superseded"}:
                continue
            if candidate.status == "stale" and not query.allow_stale:
                continue
            if visibility_mode == "auto" and candidate.visibility != "auto_and_tool":
                continue
            if visibility_mode == "tool" and candidate.visibility not in {"auto_and_tool", "tool_only"}:
                continue
            if layer_set and candidate.kind not in layer_set:
                continue
            final_candidates.append(candidate)
        return final_candidates

    def _fuse_candidates(
        self,
        canonical: list[MemoryCandidate],
        hybrid: list[MemoryCandidate],
        query: MemoryQuery,
    ) -> list[MemoryCandidate]:
        excluded = {str(item) for item in query.exclude_ids or [] if str(item).strip()}
        merged: dict[str, MemoryCandidate] = {}
        for c in canonical or []:
            if c.id in excluded:
                continue
            c.metadata = dict(c.metadata or {})
            c.metadata["_canon_score"] = float(c.relevance_score or 0.0)
            c.metadata.setdefault("_hybrid_score", 0.0)
            merged[c.id] = c
        for h in hybrid or []:
            if h.id in excluded:
                continue
            h.metadata = dict(h.metadata or {})
            if h.id in merged:
                existing = merged[h.id]
                existing.metadata["_hybrid_score"] = max(
                    float(existing.metadata.get("_hybrid_score", 0.0)),
                    float(h.relevance_score or 0.0),
                )
                existing.metadata["_canon_score"] = max(
                    float(existing.metadata.get("_canon_score", 0.0)),
                    float(existing.relevance_score or 0.0),
                )
            else:
                h.metadata.setdefault("_canon_score", 0.0)
                h.metadata["_hybrid_score"] = float(h.relevance_score or 0.0)
                merged[h.id] = h

        for item in merged.values():
            canon = float(item.metadata.get("_canon_score", 0.0))
            hybrid_score = float(item.metadata.get("_hybrid_score", 0.0))
            conflict_penalty = 0.0
            if (item.metadata or {}).get("corrected_by") or (item.metadata or {}).get("contradicted_by"):
                conflict_penalty = float(self.scoring.conflict_penalty or 0.0)
            canon_weighted = canon * self.scoring.canonical_weight
            hybrid_weighted = hybrid_score * self.scoring.hybrid_weight
            importance_weighted = float(item.importance or 0.0) * self.scoring.importance_weight
            confidence_weighted = float(item.confidence or 0.0) * self.scoring.confidence_weight
            stale_penalty = self.scoring.stale_penalty if item.status == "stale" else 0.0
            item.relevance_score = (
                canon_weighted + hybrid_weighted + importance_weighted + confidence_weighted
                - conflict_penalty - stale_penalty
            )
            item.metadata["_score_breakdown"] = {
                "canonical": round(canon_weighted, 4),
                "hybrid": round(hybrid_weighted, 4),
                "importance": round(importance_weighted, 4),
                "confidence": round(confidence_weighted, 4),
                "conflict_penalty": round(conflict_penalty, 4),
                "stale_penalty": round(stale_penalty, 4),
            }
        ranked = sorted(merged.values(), key=lambda item: item.relevance_score, reverse=True)
        return ranked

    async def _hydrate_candidate_metadata(self, candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
        pending_ids: list[str] = []
        for item in candidates:
            if (
                float(item.created_at or 0.0) <= 0.0
                or not str(item.kind or "").strip()
                or float(item.importance or 0.0) <= 0.0
                or float(item.last_access_time or 0.0) <= 0.0
                or int(item.access_count or 0) < 0
                or float(item.decay_score or 0.0) <= 0.0
            ):
                pending_ids.append(item.id)
        if not pending_ids:
            return candidates
        meta_by_id = await self.store.batch_get_memory_meta(pending_ids)
        for item in candidates:
            payload = meta_by_id.get(item.id)
            if not payload:
                continue
            if float(item.created_at or 0.0) <= 0.0:
                item.created_at = float(payload.get("created_at") or 0.0)
                if item.created_at > 0:
                    item.recency_score = 1.0 / (1.0 + max(0.0, (time.time() - item.created_at) / 86400))  # ponytail: NTP guard
            item.updated_at = float(payload.get("updated_at") or item.updated_at or 0.0)
            item.last_access_time = float(payload.get("last_access_time") or item.last_access_time or 0.0)
            item.importance = float(payload.get("importance") or item.importance or 0.5)
            item.kind = str(payload.get("kind") or item.kind or "memory")
            item.status = str(payload.get("status") or item.status or "active")
            item.visibility = str(payload.get("visibility") or item.visibility or "auto_and_tool")
            item.sender_id = str(payload.get("sender_id") or item.sender_id or "")
            item.superseded_by = str(payload.get("superseded_by") or item.superseded_by or "")
            item.access_count = int(payload.get("access_count") or item.access_count or 0)
            item.decay_score = float(payload.get("decay_score") or item.decay_score or 1.0)
            merged_metadata = dict(payload.get("metadata") or {})
            merged_metadata.update(dict(item.metadata or {}))
            item.metadata = merged_metadata
            item.metadata_hydrated = True
        return candidates

    async def _rewrite_queries(self, query: MemoryQuery) -> list[str]:
        base_query = str(query.query or "").strip()
        if not base_query:
            return []
        gateway = getattr(self.engine, "gateway", None) if self.engine else None
        if not gateway or not hasattr(gateway, "call_data_process_task"):
            return [base_query]
        prompt = (
            "Rewrite the user memory search request into at most 3 short search queries. "
            "Return JSON only: {\"queries\": [\"...\"]}.\n"
            f"Request: {base_query}"
        )
        try:
            response = await gateway.call_data_process_task(
                prompt=prompt,
                is_json=True,
                lane_key=LaneKey(subsystem="bg", task_family="query_rewrite", scope_id="global", scope_kind="global"),
            )
            if isinstance(response, str):
                response = json.loads(response)
            queries = response.get("queries", []) if isinstance(response, dict) else []
            cleaned = [str(item).strip() for item in queries if str(item).strip()]
        except Exception as exc:
            logger.warning(f"[MemoryRetrievalService] deep query rewrite degraded: {exc}")
            cleaned = []
        result = [base_query]
        for item in cleaned:
            if item not in result:
                result.append(item)
            if len(result) >= 3:
                break
        return result

    async def _call_deep_json(self, prompt: str) -> dict:
        gateway = getattr(self.engine, "gateway", None) if self.engine else None
        if not gateway or not hasattr(gateway, "call_data_process_task"):
            return {}
        try:
            response = await gateway.call_data_process_task(prompt=prompt, is_json=True)
        except TypeError:
            response = await gateway.call_data_process_task(prompt, is_json=True)
        if isinstance(response, str):
            return json.loads(response)
        return response if isinstance(response, dict) else {}

    @staticmethod
    def _candidate_payload(candidates: list[MemoryCandidate]) -> list[dict]:
        payload = []
        for item in candidates:
            payload.append(
                {
                    "id": item.id,
                    "kind": item.kind,
                    "summary": item.summary or item.content[:240],
                    "importance": item.importance,
                    "confidence": item.confidence,
                    "status": item.status,
                }
            )
        return payload

    async def _rerank_candidates(self, query: MemoryQuery, candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
        if len(candidates) <= 1:
            return candidates
        prompt = (
            "Rerank these memory candidates for the query. "
            "Return JSON only: {\"ids\": [\"memory_id\", ...]}.\n"
            f"Query: {query.query}\n"
            f"Candidates: {json.dumps(self._candidate_payload(candidates), ensure_ascii=False)}"
        )
        try:
            data = await self._call_deep_json(prompt)
            ranked_ids = [str(item) for item in data.get("ids", []) if str(item).strip()]
        except Exception as exc:
            logger.warning(f"[MemoryRetrievalService] deep rerank degraded: {exc}")
            ranked_ids = []
        if not ranked_ids:
            return candidates
        by_id = {item.id: item for item in candidates}
        ranked = [by_id[memory_id] for memory_id in ranked_ids if memory_id in by_id]
        ranked.extend(item for item in candidates if item.id not in set(ranked_ids))
        for index, item in enumerate(ranked):
            item.relevance_score = max(item.relevance_score, 1.0 - index * 0.05)
        return ranked

    async def _compress_guidance(self, query: MemoryQuery, candidates: list[MemoryCandidate]) -> str:
        if not candidates:
            return ""
        prompt = (
            "Compress memory candidates into brief guidance for a reply. "
            "Do not quote raw memory text. Return JSON only: {\"guidance\":\"...\"}.\n"
            f"Query: {query.query}\n"
            f"Candidates: {json.dumps(self._candidate_payload(candidates), ensure_ascii=False)}"
        )
        try:
            data = await self._call_deep_json(prompt)
            return str(data.get("guidance") or "").strip()[:500]
        except Exception as exc:
            logger.warning(f"[MemoryRetrievalService] deep compress degraded: {exc}")
            return ""

    @staticmethod
    def render_recall(query: MemoryQuery, candidates: list[MemoryCandidate]) -> str:
        if not candidates:
            return f"No relevant memory found for '{query.query}'."
        lines = []
        for item in candidates:
            text = item.summary or item.content
            if item.status == "stale":
                text = f"(possibly stale) {text}"
            lines.append(f"- {text}")
        return (
            f"Relevant memory about '{query.query}':\n"
            + "\n".join(lines)
            + "\n(use these memories naturally in the follow-up reply)"
        )


__all__ = ["MemoryRetrievalService"]
