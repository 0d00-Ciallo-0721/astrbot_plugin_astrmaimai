import asyncio
import sqlite3
import time
from types import SimpleNamespace

import pytest

from astrmai.infrastructure.gateway.model_gateway import GlobalModelGateway
from astrmai.infrastructure.gateway.gateway_exceptions import (
    GatewayQueueTimeout,
    ProviderRequestStartRejected,
    LLMCascadeFailureException,
)
from astrmai.infrastructure.runtime.lane_manager import LaneKey
from astrmai.infrastructure.runtime.runtime_contracts import (
    FailureKind,
    LLMCallDiagnostics,
    LLMCallResult,
)
from astrmai.shared.constants.defaults import GatewaySettings


TASK = "learning.expression_enrichment"


class _Response:
    def __init__(self, text: str, request_id: str):
        self.completion_text = text
        self.id = request_id
        self.usage = SimpleNamespace(input=3, input_cached=0, output=2)


class _Context:
    def __init__(self):
        self.calls = []
        self.hold_first = asyncio.Event()

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        prompt = str(kwargs.get("prompt") or "")
        if prompt == "hold":
            await self.hold_first.wait()
        return _Response('{"prompt":"%s"}' % prompt, f"request-{prompt}")


def _gateway(context: _Context) -> GlobalModelGateway:
    settings = GatewaySettings(
        max_concurrent_llm_calls=2,
        critical_path_reserved_slots=1,
        llm_retries=0,
        api_timeout=1.0,
        semaphore_wait_timeout_sec=1.0,
        task_models=("openai/model-a",),
    )
    return GlobalModelGateway(context, SimpleNamespace(), settings=settings)


def _multi_model_gateway(context: _Context) -> GlobalModelGateway:
    settings = GatewaySettings(
        max_concurrent_llm_calls=2,
        critical_path_reserved_slots=1,
        llm_retries=0,
        api_timeout=1.0,
        semaphore_wait_timeout_sec=1.0,
        task_models=("openai/model-a", "openai/model-b"),
    )
    return GlobalModelGateway(context, SimpleNamespace(), settings=settings)


def _lane(scope: str) -> LaneKey:
    return LaneKey(
        subsystem="bg",
        task_family=TASK,
        scope_id=scope,
        scope_kind="chat",
    )


@pytest.mark.asyncio
async def test_data_process_result_is_additive_and_legacy_wrapper_is_compatible():
    context = _Context()
    gateway = _gateway(context)

    result = await gateway.call_data_process_task_result(
        prompt="result",
        is_json=True,
        lane_key=_lane("chat-1"),
        base_origin="chat-1",
        max_retries_override=0,
        max_models_override=1,
        use_fallback=False,
        reserve_for_reply=False,
    )
    legacy = await gateway.call_data_process_task(
        prompt="legacy",
        is_json=True,
        lane_key=_lane("chat-1"),
        base_origin="chat-1",
        max_retries_override=0,
        max_models_override=1,
        use_fallback=False,
        reserve_for_reply=False,
    )

    assert isinstance(result, LLMCallResult)
    assert isinstance(result.call_diagnostics, LLMCallDiagnostics)
    assert result.parsed_json == {"prompt": "result"}
    assert legacy == {"prompt": "legacy"}
    assert result.call_diagnostics.provider_id == "openai/model-a"
    assert result.call_diagnostics.model_id == "openai/model-a"
    assert result.call_diagnostics.identity_source == "chat_provider_id"
    assert result.call_diagnostics.provider_request_id == "request-result"
    assert result.call_diagnostics.provider_request_started is True
    assert result.call_diagnostics.provider_latency_ms >= 0
    assert result.fallback_used is False


@pytest.mark.asyncio
async def test_selected_provider_follows_router_order_and_binds_actual_call(monkeypatch):
    context = _Context()
    gateway = _multi_model_gateway(context)

    monkeypatch.setattr(
        gateway.router,
        "get_ranked_models",
        lambda _pool, models, **_kwargs: list(reversed(models)),
    )
    selection = gateway.select_data_process_provider(
        prompt="selected",
        is_json=True,
        lane_key=_lane("chat-selected"),
        base_origin="chat-selected",
        use_fallback=False,
        allow_cooldown_override=False,
    )

    assert selection.provider_id == "openai/model-b"
    assert selection.model_id == "openai/model-b"
    result = await gateway.call_data_process_task_result(
        prompt="selected",
        is_json=True,
        lane_key=_lane("chat-selected"),
        base_origin="chat-selected",
        max_retries_override=0,
        max_models_override=1,
        use_fallback=False,
        allow_cooldown_override=False,
        selected_model_id=selection.model_id,
    )

    assert result.call_diagnostics is not None
    assert result.call_diagnostics.provider_id == selection.provider_id
    assert context.calls[-1]["chat_provider_id"] == "openai/model-b"


@pytest.mark.asyncio
async def test_provider_start_hook_runs_after_gateway_admission_before_provider():
    order = []

    class _OrderedContext(_Context):
        async def llm_generate(self, **kwargs):
            order.append("Provider")
            return await super().llm_generate(**kwargs)

    context = _OrderedContext()
    gateway = _gateway(context)

    async def _mark_started(identity):
        order.append("Fence")
        assert identity.provider_id == "openai/model-a"
        assert identity.model_id == "openai/model-a"
        assert identity.gateway_call_id
        return True

    result = await gateway.call_data_process_task_result(
        prompt="fenced",
        is_json=True,
        lane_key=_lane("chat-fenced"),
        base_origin="chat-fenced",
        max_retries_override=0,
        max_models_override=1,
        use_fallback=False,
        on_provider_request_start=_mark_started,
    )

    assert result.ok is True
    assert order == ["Fence", "Provider"]


