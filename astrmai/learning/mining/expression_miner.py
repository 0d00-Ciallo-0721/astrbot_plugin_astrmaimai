from __future__ import annotations

from typing import Any, List

from astrbot.api import logger

from ...infrastructure.persistence import MessageLog
from ..dedup import GLOBAL_CANDIDATE_REGISTRY, expression_fingerprint
from .expression_candidate_extractor import ExpressionCandidateExtractor
from .expression_pattern_enricher import ExpressionPatternEnricher
from .expression_results import ExpressionEnrichmentResult
from .learning_input_policy import LearningInputPolicy


class ExpressionMiner:
    """
    表达习惯挖掘器。

    现在采用“确定性候选提取 + LLM 增强”两段式，不再和黑话共用 joint prompt。
    """

    def __init__(self, gateway, config=None, memory_engine=None):
        self.gateway = gateway
        self.config = config if config else gateway.config
        self.memory_engine = memory_engine
        evolution_config = getattr(self.config, "evolution", None)
        self.candidate_extractor = ExpressionCandidateExtractor(
            min_count=getattr(evolution_config, "expression_min_count", 2)
        )
        self.expression_min_distinct_turns = max(
            int(getattr(evolution_config, "expression_min_distinct_turns", 3) or 3),
            int(getattr(evolution_config, "expression_min_count", 2) or 2),
        )
        self.enricher = ExpressionPatternEnricher(gateway, config=self.config)
        self.input_policy = LearningInputPolicy()
        self.last_report: dict[str, Any] = {}
        self.last_result = ExpressionEnrichmentResult(status="completed", reason="not_run")

    @staticmethod
    def normalize_messages(messages: List[MessageLog]) -> list[MessageLog]:
        return list(LearningInputPolicy().normalize(messages))

    _normalize_messages = normalize_messages

    async def _existing_patterns(self, group_id: str) -> set[str]:
        service = getattr(self.memory_engine, "expression_pattern_service", None) if self.memory_engine else None
        if not service or not hasattr(service, "list_patterns"):
            return set()
        try:
            rows = await service.list_patterns(
                group_id,
                limit=2000,
                only_checked=False,
                include_rejected=True,
                statuses=["active", "review_pending", "rejected", "stale"],
            )
            return {
                service.normalize_text(getattr(item, "expression", ""))
                for item in rows
                if getattr(item, "expression", "")
            }
        except Exception as exc:
            logger.debug(f"[ExpressionMiner] canonical preload degraded: {exc}")
            return set()

    async def mine(self, group_id: str, messages: List[MessageLog]) -> list[dict[str, Any]]:
        min_context = max(
            1,
            int(
                getattr(
                    self.config.evolution,
                    "expression_min_valid_messages",
                    getattr(self.config.evolution, "min_mining_context", 10),
                )
                or 30
            ),
        )
        normalized = self.input_policy.normalize(messages)
        if len(normalized) < min_context:
            self.last_result = ExpressionEnrichmentResult(
                status="completed",
                reason="insufficient_context",
            )
            self.last_report = {
                "group_id": group_id,
                "input_messages": len(messages or []),
                "normalized_messages": len(normalized),
                "min_context": min_context,
                "candidate_count": 0,
                "enriched_count": 0,
                "reason": "insufficient_context",
                "input_policy": dict(self.input_policy.last_report),
            }
            return []
        existing = await self._existing_patterns(group_id)
        candidates = await self.candidate_extractor.extract(
            group_id,
            normalized,
            existing_patterns=existing,
        )
        if not candidates:
            self.last_result = ExpressionEnrichmentResult(
                status="completed",
                reason="no_candidates",
            )
            self.last_report = {
                "group_id": group_id,
                "input_messages": len(messages or []),
                "normalized_messages": len(normalized),
                "min_context": min_context,
                "existing_patterns": len(existing),
                **dict(self.candidate_extractor.last_report or {}),
                "input_policy": dict(self.input_policy.last_report),
                "enriched_count": 0,
            }
            return []
        min_distinct_turns = self.expression_min_distinct_turns
        candidates = [
            item
            for item in candidates
            if (
                "distinct_turn_count" not in item
                and "evidence_message_ids" not in item
            )
            or int(item.get("distinct_turn_count") or len(item.get("evidence_message_ids") or []) or 0) >= min_distinct_turns
        ]
        if not candidates:
            self.last_result = ExpressionEnrichmentResult(
                status="completed",
                reason="insufficient_distinct_expression_evidence",
            )
            self.last_report = {
                "group_id": group_id,
                "input_messages": len(messages or []),
                "normalized_messages": len(normalized),
                "min_distinct_turns": min_distinct_turns,
                "candidate_count": 0,
                "enriched_count": 0,
                "reason": "insufficient_distinct_expression_evidence",
                "input_policy": dict(self.input_policy.last_report),
            }
            return []
        candidate_fingerprints = {
            expression_fingerprint(
                group_id,
                str(item.get("habit_type") or "sentence_pattern"),
                str(item.get("normalized_expression") or item.get("expression") or ""),
                str(item.get("situation") or "日常回应"),
            ): item
            for item in candidates
        }
        claimed, in_flight = GLOBAL_CANDIDATE_REGISTRY.claim(candidate_fingerprints)
        candidates = [
            item
            for item in candidates
            if expression_fingerprint(
                group_id,
                str(item.get("habit_type") or "sentence_pattern"),
                str(item.get("normalized_expression") or item.get("expression") or ""),
                str(item.get("situation") or "日常回应"),
            ) in claimed
        ]
        if not candidates:
            self.last_result = ExpressionEnrichmentResult(status="completed", reason="all_candidates_in_flight")
            self.last_report = {
                "group_id": group_id,
                "candidate_count": 0,
                "skipped_in_flight": len(in_flight),
                "enriched_count": 0,
                "reason": "all_candidates_in_flight",
                "input_policy": dict(self.input_policy.last_report),
            }
            return []
        try:
            enrichment = await self.enricher.enrich(group_id, candidates)
        finally:
            GLOBAL_CANDIDATE_REGISTRY.release(claimed)
        if isinstance(enrichment, ExpressionEnrichmentResult):
            self.last_result = enrichment
            enriched = list(enrichment.items)
        else:
            # Compatibility for tests and third-party wrappers that still return the old list contract.
            enriched = list(enrichment or [])
            self.last_result = ExpressionEnrichmentResult(
                status="completed" if enriched else "all_rejected",
                items=enriched,
                input_count=len(candidates),
                returned_count=len(enriched),
                rejected_count=max(len(candidates) - len(enriched), 0),
                reason="legacy_enricher_result",
            )
        self.last_report = {
            "group_id": group_id,
            "input_messages": len(messages or []),
            "normalized_messages": len(normalized),
            "min_context": min_context,
            "existing_patterns": len(existing),
            "skipped_in_flight": len(in_flight),
            **dict(self.candidate_extractor.last_report or {}),
            "input_policy": dict(self.input_policy.last_report),
            "enriched_count": len(enriched),
            "reason": self.last_result.reason,
            "enrichment": self.last_result.to_report(),
        }
        logger.info(
            f"[ExpressionMiner] 表达习惯挖掘完成: {group_id} -> "
            f"status={self.last_result.status}, patterns={len(enriched)}"
        )
        return enriched

    async def mine_bundle(self, group_id: str, messages: List[MessageLog]) -> dict[str, list[Any]]:
        return {"patterns": await self.mine(group_id, messages), "jargons": []}

    async def mine_jargons(self, group_id: str, messages: List[MessageLog]) -> list[Any]:
        return []


__all__ = ["ExpressionMiner"]
