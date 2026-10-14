import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrmai.conversation.execution.reply_artifact_builder import ReplyArtifactMixin
from astrmai.conversation.contracts.reply_artifact import VisibleReplyArtifact
from astrmai.conversation.contracts.focus_context import FreshnessState
from astrmai.conversation.contracts.turn_identity import TurnIdentity, build_turn_send_key
from astrmai.infrastructure.runtime.chat_runtime_coordinator import ChatRuntimeCoordinator

from tests.helpers.atwake_stage00_fixtures import (
    FakeEvent, FakeEvolution, FakeGateway, executor_fixture,
)


def test_host_send_errors_are_delivery_failures_not_model_failures():
    from astrmai.conversation.execution.delivery_failure import send_host_message, DeliveryFailure

    error = RuntimeError("connection lost after request")
    context = SimpleNamespace(send_message=AsyncMock(side_effect=error))
    with pytest.raises(DeliveryFailure) as raised:
        asyncio.run(send_host_message(context, "ff:GroupMessage:g", object()))
    assert raised.value.kind == "uncertain"
    assert raised.value.__cause__ is error
    assert context.send_message.await_count == 1


def test_membership_rejection_requires_explicit_group_exit_evidence():
    from astrmai.conversation.execution.delivery_failure import classify_delivery_failure

    rejected = classify_delivery_failure(
        RuntimeError('EventChecker result=110: "发送失败，你已被移出该群"')
    )
    generic = classify_delivery_failure(RuntimeError("retcode=1200 action failed"))
    assert rejected.kind == "rejected"
    assert generic.kind == "uncertain"


@pytest.mark.parametrize("value", [False, True, None, "message-id"])
def test_host_receipt_contract_is_preserved(value):
    from astrmai.conversation.execution.delivery_failure import send_host_message

    context = SimpleNamespace(send_message=AsyncMock(return_value=value))
    assert asyncio.run(send_host_message(context, "ff:GroupMessage:g", object())) is value


def test_host_send_cancellation_propagates():
    from astrmai.conversation.execution.delivery_failure import send_host_message

    context = SimpleNamespace(send_message=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(send_host_message(context, "ff:GroupMessage:g", object()))


@pytest.mark.asyncio
async def test_uncertain_plain_send_keeps_real_send_claim_non_retryable():
    from astrmai.conversation.execution.reply_artifact_builder import ReplyArtifactMixin
    from astrmai.conversation.contracts.reply_artifact import VisibleReplyArtifact
    from astrmai.conversation.contracts.focus_context import FreshnessState
    from astrmai.conversation.contracts.turn_identity import TurnIdentity, build_turn_send_key
    from astrmai.infrastructure.runtime.chat_runtime_coordinator import ChatRuntimeCoordinator

    context = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError("receipt lost")))
    coordinator = ChatRuntimeCoordinator()
    event = FakeEvent()
    turn = TurnIdentity("group", event.unified_msg_origin, "thread", 1)
    event.set_extra("astrmai_turn_identity", turn)
    sender = ReplyArtifactMixin()
    sender.config = SimpleNamespace()
    sender.state_engine = SimpleNamespace(gateway=SimpleNamespace(context=context))
    sender.runtime_coordinator = coordinator
    sender._check_reply_freshness = AsyncMock(return_value=(FreshnessState.FRESH, ""))
    artifact = VisibleReplyArtifact("body", ["body"], "body")
    with pytest.raises(TimeoutError):
        await sender._send_segments(event, event.unified_msg_origin, artifact, [])
    claim = await coordinator.get_send_claim(event.unified_msg_origin, build_turn_send_key(turn))
    assert claim["status"] == "claimed"
    assert await coordinator.claim_send(event.unified_msg_origin, build_turn_send_key(turn)) is False
    assert artifact.sent is False
    assert artifact.metadata["delivery_uncertain"] is True
    context.send_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [False, True, "exception", "cancelled"])
