from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, replace
from typing import Any, Mapping

from ..persistence.review_repository import LearningReviewRepository, ReviewMutation
from .contracts import (
    DECISIONS,
    REASONS,
    ReviewDecision,
    reviewer_model_identity,
    sanitize_diagnostics,
)


@dataclass(frozen=True, slots=True)
class ReviewWorkRequest:
    candidate_id: str
    candidate_revision: int
    scope_id: str
    reviewer_id: str
    reviewer_kind: str
    reviewer_attempt_id: str
    owner: str
    rubric_version: str
    prompt_version: str
    pair_order: str
    prompt: str
    system_prompt: str
    source_evidence_ids: tuple[str, ...]
    source_example_ids: tuple[str, ...]
    reviewer_profile_version: str
    model_example_ids: tuple[str, ...] = ()
    lease_seconds: float = 90.0


@dataclass(frozen=True, slots=True)
class ReviewWorkResult:
    status: str
    failure_stage: str = ""
    failure_kind: str = ""
    retryable: bool = False
    decision_id: str = ""
    mutation: ReviewMutation | None = None
    provider_report: Mapping[str, Any] | None = None


class ReviewOrchestrator:
    """Single review execution path over the shared learning Provider adapter."""

    def __init__(
        self, repository: LearningReviewRepository, provider_adapter, *,
        automatic_quorum_enabled: bool = False,
        expected_reviewer_ids: tuple[str, ...] = (),
    ) -> None:
        self.repository = repository
        self.provider_adapter = provider_adapter
        self.automatic_quorum_enabled = bool(automatic_quorum_enabled)
        self.expected_reviewer_ids = tuple(expected_reviewer_ids)
        self._accepting_claims = True

    def stop_claiming(self) -> None:
        self._accepting_claims = False

    async def run_configured_quorum(
        self, request: ReviewWorkRequest,
    ) -> ReviewWorkResult:
        reviewer_ids = tuple(dict.fromkeys(
            str(value).strip() for value in self.expected_reviewer_ids
            if str(value).strip()
        ))
        if len(reviewer_ids) < 2:
            return ReviewWorkResult(
                "blocked", "review_claim", "reviewer_configuration_missing", False,
            )
        completed = await self.repository.list_decisions(
            request.candidate_id, request.candidate_revision,
        )
        completed_pairs = {
            (item.reviewer_id, item.pair_order)
            for item in completed
            if item.reviewer_kind != "rule"
            and item.decision in {"approved", "rejected"}
            and item.pair_order in {"ab", "ba"}
        }
        last_result: ReviewWorkResult | None = None
        for reviewer_id in reviewer_ids:
            for pair_order in ("ab", "ba"):
                if (reviewer_id, pair_order) in completed_pairs:
                    continue
                attempt_root = (
                    f"{request.candidate_id}:{request.candidate_revision}:"
                    f"{reviewer_id}:{pair_order}"
                )
                variant = replace(
                    request,
                    reviewer_id=reviewer_id,
                    reviewer_attempt_id=attempt_root,
                    owner=f"{request.owner}:{reviewer_id}:{pair_order}",
                    prompt_version=f"{request.prompt_version}:{pair_order}",
                    pair_order=pair_order,
                    prompt=(
                        "比较顺序：A=证据支持时保留候选；B=证据不足或冲突时拒绝候选。\n"
                        f"本次先评估{'A 后评估 B' if pair_order == 'ab' else 'B 后评估 A'}。\n"
                        f"{request.prompt}"
                    ),
                    system_prompt=(
                        f"{request.system_prompt}\n"
                        f"本次使用 {pair_order.upper()} 顺序独立审核；不得参考另一顺序的结论。"
                    ),
                )
                last_result = await self.run(variant)
                if last_result.status != "completed":
                    return last_result
        quorum = await self.repository.quorum(
            request.candidate_id, request.candidate_revision,
            expected_reviewer_ids=reviewer_ids,
        )
        if quorum.status != "completed" or not quorum.order_invariant:
            return ReviewWorkResult(
                "human_review_pending", "review_quorum", quorum.reason, False,
                mutation=None if last_result is None else last_result.mutation,
                provider_report=None if last_result is None else last_result.provider_report,
            )
        if self.automatic_quorum_enabled:
            final = await self.repository.finalize_quorum(
                request.candidate_id, request.candidate_revision,
                created_at=time.time(), expected_reviewer_ids=reviewer_ids,
            )
            if final is None:
                return ReviewWorkResult(
                    "retry_wait", "review_quorum", "quorum_finalize_conflict", True,
                )
        return last_result or ReviewWorkResult("completed")

    @staticmethod
    def _parse(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            return dict(parsed) if isinstance(parsed, dict) else None
        return None

    async def run(self, request: ReviewWorkRequest) -> ReviewWorkResult:
        if not self._accepting_claims:
            return ReviewWorkResult("blocked", "review_claim", "shutdown", True)
        if not await self.repository.schema_ready():
            return ReviewWorkResult(
                "blocked", "review_claim", "review_schema_unavailable", False
            )
        claimed_at = time.time()
        claim = await self.repository.claim(
            candidate_id=request.candidate_id,
            expected_revision=request.candidate_revision,
            owner=request.owner,
            reviewer_id=request.reviewer_id,
            reviewer_kind=request.reviewer_kind,
            reviewer_attempt_id=request.reviewer_attempt_id,
            now=claimed_at,
            lease_seconds=request.lease_seconds,
        )
        if not claim.applied or claim.attempt is None:
            return ReviewWorkResult(
                "blocked", "review_claim", claim.failure_kind or "claim_conflict", True,
                mutation=claim,
            )
        attempt = claim.attempt
        provider_attempt_revision = attempt.revision

        async def provider_start(context: Any) -> ReviewMutation:
            nonlocal provider_attempt_revision
            started_at = float(getattr(context, "started_at", 0.0) or time.time())
            mutation = await self.repository.mark_provider_started(
                attempt_id=attempt.attempt_id,
                candidate_id=request.candidate_id,
                candidate_revision=request.candidate_revision,
                owner=request.owner,
                lease_token=attempt.lease_token,
                expected_revision=provider_attempt_revision,
                started_at=started_at,
            )
            if mutation.applied and mutation.attempt is not None:
                provider_attempt_revision = mutation.attempt.revision
            return mutation

        try:
            provider = await self.provider_adapter.call(
                task_name=f"learning.review:{request.reviewer_id}",
                scope_id=request.scope_id,
                prompt=request.prompt,
                system_prompt=request.system_prompt,
                is_json=True,
                run_id=(
                    f"review:{request.candidate_id}:{request.candidate_revision}:"
                    f"{request.reviewer_id}:{request.pair_order}"
                ),
                work_attempt_id=attempt.attempt_id,
                candidate_work_attempt=attempt.review_work_attempt,
                on_provider_request_start=provider_start,
                candidate_id=request.candidate_id,
            )
        except asyncio.CancelledError:
            await self.repository.abort(
                attempt_id=attempt.attempt_id, candidate_id=request.candidate_id,
                candidate_revision=request.candidate_revision, owner=request.owner,
                lease_token=attempt.lease_token, expected_revision=provider_attempt_revision,
                now=time.time(), status="retry_wait", failure_stage="review_provider",
                failure_kind="cancelled", retryable=True, retry_at=time.time() + 5.0,
            )
            raise
        report = sanitize_diagnostics(
            provider.to_report() if hasattr(provider, "to_report") else {}
        )
        if not bool(getattr(provider, "ok", False)):
            retryable = bool(getattr(provider, "retryable", False))
            failure_stage = str(getattr(provider, "failure_stage", "provider") or "provider")
            failure_kind = str(getattr(provider, "failure_kind", "provider_failure") or "provider_failure")
            status = "retry_wait" if retryable else "blocked"
            mutation = await self.repository.abort(
                attempt_id=attempt.attempt_id, candidate_id=request.candidate_id,
                candidate_revision=request.candidate_revision, owner=request.owner,
                lease_token=attempt.lease_token, expected_revision=provider_attempt_revision,
                now=time.time(), status=status, failure_stage=failure_stage,
                failure_kind=failure_kind, retryable=retryable,
                retry_at=float(getattr(provider, "retry_at", 0.0) or 0.0),
                diagnostics=dict(report),
            )
            return ReviewWorkResult(status, failure_stage, failure_kind, retryable, mutation=mutation, provider_report=report)
        parsed = self._parse(getattr(provider, "value", None))
        decision_value = str((parsed or {}).get("decision") or "").strip().lower()
        reason = str((parsed or {}).get("reason") or "").strip().lower()
        if parsed is None or decision_value not in DECISIONS or reason not in REASONS:
            mutation = await self.repository.abort(
                attempt_id=attempt.attempt_id, candidate_id=request.candidate_id,
                candidate_revision=request.candidate_revision, owner=request.owner,
                lease_token=attempt.lease_token, expected_revision=provider_attempt_revision,
                now=time.time(), status="quarantined", failure_stage="response_parse",
                failure_kind="invalid_contract", retryable=False, diagnostics=dict(report),
            )
            return ReviewWorkResult("quarantined", "response_parse", "invalid_contract", False, mutation=mutation, provider_report=report)
        confidence = parsed.get("confidence")
        payload_key = json.dumps(
            {"attempt": attempt.attempt_id, "decision": decision_value, "reason": reason},
            sort_keys=True, separators=(",", ":"),
        )
        decision_id = "review-decision:" + hashlib.sha256(payload_key.encode("utf-8")).hexdigest()
        provider_source_id = str(getattr(provider, "provider_id", "") or "").strip()
        model_id = str(getattr(provider, "model_id", "") or "").strip()
        try:
            model_identity = (
                reviewer_model_identity(
                    provider_source_id=provider_source_id,
                    model_id=model_id,
                    reviewer_profile_version=request.reviewer_profile_version,
                )
                if request.reviewer_kind == "model"
                else ""
            )
            decision_diagnostics = dict(report)
            decision_diagnostics.update({
                "provider_source_id": provider_source_id,
                "model_id": model_id,
                "reviewer_profile_version": request.reviewer_profile_version,
            })
            decision = ReviewDecision(
                decision_id=decision_id,
                candidate_id=request.candidate_id,
                candidate_revision=request.candidate_revision,
                decision=decision_value,
                reason=reason,
                reviewer_id=request.reviewer_id,
                reviewer_kind=request.reviewer_kind,
                reviewer_attempt_id=attempt.reviewer_attempt_id,
                rubric_version=request.rubric_version,
                prompt_version=request.prompt_version,
                model_identity=model_identity,
                source_evidence_ids=request.source_evidence_ids,
                source_example_ids=request.source_example_ids,
                model_example_ids=request.model_example_ids,
                confidence=confidence,
                expected_revision=request.candidate_revision,
                pair_order=request.pair_order,
                diagnostics=decision_diagnostics,
                created_at=time.time(),
            )
        except (TypeError, ValueError):
            mutation = await self.repository.abort(
                attempt_id=attempt.attempt_id, candidate_id=request.candidate_id,
                candidate_revision=request.candidate_revision, owner=request.owner,
                lease_token=attempt.lease_token, expected_revision=provider_attempt_revision,
                now=time.time(), status="quarantined", failure_stage="review_contract",
                failure_kind="invalid_contract", retryable=False, diagnostics=dict(report),
            )
            return ReviewWorkResult("quarantined", "review_contract", "invalid_contract", False, mutation=mutation, provider_report=report)
        mutation = await self.repository.settle(
            attempt_id=attempt.attempt_id, owner=request.owner,
            lease_token=attempt.lease_token,
            expected_attempt_revision=provider_attempt_revision,
            finished_at=time.time(), decision=decision,
        )
        if not mutation.applied and not mutation.idempotent:
            return ReviewWorkResult("retry_wait", "review_settlement", mutation.failure_kind, True, mutation=mutation, provider_report=report)
        if self.automatic_quorum_enabled and len(self.expected_reviewer_ids) >= 2:
            await self.repository.finalize_quorum(
                request.candidate_id, request.candidate_revision,
                created_at=time.time(), expected_reviewer_ids=self.expected_reviewer_ids,
            )
        return ReviewWorkResult("completed", decision_id=decision_id, mutation=mutation, provider_report=report)

    async def shutdown(self) -> int:
        self.stop_claiming()
        if not await self.repository.schema_ready():
            return 0
        return await self.repository.settle_running_for_shutdown(now=time.time())


__all__ = ["ReviewOrchestrator", "ReviewWorkRequest", "ReviewWorkResult"]
