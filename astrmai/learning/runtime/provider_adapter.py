from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping

from ...infrastructure.gateway.gateway_exceptions import (
    GatewayQueueTimeout,
    GatewayShutdownRejected,
    LLMCascadeFailureException,
    ProviderRequestStartRejected,
)
from ...infrastructure.runtime.background_task_budget import (
    BackgroundTaskExecutionTimeout,
    BackgroundTaskQueueFull,
    BackgroundTaskQueueTimeout,
)
from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.runtime.runtime_contracts import (
    FailureKind,
    LLMCallDiagnostics,
    LLMCallResult,
    LLMProviderSelection,
)
from ..persistence.provider_circuit_store import (
    CircuitDecision,
    CircuitMutation,
    LearningProviderCircuitStore,
)
from .learning_lane import LearningLaneBudget, LearningWorkRequest, LearningWorkResult


@dataclass(frozen=True, slots=True)
class LearningFailure:
    stage: str
    failure_stage: str
    failure_kind: str
    retryable: bool
    error_type: str = ""
    error_summary: str = ""


@dataclass(frozen=True, slots=True)
class LearningProviderAttemptResult:
    ok: bool
    value: Any = None
    run_id: str = ""
    work_attempt_id: str = ""
    task_name: str = ""
    scope_id: str = ""
    candidate_work_attempt: int = 0
    provider_attempt: int = 0
    provider_request_started: bool = False
    provider_id: str = ""
    provider_family: str = ""
    model_id: str = ""
    request_id: str = ""
    identity_source: str = ""
    fallback_used: bool = False
    logical_queue_wait_ms: float = 0.0
    runtime_queue_wait_ms: float = 0.0
    background_semaphore_wait_ms: float = 0.0
    global_semaphore_wait_ms: float = 0.0
    provider_latency_ms: float = 0.0
    failure_stage: str = ""
    failure_kind: str = ""
    retryable: bool = False
    retry_at: float = 0.0
    circuit_state: str = "closed"
    circuit_revision: int = 0
    circuit_until: float = 0.0
    lease_owner: str = ""
    lease_token: str = ""
    lease_until: float = 0.0
    circuit_settlement: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    diagnostics: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def to_report(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "run_id": self.run_id,
            "work_attempt_id": self.work_attempt_id,
            "task_name": self.task_name,
            "scope_id": self.scope_id,
            "candidate_work_attempt": self.candidate_work_attempt,
            "provider_attempt": self.provider_attempt,
            "provider_request_started": self.provider_request_started,
            "provider_id": self.provider_id,
            "provider_family": self.provider_family,
            "model_id": self.model_id,
            "request_id": self.request_id,
            "identity_source": self.identity_source,
            "fallback_used": self.fallback_used,
            "logical_queue_wait_ms": self.logical_queue_wait_ms,
            "runtime_queue_wait_ms": self.runtime_queue_wait_ms,
            "background_semaphore_wait_ms": self.background_semaphore_wait_ms,
            "global_semaphore_wait_ms": self.global_semaphore_wait_ms,
            "provider_latency_ms": self.provider_latency_ms,
            "failure_stage": self.failure_stage,
            "failure_kind": self.failure_kind,
            "retryable": self.retryable,
            "retry_at": self.retry_at,
            "circuit_state": self.circuit_state,
            "circuit_revision": self.circuit_revision,
            "circuit_until": self.circuit_until,
            "lease_owner": self.lease_owner,
            "lease_token": self.lease_token,
            "lease_until": self.lease_until,
            "circuit_settlement": dict(self.circuit_settlement),
            "diagnostics": dict(self.diagnostics),
        }


class _RuntimeBudgetUnavailable(RuntimeError):
    pass


