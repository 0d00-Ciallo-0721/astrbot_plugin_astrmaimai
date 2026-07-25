from __future__ import annotations

import asyncio
from dataclasses import replace
from difflib import SequenceMatcher
import json
import math
import re
import time

from astrbot.api import logger

from ..contracts.memory_query import MemoryCandidate, MemoryQuery
from .memory_scoring import DEFAULT_MEMORY_SCORING, MemoryScoringConfig, rerank_candidates, scoring_from_config
from .expression_pattern_retrieval_policy import ExpressionPatternRetrievalPolicy
from .jargon_retrieval_policy import JargonRetrievalPolicy
from .v2_store import MemoryV2Store
from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.runtime.turn_call_ledger import clamp_timeout_to_turn_budget


class MemoryRetrievalService:
    _RRF_K = 60.0
    _RRF_SOURCE_WEIGHTS = {
        "canonical_fts": 1.0,
        "faiss": 0.9,
        "legacy_bm25": 0.8,
        "legacy_hybrid": 0.75,
        "fallback": 0.65,
    }
    _MMR_LAMBDA = 0.75

    def __init__(self, store: MemoryV2Store, engine=None, scoring: MemoryScoringConfig | None = None):
        self.store = store
        self.engine = engine
        self.scoring = scoring or scoring_from_config(getattr(engine, "config", None)) or DEFAULT_MEMORY_SCORING
        self.jargon_policy = JargonRetrievalPolicy(store)
        self.expression_pattern_policy = ExpressionPatternRetrievalPolicy(store)

    def refresh_config(self, config) -> None:
        self.scoring = scoring_from_config(config)

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

    @classmethod
    def _mark_degraded(cls, query: MemoryQuery, component: str) -> None:
        trace = cls._trace_bucket(query)
        degraded = trace.setdefault("degraded_components", [])
        if component not in degraded:
            degraded.append(component)

    @staticmethod
    def _matched_sources(value) -> list[str]:
        values = [value] if isinstance(value, str) else list(value or [])
        return sorted({str(item) for item in values if str(item).strip()})

    @staticmethod
    def _candidate_trace_payload(candidates, *, limit=8, include_content=False):
        payload = []
        for item in list(candidates or [])[:max(int(limit or 0), 0)]:
            metadata = dict(getattr(item, "metadata", {}) or {})
            entry = {
                "id": str(getattr(item, "id", "") or ""),
                "kind": str(getattr(item, "kind", "") or ""),
                "source": str(getattr(item, "source", "") or ""),
                "matched_by": MemoryRetrievalService._matched_sources(metadata.get("matched_by")),
                "status": str(getattr(item, "status", "") or ""),
                "visibility": str(getattr(item, "visibility", "") or ""),
                "relevance_score": round(float(getattr(item, "relevance_score", 0.0) or 0.0), 4),
                "importance": round(float(getattr(item, "importance", 0.0) or 0.0), 4),
                "confidence": round(float(getattr(item, "confidence", 0.0) or 0.0), 4),
                "score_breakdown": metadata.get("_score_breakdown"),
            }
            if include_content:
                entry["summary_preview"] = str(
                    getattr(item, "summary", "") or getattr(item, "content", "") or ""
                )[:120]
            payload.append(entry)
        return payload

    @staticmethod
    def _result_to_candidate(result, query: MemoryQuery) -> MemoryCandidate:
        metadata = getattr(result, "metadata", {}) or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        metadata = dict(metadata)
        matched_by = MemoryRetrievalService._matched_sources(metadata.get("matched_by"))
        source = str(getattr(result, "source", "") or "")
        if not matched_by:
            matched_by = ["faiss" if source == "vector" else "legacy_bm25" if source == "bm25" else "legacy_hybrid"]
        metadata["matched_by"] = matched_by
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

    @staticmethod
    def _explicit_candidate_limit(query: MemoryQuery) -> int | None:
        metadata = query.metadata or {}
        value = metadata.get("candidate_limit", metadata.get("retrieval_candidate_k"))
        if value is None:
            return None
        try:
            return max(int(value), max(int(query.top_k or 5), 1))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _candidate_limit(cls, query: MemoryQuery) -> int:
        return cls._explicit_candidate_limit(query) or max(int(query.top_k or 5), 1)

    @staticmethod
    def _clone_candidate(candidate: MemoryCandidate) -> MemoryCandidate:
        return replace(
            candidate,
            tags=list(candidate.tags or []),
            metadata=dict(candidate.metadata or {}),
        )

    @staticmethod
    def _get_candidate_sort_score(candidate: MemoryCandidate) -> float:
        metadata = candidate.metadata or {}
        return float(metadata.get("_final_relevance_score", candidate.relevance_score) or 0.0)

    @classmethod
    def _stable_sort_key(cls, candidate: MemoryCandidate) -> tuple:
        metadata = candidate.metadata or {}
        return (
            -cls._get_candidate_sort_score(candidate),
            -float(metadata.get("_base_relevance_score", candidate.relevance_score) or 0.0),
            -len(cls._matched_sources(metadata.get("matched_by"))),
            -float(candidate.updated_at or candidate.created_at or 0.0),
            str(candidate.id or ""),
        )

    @staticmethod
    def _normalized_content(candidate: MemoryCandidate) -> str:
        text = str(candidate.summary or candidate.content or "").lower()
        for source, target in (
            ("特别喜欢", "喜欢"),
            ("很喜欢", "喜欢"),
            ("爱吃", "喜欢吃"),
            ("偏爱", "喜欢"),
            ("用户", ""),
        ):
            text = text.replace(source, target)
        return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)

    def _deduplicate_candidates(self, candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
        merged: list[MemoryCandidate] = []
        identities: dict[str, int] = {}
        for original in candidates:
            candidate = self._clone_candidate(original)
            canonical_id = str(candidate.metadata.get("canonical_id") or "")
            keys = [f"id:{candidate.id}"]
            if canonical_id:
                keys.append(f"canonical:{canonical_id}")
            existing_index = next((identities[key] for key in keys if key in identities), None)
            if existing_index is None:
                normalized = self._normalized_content(candidate)
                has_cjk = any("\u4e00" <= char <= "\u9fff" for char in normalized)
                if len(normalized) >= 4 and has_cjk:
                    existing_index = next(
                        (
                            index
                            for index, item in enumerate(merged)
                            if SequenceMatcher(
                                None,
                                normalized,
                                self._normalized_content(item),
                            ).ratio() >= 0.88
                        ),
                        None,
                    )
            if existing_index is None:
                existing_index = len(merged)
                merged.append(candidate)
            else:
                existing = merged[existing_index]
                existing.relevance_score = max(existing.relevance_score, candidate.relevance_score)
                existing.metadata["matched_by"] = sorted(
                    set(self._matched_sources(existing.metadata.get("matched_by")))
                    | set(self._matched_sources(candidate.metadata.get("matched_by")))
                )
                for score_key in ("_canon_score", "_hybrid_score", "_rrf_raw_score", "_rrf_score"):
                    if score_key in candidate.metadata:
                        existing.metadata[score_key] = max(
                            float(existing.metadata.get(score_key, 0.0) or 0.0),
                            float(candidate.metadata.get(score_key, 0.0) or 0.0),
                        )
            for key in keys:
                identities[key] = existing_index
        return merged

    def _intent_rerank(self, query: MemoryQuery, candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
        if not candidates:
            return []
        raw_scores = [
            score if math.isfinite(score) else 0.0
            for score in (float(item.relevance_score or 0.0) for item in candidates)
        ]
        low, high = min(raw_scores), max(raw_scores)
        if all(0.0 <= score <= 1.0 for score in raw_scores):
            base_scores = raw_scores
        elif high > low:
            base_scores = [(score - low) / (high - low) for score in raw_scores]
        else:
            base_scores = [1.0 for _score in raw_scores]

        intents = set((query.metadata or {}).get("intents") or [])
        top_base = max(base_scores, default=0.0)
        reranked: list[MemoryCandidate] = []
        food_terms = {"火锅", "芒果", "食物", "吃", "爱吃", "口味", "饭", "菜", "忌口", "餐"}
        non_food_terms = {"颜色", "蓝色", "音乐", "游戏", "跑步", "安静", "地点"}
        preference_terms = {"喜欢", "偏好", "爱好"}
        dislike_terms = {"不喜欢", "讨厌", "厌恶", "忌口"}

        for rank, (original, base_score) in enumerate(zip(candidates, base_scores), start=1):
            item = self._clone_candidate(original)
            text = f"{item.summary} {item.content}".lower()
            eligible = base_score >= 0.2 or rank <= 5
            boost = 0.0
            penalty = 0.0
            reasons: list[str] = []
            if eligible and "food_preference" in intents and any(term in text for term in food_terms):
                boost += base_score * 0.25
                reasons.append("food_domain_match")
            elif "food_preference" in intents and any(term in text for term in non_food_terms):
                penalty += base_score * 0.15
                reasons.append("food_domain_mismatch")
            if eligible and "preference_general" in intents and any(term in text for term in preference_terms):
                boost += base_score * 0.08
                reasons.append("preference_match")
            if eligible and "dislike" in intents and any(term in text for term in dislike_terms):
                boost += base_score * 0.12
                reasons.append("dislike_match")
            if eligible and "identity" in intents and item.kind in {"identity", "profile", "fact"}:
                boost += base_score * 0.2
                reasons.append("identity_kind_match")

            boost = min(boost, base_score * 0.3)
            penalty = min(penalty, base_score * 0.3)
            final_score = min(max(base_score + boost - penalty, 0.0), 1.0)
            if top_base > 0.0 and base_score < top_base * 0.5:
                final_score = min(final_score, max(top_base - 1e-6, 0.0))
            breakdown = dict(item.metadata.get("_score_breakdown") or {})
            breakdown["intent"] = {
                "boost": round(boost, 4),
                "penalty": round(penalty, 4),
                "reasons": reasons,
            }
            item.metadata["_base_relevance_score"] = round(base_score, 6)
            item.metadata["_intent_boost"] = round(boost, 6)
            item.metadata["_intent_penalty"] = round(penalty, 6)
            item.metadata["_final_relevance_score"] = round(final_score, 6)
            item.metadata["_score_breakdown"] = breakdown
            item.relevance_score = final_score
            reranked.append(item)
        return sorted(reranked, key=self._stable_sort_key)

    @classmethod
    def _rrf_source_weight(cls, candidate: MemoryCandidate, default_source: str) -> float:
        sources = cls._matched_sources((candidate.metadata or {}).get("matched_by")) or [default_source]
        return max((cls._RRF_SOURCE_WEIGHTS.get(source, 0.7) for source in sources), default=0.7)

    @classmethod
    def _candidate_similarity(cls, left: MemoryCandidate, right: MemoryCandidate) -> float:
        left_text = cls._normalized_content(left)
        right_text = cls._normalized_content(right)
        if not left_text or not right_text:
            return 0.0
        return SequenceMatcher(None, left_text, right_text).ratio()

    def _mmr_rerank(self, candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
        if len(candidates) <= 1:
            return [self._clone_candidate(item) for item in candidates]

        working = [self._clone_candidate(item) for item in candidates]
        raw_scores = [self._get_candidate_sort_score(item) for item in working]
        low, high = min(raw_scores), max(raw_scores)
        if all(0.0 <= score <= 1.0 for score in raw_scores):
            normalized_scores = {id(item): score for item, score in zip(working, raw_scores)}
        elif high > low:
            normalized_scores = {
                id(item): (score - low) / (high - low)
                for item, score in zip(working, raw_scores)
            }
        else:
            normalized_scores = {id(item): 1.0 for item in working}

        remaining = sorted(working, key=self._stable_sort_key)
        selected: list[MemoryCandidate] = []
        while remaining:
            scored: list[tuple[MemoryCandidate, float, float, float]] = []
            for item in remaining:
                relevance = normalized_scores[id(item)]
                similarity = max(
                    (self._candidate_similarity(item, chosen) for chosen in selected),
                    default=0.0,
                )
                mmr_score = self._MMR_LAMBDA * relevance - (1.0 - self._MMR_LAMBDA) * similarity
                scored.append((item, similarity, mmr_score, relevance))
            best_item, best_similarity, best_mmr_score, _relevance = min(
                scored,
                key=lambda row: (-row[2], -row[3], self._stable_sort_key(row[0])),
            )
            best_item.metadata["_mmr_score"] = round(best_mmr_score, 6)
            best_item.metadata["_mmr_penalty"] = round((1.0 - self._MMR_LAMBDA) * best_similarity, 6)
            breakdown = dict(best_item.metadata.get("_score_breakdown") or {})
            breakdown["mmr"] = {
                "score": round(best_mmr_score, 4),
                "similarity_penalty": round((1.0 - self._MMR_LAMBDA) * best_similarity, 4),
            }
            best_item.metadata["_score_breakdown"] = breakdown
            selected.append(best_item)
            remaining.remove(best_item)
        return selected

    def _finalize_candidates(self, query: MemoryQuery, candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
        rerank_enabled = bool((query.metadata or {}).get("intent_rerank_enabled", False))
        ranked = self._deduplicate_candidates(list(candidates))
        if rerank_enabled:
            try:
                ranked = self._intent_rerank(query, ranked)
            except Exception as exc:
                logger.warning(f"[MemoryRetrievalService] intent rerank degraded: {exc}")
                self._mark_degraded(query, "intent_rerank")
                ranked = self._deduplicate_candidates(list(candidates))
        if bool((query.metadata or {}).get("memory_mmr_enabled", False)):
            try:
                ranked = self._mmr_rerank(ranked)
            except Exception as exc:
                logger.warning(f"[MemoryRetrievalService] MMR degraded: {exc}")
                self._mark_degraded(query, "mmr")
        limit = max(int(query.top_k or 5), 1)
        if bool((query.metadata or {}).get("adaptive_top_k_enabled", False)) and ranked:
            high_confidence_count = sum(
                1 for item in ranked if float(item.confidence if item.confidence is not None else 0.5) >= 0.6
            )
            if high_confidence_count > 0:
                limit = min(limit, high_confidence_count)
            else:
                eligible = [
                    item for item in ranked
                    if self._get_candidate_sort_score(item) >= 0.15
                ]
                limit = min(limit, 3, len(eligible)) if eligible else 0
        selected = ranked[:limit]
        trace = self._trace_bucket(query)
        trace["retrieval"] = {
            "candidate_count": len(candidates),
            "selected_ids": [str(item.id or "") for item in selected],
            "candidates": self._candidate_trace_payload(selected),
        }
        if bool((query.metadata or {}).get("memory_retrieval_debug_trace_enabled", False)):
            trace["retrieval_debug"] = {
                "query": str(query.query or ""),
                "candidates": self._candidate_trace_payload(ranked, include_content=True),
            }
        return selected

    async def retrieve(self, query: MemoryQuery) -> list[MemoryCandidate]:
        if query.policy == "deep" or (query.think_level is not None and query.think_level >= 3):
            return await self.retrieve_deep(query)
        candidate_limit = self._candidate_limit(query)
        candidates = await self._retrieve_queries(
            query,
            [query.query],
            top_k=query.top_k,
            candidate_pool_limit=candidate_limit,
        )
        return await self._finalize_retrieval(query, candidates)

    async def retrieve_deep(self, query: MemoryQuery) -> list[MemoryCandidate]:
        queries = [query.query]
        try:
            queries = await self._rewrite_queries(query)
            candidate_pool_limit = self._explicit_candidate_limit(query) or max(
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
            return await self._finalize_retrieval(query, candidates)
        except Exception as exc:
            logger.warning(f"[MemoryRetrievalService] deep retrieval degraded: {exc}")
            candidates = await self._retrieve_queries(
                query,
                [query.query],
                top_k=query.top_k,
                candidate_pool_limit=self._candidate_limit(query),
            )
            return await self._finalize_retrieval(query, candidates)

    async def _finalize_retrieval(
        self,
        query: MemoryQuery,
        candidates: list[MemoryCandidate],
    ) -> list[MemoryCandidate]:
        selected = self._finalize_candidates(query, candidates)
        try:
            finalize_access = getattr(self.store, "finalize_access", None)
            if finalize_access is not None:
                await finalize_access(selected, allow_stale=query.allow_stale)
        except Exception as exc:
            logger.warning(f"[MemoryRetrievalService] access tracking degraded: {exc}")
            self._mark_degraded(query, "access_tracking")
        return selected

    async def _retrieve_queries(
        self,
        query: MemoryQuery,
        queries: list[str],
        *,
        top_k: int | None = None,
        candidate_pool_limit: int | None = None,
    ) -> list[MemoryCandidate]:
        candidates: list[MemoryCandidate] = []
        seen: set[str] = set()
        self._trace_bucket(query)
        limit = max(int(top_k or query.top_k or 5), 1)
        collection_limit = max(int(candidate_pool_limit or limit), limit)
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
            if len(candidates) >= collection_limit:
                break  # ponytail: M11 — stop early instead of wasting more queries
        return candidates[:collection_limit]

    async def _retrieve_once(self, query: MemoryQuery) -> list[MemoryCandidate]:
        visibility_mode = str(query.metadata.get("visibility_mode") or "")
        query_layers = {str(item) for item in query.layers or [] if str(item).strip()}
        trace = self._trace_bucket(query)
        if query.intent == "jargon" or query_layers == {"jargon"}:
            search_step = {
                "layers": sorted(query_layers),
                "visibility_mode": visibility_mode,
                "allow_stale": bool(query.allow_stale),
            }
            if bool(query.metadata.get("memory_retrieval_debug_trace_enabled", False)):
                search_step["query"] = str(query.query or "")
            trace.setdefault("search_steps", []).append(search_step)
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
            search_step = {
                "layers": sorted(query_layers),
                "visibility_mode": visibility_mode,
                "allow_stale": bool(query.allow_stale),
            }
            if bool(query.metadata.get("memory_retrieval_debug_trace_enabled", False)):
                search_step["query"] = str(query.query or "")
            trace.setdefault("search_steps", []).append(search_step)
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
            candidate_limit=self._explicit_candidate_limit(query),
            exclude_ids=query.exclude_ids,
            allow_stale=query.allow_stale,
            visibility_mode=visibility_mode,
            track_access=False,
        )
        hybrid_task = self._hybrid_search(query, visibility_mode)
        canonical_results, hybrid_results = await asyncio.gather(canonical_task, hybrid_task, return_exceptions=True)
        if isinstance(canonical_results, Exception):
            logger.warning(f"[MemoryRetrievalService] canonical search degraded: {canonical_results}")
            self._mark_degraded(query, "canonical")
            canonical_results = []
        if isinstance(hybrid_results, Exception):
            logger.warning(f"[MemoryRetrievalService] hybrid search degraded: {hybrid_results}")
            self._mark_degraded(query, "hybrid")
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
                top_k=self._explicit_candidate_limit(query) or max(int(query.top_k or 5), 1),
                session_id=self._resolved_session_id(query),
                persona_id=query.persona_id or None,
            )
        except Exception as exc:
            logger.debug(f"[MemoryRetrievalService] hybrid engine search failed: {exc}")
            self._mark_degraded(query, "hybrid")
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
                hydrated = self._clone_candidate(canonical)
                hydrated.relevance_score = max(float(candidate.relevance_score or 0.0), float(hydrated.relevance_score or 0.0))
                hydrated.metadata["matched_by"] = sorted(
                    set(self._matched_sources(hydrated.metadata.get("matched_by")))
                    | set(self._matched_sources(candidate.metadata.get("matched_by")))
                )
                candidate = hydrated
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
        rrf_raw_scores: dict[str, float] = {}

        def identity(candidate: MemoryCandidate) -> str:
            return str((candidate.metadata or {}).get("canonical_id") or candidate.id or "")

        for rank, original in enumerate(canonical or [], start=1):
            c = self._clone_candidate(original)
            if c.id in excluded:
                continue
            key = identity(c)
            c.metadata["_canon_score"] = float(c.relevance_score or 0.0)
            c.metadata.setdefault("_hybrid_score", 0.0)
            c.metadata["matched_by"] = self._matched_sources(c.metadata.get("matched_by")) or ["canonical_fts"]
            merged[key] = c
            rrf_raw_scores[key] = rrf_raw_scores.get(key, 0.0) + self._rrf_source_weight(c, "canonical_fts") / (self._RRF_K + rank)
        for rank, original in enumerate(hybrid or [], start=1):
            h = self._clone_candidate(original)
            if h.id in excluded:
                continue
            key = identity(h)
            rrf_raw_scores[key] = rrf_raw_scores.get(key, 0.0) + self._rrf_source_weight(h, "legacy_hybrid") / (self._RRF_K + rank)
            if key in merged:
                existing = merged[key]
                existing.metadata["_hybrid_score"] = max(
                    float(existing.metadata.get("_hybrid_score", 0.0)),
                    float(h.relevance_score or 0.0),
                )
                existing.metadata["_canon_score"] = max(
                    float(existing.metadata.get("_canon_score", 0.0)),
                    float(existing.relevance_score or 0.0),
                )
                existing.metadata["matched_by"] = sorted(
                    set(self._matched_sources(existing.metadata.get("matched_by")))
                    | set(self._matched_sources(h.metadata.get("matched_by")))
                )
            else:
                h.metadata.setdefault("_canon_score", 0.0)
                h.metadata["_hybrid_score"] = float(h.relevance_score or 0.0)
                h.metadata["matched_by"] = self._matched_sources(h.metadata.get("matched_by")) or ["legacy_hybrid"]
                merged[key] = h

        rrf_enabled = bool((query.metadata or {}).get("memory_rrf_fusion_enabled", False))
        max_rrf_raw = max(rrf_raw_scores.values(), default=0.0)
        for item in merged.values():
            canon = float(item.metadata.get("_canon_score", 0.0))
            hybrid_score = float(item.metadata.get("_hybrid_score", 0.0))
            conflict_penalty = 0.0
            if (item.metadata or {}).get("corrected_by") or (item.metadata or {}).get("contradicted_by"):
                conflict_penalty = float(self.scoring.conflict_penalty or 0.0)
            importance_weighted = float(item.importance or 0.0) * self.scoring.importance_weight
            confidence_weighted = float(item.confidence or 0.0) * self.scoring.confidence_weight
            recency_weighted = float(item.recency_score or 0.0) * self.scoring.recency_weight
            stale_penalty = self.scoring.stale_penalty if item.status == "stale" else 0.0
            if rrf_enabled:
                key = identity(item)
                rrf_raw = float(rrf_raw_scores.get(key, 0.0))
                rrf_normalized = rrf_raw / max_rrf_raw if max_rrf_raw > 0.0 else 0.0
                rrf_weighted = rrf_normalized * (self.scoring.canonical_weight + self.scoring.hybrid_weight)
                item.relevance_score = (
                    rrf_weighted + importance_weighted + confidence_weighted + recency_weighted
                    - conflict_penalty - stale_penalty
                )
                item.metadata["_rrf_raw_score"] = round(rrf_raw, 8)
                item.metadata["_rrf_score"] = round(rrf_normalized, 6)
                item.metadata["_fusion_mode"] = "rrf"
                item.metadata["_score_breakdown"] = {
                    "rrf": round(rrf_weighted, 4),
                    "rrf_raw": round(rrf_raw, 8),
                    "importance": round(importance_weighted, 4),
                    "confidence": round(confidence_weighted, 4),
                    "recency": round(recency_weighted, 4),
                    "conflict_penalty": round(conflict_penalty, 4),
                    "stale_penalty": round(stale_penalty, 4),
                }
            else:
                canon_weighted = canon * self.scoring.canonical_weight
                hybrid_weighted = hybrid_score * self.scoring.hybrid_weight
                item.relevance_score = (
                    canon_weighted + hybrid_weighted + importance_weighted + confidence_weighted
                    - conflict_penalty - stale_penalty
                )
                item.metadata["_fusion_mode"] = "weighted"
                item.metadata["_score_breakdown"] = {
                    "canonical": round(canon_weighted, 4),
                    "hybrid": round(hybrid_weighted, 4),
                    "importance": round(importance_weighted, 4),
                    "confidence": round(confidence_weighted, 4),
                    "conflict_penalty": round(conflict_penalty, 4),
                    "stale_penalty": round(stale_penalty, 4),
                }
        ranked = sorted(merged.values(), key=self._stable_sort_key)
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
            self._record_query_rewrite_trace(query, status="gateway_unavailable", elapsed_ms=0.0, rewrite_count=0)
            return [base_query]
        timing = getattr(getattr(self.engine, "config", None), "timing", None)
        configured_timeout_sec = max(0.01, float(getattr(timing, "query_rewrite_timeout_sec", 8.0) or 8.0))
        timeout_sec = clamp_timeout_to_turn_budget(
            None,
            configured_timeout_sec,
            reserve_for_reply=True,
        )
        if timeout_sec <= 0.0:
            self._record_query_rewrite_trace(
                query,
                status="budget_exhausted",
                elapsed_ms=0.0,
                rewrite_count=0,
                configured_timeout_sec=configured_timeout_sec,
                effective_timeout_sec=0.0,
            )
            return [base_query]
        prompt = (
            "Rewrite the user memory search request into at most 3 short search queries. "
            "Return JSON only: {\"queries\": [\"...\"]}.\n"
            f"Request: {base_query}"
        )
        started = time.perf_counter()
        status = "success"
        cancellation_requested = False
        try:
            task = asyncio.create_task(
                gateway.call_data_process_task(
                    prompt=prompt,
                    is_json=True,
                    lane_key=LaneKey(
                        subsystem="bg",
                        task_family="query_rewrite",
                        scope_id="global",
                        scope_kind="global",
                    ),
                    timeout_override=timeout_sec,
                    max_retries_override=0,
                    max_models_override=1,
                    use_fallback=False,
                    allow_cooldown_override=False,
                    reserve_for_reply=True,
                )
            )
            done, _ = await asyncio.wait({task}, timeout=timeout_sec)
            if task not in done:
                cancellation_requested = True
                task.cancel()
                task.add_done_callback(self._consume_background_task)
                raise asyncio.TimeoutError("query rewrite hard deadline exceeded")
            response = task.result()
            if isinstance(response, str):
                response = json.loads(response)
            queries = response.get("queries", []) if isinstance(response, dict) else []
            cleaned = [str(item).strip() for item in queries if str(item).strip()]
            if not cleaned:
                status = "empty"
        except Exception as exc:
            logger.warning(f"[MemoryRetrievalService] deep query rewrite degraded: {exc}")
            cleaned = []
            status = "timeout" if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in str(exc).lower() else "error"
        result = [base_query]
        for item in cleaned:
            if item not in result:
                result.append(item)
            if len(result) >= 3:
                break
        self._record_query_rewrite_trace(
            query,
            status=status,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            rewrite_count=max(0, len(result) - 1),
            configured_timeout_sec=configured_timeout_sec,
            effective_timeout_sec=timeout_sec,
            cancellation_requested=cancellation_requested,
        )
        return result

    @staticmethod
    def _consume_background_task(task: asyncio.Task) -> None:
        try:
            task.result()
        except BaseException:
            pass

    @staticmethod
    def _record_query_rewrite_trace(
        query: MemoryQuery,
        *,
        status: str,
        elapsed_ms: float,
        rewrite_count: int,
        configured_timeout_sec: float = 0.0,
        effective_timeout_sec: float = 0.0,
        cancellation_requested: bool = False,
    ) -> None:
        metadata = dict(query.metadata or {})
        metadata["query_rewrite_trace"] = {
            "status": str(status or "unknown"),
            "elapsed_ms": round(max(0.0, float(elapsed_ms or 0.0)), 2),
            "rewrite_count": max(0, int(rewrite_count or 0)),
            "original_query_fallback": str(status or "") != "success",
            "configured_timeout_sec": round(max(0.0, float(configured_timeout_sec or 0.0)), 3),
            "effective_timeout_sec": round(max(0.0, float(effective_timeout_sec or 0.0)), 3),
            "cancellation_requested": bool(cancellation_requested),
        }
        query.metadata = metadata

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
        original_top = max((float(item.relevance_score or 0.0) for item in candidates), default=0.0)
        for index, item in enumerate(ranked):
            base_score = max(0.0, min(1.0, float(item.relevance_score or 0.0)))
            positional_boost = min(base_score * 0.2, max(0.0, 0.08 - index * 0.01))
            final_score = min(1.0, base_score + positional_boost)
            if original_top > 0.0 and base_score < original_top * 0.5:
                final_score = min(final_score, max(original_top - 1e-6, 0.0))
            item.metadata = dict(item.metadata or {})
            item.metadata["_base_relevance_score"] = round(base_score, 6)
            item.metadata["_deep_rerank_boost"] = round(positional_boost, 6)
            item.metadata["_final_relevance_score"] = round(final_score, 6)
            item.relevance_score = final_score
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
