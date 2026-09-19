from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..persistence.candidate_ledger import (
    CandidateEvidence,
    CandidateLedger,
    EnrichmentSettlement,
    WorkAttemptLease,
)


logger = logging.getLogger(__name__)


def _diagnostic_stage(failure_stage: str, status: str) -> str:
    if failure_stage in {"provider_request", "gateway_admission", "runtime_admission"}:
        return "provider" if failure_stage == "provider_request" else "enrichment_admission"
    if failure_stage == "response_parse":
        return "parse"
    if failure_stage == "persistence" or status in {"enriched", "rejected"}:
        return "persist"
    return "enrichment_admission"


@dataclass(frozen=True, slots=True)
class EnrichmentWorkerResult:
    scanned: int = 0
    claimed: int = 0
    enriched: int = 0
    rejected: int = 0
    retry_wait: int = 0
    quarantined: int = 0
    blocked: int = 0
    conflicts: int = 0


class LearningEnrichmentWorker:
    """Lease-based owner for durable candidate enrichment work attempts."""

    def __init__(
        self,
        *,
        ledger: CandidateLedger,
        expression_enricher,
        jargon_enricher,
        save_patterns: Callable[..., Awaitable[Any]],
        save_jargons: Callable[..., Awaitable[Any]],
        owner: str | None = None,
        max_attempts: int = 3,
        lease_seconds: float = 60.0,
        lease_heartbeat_interval: float | None = None,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
        claim_allowed: Callable[[], bool] = lambda: True,
    ) -> None:
        self.ledger = ledger
        self.expression_enricher = expression_enricher
        self.jargon_enricher = jargon_enricher
        self.save_patterns = save_patterns
        self.save_jargons = save_jargons
        self.owner = str(owner or f"learning-enrichment:{uuid.uuid4().hex[:12]}")
        self.max_attempts = max(1, int(max_attempts))
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.lease_heartbeat_interval = max(
            0.01,
            float(
                lease_heartbeat_interval
                if lease_heartbeat_interval is not None
                else self.lease_seconds / 3.0
            ),
        )
        self.clock = clock
        self.jitter = jitter
        self.claim_allowed = claim_allowed
        self._accepting = True
        self._active: set[asyncio.Task] = set()

    def begin_drain(self) -> None:
        self._accepting = False

    async def shutdown(self) -> None:
        self.begin_drain()
        tasks = tuple(task for task in self._active if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def retry_at(self, candidate_work_attempt: int) -> float:
        delay = min(
            3600.0,
            5.0 * (2 ** max(0, int(candidate_work_attempt) - 1))
            + max(0.0, float(self.jitter())),
        )
        return float(self.clock()) + delay

    async def run_due_once(self, *, limit: int = 10) -> EnrichmentWorkerResult:
        if not self._accepting or not self.claim_allowed():
            return EnrichmentWorkerResult()
        now = float(self.clock())
        due = await self.ledger.list_due_enrichment(now=now, limit=limit)
        counts = {
            "scanned": len(due),
            "claimed": 0,
            "enriched": 0,
            "rejected": 0,
            "retry_wait": 0,
            "quarantined": 0,
            "blocked": 0,
            "conflicts": 0,
        }
        for candidate in due:
            if not self._accepting or not self.claim_allowed():
                break
            task_name = f"learning.{candidate.candidate_family}_enrichment"
            lease = await self.ledger.claim_enrichment(
                candidate_id=candidate.candidate_id,
                expected_revision=candidate.revision,
                owner=self.owner,
                run_id=f"enrichment:{uuid.uuid4().hex[:16]}",
                task_name=task_name,
                lease_seconds=self.lease_seconds,
                now=float(self.clock()),
            )
            if lease is None:
                counts["conflicts"] += 1
                continue
            counts["claimed"] += 1
            task = asyncio.create_task(self._process(lease))
            self._active.add(task)
            try:
                status = await task
            finally:
                self._active.discard(task)
            counts[status if status in counts else "conflicts"] += 1
        return EnrichmentWorkerResult(**counts)

    async def _process(self, lease: WorkAttemptLease) -> str:
        settlement_started_at = float(self.clock())
        lease_lost = asyncio.Event()
        owner_task = asyncio.current_task()
        heartbeat = asyncio.create_task(
            self._renew_lease_loop(lease, lease_lost, owner_task)
        )

        async def _stop_heartbeat() -> None:
            if not heartbeat.done():
                heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

        async def _mark_started(identity):
            provider_request_id = str(
                getattr(identity, "provider_request_id", "") or ""
            )
            identity_source = str(identity.identity_source or "")
            if not provider_request_id:
                identity_source = (
                    f"{identity_source}|request_id_unavailable"
                    if identity_source
                    else "request_id_unavailable"
                )
            return await self.ledger.mark_provider_started(
                candidate_id=lease.candidate_id,
                attempt_id=lease.attempt_id,
                owner=lease.owner,
                lease_token=lease.lease_token,
                expected_revision=lease.expected_revision,
                provider_id=identity.provider_id,
                provider_family=identity.provider_family,
                model_id=identity.model_id,
                identity_source=identity_source,
                gateway_call_id=identity.gateway_call_id,
                provider_request_id=provider_request_id,
                started_at=identity.started_at,
                now=float(self.clock()),
            )

        provider_kwargs = {
            "run_id": lease.run_id,
            "work_attempt_id": lease.attempt_id,
            "candidate_work_attempt": lease.candidate_work_attempt,
            "candidate_id": lease.candidate_id,
            "on_provider_request_start": _mark_started,
        }
        payload = dict(lease.candidate.source_payload)
        payload["candidate_id"] = lease.candidate_id
        try:
            try:
                if lease.candidate.candidate_family == "expression":
                    enrichment = await self.expression_enricher.enrich(
                        lease.scope_id,
                        [payload],
                        provider_call_kwargs=provider_kwargs,
                    )
                    provider_attempt = getattr(
                        self.expression_enricher, "last_provider_attempt", None
                    )
                else:
                    enrichment = await self.jargon_enricher.enrich(
                        lease.scope_id,
                        [payload],
                        provider_call_kwargs=provider_kwargs,
                    )
                    provider_attempt = getattr(
                        self.jargon_enricher, "last_provider_attempt", None
                    )
                settlement = await self._build_outcome(
                    lease,
                    enrichment,
                    provider_attempt,
                    lease_lost=lease_lost,
                    settlement_started_at=settlement_started_at,
                )
            except asyncio.CancelledError:
                if lease_lost.is_set():
                    return "conflicts"
                settlement = self._failure_outcome(
                    lease,
                    settlement_started_at=settlement_started_at,
                    failure_stage="worker",
                    failure_kind="cancelled",
                    retryable=True,
                )
                try:
                    await _stop_heartbeat()
                    await asyncio.shield(
                        self._settle_with_diagnostic(lease, settlement)
                    )
                except Exception as exc:
                    logger.warning(
                        "candidate cancellation settlement deferred to lease recovery: %s",
                        type(exc).__name__,
                    )
                raise
            except Exception as exc:
                failure_kind = (
                    "persist_locked" if "locked" in str(exc).lower() else "worker_error"
                )
                settlement = self._failure_outcome(
                    lease,
                    settlement_started_at=settlement_started_at,
                    failure_stage=(
                        "persistence" if failure_kind == "persist_locked" else "worker"
                    ),
                    failure_kind=failure_kind,
                    retryable=failure_kind == "persist_locked",
                    diagnostics={"error_type": type(exc).__name__},
                )
            await _stop_heartbeat()
            result = await self._settle_with_diagnostic(lease, settlement)
            if not result.applied and not result.idempotent:
                return "conflicts"
            return settlement.status
        finally:
            await _stop_heartbeat()

    async def _renew_lease_loop(
        self,
        lease: WorkAttemptLease,
        lease_lost: asyncio.Event,
        owner_task: asyncio.Task | None,
    ) -> None:
        while True:
            await asyncio.sleep(self.lease_heartbeat_interval)
            now = float(self.clock())
            try:
                renewed = await self.ledger.renew_enrichment_lease(
                    candidate_id=lease.candidate_id,
                    attempt_id=lease.attempt_id,
                    owner=lease.owner,
                    lease_token=lease.lease_token,
                    expected_revision=lease.expected_revision,
                    lease_until=now + self.lease_seconds,
                    now=now,
                )
            except Exception as exc:
                logger.warning(
                    "candidate lease renewal failed candidate_id=%s error=%s",
                    lease.candidate_id,
                    type(exc).__name__,
                )
                renewed = False
            if not renewed:
                lease_lost.set()
                if owner_task is not None and not owner_task.done():
                    owner_task.cancel()
                return

    async def _settle_with_diagnostic(
        self, lease: WorkAttemptLease, settlement: EnrichmentSettlement
    ):
        result = await self._settle(lease, settlement)
        if result.applied or result.idempotent:
            diagnostic_status = (
                "completed"
                if settlement.status in {"enriched", "rejected"}
                else settlement.status
            )
            await self.ledger.append_stage_diagnostic(
                run_id=lease.run_id,
                candidate_id=lease.candidate_id,
                stage=_diagnostic_stage(
                    settlement.failure_stage, settlement.status
                ),
                status=diagnostic_status,
                failure_stage=settlement.failure_stage,
                failure_kind=settlement.failure_kind,
                retryable=settlement.retryable,
                attempt=lease.candidate_work_attempt,
                provider_id=settlement.provider_id,
                model_id=settlement.model_id,
                input_count=settlement.input_count,
                output_count=settlement.output_count,
                diagnostics=settlement.diagnostics,
                created_at=settlement.finished_at,
            )
        return result

    async def _build_outcome(
        self,
        lease: WorkAttemptLease,
        enrichment,
        provider_attempt,
        *,
        lease_lost: asyncio.Event,
        settlement_started_at: float,
    ) -> EnrichmentSettlement:
        report = enrichment.to_report() if hasattr(enrichment, "to_report") else {}
        provider_report = self._provider_attempt_report(provider_attempt)
        items = list(getattr(enrichment, "items", ()) or ())
        terminal = bool(getattr(enrichment, "terminal", False))
        provider_ok = bool(getattr(provider_attempt, "ok", False))
        if not terminal or not provider_ok:
            failure_stage = str(
                getattr(provider_attempt, "failure_stage", "")
                or ("response_parse" if provider_ok else "provider_request")
            )
            failure_kind = str(
                getattr(provider_attempt, "failure_kind", "")
                or getattr(enrichment, "reason", "")
                or getattr(enrichment, "status", "invalid_output")
            )
            retryable = bool(
                getattr(provider_attempt, "retryable", False)
                or getattr(enrichment, "retryable", False)
            )
            return self._failure_outcome(
                lease,
                settlement_started_at=settlement_started_at,
                failure_stage=failure_stage,
                failure_kind=failure_kind,
                retryable=retryable,
                diagnostics={
                    "enrichment": report,
                    "provider_attempt": provider_report,
                },
                provider_id=str(getattr(provider_attempt, "provider_id", "") or ""),
                model_id=str(getattr(provider_attempt, "model_id", "") or ""),
                provider_attempt=provider_attempt,
            )
        if not items:
            if str(getattr(enrichment, "status", "")) == "all_rejected":
                finished_at = float(self.clock())
                return EnrichmentSettlement.build(
                    status="rejected",
                    diagnostics={
                        "enrichment": report,
                        "provider_attempt": provider_report,
                    },
                    provider_id=str(
                        getattr(provider_attempt, "provider_id", "") or ""
                    ),
                    model_id=str(
                        getattr(provider_attempt, "model_id", "") or ""
                    ),
                    **self._attempt_metrics(
                        provider_attempt,
                        report,
                        finished_at - settlement_started_at,
                    ),
                    settlement_started_at=settlement_started_at,
                    finished_at=finished_at,
                )
            return self._failure_outcome(
                lease,
                settlement_started_at=settlement_started_at,
                failure_stage="response_parse",
                failure_kind="candidate_rejected",
                retryable=False,
                diagnostics={
                    "enrichment": report,
                    "provider_attempt": provider_report,
                },
                provider_id=str(getattr(provider_attempt, "provider_id", "") or ""),
                model_id=str(getattr(provider_attempt, "model_id", "") or ""),
                provider_attempt=provider_attempt,
            )
        source_evidence = tuple(
            item
            for item in await self.ledger.list_evidence(lease.candidate_id)
            if item.eligible and not item.is_generated
        )
        canonical_persistence_id = self._canonical_persistence_id(
            lease, source_evidence
        )
        if not await self._renew_lease_before_persistence(lease, lease_lost):
            raise asyncio.CancelledError()
        if lease.candidate.candidate_family == "expression":
            persistence = await self.save_patterns(
                items,
                mining_batch_id=canonical_persistence_id,
                source="learning_candidate_worker",
                candidate_id=lease.candidate_id,
                candidate_revision=lease.expected_revision,
                candidate_persistence_id=canonical_persistence_id,
            )
        else:
            persistence = await self.save_jargons(
                lease.scope_id,
                items,
                mining_batch_id=canonical_persistence_id,
                candidate_id=lease.candidate_id,
                candidate_revision=lease.expected_revision,
                candidate_persistence_id=canonical_persistence_id,
            )
        persistence_report = persistence.to_report()
        if not bool(getattr(persistence, "complete", False)):
            failure_kind = str(
                persistence_report.get("failure_kind") or "persistence_partial"
            )
            return self._failure_outcome(
                lease,
                settlement_started_at=settlement_started_at,
                failure_stage=str(
                    persistence_report.get("failure_stage") or "persistence"
                ),
                failure_kind=failure_kind,
                retryable=bool(persistence_report.get("retryable", True)),
                diagnostics={
                    "enrichment": report,
                    "provider_attempt": provider_report,
                    "persistence": persistence_report,
                },
                provider_id=str(getattr(provider_attempt, "provider_id", "") or ""),
                model_id=str(getattr(provider_attempt, "model_id", "") or ""),
                provider_attempt=provider_attempt,
            )
        canonical_ids = tuple(str(item) for item in persistence_report.get("memory_ids", ()))
        if not canonical_ids or any(not item for item in canonical_ids):
            return self._failure_outcome(
                lease,
                settlement_started_at=settlement_started_at,
                failure_stage="persistence",
                failure_kind="empty_persistence_id",
                retryable=True,
                diagnostics={
                    "enrichment": report,
                    "provider_attempt": provider_report,
                    "persistence": persistence_report,
                },
                provider_attempt=provider_attempt,
            )
        model_examples = tuple(
            str(item).strip()
            for item in (items[0].get("model_examples") or ())
            if str(item).strip()
        )
        generated_evidence = (
            CandidateEvidence.generated(
                candidate_id=lease.candidate_id,
                batch_id=lease.candidate.first_discovered_batch_id,
                payload={
                    "model_examples": model_examples,
                    "generation_identity": canonical_persistence_id,
                },
                created_at=float(self.clock()),
            ),
        ) if model_examples else ()
        finished_at = float(self.clock())
        return EnrichmentSettlement.build(
            status="enriched",
            enrichment_payload=items[0],
            diagnostics={
                "enrichment": report,
                "provider_attempt": provider_report,
                "persistence": persistence_report,
                "canonical_persistence_id": canonical_persistence_id,
            },
            provider_id=str(getattr(provider_attempt, "provider_id", "") or ""),
            model_id=str(getattr(provider_attempt, "model_id", "") or ""),
            canonical_ids=canonical_ids,
            generated_evidence=generated_evidence,
            **self._attempt_metrics(
                provider_attempt,
                report,
                finished_at - settlement_started_at,
            ),
            settlement_started_at=settlement_started_at,
            finished_at=finished_at,
        )

    async def _renew_lease_before_persistence(
        self,
        lease: WorkAttemptLease,
        lease_lost: asyncio.Event,
    ) -> bool:
        if lease_lost.is_set():
            return False
        now = float(self.clock())
        try:
            renewed = await self.ledger.renew_enrichment_lease(
                candidate_id=lease.candidate_id,
                attempt_id=lease.attempt_id,
                owner=lease.owner,
                lease_token=lease.lease_token,
                expected_revision=lease.expected_revision,
                lease_until=now + self.lease_seconds,
                now=now,
            )
        except Exception as exc:
            logger.warning(
                "candidate pre-persistence lease renewal failed candidate_id=%s error=%s",
                lease.candidate_id,
                type(exc).__name__,
            )
            renewed = False
        if not renewed:
            lease_lost.set()
            return False
        return not lease_lost.is_set()

    @staticmethod
    def _provider_attempt_report(provider_attempt) -> dict[str, Any]:
        if provider_attempt is None:
            return {}
        reporter = getattr(provider_attempt, "to_report", None)
        if callable(reporter):
            return dict(reporter())
        fields = (
            "ok",
            "run_id",
            "work_attempt_id",
            "candidate_work_attempt",
            "provider_attempt",
            "provider_request_started",
            "provider_id",
            "provider_family",
            "model_id",
            "request_id",
            "identity_source",
            "logical_queue_wait_ms",
            "runtime_queue_wait_ms",
            "background_semaphore_wait_ms",
            "global_semaphore_wait_ms",
            "provider_latency_ms",
            "failure_stage",
            "failure_kind",
            "retryable",
        )
        return {
            name: getattr(provider_attempt, name)
            for name in fields
            if hasattr(provider_attempt, name)
        }

    @staticmethod
    def _attempt_metrics(
        provider_attempt,
        enrichment_report: dict[str, Any],
        elapsed_seconds: float,
    ) -> dict[str, Any]:
        logical = max(
            0.0,
            float(getattr(provider_attempt, "logical_queue_wait_ms", 0.0) or 0.0),
        )
        runtime = max(
            0.0,
            float(getattr(provider_attempt, "runtime_queue_wait_ms", 0.0) or 0.0),
        )
        background = max(
            0.0,
            float(
                getattr(provider_attempt, "background_semaphore_wait_ms", 0.0)
                or 0.0
            ),
        )
        global_wait = max(
            0.0,
            float(getattr(provider_attempt, "global_semaphore_wait_ms", 0.0) or 0.0),
        )
        return {
            "queue_wait_ms": logical + runtime + background + global_wait,
            "logical_queue_wait_ms": logical,
            "runtime_budget_wait_ms": runtime,
            "gateway_background_wait_ms": background,
            "gateway_global_wait_ms": global_wait,
            "provider_latency_ms": max(
                0.0,
                float(getattr(provider_attempt, "provider_latency_ms", 0.0) or 0.0),
            ),
            "elapsed_ms": max(0.0, float(elapsed_seconds) * 1000.0),
            "input_count": max(
                0, int(enrichment_report.get("input_count", 0) or 0)
            ),
            "output_count": max(
                0, int(enrichment_report.get("returned_count", 0) or 0)
            ),
        }

    @staticmethod
    def _canonical_persistence_id(
        lease: WorkAttemptLease,
        source_evidence: tuple[CandidateEvidence, ...],
    ) -> str:
        payload = json.dumps(
            {
                "candidate_id": lease.candidate_id,
                "source_evidence_ids": sorted(
                    item.evidence_id for item in source_evidence
                ),
                "version": 1,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"candidate-enrichment:{hashlib.sha256(payload).hexdigest()}"

    def _failure_outcome(
        self,
        lease: WorkAttemptLease,
        *,
        settlement_started_at: float,
        failure_stage: str,
        failure_kind: str,
        retryable: bool,
        diagnostics: dict[str, Any] | None = None,
        provider_id: str = "",
        model_id: str = "",
        provider_attempt=None,
    ) -> EnrichmentSettlement:
        exhausted = lease.candidate_work_attempt >= self.max_attempts
        blocked = not retryable
        status = "quarantined" if exhausted and retryable else (
            "blocked" if blocked else "retry_wait"
        )
        final_kind = "retry_exhausted" if status == "quarantined" else failure_kind
        retry_at = self.retry_at(lease.candidate_work_attempt) if status == "retry_wait" else 0.0
        finished_at = float(self.clock())
        enrichment_report = dict((diagnostics or {}).get("enrichment") or {})
        return EnrichmentSettlement.build(
            status=status,
            retryable=status == "retry_wait",
            retry_at=retry_at,
            failure_stage=failure_stage,
            failure_kind=final_kind,
            diagnostics={
                **dict(diagnostics or {}),
                "original_failure_kind": failure_kind,
            },
            provider_id=provider_id,
            model_id=model_id,
            **self._attempt_metrics(
                provider_attempt,
                enrichment_report,
                finished_at - settlement_started_at,
            ),
            settlement_started_at=settlement_started_at,
            finished_at=finished_at,
        )

    async def _settle(self, lease: WorkAttemptLease, result: EnrichmentSettlement):
        return await self.ledger.settle_enrichment(
            candidate_id=lease.candidate_id,
            attempt_id=lease.attempt_id,
            owner=lease.owner,
            lease_token=lease.lease_token,
            expected_revision=lease.expected_revision,
            result=result,
        )


__all__ = ["EnrichmentWorkerResult", "LearningEnrichmentWorker"]
