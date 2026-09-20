from __future__ import annotations

import hashlib
import math
from astrbot.api import logger

from ..dedup import GLOBAL_CANDIDATE_REGISTRY, jargon_fingerprint, normalize_jargon_term
from ..quality.contracts import LearningQualityProfile
from .candidate_quality import scan_jargon_shadow
from .jargon_candidate_extractor import JargonCandidateExtractor
from .jargon_enricher import JargonEnricher
from .jargon_identity import resolve_jargon_identity
from .learning_input_policy import LearningInputPolicy
from .learning_attribution import LearningAttributionAdapter
from .learning_evidence import durable_message_evidence_id
from typing import Any, Iterable, List, Sequence


class JargonMiner:
    def __init__(
        self,
        expression_miner,
        min_messages: int = 1,
        memory_engine=None,
        background_task_budget=None,
        provider_adapter=None,
    ):
        self.expression_miner = expression_miner
        self.min_messages = max(int(min_messages or 1), 1)
        self.memory_engine = memory_engine
        gateway = getattr(expression_miner, "gateway", None)
        config = getattr(expression_miner, "config", None)
        self.candidate_extractor = JargonCandidateExtractor(
            min_count=getattr(getattr(config, "evolution", None), "jargon_min_count", 2)
        )
        self.enricher = (
            JargonEnricher(
                gateway,
                config=config,
                background_task_budget=background_task_budget,
                provider_adapter=provider_adapter,
            )
            if gateway is not None
            else None
        )
        self.input_policy = LearningInputPolicy()
        self.last_report: dict[str, Any] = {}

    def normalize_messages(self, messages: Iterable | None) -> List:
        if not messages:
            return []
        return list(LearningInputPolicy().normalize(messages))

    _normalize_messages = normalize_messages

    def _quality_shadow(self, messages: list[Any]) -> dict[str, Any] | None:
        config = getattr(self.expression_miner, "config", None)
        evolution = getattr(config, "evolution", None)
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
            adapter = LearningAttributionAdapter()
            facts: list[dict[str, Any]] = []
            timestamps: list[float] = []
            for message in messages:
                attribution = adapter.attribute(message)
                try:
                    timestamp = float(getattr(message, "timestamp", 0.0) or 0.0)
                except (TypeError, ValueError):
                    timestamp = float("nan")
                if math.isfinite(timestamp):
                    timestamps.append(timestamp)
                facts.append(
                    {
                        "source_id": durable_message_evidence_id(message),
                        "content": str(getattr(message, "content", "") or ""),
                        "timestamp": timestamp,
                        "eligible": bool(
                            getattr(message, "learning_evidence_eligible", True)
                            and attribution.evidence_eligible
                        ),
                    }
                )
            profile = LearningQualityProfile.quality_v1()
            if not timestamps:
                return {
                    "status": "insufficient_data",
                    "reason": "timestamp_unavailable",
                    "profile_version": profile.version,
                    "profile_hash": profile.parameters_hash,
                    "provider_call_count": 0,
                }
            scan = scan_jargon_shadow(
                facts,
                profile=profile,
                window_end=math.nextafter(max(timestamps), math.inf),
            )
            candidates = scan.pop("candidates", {})
            decision_counts: dict[str, int] = {}
            ranked: list[dict[str, Any]] = []
            for term, item in candidates.items():
                if int(item["support_count"]) < int(profile.parameters["jargon_min_support"]):
                    decision = "insufficient_data"
                elif (
                    item["left_entropy_bits"] is not None
                    and item["right_entropy_bits"] is not None
                    and float(item["left_entropy_bits"]) >= float(profile.parameters["jargon_min_left_entropy_bits"])
                    and float(item["right_entropy_bits"]) >= float(profile.parameters["jargon_min_right_entropy_bits"])
                ):
                    decision = "high_confidence_shadow"
                else:
                    decision = "low_confidence_shadow"
                decision_counts[decision] = decision_counts.get(decision, 0) + 1
                ranked.append(
                    {
                        "candidate_key": hashlib.sha256(
                            term.encode("utf-8")
                        ).hexdigest()[:16],
                        "decision": decision,
                        **item,
                    }
                )
            ranked.sort(
                key=lambda item: (
                    -int(item["support_count"]),
                    -float(item["burst_ratio"] or 0.0),
                    str(item["candidate_key"]),
                )
            )
            return {
                **scan["report"],
                "decision_counts": dict(sorted(decision_counts.items())),
                "top_candidates": ranked[:20],
            }
        except Exception as exc:
            logger.warning(f"[JargonMiner] quality shadow degraded: {exc}")
            return {
                "status": "partial",
                "reason": "shadow_error",
                "error_type": type(exc).__name__,
                "provider_call_count": 0,
            }

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
            self.last_report = {
                "group_id": group_id,
                "candidate_count": 0,
                "reason": "miner_unavailable",
                "discovery_provider_call_count": 0,
                "pipeline_contains_enrichment": False,
            }
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
                "discovery_provider_call_count": 0,
                "pipeline_contains_enrichment": False,
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
        quality_shadow = self._quality_shadow(normalized)
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
                "discovery_provider_call_count": 0,
                "pipeline_contains_enrichment": False,
                **({"quality_shadow": quality_shadow} if quality_shadow is not None else {}),
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
                "discovery_provider_call_count": 0,
                "pipeline_contains_enrichment": True,
                "provider_attempt": {"provider_attempt": 0, "reason": "enricher_unavailable"},
                **({"quality_shadow": quality_shadow} if quality_shadow is not None else {}),
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
                "discovery_provider_call_count": 0,
                "pipeline_contains_enrichment": False,
                **({"quality_shadow": quality_shadow} if quality_shadow is not None else {}),
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
            "discovery_provider_call_count": 0,
            "pipeline_contains_enrichment": True,
            "provider_attempt": (
                self.enricher.last_provider_attempt.to_report()
                if getattr(self.enricher, "last_provider_attempt", None) is not None
                else {"provider_attempt": None, "source": "legacy_gateway_compatibility"}
            ),
            **({"quality_shadow": quality_shadow} if quality_shadow is not None else {}),
        }
        return enriched
