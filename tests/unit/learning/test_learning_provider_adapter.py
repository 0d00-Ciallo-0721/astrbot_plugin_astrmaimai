import asyncio
from dataclasses import replace
import sqlite3

import pytest

from astrmai.infrastructure.gateway.gateway_exceptions import (
    GatewayQueueTimeout,
    GatewayShutdownRejected,
    LLMCascadeFailureException,
)
from astrmai.infrastructure.runtime.background_task_budget import BackgroundTaskQueueFull
from astrmai.infrastructure.runtime.runtime_contracts import (
    FailureKind,
    LLMCallDiagnostics,
    LLMCallResult,
    LLMProviderSelection,
)
from astrmai.infrastructure.persistence.persistence_schema import _run_migrations
from astrmai.learning.persistence.provider_circuit_store import (
    CircuitDecision,
    CircuitMutation,
    LearningProviderCircuitStore,
    LearningProviderCircuitState,
)
from astrmai.learning.runtime.learning_lane import LearningWorkResult
from astrmai.learning.runtime.provider_adapter import LearningProviderCallAdapter


TASK = "learning.expression_enrichment"


class _Lane:
    def __init__(self, order, result=None):
        self.order = order
        self.result = result

    async def run_learning_work(self, request, factory):
        self.order.append("L")
        if self.result is not None:
            return self.result
        value = await factory()
        return LearningWorkResult(
            value=value,
            acquired=True,
            status="completed",
            stage="provider_request",
            failure_kind=None,
            queue_wait_ms=1.0,
            execution_ms=2.0,
        )


class _RuntimeBudget:
    def __init__(self, order, error=None):
        self.order = order
        self.error = error

    async def run(self, factory, **kwargs):
        self.order.append("R")
        if self.error is not None:
            raise self.error
        callback = kwargs.get("on_acquired")
        if callback:
            callback()
        return await factory()


class _Gateway:
    def __init__(self, order, result=None, error=None, selected_model="provider/model-a"):
        self.order = order
        self.result = result
        self.error = error
        self.kwargs = None
        self.selected_model = selected_model

    def _task_models(self):
        return ["provider/model-a"]

    def _provider_capabilities(self, _model):
        return type("Caps", (), {"provider_family": "native_chat"})()

    def select_data_process_provider(self, **_kwargs):
        return LLMProviderSelection(
            provider_id=self.selected_model,
            provider_family="native_chat",
            model_id=self.selected_model,
            identity_source="chat_provider_id",
        )

    async def call_data_process_task_result(self, **kwargs):
        self.kwargs = kwargs
        self.order.extend(["B", "G", "Provider"])
        if self.error is not None:
            raise self.error
        return self.result


class _BlockingGateway(_Gateway):
    def __init__(self, order):
        super().__init__(order)
        self.started = asyncio.Event()

    async def call_data_process_task_result(self, **kwargs):
        self.kwargs = kwargs
        self.order.extend(["B", "G", "Provider"])
        self.started.set()
        await asyncio.Event().wait()


def _circuit_state(*, state="closed", revision=1, failure_count=0):
    return LearningProviderCircuitState(
        provider_key="id:provider/model-a",
        task_family=TASK,
        state=state,
        failure_count=failure_count,
        window_started_at=0.0,
        last_failure_at=0.0,
        circuit_until=100.0 if state == "open" else 0.0,
        half_open_owner="",
        half_open_token="",
        lease_until=0.0,
        revision=revision,
        updated_at=1.0,
    )


class _Circuit:
    def __init__(self, decision, mutations=None):
        self.decision = decision
        self.mutations = dict(mutations or {})
        self.calls = []

    async def check_or_claim(self, **kwargs):
        self.calls.append(("check", kwargs))
        return self.decision

    async def record_success(self, **kwargs):
        self.calls.append(("success", kwargs))
        mutation = self.mutations.get(
            "success",
            CircuitMutation(applied=True, state=_circuit_state(revision=2)),
        )
        return replace(mutation, settlement_id=kwargs.get("settlement_id", ""))

    async def record_failure(self, **kwargs):
        self.calls.append(("failure", kwargs))
        mutation = self.mutations.get(
            "failure",
            CircuitMutation(
                applied=True,
                state=_circuit_state(state="open", revision=2, failure_count=3),
            ),
        )
        return replace(mutation, settlement_id=kwargs.get("settlement_id", ""))

    async def abort_half_open(self, **kwargs):
        self.calls.append(("abort", kwargs))
        mutation = self.mutations.get(
            "abort",
            CircuitMutation(applied=True, state=_circuit_state(state="open", revision=2)),
        )
        return replace(mutation, settlement_id=kwargs.get("settlement_id", ""))