async def test_tts_only_delivery_preserves_false_uncertain_and_cancelled_boundaries(receipt):
    from astrmai.conversation.execution.reply_artifact_builder import ReplyArtifactMixin
    from astrmai.conversation.contracts.reply_artifact import VisibleReplyArtifact
    from astrmai.conversation.contracts.focus_context import FreshnessState
    from astrmai.conversation.contracts.turn_identity import TurnIdentity, build_turn_send_key
    from astrmai.infrastructure.runtime.chat_runtime_coordinator import ChatRuntimeCoordinator

    context = SimpleNamespace(send_message=AsyncMock(return_value=receipt))
    if receipt == "exception":
        context.send_message.side_effect = TimeoutError("tts receipt lost")
    elif receipt == "cancelled":
        context.send_message.side_effect = asyncio.CancelledError()
    event = FakeEvent()
    coordinator = ChatRuntimeCoordinator()
    turn = TurnIdentity("group", event.unified_msg_origin, "tts-thread", 1)
    event.set_extra("astrmai_turn_identity", turn)
    sender = ReplyArtifactMixin()
    sender.config = SimpleNamespace()
    sender.state_engine = SimpleNamespace(gateway=SimpleNamespace(context=context))
    sender.runtime_coordinator = coordinator
    sender._check_reply_freshness = AsyncMock(return_value=(FreshnessState.FRESH, ""))
    sender.tts_bridge = SimpleNamespace(
        should_try_tts=lambda *args: True,
        should_send_text=lambda: False,
        build_tts_chain=AsyncMock(return_value=[object()]),
    )
    artifact = VisibleReplyArtifact("body", ["body"], "body")
    if receipt in ("exception", "cancelled"):
        with pytest.raises(TimeoutError if receipt == "exception" else asyncio.CancelledError):
            await sender._send_segments(event, event.unified_msg_origin, artifact, [])
    else:
        assert await sender._send_segments(event, event.unified_msg_origin, artifact, []) is receipt
    claim = await coordinator.get_send_claim(event.unified_msg_origin, build_turn_send_key(turn))
    assert claim["status"] == ("failed" if receipt is False else "committed" if receipt is True else "claimed")
    if receipt is False:
        assert artifact.metadata["send_failure_reason"] == "host_send_returned_false"
        assert not event.get_extra("astrmai_reply_sent", False)
    context.send_message.assert_awaited_once()


@pytest.mark.parametrize("mode", ["chat", "tool"])
@pytest.mark.parametrize("kind", ["rejected", "uncertain"])
def test_delivery_failure_stops_executor_cascade(mode, kind):
    from astrmai.conversation.execution.delivery_failure import DeliveryFailure

    with executor_fixture() as mod:
        gateway = FakeGateway(models=["model-a", "model-b"])
        async def _chat(**kwargs):
            gateway.calls.append(("chat", kwargs))
            return SimpleNamespace(text="ordinary reply")
        gateway.chat_in_lane_result = _chat
        event = FakeEvent()
        reply = SimpleNamespace(handle_reply=AsyncMock(return_value=SimpleNamespace(sent=True)))
        executor = mod.ConcurrentExecutor(
            context=SimpleNamespace(), gateway=gateway, reply_engine=reply,
            evolution_manager=FakeEvolution(),
        )
        executor._finalize_reply = AsyncMock(side_effect=DeliveryFailure(kind, "platform failure"))
        runtime = {
            "dialog_lane_key": SimpleNamespace(), "dialog_base_origin": event.unified_msg_origin,
            "prefix_hash": "hash", "raw_user_text": "hello", "bot_id": "bot",
            "max_steps": 2, "timeout": 5,
        }
        if mode == "chat":
            result = asyncio.run(executor._run_text_mode(event, event.unified_msg_origin, "prompt", "system", runtime))
        else:
            result = asyncio.run(executor._run_tool_mode(event, event.unified_msg_origin, event, "prompt", "system", [SimpleNamespace(name="lookup")], runtime))
        assert result is None
        assert len(gateway.calls) == 1
        assert reply.handle_reply.await_count == 0
        assert event.get_extra("astrmai_execution_status") == "delivery_" + kind
        assert not event.get_extra("astrmai_reply_sent", False)


