from __future__ import annotations

import asyncio
import json
import time

from astrbot.api import logger

from ..contracts.memory_query import MemoryCandidate, MemoryQuery
from .memory_scoring import DEFAULT_MEMORY_SCORING, MemoryScoringConfig
from .expression_pattern_retrieval_policy import ExpressionPatternRetrievalPolicy
from .jargon_retrieval_policy import JargonRetrievalPolicy
from .v2_store import MemoryV2Store


class MemoryRetrievalService:
    def __init__(self, store: MemoryV2Store, engine=None, scoring: MemoryScoringConfig | None = None):
        self.store = store
        self.engine = engine
        self.scoring = scoring or DEFAULT_MEMORY_SCORING
        self.jargon_policy = JargonRetrievalPolicy(store)
        self.expression_pattern_policy = ExpressionPatternRetrievalPolicy(store)

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
        created_at = float(metadata.get("create_time") or time.time())
        return MemoryCandidate(
            id=memory_id,
            kind=str(metadata.get("kind") or ("persona_lore" if metadata.get("session_id") == "__self_lore__" else "memory")),
            source=str(metadata.get("source") or "hybrid_index"),
            summary=content[:240],
            content=content,
            session_id=str(metadata.get("session_id") or query.session_id or ""),
            persona_id=str(metadata.get("persona_id") or query.persona_id or ""),
            importance=float(metadata.get("importance") or 0.5),
            confidence=0.75,
            relevance_score=float(getattr(result, "score", 0.0) or 0.0),
            recency_score=1.0 / (1.0 + max(0.0, (time.time() - created_at) / 86400)),
            status=str(metadata.get("status") or "active"),
            visibility=str(metadata.get("visibility") or "auto_and_tool"),
            created_at=created_at,
            updated_at=created_at,
            last_access_time=float(metadata.get("last_access_time") or 0.0),
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
            candidates = await self._retrieve_queries(query, queries, top_k=max(int(query.top_k or 5) * 2, 8))
            candidates = await self._rerank_candidates(query, candidates)
            guidance = await self._compress_guidance(query, candidates[: max(int(query.top_k or 5), 1)])
            if guidance:
                for item in candidates:
                    item.metadata.setdefault("deep_guidance", guidance)
            return candidates[: max(int(query.top_k or 5), 1)]
        except Exception as exc:
            logger.debug(f"[MemoryRetrievalService] deep retrieval degraded: {exc}")
            return await self._retrieve_queries(query, [query.query], top_k=query.top_k)

    async def _retrieve_queries(self, query: MemoryQuery, queries: list[str], *, top_k: int | None = None) -> list[MemoryCandidate]:
        candidates: list[MemoryCandidate] = []
        seen: set[str] = set()
        limit = max(int(top_k or query.top_k or 5), 1)
        for query_text in queries:
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
                include_feedback=query.include_feedback,
                include_persona_lore=query.include_persona_lore,
                exclude_ids=list(query.exclude_ids or []),
                allow_stale=query.allow_stale,
                retrieve_keys=list(query.retrieve_keys or []),
                metadata=dict(query.metadata or {}),
            )
            for item in await self._retrieve_once(scoped_query):
                if item.id in seen:
                    continue
                seen.add(item.id)
                candidates.append(item)
                if len(candidates) >= limit:
                    break
            if len(candidates) >= limit:
                break
        return candidates[:limit]

    async def _retrieve_once(self, query: MemoryQuery) -> list[MemoryCandidate]:
        visibility_mode = str(query.metadata.get("visibility_mode") or "")
        query_layers = {str(item) for item in query.layers or [] if str(item).strip()}
        if query.intent == "jargon" or query_layers == {"jargon"}:
            return await self.jargon_policy.search(
                query=query.query,
                session_id=query.session_id,
                persona_id=query.persona_id,
                top_k=query.top_k,
                exclude_ids=query.exclude_ids,
                allow_stale=query.allow_stale,
                visibility_mode=visibility_mode,
            )
        if query.intent == "expression_pattern" or query_layers == {"expression_pattern"}:
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
        canonical_task = self.store.search(
            query.query,
            session_id=query.session_id,
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
            logger.debug(f"[MemoryRetrievalService] canonical search degraded: {canonical_results}")
            canonical_results = []
        if isinstance(hybrid_results, Exception):
            logger.debug(f"[MemoryRetrievalService] hybrid search degraded: {hybrid_results}")
            hybrid_results = []
        return self._fuse_candidates(canonical_results, hybrid_results, query)

    async def _hybrid_search(self, query: MemoryQuery, visibility_mode: str) -> list[MemoryCandidate]:
        if not self.engine or not hasattr(self.engine, "_search_memories"):
            return []
        try:
            session_id = "__self_lore__" if query.include_persona_lore or "persona_lore" in query.layers else query.session_id
            results = await self.engine._search_memories(
                query.query,
                top_k=max(int(query.top_k or 5), 1),
                session_id=session_id,
                persona_id=query.persona_id or None,
            )
        except Exception as exc:
            logger.debug(f"[MemoryRetrievalService] hybrid engine search failed: {exc}")
            return []

        excluded = {str(item) for item in query.exclude_ids or [] if str(item).strip()}
        layer_set = {str(item) for item in query.layers or [] if str(item).strip()}
        candidates: list[MemoryCandidate] = []
        for result in results:
            candidate = self._result_to_candidate(result, query)
            canonical_id = str(candidate.metadata.get("canonical_id") or candidate.id or "")
            if canonical_id and not canonical_id.startswith("idx_"):
                canonical = await self.store.get_by_id(canonical_id, allow_stale=query.allow_stale)
                if not canonical:
                    continue
                canonical.relevance_score = max(candidate.relevance_score, canonical.relevance_score)
                candidate = canonical
            if candidate.id in excluded:
                continue
            if candidate.status in {"deleted", "merged", "deprecated", "review_pending", "rejected"}:
                continue
            if candidate.status == "stale" and not query.allow_stale:
                continue
            if visibility_mode == "auto" and candidate.visibility != "auto_and_tool":
                continue
            if visibility_mode == "tool" and candidate.visibility not in {"auto_and_tool", "tool_only"}:
                continue
            if layer_set and candidate.kind not in layer_set:
                continue
            candidates.append(candidate)
        return candidates

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
            item.relevance_score = (
                canon * self.scoring.canonical_weight
                + hybrid_score * self.scoring.hybrid_weight
                + float(item.importance or 0.0) * self.scoring.importance_weight
                + float(item.recency_score or 0.0) * self.scoring.recency_weight
                + float(item.confidence or 0.0) * self.scoring.confidence_weight
                - (self.scoring.stale_penalty if item.status == "stale" else 0.0)
            )
        ranked = sorted(merged.values(), key=lambda item: item.relevance_score, reverse=True)
        return ranked[: max(int(query.top_k or 5), 1)]

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
            response = await gateway.call_data_process_task(prompt=prompt, is_json=True)
            if isinstance(response, str):
                response = json.loads(response)
            queries = response.get("queries", []) if isinstance(response, dict) else []
            cleaned = [str(item).strip() for item in queries if str(item).strip()]
        except Exception as exc:
            logger.debug(f"[MemoryRetrievalService] deep query rewrite degraded: {exc}")
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
            logger.debug(f"[MemoryRetrievalService] deep rerank degraded: {exc}")
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
            logger.debug(f"[MemoryRetrievalService] deep compress degraded: {exc}")
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
