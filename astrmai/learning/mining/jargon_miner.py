from __future__ import annotations

from astrbot.api import logger

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
                    session_id=group_id,
                    kinds=["jargon"],
                    statuses=["active", "review_pending", "rejected", "stale"],
                    limit=200,
                )
                existing_terms = {str(item.content or "").strip().lower() for item in rows if str(item.content or "").strip()}
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
        enriched = await self.enricher.enrich(group_id, candidates)
        self.last_report = {
            "group_id": group_id,
            "normalized_messages": len(normalized),
            "existing_terms": len(existing_terms),
            **dict(getattr(self.candidate_extractor, "last_report", {}) or {}),
            "enriched_count": len(enriched),
            "reason": "completed" if enriched else "enrichment_empty",
        }
        return enriched
