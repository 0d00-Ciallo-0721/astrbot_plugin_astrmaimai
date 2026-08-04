from __future__ import annotations

from astrbot.api import logger

from ..dedup import GLOBAL_CANDIDATE_REGISTRY, jargon_fingerprint, normalize_jargon_term
from .jargon_candidate_extractor import JargonCandidateExtractor
from .jargon_enricher import JargonEnricher
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
        self.last_report: dict[str, Any] = {}

    def _normalize_messages(self, messages: Iterable | None) -> List:
        if not messages:
            return []
        normalized = []
        for message in messages:
            if message is None:
                continue
            content = getattr(message, 'content', None)
            if content is None:
                normalized.append(message)
                continue
            if str(content).strip():
                normalized.append(message)
        return normalized

    async def mine(self, group_id: str, messages: Sequence | None):
        if not group_id or self.expression_miner is None:
            self.last_report = {"group_id": group_id, "candidate_count": 0, "reason": "miner_unavailable"}
            return []
        normalized = self._normalize_messages(messages)
        if len(normalized) < self.min_messages:
            self.last_report = {
                "group_id": group_id,
                "input_messages": len(messages or []),
                "normalized_messages": len(normalized),
                "min_messages": self.min_messages,
                "candidate_count": 0,
                "reason": "insufficient_context",
            }
            return []
        existing_terms = set()
        store = getattr(getattr(self.memory_engine, "v2_store", None), "list_candidates", None)
        if callable(store):
            try:
                rows = await self.memory_engine.v2_store.list_candidates(
                    session_id="",
                    kinds=["jargon"],
                    statuses=["active", "review_pending", "rejected", "stale"],
                    limit=10000,
                )
                existing_terms = {
                    normalize_jargon_term(term)
                    for item in rows
                    for term in [item.content, *(dict(item.metadata or {}).get("aliases", []) or [])]
                    if normalize_jargon_term(term)
                }
            except Exception as exc:
                logger.debug(f"[JargonMiner] canonical jargon preload degraded: {exc}")
        candidates = await self.candidate_extractor.extract(group_id, normalized, existing_terms=existing_terms)
        if not candidates:
            self.last_report = {
                "group_id": group_id,
                "normalized_messages": len(normalized),
                "existing_terms": len(existing_terms),
                **dict(getattr(self.candidate_extractor, "last_report", {}) or {}),
            }
            return []
        if not self.enricher:
            self.last_report = {
                "group_id": group_id,
                "normalized_messages": len(normalized),
                "existing_terms": len(existing_terms),
                **dict(getattr(self.candidate_extractor, "last_report", {}) or {}),
                "enriched_count": len(candidates),
                "reason": "completed_without_enricher",
            }
            return candidates
        candidate_fingerprints = {
            jargon_fingerprint(str(item.get("content") or "")): item
            for item in candidates
        }
        claimed, in_flight = GLOBAL_CANDIDATE_REGISTRY.claim(candidate_fingerprints)
        candidates = [
            item
            for item in candidates
            if jargon_fingerprint(str(item.get("content") or "")) in claimed
        ]
        if not candidates:
            self.last_report = {
                "group_id": group_id,
                "existing_terms": len(existing_terms),
                "candidate_count": 0,
                "skipped_in_flight": len(in_flight),
                "reason": "all_candidates_in_flight",
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
            "skipped_in_flight": len(in_flight),
            **dict(getattr(self.candidate_extractor, "last_report", {}) or {}),
            "enriched_count": len(enriched),
            "reason": enrichment_reason,
            "enrichment": enrichment_report,
        }
        return enriched
