from __future__ import annotations

import json
import time

from astrbot.api import logger

from ..contracts.memory_query import MemoryCandidate, MemoryQuery
from .v2_store import MemoryV2Store


class MemoryRetrievalService:
    def __init__(self, store: MemoryV2Store, engine=None):
        self.store = store
        self.engine = engine

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
        candidates = await self.store.search(
            query.query,
            session_id=query.session_id,
            persona_id=query.persona_id,
            layers=query.layers,
            top_k=query.top_k,
            exclude_ids=query.exclude_ids,
            allow_stale=query.allow_stale,
            visibility_mode=visibility_mode,
        )
        if len(candidates) >= max(int(query.top_k or 5), 1):
            return candidates[: query.top_k]

        if not self.engine or not hasattr(self.engine, "_search_memories"):
            return candidates
        try:
            session_id = "__self_lore__" if query.include_persona_lore or "persona_lore" in query.layers else query.session_id
            results = await self.engine._search_memories(
                query.query,
                top_k=max(int(query.top_k or 5), 1),
                session_id=session_id,
                persona_id=query.persona_id or None,
            )
        except Exception:
            results = []

        seen = {item.id for item in candidates}
        excluded = set(query.exclude_ids or [])
        for result in results:
            candidate = self._result_to_candidate(result, query)
            canonical_id = str(candidate.metadata.get("canonical_id") or candidate.id or "")
            if canonical_id and not canonical_id.startswith("idx_"):
                canonical = await self.store.get_by_id(canonical_id, allow_stale=query.allow_stale)
                if not canonical:
                    continue
                canonical.relevance_score = max(candidate.relevance_score, canonical.relevance_score)
                candidate = canonical
            if candidate.id in seen or candidate.id in excluded:
                continue
            if candidate.status in {"deleted", "merged", "deprecated"}:
                continue
            if candidate.status == "stale" and not query.allow_stale:
                continue
            if visibility_mode == "auto" and candidate.visibility != "auto_and_tool":
                continue
            if visibility_mode == "tool" and candidate.visibility not in {"auto_and_tool", "tool_only"}:
                continue
            if query.layers and candidate.kind not in set(query.layers):
                continue
            seen.add(candidate.id)
            candidates.append(candidate)
            if len(candidates) >= max(int(query.top_k or 5), 1):
                break
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