@pytest.mark.asyncio
async def test_provider_start_hook_rejection_prevents_provider_call():
    context = _Context()
    gateway = _gateway(context)

    async def _reject(_identity):
        return SimpleNamespace(
            applied=False,
            conflict=True,
            failure_stage="provider_start_fence",
            failure_kind="cas_conflict",
        )

    with pytest.raises(ProviderRequestStartRejected) as raised:
        await gateway.call_data_process_task_result(
            prompt="rejected",
            is_json=True,
            lane_key=_lane("chat-rejected"),
            base_origin="chat-rejected",
            max_retries_override=0,
            max_models_override=1,
            use_fallback=False,
            on_provider_request_start=_reject,
        )

    assert raised.value.failure_kind == "cas_conflict"
    assert raised.value.call_diagnostics.provider_request_started is False
    assert context.calls == []


@pytest.mark.asyncio
async def test_provider_start_persistence_lock_is_fail_closed_before_provider():
    context = _Context()
    gateway = _gateway(context)

    async def _locked(_identity):
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(ProviderRequestStartRejected) as raised:
        await gateway.call_data_process_task_result(
            prompt="locked",
            is_json=True,
            lane_key=_lane("chat-locked"),
            base_origin="chat-locked",
            max_retries_override=0,
            max_models_override=1,
            use_fallback=False,
            on_provider_request_start=_locked,
        )

    assert raised.value.failure_stage == "persistence"
    assert raised.value.failure_kind == "persist_locked"
    assert raised.value.call_diagnostics.provider_request_started is False
    assert context.calls == []


def test_selected_provider_skips_cooled_first_model_without_claiming_probe(monkeypatch):
    context = _Context()
    gateway = _multi_model_gateway(context)
    monkeypatch.setattr(
        gateway.router,
        "get_ranked_models",
        lambda _pool, models, **_kwargs: list(models),
    )
    gateway._model_cooldowns[(TASK, "openai/model-a")] = {
        "until": time.monotonic() + 60.0,
        "reason": "provider_5xx",
    }

    selection = gateway.select_data_process_provider(
        prompt="selected",
        is_json=True,
        lane_key=_lane("chat-selected"),
        base_origin="chat-selected",
        use_fallback=False,
        allow_cooldown_override=False,
    )

    assert selection.model_id == "openai/model-b"


@pytest.mark.asyncio
async def test_background_and_global_wait_diagnostics_are_per_call_and_do_not_cross():
    context = _Context()
    gateway = _gateway(context)

    first_task = asyncio.create_task(
        gateway.call_data_process_task_result(
            prompt="hold",
            is_json=True,
            lane_key=_lane("chat-hold"),
            base_origin="chat-hold",
            max_retries_override=0,
            max_models_override=1,
            use_fallback=False,
        )
    )
    for _ in range(100):
        if context.calls:
            break
        await asyncio.sleep(0)

    second_task = asyncio.create_task(
        gateway.call_data_process_task_result(
            prompt="second",
            is_json=True,
            lane_key=_lane("chat-second"),
            base_origin="chat-second",
            max_retries_override=0,
            max_models_override=1,
            use_fallback=False,
        )
    )
    for _ in range(100):
        waiters = getattr(gateway._background_semaphore, "_waiters", None)
        if waiters:
            break
        await asyncio.sleep(0)
    assert getattr(gateway._background_semaphore, "_waiters", None)
    await asyncio.sleep(0.02)
    context.hold_first.set()
    first, second = await asyncio.gather(first_task, second_task)

    first_diag = first.call_diagnostics
    second_diag = second.call_diagnostics
    assert first_diag is not None and second_diag is not None
    assert first_diag.gateway_call_id != second_diag.gateway_call_id
    assert first_diag.provider_request_id == "request-hold"
    assert second_diag.provider_request_id == "request-second"
    assert second_diag.background_semaphore_wait_ms >= 10.0
    assert second_diag.global_semaphore_wait_ms >= 0.0
    assert first_diag.provider_latency_ms >= 10.0


@pytest.mark.asyncio
async def test_monotonic_hard_deadline_bounds_gateway_admission_before_provider_start():
    context = _Context()
    gateway = _gateway(context)
    await gateway._background_semaphore.acquire()
    try:
        with pytest.raises(GatewayQueueTimeout) as raised:
            await gateway.call_data_process_task_result(
                prompt="queued",
                is_json=True,
                lane_key=_lane("chat-queued"),
                base_origin="chat-queued",
                max_retries_override=0,
                max_models_override=1,
                use_fallback=False,
                hard_deadline_monotonic=time.monotonic() + 0.02,
            )
        diagnostics = raised.value.call_diagnostics
        assert raised.value.stage == "gateway.background_semaphore_wait"
        assert diagnostics.provider_request_started is False
        assert context.calls == []
    finally:
        gateway._background_semaphore.release()


@pytest.mark.asyncio
async def test_monotonic_hard_deadline_classifies_started_request_as_provider_timeout():
    context = _Context()
    gateway = _gateway(context)

    with pytest.raises(LLMCascadeFailureException) as raised:
        await gateway.call_data_process_task_result(
            prompt="hold",
            is_json=True,
            lane_key=_lane("chat-timeout"),
            base_origin="chat-timeout",
            max_retries_override=0,
            max_models_override=1,
            use_fallback=False,
            hard_deadline_monotonic=time.monotonic() + 0.03,
        )

    diagnostics = raised.value.call_diagnostics
    assert diagnostics.provider_request_started is True
    assert diagnostics.provider_latency_ms >= 10.0
    assert raised.value.last_failure_kind == FailureKind.TIMEOUT.value