def _success_result(value=None):
    return LLMCallResult(
        ok=True,
        parsed_json={"items": []} if value is None else value,
        model_id="provider/model-a",
        provider_family="native_chat",
        call_diagnostics=LLMCallDiagnostics(
            gateway_call_id="gateway-1",
            provider_request_id="request-1",
            provider_id="provider/model-a",
            provider_family="native_chat",
            model_id="provider/model-a",
            identity_source="chat_provider_id",
            background_semaphore_wait_ms=3.0,
            global_semaphore_wait_ms=4.0,
            provider_latency_ms=5.0,
            provider_request_started=True,
        ),
    )


@pytest.mark.asyncio
async def test_adapter_acquires_l_r_b_g_once_and_uses_single_provider_attempt():
    order = []
    gateway = _Gateway(order, result=_success_result())
    adapter = LearningProviderCallAdapter(
        learning_lane=_Lane(order),
        runtime_budget=_RuntimeBudget(order),
        gateway=gateway,
    )

    result = await adapter.call(
        task_name=TASK,
        scope_id="chat-1",
        prompt="prompt",
        hard_timeout_sec=2.0,
    )

    assert result.ok is True
    assert result.value == {"items": []}
    assert result.candidate_work_attempt == 0
    assert result.provider_attempt == 1
    assert order == ["L", "R", "B", "G", "Provider"]
    assert gateway.kwargs["max_retries_override"] == 0
    assert gateway.kwargs["max_models_override"] == 1
    assert gateway.kwargs["reserve_for_reply"] is False
    assert gateway.kwargs["lane_key"].subsystem == "bg"
    assert gateway.kwargs["lane_key"].task_family == TASK
    assert gateway.kwargs["lane_key"].scope_kind == "chat"


@pytest.mark.asyncio
async def test_logical_admission_failure_never_reaches_runtime_or_provider():
    order = []
    lane_result = LearningWorkResult(
        value=None,
        acquired=False,
        status="queue_timeout",
        stage="queue_admission",
        failure_kind="queue_timeout",
        queue_wait_ms=10.0,
        execution_ms=0.0,
    )
    adapter = LearningProviderCallAdapter(
        learning_lane=_Lane(order, result=lane_result),
        runtime_budget=_RuntimeBudget(order),
        gateway=_Gateway(order, result=_success_result()),
    )

    result = await adapter.call(task_name=TASK, scope_id="chat-1", prompt="prompt")

    assert result.ok is False
    assert result.failure_stage == "queue_admission"
    assert result.failure_kind == "queue_timeout"
    assert result.provider_attempt == 0
    assert order == ["L"]


@pytest.mark.asyncio
async def test_gateway_admission_timeout_is_not_provider_timeout():
    order = []
    error = GatewayQueueTimeout("gateway.background_semaphore_wait", 0.1)
    error.call_diagnostics = LLMCallDiagnostics(
        gateway_call_id="gateway-queue",
        background_semaphore_wait_ms=100.0,
        provider_request_started=False,
    )
    adapter = LearningProviderCallAdapter(
        learning_lane=_Lane(order),
        runtime_budget=_RuntimeBudget(order),
        gateway=_Gateway(order, error=error),
    )

    result = await adapter.call(task_name=TASK, scope_id="chat-1", prompt="prompt")

    assert result.failure_stage == "gateway_admission"
    assert result.failure_kind == "background_semaphore_timeout"
    assert result.provider_attempt == 0


@pytest.mark.asyncio
async def test_started_provider_timeout_and_empty_response_are_distinct():
    timeout_diag = LLMCallDiagnostics(
        gateway_call_id="gateway-timeout",
        provider_id="provider/model-a",
        provider_family="native_chat",
        model_id="provider/model-a",
        identity_source="chat_provider_id;request_id_unavailable",
        provider_latency_ms=20.0,
        provider_request_started=True,
    )
    timeout_error = LLMCascadeFailureException(
        "timed out",
        last_failure_kind=FailureKind.TIMEOUT.value,
        model_id="provider/model-a",
        call_diagnostics=timeout_diag,
    )
    timeout_adapter = LearningProviderCallAdapter(
        learning_lane=_Lane([]),
        runtime_budget=_RuntimeBudget([]),
        gateway=_Gateway([], error=timeout_error),
    )
    timeout_result = await timeout_adapter.call(
        task_name=TASK, scope_id="chat-1", prompt="prompt"
    )
    assert timeout_result.failure_stage == "provider_request"
    assert timeout_result.failure_kind == "provider_timeout"
    assert timeout_result.provider_attempt == 1

    empty_adapter = LearningProviderCallAdapter(
        learning_lane=_Lane([]),
        runtime_budget=_RuntimeBudget([]),
        gateway=_Gateway([], result=_success_result(value={})),
    )
    empty_result = await empty_adapter.call(
        task_name=TASK, scope_id="chat-1", prompt="prompt"
    )
    assert empty_result.failure_stage == "response_parse"
    assert empty_result.failure_kind == "empty_response"
    assert empty_result.provider_attempt == 1


