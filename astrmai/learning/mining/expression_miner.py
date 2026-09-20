from __future__ import annotations

import math
from typing import Any, List

from astrbot.api import logger

from ...infrastructure.persistence import MessageLog
from ..dedup import GLOBAL_CANDIDATE_REGISTRY, expression_fingerprint
from ..quality.contracts import LearningQualityProfile
from .candidate_quality import build_expression_shadow_report
from .expression_candidate_extractor import ExpressionCandidateExtractor
from .expression_pattern_enricher import ExpressionPatternEnricher
from .expression_results import ExpressionEnrichmentResult
from .learning_input_policy import LearningInputPolicy
from .learning_attribution import LearningAttributionAdapter
from .learning_evidence import durable_message_evidence_id, message_evidence_id


class ExpressionMiner:
    """
    表达习惯挖掘器。

    现在采用“确定性候选提取 + LLM 增强”两段式，不再和黑话共用 joint prompt。
    """

    def __init__(
        self,
        gateway,
        config=None,
        memory_engine=None,
        background_task_budget=None,
        provider_adapter=None,
    ):
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
        self.enricher = ExpressionPatternEnricher(
            gateway,
            config=self.config,
            background_task_budget=background_task_budget,
            provider_adapter=provider_adapter,
        )
        self.input_policy = LearningInputPolicy()
        self.last_report: dict[str, Any] = {}
        self.last_result = ExpressionEnrichmentResult(status="completed", reason="not_run")

    @staticmethod
    def normalize_messages(messages: List[MessageLog]) -> list[MessageLog]:
        return list(LearningInputPolicy().normalize(messages))

    _normalize_messages = normalize_messages

    def _quality_shadow(
        self,
        candidates: list[dict[str, Any]],
        messages: list[Any],
    ) -> dict[str, Any] | None:
        evolution = getattr(self.config, "evolution", None)
        if not bool(getattr(evolution, "learning_quality_shadow_enabled", True)):
            return None
        profile_version = str(
            getattr(evolution, "learning_quality_profile_version", "quality-v1")
            or "quality-v1"
        )
        if profile_version != "quality-v1":
            return {
                "status": "blocked",
                "reason": "unknown_profile",
                "profile_version": profile_version,
                "provider_call_count": 0,
            }
        try:
            profile = LearningQualityProfile.quality_v1()
            adapter = LearningAttributionAdapter()
            facts: list[dict[str, Any]] = []
            identity_aliases: dict[str, str] = {}
            timestamps: list[float] = []
            for index, message in enumerate(messages):
                attribution = adapter.attribute(message)
                display_id = message_evidence_id(message, fallback_index=index)
                durable_id = durable_message_evidence_id(message)
                previous = identity_aliases.get(display_id)
                if previous is not None and previous != durable_id:
                    return {
                        "status": "blocked",
                        "reason": "source_identity_conflict",
                        "provider_call_count": 0,
                    }
                identity_aliases[display_id] = durable_id
                try:
                    timestamp = float(getattr(message, "timestamp", 0.0) or 0.0)
                except (TypeError, ValueError):
                    timestamp = float("nan")
                if math.isfinite(timestamp):
                    timestamps.append(timestamp)
                facts.append(
                    {
                        "source_id": durable_id,
                        "timestamp": timestamp,
                        "eligible": bool(
                            getattr(message, "learning_evidence_eligible", True)
                            and attribution.evidence_eligible
                        ),
                        "known_scope": bool(attribution.scope_id),
                        "eligible_for_speaker_stats": attribution.eligible_for_speaker_stats,
                        "speaker_scope_id": attribution.speaker_scope_id,
                    }
                )
            if not timestamps:
                return {
                    "status": "insufficient_data",
                    "reason": "timestamp_unavailable",
                    "profile_version": profile.version,
                    "profile_hash": profile.parameters_hash,
                    "provider_call_count": 0,
                }
            typed_candidates = [
                {
                    **candidate,
                    "source_message_ids": [
                        identity_aliases.get(str(source_id), str(source_id))
                        for source_id in candidate.get("source_message_ids", ())
                    ],
                }
                for candidate in candidates
            ]
            return build_expression_shadow_report(
                typed_candidates,
                facts,
                profile=profile,
                window_end=math.nextafter(max(timestamps), math.inf),
            )
        except Exception as exc:
            logger.warning(f"[ExpressionMiner] quality shadow degraded: {exc}")
            return {
                "status": "partial",
                "reason": "shadow_error",
                "error_type": type(exc).__name__,
                "provider_call_count": 0,
            }

    async def _existing_patterns(self, group_id: str) -> set[str]:
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
                "discovery_provider_call_count": 0,
                "pipeline_contains_enrichment": False,
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
            quality_shadow = self._quality_shadow([], normalized)
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
                "discovery_provider_call_count": 0,
                "pipeline_contains_enrichment": False,
                **({"quality_shadow": quality_shadow} if quality_shadow is not None else {}),
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
        quality_shadow = self._quality_shadow(candidates, normalized)
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
                "discovery_provider_call_count": 0,
                "pipeline_contains_enrichment": False,
                "input_policy": dict(self.input_policy.last_report),
                **({"quality_shadow": quality_shadow} if quality_shadow is not None else {}),
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
                "discovery_provider_call_count": 0,
                "pipeline_contains_enrichment": False,
                "input_policy": dict(self.input_policy.last_report),
                **({"quality_shadow": quality_shadow} if quality_shadow is not None else {}),
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
            "discovery_provider_call_count": 0,
            "pipeline_contains_enrichment": True,
            "provider_attempt": (
                self.enricher.last_provider_attempt.to_report()
                if getattr(self.enricher, "last_provider_attempt", None) is not None
                else {"provider_attempt": None, "source": "legacy_gateway_compatibility"}
            ),
            **({"quality_shadow": quality_shadow} if quality_shadow is not None else {}),
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
