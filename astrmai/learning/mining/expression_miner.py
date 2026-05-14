from __future__ import annotations

from typing import Any, List

from astrbot.api import logger

from ...infrastructure.persistence import MessageLog
from .expression_candidate_extractor import ExpressionCandidateExtractor
from .expression_pattern_enricher import ExpressionPatternEnricher


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
        self.enricher = ExpressionPatternEnricher(gateway, config=self.config)

    @staticmethod
    def _normalize_messages(messages: List[MessageLog]) -> list[MessageLog]:
        normalized: list[MessageLog] = []
        for message in messages or []:
            if message is None:
                continue
            content = str(getattr(message, "content", "") or "").strip()
            if not content or content.startswith("[") or len(content) > 100:
                continue
            normalized.append(message)
        return normalized

    async def _existing_patterns(self, group_id: str) -> set[str]:
        service = getattr(self.memory_engine, "expression_pattern_service", None) if self.memory_engine else None
        if not service or not hasattr(service, "list_patterns"):
            return set()
        try:
            rows = await service.list_patterns(
                group_id,
                limit=200,
                only_checked=False,
                include_rejected=True,
                statuses=["active", "review_pending", "rejected", "stale"],
            )
            return {service.normalize_text(getattr(item, "expression", "")) for item in rows if getattr(item, "expression", "")}
        except Exception as exc:
            logger.debug(f"[ExpressionMiner] canonical preload degraded: {exc}")
            return set()

    async def mine(self, group_id: str, messages: List[MessageLog]) -> list[dict[str, Any]]:
        min_context = getattr(self.config.evolution, "min_mining_context", 10)
        normalized = self._normalize_messages(messages)
        if len(normalized) < min_context:
            return []
        existing = await self._existing_patterns(group_id)
        candidates = await self.candidate_extractor.extract(
            group_id,
            normalized,
            existing_patterns=existing,
        )
        if not candidates:
            return []
        enriched = await self.enricher.enrich(group_id, candidates)
        logger.info(f"[ExpressionMiner] 表达习惯挖掘完成: {group_id} -> patterns={len(enriched)}")
        return enriched

    async def mine_bundle(self, group_id: str, messages: List[MessageLog]) -> dict[str, list[Any]]:
        return {"patterns": await self.mine(group_id, messages), "jargons": []}

    async def mine_jargons(self, group_id: str, messages: List[MessageLog]) -> list[Any]:
        return []


__all__ = ["ExpressionMiner"]