@pytest.mark.asyncio
async def test_circuit_open_short_circuits_all_resource_admission():
    order = []
    circuit = _Circuit(
        CircuitDecision(
            allowed=False,
            reason="circuit_open",
            provider_key="id:provider/model-a",
            task_family=TASK,
            revision=3,
            retry_at=100.0,
        )
    )
    adapter = LearningProviderCallAdapter(
        learning_lane=_Lane(order),
        runtime_budget=_RuntimeBudget(order),
        gateway=_Gateway(order, result=_success_result()),
        circuit_store=circuit,
    )

    result = await adapter.call(task_name=TASK, scope_id="chat-1", prompt="prompt")

    assert result.failure_stage == "circuit"
    assert result.failure_kind == "circuit_open"
    assert result.provider_attempt == 0
    assert order == []


@pytest.mark.asyncio
async def test_cancelled_error_propagates_to_owner():
    circuit = _Circuit(
        CircuitDecision(
            allowed=True,
            reason="half_open_probe",
            provider_key="id:provider/model-a",
            task_family=TASK,
            revision=4,
            lease_owner="worker-1",
            lease_token="lease-1",
            lease_until=100.0,
        )
    )
    gateway = _BlockingGateway([])
    adapter = LearningProviderCallAdapter(
        learning_lane=_Lane([]),
        runtime_budget=_RuntimeBudget([]),
        gateway=gateway,
        circuit_store=circuit,
        owner="worker-1",
    )

    task = asyncio.create_task(
        adapter.call(task_name=TASK, scope_id="chat-1", prompt="prompt")
    )
    await gateway.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    abort = [kwargs for name, kwargs in circuit.calls if name == "abort"]
    assert len(abort) == 1
    assert abort[0]["provider_key"] == "id:provider/model-a"
    assert abort[0]["task_family"] == TASK
    assert abort[0]["owner"] == "worker-1"
    assert abort[0]["lease_token"] == "lease-1"
    assert abort[0]["expected_revision"] == 4
    assert abort[0]["settlement_id"].endswith(":abort")
    cancellation_settlement = cancelled.value.circuit_settlement
    assert cancellation_settlement["action"] == "abort"
    assert cancellation_settlement["settlement_id"].endswith(":abort")
    assert cancellation_settlement["applied"] is True
    assert cancellation_settlement["conflict"] is False
    assert cancellation_settlement["idempotent"] is False
    assert cancellation_settlement["resulting_state"] == "open"
    assert cancellation_settlement["resulting_revision"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lane_result", "runtime_error", "gateway_error"),
    [
        (
            LearningWorkResult(
                value=None,
                acquired=False,
                status="queue_timeout",
                stage="queue_admission",
                failure_kind="queue_timeout",
                queue_wait_ms=1.0,
                execution_ms=0.0,
            ),
            None,
            None,
        ),
        (None, BackgroundTaskQueueFull("full"), None),
        (None, None, GatewayQueueTimeout("gateway.background_semaphore_wait", 0.1)),
        (None, None, GatewayShutdownRejected("shutdown")),
    ],
)
async def test_pre_request_failure_aborts_half_open_probe(
    lane_result, runtime_error, gateway_error
):
    circuit = _Circuit(
        CircuitDecision(
            allowed=True,
            reason="half_open_probe",
            provider_key="id:provider/model-a",
            task_family=TASK,
            revision=7,
            lease_owner="worker-1",
            lease_token="lease-7",
            lease_until=200.0,
        )
    )
    adapter = LearningProviderCallAdapter(
        learning_lane=_Lane([], result=lane_result),
        runtime_budget=_RuntimeBudget([], error=runtime_error),
        gateway=_Gateway([], error=gateway_error, result=_success_result()),
        circuit_store=circuit,
        owner="worker-1",
    )

    result = await adapter.call(task_name=TASK, scope_id="chat-1", prompt="prompt")

    assert result.ok is False
    assert result.provider_request_started is False
    assert [name for name, _ in circuit.calls].count("abort") == 1
    assert not [name for name, _ in circuit.calls if name in {"success", "failure"}]
    assert result.circuit_settlement["action"] == "abort"
    assert result.circuit_settlement["applied"] is True


