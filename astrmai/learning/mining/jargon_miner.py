from __future__ import annotations

from astrbot.api import logger

from ..dedup import GLOBAL_CANDIDATE_REGISTRY, jargon_fingerprint, normalize_jargon_term
from .jargon_candidate_extractor import JargonCandidateExtractor
from .jargon_enricher import JargonEnricher
from .jargon_identity import resolve_jargon_identity
from .learning_input_policy import LearningInputPolicy
from typing import Any, Iterable, List, Sequence


class JargonMiner:
    def __init__(self, expression_miner, min_messages: int = 1, memory_engine=None):
        self.expression_miner = expression_miner
        self.min_messages = max(int(min_messages or 1), 1)
        self.memory_engine = memory_engine
        gateway = getattr(expression_miner, "gateway", None)
        config = getattr(expression_miner, "config", None)
        self.candidate_extractor = JargonCandidateExtractor(
            min_count=getattr(getattr(config, "evolution", None), "jargon_min_count", 2)
        )
        self.enricher = JargonEnricher(gateway, config=config) if gateway is not None else None
        self.input_policy = LearningInputPolicy()
        self.last_report: dict[str, Any] = {}

    def normalize_messages(self, messages: Iterable | None) -> List:
        if not messages:
            return []
        return list(LearningInputPolicy().normalize(messages))

    _normalize_messages = normalize_messages

    async def _existing_expression_terms(self, group_id: str) -> set[str]:
        service = getattr(self.memory_engine, "expression_pattern_service", None) if self.memory_engine else None
        if not service or not hasattr(service, "list_patterns"):
            return set()
        try:
            rows = await service.list_patterns(
                group_id,
                limit=2000,
                only_checked=False,
                include_rejected=False,
                statuses=["active", "review_pending", "stale"],
            )
            return {
                normalized
                for item in rows
                for normalized in [normalize_jargon_term(getattr(item, "expression", ""))]
                if normalized
            }
        except Exception as exc:
            logger.debug(f"[JargonMiner] expression preload degraded: {exc}")
            return set()

    async def mine(self, group_id: str, messages: Sequence | None):
        if not group_id or self.expression_miner is None:
            self.last_report = {"group_id": group_id, "candidate_count": 0, "reason": "miner_unavailable"}
            return []
        normalized = self.input_policy.normalize(messages)
        if len(normalized) < self.min_messages:
            self.last_report = {
                "group_id": group_id,
                "input_messages": len(messages or []),
                "normalized_messages": len(normalized),
                "min_messages": self.min_messages,
                "candidate_count": 0,
                "reason": "insufficient_context",
                "input_policy": dict(self.input_policy.last_report),
            }
            return []
        existing_terms: dict[str, str] = {}
        existing_records: list[Any] = []
        store = getattr(getattr(self.memory_engine, "v2_store", None), "list_candidates", None)
        if callable(store):
            try:
                rows = await self.memory_engine.v2_store.list_candidates(
                    session_id="",
                    kinds=["jargon"],
                    statuses=["active", "review_pending", "rejected", "stale"],
                    limit=10000,
                )
                existing_records = list(rows or [])
                for item in existing_records:
                    metadata = dict(item.metadata or {})
                    canonical = str(item.content or metadata.get("canonical_term") or "").strip()
                    for term in [canonical, *(metadata.get("surface_forms") or []), *(metadata.get("aliases") or [])]:
                        normalized_term = normalize_jargon_term(term)
                        if normalized_term:
                            existing_terms[normalized_term] = canonical or str(term).strip()
            except Exception as exc:
                logger.debug(f"[JargonMiner] canonical jargon preload degraded: {exc}")
        expression_terms = await self._existing_expression_terms(group_id)
        candidates = await self.candidate_extractor.extract(
            group_id,
            normalized,
            existing_terms=existing_terms,
            blocked_terms=expression_terms,
        )
        for candidate in candidates:
            observed = str(candidate.get("content") or "")
            canonical, similarity = resolve_jargon_identity(observed, existing_records)
            candidate["canonical_form"] = canonical or observed
            candidate["identity_similarity"] = similarity
            candidate["existing_identity"] = bool(similarity >= 0.9)
            candidate["surface_forms"] = list(
                dict.fromkeys([canonical or observed, observed, *(candidate.get("surface_forms") or [])])
            )[:12]
        if not candidates:
            self.last_report = {
                "group_id": group_id,
                "normalized_messages": len(normalized),
                "existing_terms": len(existing_terms),
                "expression_terms": len(expression_terms),
                **dict(getattr(self.candidate_extractor, "last_report", {}) or {}),
                "input_policy": dict(self.input_policy.last_report),
            }
            return []
        if not self.enricher:
            self.last_report = {
                "group_id": group_id,
                "normalized_messages": len(normalized),
                "existing_terms": len(existing_terms),
                "expression_terms": len(expression_terms),
                **dict(getattr(self.candidate_extractor, "last_report", {}) or {}),
                "enriched_count": len(candidates),
                "reason": "completed_without_enricher",
                "input_policy": dict(self.input_policy.last_report),
            }
            return candidates
        candidate_fingerprints = {
            jargon_fingerprint(str(item.get("canonical_form") or item.get("content") or "")): item
            for item in candidates
        }
        claimed, in_flight = GLOBAL_CANDIDATE_REGISTRY.claim(candidate_fingerprints)
        candidates = [
            item
            for item in candidates
            if jargon_fingerprint(str(item.get("canonical_form") or item.get("content") or "")) in claimed
        ]
        if not candidates:
            self.last_report = {
                "group_id": group_id,
                "existing_terms": len(existing_terms),
                "candidate_count": 0,
                "skipped_in_flight": len(in_flight),
                "reason": "all_candidates_in_flight",
                "input_policy": dict(self.input_policy.last_report),
            }
            return []
        try:
            enrichment_result = await self.enricher.enrich(group_id, candidates)
        finally:
            GLOBAL_CANDIDATE_REGISTRY.release(claimed)
        if isinstance(enrichment_result, list):
            enriched = list(enrichment_result)
            enrichment_report = {
                "status": "completed",
                "terminal": True,
                "retryable": False,
                "reason": "legacy_enricher_result",
                "input_count": len(candidates),
                "accepted_count": len(enriched),
            }
            enrichment_reason = "completed"
        else:
            enriched = list(enrichment_result.items)
            enrichment_report = enrichment_result.to_report()
            enrichment_reason = enrichment_result.reason
        self.last_report = {
            "group_id": group_id,
            "normalized_messages": len(normalized),
            "existing_terms": len(existing_terms),
            "expression_terms": len(expression_terms),
            "skipped_in_flight": len(in_flight),
            **dict(getattr(self.candidate_extractor, "last_report", {}) or {}),
            "input_policy": dict(self.input_policy.last_report),
            "enriched_count": len(enriched),
            "identity_merged_candidates": sum(
                bool(item.get("existing_identity")) for item in enriched if isinstance(item, dict)
            ),
            "multi_sense_candidates": sum(
                len(item.get("proposed_senses") or []) > 1 for item in enriched if isinstance(item, dict)
            ),
            "proposed_sense_count": sum(
                len(item.get("proposed_senses") or []) for item in enriched if isinstance(item, dict)
            ),
            "reason": enrichment_reason,
            "enrichment": enrichment_report,
        }
        return enriched