def test_non_delivery_model_error_still_switches_models():
    with executor_fixture() as mod:
        gateway = FakeGateway(models=["model-a", "model-b"], tool_responses={"model-a": RuntimeError("provider unavailable")})
        reply = SimpleNamespace(handle_reply=AsyncMock(return_value=SimpleNamespace(sent=True)))
        executor = mod.ConcurrentExecutor(
            context=SimpleNamespace(), gateway=gateway, reply_engine=reply,
            evolution_manager=FakeEvolution(),
        )
        asyncio.run(executor.execute(FakeEvent(), "prompt", "system", tools=[SimpleNamespace(name="lookup")]))
        assert len(gateway.calls) == 2
        assert reply.handle_reply.await_count == 1


def test_false_visible_receipt_is_delivery_rejection_not_model_failure():
    with executor_fixture() as mod:
        gateway = FakeGateway(models=["model-a", "model-b"])
        gateway.chat_in_lane_result = AsyncMock(return_value=SimpleNamespace(text="ordinary reply"))
        reply = SimpleNamespace(
            handle_reply=AsyncMock(
                return_value=SimpleNamespace(
                    sent=False,
                    metadata={
                        "send_status": "failed",
                        "send_failure_reason": "host_send_returned_false",
                    },
                )
            )
        )
        executor = mod.ConcurrentExecutor(
            context=SimpleNamespace(), gateway=gateway, reply_engine=reply,
            evolution_manager=FakeEvolution(),
        )
        event = FakeEvent()
        result = asyncio.run(executor.execute(event, "prompt", "system"))
        assert result is None
        assert gateway.chat_in_lane_result.await_count == 1
        assert reply.handle_reply.await_count == 1
        assert event.get_extra("astrmai_execution_status") == "delivery_rejected"


@pytest.mark.parametrize("mode", ["chat", "tool"])
def test_marked_original_delivery_exception_preserves_type_and_stops_cascade(mode):
    from astrmai.conversation.execution.delivery_failure import classify_delivery_failure

    with executor_fixture() as mod:
        gateway = FakeGateway(models=["model-a", "model-b"])

        async def _chat(**kwargs):
            gateway.calls.append(("chat", kwargs))
            return SimpleNamespace(text="ordinary reply")

        gateway.chat_in_lane_result = _chat
        error = TimeoutError("receipt lost")
        classify_delivery_failure(error)
        executor = mod.ConcurrentExecutor(
            context=SimpleNamespace(), gateway=gateway,
            reply_engine=SimpleNamespace(handle_reply=AsyncMock()),
            evolution_manager=FakeEvolution(),
        )
        executor._finalize_reply = AsyncMock(side_effect=error)
        event = FakeEvent()
        runtime = {
            "dialog_lane_key": SimpleNamespace(),
            "dialog_base_origin": event.unified_msg_origin,
            "prefix_hash": "hash",
            "raw_user_text": "hello",
            "bot_id": "bot",
            "max_steps": 2,
            "timeout": 5,
        }
        if mode == "chat":
            result = asyncio.run(
                executor._run_text_mode(
                    event, event.unified_msg_origin, "prompt", "system", runtime
                )
            )
        else:
            result = asyncio.run(
                executor._run_tool_mode(
                    event,
                    event.unified_msg_origin,
                    event,
                    "prompt",
                    "system",
                    [SimpleNamespace(name="lookup")],
                    runtime,
                )
            )
        assert result is None
        assert len(gateway.calls) == 1
        assert event.get_extra("astrmai_execution_status") == "delivery_uncertain"