@pytest.mark.asyncio
async def test_provider_started_failure_records_failure_without_aborting_probe():
    diagnostics = LLMCallDiagnostics(
        provider_id="provider/model-a",
        provider_family="native_chat",
        model_id="provider/model-a",
        identity_source="chat_provider_id",
        provider_request_started=True,
    )
    error = LLMCascadeFailureException(
        "provider failed",
        last_failure_kind=FailureKind.UNKNOWN.value,
        model_id="provider/model-a",
        call_diagnostics=diagnostics,
    )
    circuit = _Circuit(
        CircuitDecision(
            allowed=True,
            reason="half_open_probe",
            provider_key="id:provider/model-a",
            task_family=TASK,
            revision=8,
            lease_owner="worker-1",
            lease_token="lease-8",
            lease_until=200.0,
        )
    )
    adapter = LearningProviderCallAdapter(
        learning_lane=_Lane([]),
        runtime_budget=_RuntimeBudget([]),
        gateway=_Gateway([], error=error),
        circuit_store=circuit,
        owner="worker-1",
    )

    result = await adapter.call(task_name=TASK, scope_id="chat-1", prompt="prompt")

    assert result.failure_kind == "provider_error"
    assert [name for name, _ in circuit.calls].count("failure") == 1
    assert [name for name, _ in circuit.calls].count("abort") == 0
    assert result.circuit_settlement["action"] == "failure"
    assert result.circuit_settlement["settlement_id"].endswith(":failure")
    assert result.circuit_settlement["applied"] is True
    assert result.circuit_settlement["conflict"] is False
    assert result.circuit_settlement["idempotent"] is False
    assert result.circuit_settlement["resulting_state"] == "open"
    assert result.circuit_settlement["resulting_revision"] == 2


@pytest.mark.asyncio
async def test_durable_work_attempt_replay_reuses_settlement_and_counts_failure_once(
    tmp_path,
):
    db_path = tmp_path / "adapter-replay.db"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version = 144")
        _run_migrations(db)
        db.commit()
    store = LearningProviderCircuitStore(db_path)
    diagnostics = LLMCallDiagnostics(
        provider_id="provider/model-a",
        provider_family="native_chat",
        model_id="provider/model-a",
        identity_source="chat_provider_id",
        provider_request_started=True,
    )
    error = LLMCascadeFailureException(
        "provider failed",
        last_failure_kind=FailureKind.UNKNOWN.value,
        model_id="provider/model-a",
        call_diagnostics=diagnostics,
    )
    adapter = LearningProviderCallAdapter(
        learning_lane=_Lane([]),
        runtime_budget=_RuntimeBudget([]),
        gateway=_Gateway([], error=error),
        circuit_store=store,
    )

    first = await adapter.call(
        task_name=TASK,
        scope_id="chat-1",
        prompt="prompt",
        run_id="durable-run-1",
        work_attempt_id="attempt-17",
    )
    second_attempt = await adapter.call(
        task_name=TASK,
        scope_id="chat-1",
        prompt="prompt",
        run_id="durable-run-1",
        work_attempt_id="attempt-18",
    )
    replay = await adapter.call(
        task_name=TASK,
        scope_id="chat-1",
        prompt="prompt",
        run_id="durable-run-1",
        work_attempt_id="attempt-17",
    )

    assert first.circuit_settlement["settlement_id"] == replay.circuit_settlement[
        "settlement_id"
    ]
    assert first.circuit_settlement["settlement_id"] != second_attempt.circuit_settlement[
        "settlement_id"
    ]
    assert replay.work_attempt_id == "attempt-17"
    assert replay.circuit_settlement["idempotent"] is True
    assert replay.circuit_settlement["resulting_revision"] == 1
    assert replay.circuit_settlement["current_revision"] == 2
    assert replay.circuit_revision == 2
    state = await store.get_state("id:provider/model-a", TASK)
    assert state is not None
    assert state.failure_count == 2
    assert state.revision == 2