class LearningProviderCallAdapter:
    """Single-attempt adapter for the L -> R -> Gateway B/G -> Provider path."""

    def __init__(
        self,
        *,
        learning_lane: LearningLaneBudget,
        runtime_budget,
        gateway,
        circuit_store: LearningProviderCircuitStore | None = None,
        owner: str | None = None,
    ) -> None:
        self.learning_lane = learning_lane
        self.runtime_budget = runtime_budget
        self.gateway = gateway
        self.circuit_store = circuit_store
        self.owner = str(owner or f"learning-adapter:{uuid.uuid4().hex[:12]}")

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    def _select_provider(
        self,
        *,
        task_name: str,
        scope_id: str,
        prompt: str,
        system_prompt: str,
        is_json: bool,
    ) -> LLMProviderSelection:
        selector = getattr(self.gateway, "select_data_process_provider", None)
        if not callable(selector):
            return LLMProviderSelection()
        selected = selector(
            prompt=prompt,
            system_prompt=system_prompt,
            is_json=is_json,
            lane_key=LaneKey(
                subsystem="bg",
                task_family=task_name,
                scope_id=str(scope_id),
                scope_kind="chat",
            ),
            base_origin=str(scope_id),
            use_fallback=False,
            allow_cooldown_override=False,
        )
        return selected if isinstance(selected, LLMProviderSelection) else LLMProviderSelection()

    @staticmethod
    def _retry_at(retryable: bool) -> float:
        return time.time() + 5.0 if retryable else 0.0

    @staticmethod
    def _diag_payload(diagnostics: LLMCallDiagnostics | None) -> Mapping[str, Any]:
        if diagnostics is None:
            return MappingProxyType({})
        return MappingProxyType(
            {
                "gateway_call_id": diagnostics.gateway_call_id,
                "provider_request_id": diagnostics.provider_request_id,
                "provider_request_started": diagnostics.provider_request_started,
                "identity_source": diagnostics.identity_source,
            }
        )

    @staticmethod
    def _identity_matches_selection(
        diagnostics: LLMCallDiagnostics,
        selection: LLMProviderSelection,
    ) -> bool:
        return bool(
            diagnostics.provider_request_started
            and diagnostics.provider_id == selection.provider_id
            and diagnostics.model_id == selection.model_id
        )

    @staticmethod
    def _settlement_payload(
        action: str,
        mutation: CircuitMutation | None,
    ) -> Mapping[str, Any]:
        if mutation is None:
            return MappingProxyType({})
        current = mutation.current_state or mutation.state
        return MappingProxyType(
            {
                "action": action,
                "settlement_id": mutation.settlement_id,
                "applied": bool(mutation.applied),
                "conflict": bool(mutation.conflict),
                "idempotent": bool(mutation.idempotent),
                "resulting_state": mutation.resulting_state,
                "resulting_revision": mutation.resulting_revision,
                "current_state": current.state if current is not None else "",
                "current_revision": current.revision if current is not None else 0,
            }
        )

    @staticmethod
    def _settlement_conflicted(mutation: CircuitMutation | None) -> bool:
        return bool(
            mutation is not None
            and mutation.conflict
            and not mutation.applied
            and not mutation.idempotent
        )

    def _result(
        self,
        *,
        ok: bool,
        value: Any = None,
        diagnostics: LLMCallDiagnostics | None = None,
        logical_wait_ms: float = 0.0,
        runtime_wait_ms: float = 0.0,
        failure_stage: str = "",
        failure_kind: str = "",
        retryable: bool = False,
        retry_at: float | None = None,
        fallback_used: bool = False,
        circuit_state: str = "closed",
        run_id: str = "",
        work_attempt_id: str = "",
        candidate_work_attempt: int = 0,
        task_name: str = "",
        scope_id: str = "",
        selection: LLMProviderSelection | None = None,
        circuit: CircuitDecision | None = None,
        settlement_action: str = "",
        settlement: CircuitMutation | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> LearningProviderAttemptResult:
        selection = selection or LLMProviderSelection()
        diagnostics = diagnostics or LLMCallDiagnostics(
            provider_id=selection.provider_id,
            provider_family=selection.provider_family,
            model_id=selection.model_id,
            identity_source=selection.identity_source,
        )
        circuit = circuit or CircuitDecision(
            allowed=True,
            reason=circuit_state,
            provider_key="",
            task_family=task_name,
        )
        payload = dict(self._diag_payload(diagnostics))
        payload.update(dict(extra or {}))
        settlement_payload = self._settlement_payload(settlement_action, settlement)
        settled_state = (
            settlement.current_state or settlement.state
            if settlement is not None
            else None
        )
        return LearningProviderAttemptResult(
            ok=ok,
            value=value,
            run_id=run_id,
            work_attempt_id=work_attempt_id,
            candidate_work_attempt=int(candidate_work_attempt or 0),
            task_name=task_name,
            scope_id=scope_id,
            provider_attempt=1 if diagnostics.provider_request_started else 0,
            provider_request_started=diagnostics.provider_request_started,
            provider_id=diagnostics.provider_id or selection.provider_id,
            provider_family=diagnostics.provider_family or selection.provider_family,
            model_id=diagnostics.model_id or selection.model_id,
            request_id=diagnostics.provider_request_id,
            identity_source=diagnostics.identity_source,
            fallback_used=bool(fallback_used),
            logical_queue_wait_ms=max(0.0, float(logical_wait_ms or 0.0)),
            runtime_queue_wait_ms=max(0.0, float(runtime_wait_ms or 0.0)),
            background_semaphore_wait_ms=max(
                0.0, diagnostics.background_semaphore_wait_ms
            ),
            global_semaphore_wait_ms=max(0.0, diagnostics.global_semaphore_wait_ms),
            provider_latency_ms=max(0.0, diagnostics.provider_latency_ms),
            failure_stage=failure_stage,
            failure_kind=failure_kind,
            retryable=bool(retryable),
            retry_at=self._retry_at(retryable) if retry_at is None else float(retry_at),
            circuit_state=settled_state.state if settled_state is not None else circuit_state,
            circuit_revision=(
                settled_state.revision if settled_state is not None else circuit.revision
            ),
            circuit_until=(
                settled_state.circuit_until
                if settled_state is not None
                else circuit.circuit_until
            ),
            lease_owner=(
                settled_state.half_open_owner
                if settled_state is not None
                else circuit.lease_owner
            ),
            lease_token=(
                settled_state.half_open_token
                if settled_state is not None
                else circuit.lease_token
            ),
            lease_until=(
                settled_state.lease_until
                if settled_state is not None
                else circuit.lease_until
            ),
            circuit_settlement=settlement_payload,
            diagnostics=MappingProxyType(payload),
        )

    def _settlement_failure_result(
        self,
        *,
        action: str,
        settlement: CircuitMutation,
        original_ok: bool,
        original_failure_stage: str,
        original_failure_kind: str,
        diagnostics: LLMCallDiagnostics | None,
        logical_wait_ms: float,
        runtime_wait_ms: float,
        run_id: str,
        work_attempt_id: str,
        candidate_work_attempt: int,
        task_name: str,
        scope_id: str,
        selection: LLMProviderSelection,
        circuit: CircuitDecision,
        extra: Mapping[str, Any] | None = None,
    ) -> LearningProviderAttemptResult:
        details = {
            "original_ok": bool(original_ok),
            "original_failure_stage": original_failure_stage,
            "original_failure_kind": original_failure_kind,
        }
        details.update(dict(extra or {}))
        return self._result(
            ok=False,
            diagnostics=diagnostics,
            logical_wait_ms=logical_wait_ms,
            runtime_wait_ms=runtime_wait_ms,
            failure_stage="circuit_settlement",
            failure_kind="circuit_settlement_conflict",
            retryable=True,
            circuit_state=circuit.reason,
            run_id=run_id,
            work_attempt_id=work_attempt_id,
            candidate_work_attempt=candidate_work_attempt,
            task_name=task_name,
            scope_id=scope_id,
            selection=selection,
            circuit=circuit,
            settlement_action=action,
            settlement=settlement,
            extra=details,
        )

    async def _abort_half_open(
        self,
        *,
        provider_key: str,
        task_name: str,
        circuit: CircuitDecision,
        settlement_id: str,
    ) -> CircuitMutation | None:
        if self.circuit_store is None or not circuit.lease_token:
            return None
        return await self.circuit_store.abort_half_open(
            provider_key=provider_key,
            task_family=task_name,
            owner=circuit.lease_owner or self.owner,
            lease_token=circuit.lease_token,
            expected_revision=circuit.revision,
            settlement_id=settlement_id,
        )

    async def _circuit_decision(
        self,
        provider_key: str,
        task_name: str,
        hard_deadline: float,
    ) -> CircuitDecision:
        if self.circuit_store is None:
            return CircuitDecision(True, "closed", provider_key, task_name)
        return await self.circuit_store.check_or_claim(
            provider_key=provider_key,
            task_family=task_name,
            owner=self.owner,
            lease_seconds=max(0.1, self._remaining(hard_deadline)),
        )

    async def call(
        self,
        *,
        task_name: str,
        scope_id: str,
        prompt: str,
        system_prompt: str = "",
        is_json: bool = True,
        hard_timeout_sec: float = 60.0,
        logical_wait_timeout_sec: float = 10.0,
        runtime_wait_timeout_sec: float = 10.0,
        provider_timeout_sec: float = 45.0,
        run_id: str = "",
        work_attempt_id: str = "",
        candidate_work_attempt: int = 0,
        on_provider_request_start: Callable[[Any], Any] | None = None,
        candidate_id: str | None = None,
        config_revision: int = 0,
    ) -> LearningProviderAttemptResult:
        hard_deadline = time.monotonic() + max(0.1, float(hard_timeout_sec or 0.1))
        request_run_id = str(run_id or f"learning-{uuid.uuid4().hex[:12]}")
        durable_work_attempt_id = str(work_attempt_id or "").strip()
        if durable_work_attempt_id:
            attempt_digest = hashlib.sha256(
                durable_work_attempt_id.encode("utf-8")
            ).hexdigest()
            settlement_root = f"work-attempt:{attempt_digest}"
        else:
            settlement_root = f"{request_run_id}:{uuid.uuid4().hex}"
        selection = self._select_provider(
            task_name=task_name,
            scope_id=scope_id,
            prompt=prompt,
            system_prompt=system_prompt,
            is_json=is_json,
        )
        provider_key = LearningProviderCircuitStore.resolve_provider_key(
            selection.provider_id,
            selection.provider_family,
        )
        result_context = {
            "run_id": request_run_id,
            "work_attempt_id": durable_work_attempt_id,
            "candidate_work_attempt": int(candidate_work_attempt or 0),
            "task_name": task_name,
            "scope_id": str(scope_id),
            "selection": selection,
        }
        if not provider_key:
            return self._result(
                ok=False,
                failure_stage="provider_request",
                failure_kind="provider_unavailable",
                retryable=True,
                extra={"reason": "provider_selection_unavailable"},
                **result_context,
            )

        circuit = await self._circuit_decision(provider_key, task_name, hard_deadline)
        result_context["circuit"] = circuit
        if not circuit.allowed:
            return self._result(
                ok=False,
                failure_stage="circuit",
                failure_kind=circuit.reason,
                retryable=True,
                retry_at=circuit.retry_at,
                circuit_state=circuit.reason,
                **result_context,
            )

        runtime_submitted_at = 0.0
        runtime_acquired_at = 0.0

        async def _gateway_call() -> LLMCallResult:
            remaining = self._remaining(hard_deadline)
            if remaining <= 0.0:
                raise asyncio.TimeoutError("learning hard deadline exhausted")
            return await self.gateway.call_data_process_task_result(
                prompt=prompt,
                system_prompt=system_prompt,
                is_json=is_json,
                lane_key=LaneKey(
                    subsystem="bg",
                    task_family=task_name,
                    scope_id=str(scope_id),
                    scope_kind="chat",
                ),
                base_origin=str(scope_id),
                timeout_override=min(max(0.1, provider_timeout_sec), remaining),
                max_retries_override=0,
                max_models_override=1,
                use_fallback=False,
                allow_cooldown_override=False,
                reserve_for_reply=False,
                hard_deadline_monotonic=hard_deadline,
                selected_model_id=selection.model_id,
                on_provider_request_start=on_provider_request_start,
            )

        async def _runtime_call() -> LLMCallResult:
            nonlocal runtime_submitted_at, runtime_acquired_at
            if self.runtime_budget is None:
                raise _RuntimeBudgetUnavailable("runtime background budget unavailable")
            runtime_submitted_at = time.monotonic()

            def _runtime_acquired() -> None:
                nonlocal runtime_acquired_at
                runtime_acquired_at = time.monotonic()

            remaining = self._remaining(hard_deadline)
            if remaining <= 0.0:
                raise BackgroundTaskQueueTimeout("learning hard deadline exhausted")
            return await self.runtime_budget.run(
                _gateway_call,
                task_name=task_name,
                scope_id=f"learning:{scope_id}",
                wait_timeout_sec=min(max(0.1, runtime_wait_timeout_sec), remaining),
                execution_timeout_sec=remaining,
                on_acquired=_runtime_acquired,
            )

        remaining = self._remaining(hard_deadline)
        request = LearningWorkRequest(
            run_id=request_run_id,
            candidate_id=candidate_id,
            scope_id=str(scope_id),
            task_name=task_name,
            workload_family="learning",
            wait_timeout_sec=min(max(0.1, logical_wait_timeout_sec), remaining),
            execution_timeout_sec=remaining,
            config_revision=int(config_revision or 0),
        )
        try:
            work = await self.learning_lane.run_learning_work(request, _runtime_call)
        except asyncio.CancelledError as exc:
            if isinstance(exc, GatewayShutdownRejected):
                return await self._map_failure(
                    error=exc,
                    lane_status="shutdown_rejected",
                    lane_failure_kind="shutdown_rejected",
                    logical_wait_ms=0.0,
                    runtime_wait_ms=0.0,
                    provider_key=provider_key,
                    task_name=task_name,
                    circuit=circuit,
                    run_id=request_run_id,
                    scope_id=str(scope_id),
                    selection=selection,
                    work_attempt_id=durable_work_attempt_id,
                    candidate_work_attempt=int(candidate_work_attempt or 0),
                    settlement_root=settlement_root,
                )
            settlement = None
            try:
                settlement = await self._abort_half_open(
                    provider_key=provider_key,
                    task_name=task_name,
                    circuit=circuit,
                    settlement_id=f"{settlement_root}:abort",
                )
            finally:
                if settlement is not None:
                    setattr(
                        exc,
                        "circuit_settlement",
                        dict(self._settlement_payload("abort", settlement)),
                    )
                raise
        except Exception as exc:
            work = LearningWorkResult(
                value=None,
                acquired=True,
                status="failed",
                stage="provider_request",
                failure_kind="unknown",
                queue_wait_ms=0.0,
                execution_ms=0.0,
                error_type=type(exc).__name__,
                error=exc,
            )
        if work.status == "cancelled":
            if isinstance(work.error, GatewayShutdownRejected):
                return await self._map_failure(
                    error=work.error,
                    lane_status="failed",
                    lane_failure_kind="shutdown_rejected",
                    logical_wait_ms=work.queue_wait_ms,
                    runtime_wait_ms=0.0,
                    provider_key=provider_key,
                    task_name=task_name,
                    circuit=circuit,
                    run_id=request_run_id,
                    scope_id=str(scope_id),
                    selection=selection,
                    work_attempt_id=durable_work_attempt_id,
                    candidate_work_attempt=int(candidate_work_attempt or 0),
                    settlement_root=settlement_root,
                )
            settlement = None
            try:
                settlement = await self._abort_half_open(
                    provider_key=provider_key,
                    task_name=task_name,
                    circuit=circuit,
                    settlement_id=f"{settlement_root}:abort",
                )
            finally:
                cancelled = asyncio.CancelledError()
                if settlement is not None:
                    setattr(
                        cancelled,
                        "circuit_settlement",
                        dict(self._settlement_payload("abort", settlement)),
                    )
                raise cancelled
        if work.status != "completed":
            error = work.error
            runtime_wait_ms = (
                max(0.0, runtime_acquired_at - runtime_submitted_at) * 1000.0
                if runtime_submitted_at and runtime_acquired_at
                else 0.0
            )
            return await self._map_failure(
                error=error,
                lane_status=work.status,
                lane_failure_kind=str(work.failure_kind or "unknown"),
                logical_wait_ms=work.queue_wait_ms,
                runtime_wait_ms=runtime_wait_ms,
                provider_key=provider_key,
                task_name=task_name,
                circuit=circuit,
                run_id=request_run_id,
                scope_id=str(scope_id),
                selection=selection,
                work_attempt_id=durable_work_attempt_id,
                candidate_work_attempt=int(candidate_work_attempt or 0),
                settlement_root=settlement_root,
            )

        gateway_result = work.value
        runtime_wait_ms = (
            max(0.0, runtime_acquired_at - runtime_submitted_at) * 1000.0
            if runtime_submitted_at and runtime_acquired_at
            else 0.0
        )
        if not isinstance(gateway_result, LLMCallResult):
            settlement = await self._abort_half_open(
                provider_key=provider_key,
                task_name=task_name,
                circuit=circuit,
                settlement_id=f"{settlement_root}:abort",
            )
            if self._settlement_conflicted(settlement):
                return self._settlement_failure_result(
                    action="abort",
                    settlement=settlement,
                    original_ok=False,
                    original_failure_stage="response_parse",
                    original_failure_kind="invalid_output",
                    diagnostics=None,
                    logical_wait_ms=work.queue_wait_ms,
                    runtime_wait_ms=runtime_wait_ms,
                    extra=None,
                    **result_context,
                )
            return self._result(
                ok=False,
                logical_wait_ms=work.queue_wait_ms,
                runtime_wait_ms=runtime_wait_ms,
                failure_stage="response_parse",
                failure_kind="invalid_output",
                retryable=True,
                settlement_action="abort",
                settlement=settlement,
                **result_context,
            )
        diagnostics = gateway_result.call_diagnostics or LLMCallDiagnostics(
            provider_id=gateway_result.model_id,
            provider_family=gateway_result.provider_family,
            model_id=gateway_result.model_id,
            identity_source="chat_provider_id;request_id_unavailable" if gateway_result.model_id else "",
            provider_request_started=bool(gateway_result.model_id),
        )
        if diagnostics.provider_request_started and not self._identity_matches_selection(
            diagnostics,
            selection,
        ):
            settlement = await self._abort_half_open(
                provider_key=provider_key,
                task_name=task_name,
                circuit=circuit,
                settlement_id=f"{settlement_root}:abort",
            )
            mismatch_details = {
                "selected_provider_id": selection.provider_id,
                "selected_model_id": selection.model_id,
                "actual_provider_id": diagnostics.provider_id,
                "actual_model_id": diagnostics.model_id,
            }
            if self._settlement_conflicted(settlement):
                return self._settlement_failure_result(
                    action="abort",
                    settlement=settlement,
                    original_ok=False,
                    original_failure_stage="provider_request",
                    original_failure_kind="provider_identity_mismatch",
                    diagnostics=diagnostics,
                    logical_wait_ms=work.queue_wait_ms,
                    runtime_wait_ms=runtime_wait_ms,
                    extra=mismatch_details,
                    **result_context,
                )
            return self._result(
                ok=False,
                diagnostics=diagnostics,
                logical_wait_ms=work.queue_wait_ms,
                runtime_wait_ms=runtime_wait_ms,
                failure_stage="provider_request",
                failure_kind="provider_identity_mismatch",
                retryable=False,
                settlement_action="abort",
                settlement=settlement,
                extra=mismatch_details,
                **result_context,
            )
        if not gateway_result.ok:
            return await self._map_gateway_result_failure(
                gateway_result,
                diagnostics,
                work.queue_wait_ms,
                runtime_wait_ms,
                provider_key,
                task_name,
                circuit,
                request_run_id,
                str(scope_id),
                selection,
                durable_work_attempt_id,
                int(candidate_work_attempt or 0),
                settlement_root,
            )
        value = gateway_result.parsed_json if is_json else gateway_result.text
        if value is None or value == "" or value == {} or value == []:
            settlement = None
            if self.circuit_store is not None and circuit.revision > 0:
                settlement = await self.circuit_store.record_success(
                    provider_key=provider_key,
                    task_family=task_name,
                    expected_revision=circuit.revision,
                    lease_token=circuit.lease_token,
                    settlement_id=f"{settlement_root}:success",
                )
            if self._settlement_conflicted(settlement):
                return self._settlement_failure_result(
                    action="success",
                    settlement=settlement,
                    original_ok=False,
                    original_failure_stage="response_parse",
                    original_failure_kind="empty_response",
                    diagnostics=diagnostics,
                    logical_wait_ms=work.queue_wait_ms,
                    runtime_wait_ms=runtime_wait_ms,
                    extra=None,
                    **result_context,
                )
            return self._result(
                ok=False,
                diagnostics=diagnostics,
                logical_wait_ms=work.queue_wait_ms,
                runtime_wait_ms=runtime_wait_ms,
                failure_stage="response_parse",
                failure_kind="empty_response",
                retryable=True,
                fallback_used=gateway_result.fallback_used,
                settlement_action="success",
                settlement=settlement,
                **result_context,
            )
        if not diagnostics.provider_request_started or not diagnostics.provider_id:
            settlement = await self._abort_half_open(
                provider_key=provider_key,
                task_name=task_name,
                circuit=circuit,
                settlement_id=f"{settlement_root}:abort",
            )
            if self._settlement_conflicted(settlement):
                return self._settlement_failure_result(
                    action="abort",
                    settlement=settlement,
                    original_ok=False,
                    original_failure_stage="provider_request",
                    original_failure_kind="provider_unavailable",
                    diagnostics=diagnostics,
                    logical_wait_ms=work.queue_wait_ms,
                    runtime_wait_ms=runtime_wait_ms,
                    extra={"reason": "identity_unknown"},
                    **result_context,
                )
            return self._result(
                ok=False,
                diagnostics=diagnostics,
                logical_wait_ms=work.queue_wait_ms,
                runtime_wait_ms=runtime_wait_ms,
                failure_stage="provider_request",
                failure_kind="provider_unavailable",
                retryable=True,
                fallback_used=gateway_result.fallback_used,
                settlement_action="abort",
                settlement=settlement,
                extra={"reason": "identity_unknown"},
                **result_context,
            )
        settlement = None
        if self.circuit_store is not None and circuit.revision > 0:
            settlement = await self.circuit_store.record_success(
                provider_key=provider_key,
                task_family=task_name,
                expected_revision=circuit.revision,
                lease_token=circuit.lease_token,
                settlement_id=f"{settlement_root}:success",
            )
        if self._settlement_conflicted(settlement):
            return self._settlement_failure_result(
                action="success",
                settlement=settlement,
                original_ok=True,
                original_failure_stage="",
                original_failure_kind="",
                diagnostics=diagnostics,
                logical_wait_ms=work.queue_wait_ms,
                runtime_wait_ms=runtime_wait_ms,
                extra=None,
                **result_context,
            )
        return self._result(
            ok=True,
            value=value,
            diagnostics=diagnostics,
            logical_wait_ms=work.queue_wait_ms,
            runtime_wait_ms=runtime_wait_ms,
            fallback_used=gateway_result.fallback_used,
            settlement_action="success",
            settlement=settlement,
            **result_context,
        )

    async def _map_gateway_result_failure(
        self,
        result: LLMCallResult,
        diagnostics: LLMCallDiagnostics,
        logical_wait_ms: float,
        runtime_wait_ms: float,
        provider_key: str,
        task_name: str,
        circuit: CircuitDecision,
        run_id: str,
        scope_id: str,
        selection: LLMProviderSelection,
        work_attempt_id: str,
        candidate_work_attempt: int,
        settlement_root: str,
    ) -> LearningProviderAttemptResult:
        kind = getattr(result.error_kind, "value", str(result.error_kind or "unknown"))
        error = LLMCascadeFailureException(
            result.error_message,
            last_failure_kind=kind,
            model_id=result.model_id,
            call_diagnostics=diagnostics,
        )
        return await self._map_failure(
            error=error,
            lane_status="failed",
            lane_failure_kind=kind,
            logical_wait_ms=logical_wait_ms,
            runtime_wait_ms=runtime_wait_ms,
            provider_key=provider_key,
            task_name=task_name,
            circuit=circuit,
            run_id=run_id,
            scope_id=scope_id,
            selection=selection,
            work_attempt_id=work_attempt_id,
            candidate_work_attempt=candidate_work_attempt,
            settlement_root=settlement_root,
        )

    async def _map_failure(
        self,
        *,
        error: BaseException | None,
        lane_status: str,
        lane_failure_kind: str,
        logical_wait_ms: float,
        runtime_wait_ms: float,
        provider_key: str,
        task_name: str,
        circuit: CircuitDecision,
        run_id: str,
        scope_id: str,
        selection: LLMProviderSelection,
        work_attempt_id: str,
        candidate_work_attempt: int,
        settlement_root: str,
    ) -> LearningProviderAttemptResult:
        if isinstance(error, asyncio.CancelledError) and not isinstance(
            error, GatewayShutdownRejected
        ):
            raise error
        diagnostics = getattr(error, "call_diagnostics", None)
        if not isinstance(diagnostics, LLMCallDiagnostics):
            diagnostics = LLMCallDiagnostics()
        if diagnostics.provider_request_started and not self._identity_matches_selection(
            diagnostics,
            selection,
        ):
            settlement = await self._abort_half_open(
                provider_key=provider_key,
                task_name=task_name,
                circuit=circuit,
                settlement_id=f"{settlement_root}:abort",
            )
            mismatch_details = {
                "error_type": type(error).__name__ if error is not None else "",
                "selected_provider_id": selection.provider_id,
                "selected_model_id": selection.model_id,
                "actual_provider_id": diagnostics.provider_id,
                "actual_model_id": diagnostics.model_id,
            }
            if self._settlement_conflicted(settlement):
                return self._settlement_failure_result(
                    action="abort",
                    settlement=settlement,
                    original_ok=False,
                    original_failure_stage="provider_request",
                    original_failure_kind="provider_identity_mismatch",
                    diagnostics=diagnostics,
                    logical_wait_ms=logical_wait_ms,
                    runtime_wait_ms=runtime_wait_ms,
                    run_id=run_id,
                    work_attempt_id=work_attempt_id,
                    candidate_work_attempt=candidate_work_attempt,
                    task_name=task_name,
                    scope_id=scope_id,
                    selection=selection,
                    circuit=circuit,
                    extra=mismatch_details,
                )
            return self._result(
                ok=False,
                diagnostics=diagnostics,
                logical_wait_ms=logical_wait_ms,
                runtime_wait_ms=runtime_wait_ms,
                failure_stage="provider_request",
                failure_kind="provider_identity_mismatch",
                retryable=False,
                circuit_state=circuit.reason,
                settlement_action="abort",
                settlement=settlement,
                extra=mismatch_details,
                run_id=run_id,
                work_attempt_id=work_attempt_id,
                candidate_work_attempt=candidate_work_attempt,
                task_name=task_name,
                scope_id=scope_id,
                selection=selection,
                circuit=circuit,
            )
        failure_stage = "queue_admission"
        failure_kind = lane_failure_kind
        retryable = True
        if isinstance(error, GatewayShutdownRejected):
            failure_stage, failure_kind = "gateway_admission", "shutdown_rejected"
        elif isinstance(error, ProviderRequestStartRejected):
            failure_stage = error.failure_stage
            failure_kind = error.failure_kind
            retryable = failure_kind != "dependency_unavailable"
        elif lane_status in {"queue_full", "queue_timeout", "shutdown_rejected"}:
            failure_kind = lane_status
        elif isinstance(error, _RuntimeBudgetUnavailable):
            failure_stage, failure_kind, retryable = (
                "runtime_admission",
                "runtime_budget_unavailable",
                False,
            )
        elif isinstance(error, BackgroundTaskQueueFull):
            failure_stage = "runtime_admission"
            failure_kind = (
                "shutdown_rejected" if "draining" in str(error).lower() else "runtime_budget_full"
            )
        elif isinstance(error, BackgroundTaskQueueTimeout):
            failure_stage, failure_kind = "runtime_admission", "runtime_budget_timeout"
        elif isinstance(error, GatewayQueueTimeout):
            failure_stage = "gateway_admission"
            failure_kind = (
                "background_semaphore_timeout"
                if error.stage == "gateway.background_semaphore_wait"
                else "global_semaphore_timeout"
            )
        elif isinstance(error, (BackgroundTaskExecutionTimeout, asyncio.TimeoutError)):
            failure_stage, failure_kind = "provider_request", "provider_timeout"
        elif isinstance(error, LLMCascadeFailureException):
            raw_kind = str(error.last_failure_kind or "unknown")
            if raw_kind in {FailureKind.TIMEOUT.value, FailureKind.TURN_BUDGET_EXHAUSTED.value}:
                failure_stage = "provider_request"
                failure_kind = (
                    "turn_budget_exhausted"
                    if raw_kind == FailureKind.TURN_BUDGET_EXHAUSTED.value
                    else "provider_timeout"
                )
            elif raw_kind == FailureKind.JSON_DECODE_ERROR.value:
                failure_stage, failure_kind = "response_parse", "json_decode_error"
            elif raw_kind == FailureKind.EMPTY_RESPONSE.value:
                failure_stage, failure_kind = "response_parse", "empty_response"
            elif raw_kind in {
                FailureKind.BAD_PAYLOAD.value,
                FailureKind.UNSAFE_OR_EMPTY_TEXT.value,
                FailureKind.PROMPT_SCAFFOLD_TEXT.value,
                FailureKind.TOOL_PROTOCOL_TEXT.value,
            }:
                failure_stage, failure_kind = "response_parse", "invalid_output"
            elif not diagnostics.provider_request_started:
                failure_stage, failure_kind = "provider_request", "provider_unavailable"
            else:
                failure_stage, failure_kind = "provider_request", "provider_error"
        elif error is not None:
            failure_stage, failure_kind = "unknown", "unknown"

        settlement_action = ""
        settlement = None
        if (
            self.circuit_store is not None
            and failure_kind in {"provider_error", "provider_timeout"}
            and diagnostics.provider_request_started
        ):
            settlement_action = "failure"
            settlement = await self.circuit_store.record_failure(
                provider_key=provider_key,
                task_family=task_name,
                failure_kind=failure_kind,
                expected_revision=circuit.revision if circuit.revision > 0 else None,
                lease_token=circuit.lease_token,
                settlement_id=f"{settlement_root}:failure",
            )
        elif diagnostics.provider_request_started:
            if self.circuit_store is not None and circuit.revision > 0:
                settlement_action = "success"
                settlement = await self.circuit_store.record_success(
                    provider_key=provider_key,
                    task_family=task_name,
                    expected_revision=circuit.revision,
                    lease_token=circuit.lease_token,
                    settlement_id=f"{settlement_root}:success",
                )
        else:
            settlement_action = "abort"
            settlement = await self._abort_half_open(
                provider_key=provider_key,
                task_name=task_name,
                circuit=circuit,
                settlement_id=f"{settlement_root}:abort",
            )
        if self._settlement_conflicted(settlement):
            return self._settlement_failure_result(
                action=settlement_action,
                settlement=settlement,
                original_ok=False,
                original_failure_stage=failure_stage,
                original_failure_kind=failure_kind,
                diagnostics=diagnostics,
                logical_wait_ms=logical_wait_ms,
                runtime_wait_ms=runtime_wait_ms,
                run_id=run_id,
                work_attempt_id=work_attempt_id,
                candidate_work_attempt=candidate_work_attempt,
                task_name=task_name,
                scope_id=scope_id,
                selection=selection,
                circuit=circuit,
                extra={"error_type": type(error).__name__ if error is not None else ""},
            )
        return self._result(
            ok=False,
            diagnostics=diagnostics,
            logical_wait_ms=logical_wait_ms,
            runtime_wait_ms=runtime_wait_ms,
            failure_stage=failure_stage,
            failure_kind=failure_kind,
            retryable=retryable,
            circuit_state=circuit.reason,
            settlement_action=settlement_action,
            settlement=settlement,
            extra={"error_type": type(error).__name__ if error is not None else ""},
            run_id=run_id,
            work_attempt_id=work_attempt_id,
            candidate_work_attempt=candidate_work_attempt,
            task_name=task_name,
            scope_id=scope_id,
            selection=selection,
            circuit=circuit,
        )


__all__ = [
    "LearningFailure",
    "LearningProviderAttemptResult",
    "LearningProviderCallAdapter",
]