@pytest.mark.asyncio
async def test_circuit_failure_settlement_conflict_is_retryable_and_diagnostic():
    diagnostics = LLMCallDiagnostics(
        gateway_call_id="gateway-conflict",
        provider_id="provider/model-a",
        provider_family="native_chat",
        model_id="provider/model-a",
        identity_source="chat_provider_id",
        provider_request_started=True,
    )
    error = LLMCascadeFailureException(
        "provider failed",
        last_failure_kind=FailureKind.UNKNOWN.value,
        model_id="provider/model-a",
        call_diagnostics=diagnostics,
    )
    mutation = CircuitMutation(
        applied=False,
        conflict=True,
        state=_circuit_state(revision=9, failure_count=2),
    )
    circuit = _Circuit(
        CircuitDecision(
            allowed=True,
            reason="closed",
            provider_key="id:provider/model-a",
            task_family=TASK,
            revision=8,
        ),
        mutations={"failure": mutation},
    )
    adapter = LearningProviderCallAdapter(
        learning_lane=_Lane([]),
        runtime_budget=_RuntimeBudget([]),
        gateway=_Gateway([], error=error),
        circuit_store=circuit,
    )

    result = await adapter.call(
        task_name=TASK,
        scope_id="chat-1",
        prompt="prompt",
        run_id="run-conflict",
    )

    assert result.ok is False
    assert result.failure_stage == "circuit_settlement"
    assert result.failure_kind == "circuit_settlement_conflict"
    assert result.retryable is True
    assert result.circuit_revision == 9
    assert result.circuit_settlement["action"] == "failure"
    assert result.circuit_settlement["conflict"] is True
    assert result.diagnostics["original_failure_stage"] == "provider_request"
    assert result.diagnostics["original_failure_kind"] == "provider_error"


@pytest.mark.asyncio
async def test_abort_settlement_conflict_overrides_admission_result():
    lane_result = LearningWorkResult(
        value=None,
        acquired=False,
        status="queue_timeout",
        stage="queue_admission",
        failure_kind="queue_timeout",
        queue_wait_ms=1.0,
        execution_ms=0.0,
    )
    circuit = _Circuit(
        CircuitDecision(
            allowed=True,
            reason="half_open_probe",
            provider_key="id:provider/model-a",
            task_family=TASK,
            revision=4,
            lease_owner="worker-a",
            lease_token="lease-a",
            lease_until=100.0,
        ),
        mutations={
            "abort": CircuitMutation(
                applied=False,
                conflict=True,
                state=_circuit_state(state="open", revision=5),
            )
        },
    )
    adapter = LearningProviderCallAdapter(
        learning_lane=_Lane([], result=lane_result),
        runtime_budget=_RuntimeBudget([]),
        gateway=_Gateway([], result=_success_result()),
        circuit_store=circuit,
        owner="worker-a",
    )

    result = await adapter.call(task_name=TASK, scope_id="chat-1", prompt="prompt")

    assert result.failure_stage == "circuit_settlement"
    assert result.failure_kind == "circuit_settlement_conflict"
    assert result.circuit_settlement["action"] == "abort"
    assert result.diagnostics["original_failure_kind"] == "queue_timeout"


@pytest.mark.asyncio
async def test_success_settlement_conflict_does_not_report_business_success():
    circuit = _Circuit(
        CircuitDecision(
            allowed=True,
            reason="closed",
            provider_key="id:provider/model-a",
            task_family=TASK,
            revision=4,
        ),
        mutations={
            "success": CircuitMutation(
                applied=False,
                conflict=True,
                state=_circuit_state(state="open", revision=5),
            )
        },
    )
    adapter = LearningProviderCallAdapter(
        learning_lane=_Lane([]),
        runtime_budget=_RuntimeBudget([]),
        gateway=_Gateway([], result=_success_result(value={"items": [1]})),
        circuit_store=circuit,
    )

    result = await adapter.call(task_name=TASK, scope_id="chat-1", prompt="prompt")

    assert result.ok is False
    assert result.failure_stage == "circuit_settlement"
    assert result.failure_kind == "circuit_settlement_conflict"
    assert result.circuit_settlement["action"] == "success"
    assert result.diagnostics["original_ok"] is True


@pytest.mark.asyncio
async def test_adapter_binds_selected_provider_and_reports_complete_contract():
    circuit = _Circuit(
        CircuitDecision(
            allowed=True,
            reason="half_open_probe",
            provider_key="id:provider/model-b",
            task_family=TASK,
            revision=11,
            circuit_until=80.0,
            lease_owner="worker-b",
            lease_token="lease-b",
            lease_until=120.0,
        ),
        mutations={
            "success": CircuitMutation(
                applied=True,
                state=_circuit_state(revision=12),
            )
        },
    )
    gateway = _Gateway([], result=_success_result(), selected_model="provider/model-b")
    gateway.result.call_diagnostics = LLMCallDiagnostics(
        gateway_call_id="gateway-b",
        provider_request_id="request-b",
        provider_id="provider/model-b",
        provider_family="native_chat",
        model_id="provider/model-b",
        identity_source="chat_provider_id",
        provider_request_started=True,
    )
    adapter = LearningProviderCallAdapter(
        learning_lane=_Lane([]),
        runtime_budget=_RuntimeBudget([]),
        gateway=gateway,
        circuit_store=circuit,
        owner="worker-b",
    )

    result = await adapter.call(
        task_name=TASK,
        scope_id="chat-1",
        prompt="prompt",
        run_id="run-1",
        work_attempt_id="attempt-contract-1",
    )

    assert result.ok is True
    assert gateway.kwargs["selected_model_id"] == "provider/model-b"
    assert result.run_id == "run-1"
    assert result.work_attempt_id == "attempt-contract-1"
    assert result.task_name == TASK
    assert result.scope_id == "chat-1"
    assert result.provider_request_started is True
    assert result.circuit_revision == 12
    assert result.circuit_until == 0.0
    assert result.lease_owner == ""
    assert result.lease_token == ""
    assert result.lease_until == 0.0
    assert result.circuit_settlement["action"] == "success"
    assert result.circuit_settlement["applied"] is True
    assert result.circuit_settlement["resulting_revision"] == 12
    assert result.circuit_settlement["current_revision"] == 12
    report = result.to_report()
    for field in (
        "run_id",
        "work_attempt_id",
        "task_name",
        "scope_id",
        "provider_request_started",
        "circuit_revision",
        "circuit_until",
        "lease_owner",
        "lease_token",
        "lease_until",
    ):
        assert field in report


@pytest.mark.asyncio
async def test_identity_mismatch_fails_closed_and_does_not_mutate_wrong_circuit():
    circuit = _Circuit(
        CircuitDecision(
            allowed=True,
            reason="half_open_probe",
            provider_key="id:provider/model-b",
            task_family=TASK,
            revision=12,
            lease_owner="worker-b",
            lease_token="lease-b",
            lease_until=120.0,
        )
    )
    gateway = _Gateway([], result=_success_result(), selected_model="provider/model-b")
    adapter = LearningProviderCallAdapter(
        learning_lane=_Lane([]),
        runtime_budget=_RuntimeBudget([]),
        gateway=gateway,
        circuit_store=circuit,
        owner="worker-b",
    )

    result = await adapter.call(task_name=TASK, scope_id="chat-1", prompt="prompt")

    assert result.ok is False
    assert result.failure_kind == "provider_identity_mismatch"
    assert [name for name, _ in circuit.calls].count("abort") == 1
    assert [name for name, _ in circuit.calls].count("success") == 0
    assert [name for name, _ in circuit.calls].count("failure") == 0


@pytest.mark.asyncio
async def test_exception_identity_mismatch_does_not_mutate_wrong_circuit():
    diagnostics = LLMCallDiagnostics(
        provider_id="provider/model-a",
        provider_family="native_chat",
        model_id="provider/model-a",
        identity_source="chat_provider_id",
        provider_request_started=True,
    )
    error = LLMCascadeFailureException(
        "provider failed",
        last_failure_kind=FailureKind.UNKNOWN.value,
        model_id="provider/model-a",
        call_diagnostics=diagnostics,
    )
    circuit = _Circuit(
        CircuitDecision(
            allowed=True,
            reason="half_open_probe",
            provider_key="id:provider/model-b",
            task_family=TASK,
            revision=13,
            lease_owner="worker-b",
            lease_token="lease-b",
            lease_until=120.0,
        )
    )
    adapter = LearningProviderCallAdapter(
        learning_lane=_Lane([]),
        runtime_budget=_RuntimeBudget([]),
        gateway=_Gateway([], error=error, selected_model="provider/model-b"),
        circuit_store=circuit,
        owner="worker-b",
    )

    result = await adapter.call(task_name=TASK, scope_id="chat-1", prompt="prompt")

    assert result.failure_kind == "provider_identity_mismatch"
    assert [name for name, _ in circuit.calls].count("abort") == 1
    assert [name for name, _ in circuit.calls].count("failure") == 0
