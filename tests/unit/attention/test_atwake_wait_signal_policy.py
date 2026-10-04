import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers.atwake_stage00_fixtures import executor_fixture


_ISOLATED_MODULES = (
    "astrbot",
    "astrbot.api.message_components",
    "astrbot.api.all",
    "astrbot.core.agent.tool",
    "astrmai.Brain.reply_engine",
    "astrmai.infra.lane_manager",
    "astrmai.workmode",
    "astrmai.conversation.execution.executor",
)


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("abort", [None, RuntimeError, asyncio.CancelledError])
def test_executor_fixture_restores_modules_and_temp_directory(monkeypatch, missing, abort):
    # The outer context supplies known existing objects for the identity case;
    # the inner one must also preserve deliberately absent modules on abort.
    with executor_fixture():
        with monkeypatch.context() as modules:
            if missing:
                for name in _ISOLATED_MODULES:
                    modules.delitem(sys.modules, name)
            snapshot = {name: sys.modules[name] for name in _ISOLATED_MODULES if name in sys.modules}

            def run():
                nonlocal temp_path
                with executor_fixture() as module:
                    assert sys.modules["astrmai.conversation.execution.executor"] is module
                    temp_path = Path(sys.modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path())
                    assert temp_path.is_dir()
                    if abort is not None:
                        raise abort()

            temp_path = None
            if abort is None:
                run()
            else:
                with pytest.raises(abort):
                    run()
            for name in _ISOLATED_MODULES:
                assert (name in sys.modules) == (name in snapshot), name
                if name in snapshot:
                    assert sys.modules[name] is snapshot[name], name
            assert temp_path is not None and not temp_path.exists()


class _Gateway:
    def __init__(self, response):
        self.response = response
        self.config = SimpleNamespace(
            agent=SimpleNamespace(max_steps=5, timeout=10),
            infra=SimpleNamespace(api_timeout=15),
            global_settings=SimpleNamespace(
                debug_mode=False,
                enable_error_interception=False,
                admin_ids=[],
            ),
            reply=SimpleNamespace(fallback_text="fallback"),
            vision=SimpleNamespace(
                enable_vision=False,
                image_recognition_probability=1.0,
                use_native_main_reply_vision=False,
                native_main_reply_failure_cooldown_sec=180,
            ),
        )
        self.calls = []

    def get_agent_models(self):
        return ["model-a"]

    async def tool_chat_in_lane_result(self, **kwargs):
        self.calls.append(kwargs)
        response = self.response
        if callable(response):
            response = response(kwargs)
        if isinstance(response, BaseException):
            raise response
        return SimpleNamespace(text=response)


class _Reply:
    def __init__(self, result="sent", *, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def handle_reply(self, event, text, chat_id):
        self.calls.append((chat_id, text))
        if self.error is not None:
            raise self.error
        if self.result is False:
            return SimpleNamespace(sent=False, blocked_reason="transport_failed", metadata={})
        event.set_extra("astrmai_reply_sent", True)
        return SimpleNamespace(sent=True, persistable_text=text, metadata={})


class _Event:
    def __init__(self):
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self.message_str = "@bot help"
        self._extra = {}

    def get_self_id(self):
        return "bot-1"

    def get_group_id(self):
        return "group-1"

    def get_sender_id(self):
        return "user-1"

    def get_sender_name(self):
        return "User"

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


@pytest.fixture
def _executor():
    snapshot = {name: sys.modules[name] for name in _ISOLATED_MODULES if name in sys.modules}
    with executor_fixture() as module:
        def build(response, reply=None):
            gateway = _Gateway(response)
            reply = reply or _Reply()
            executor = module.ConcurrentExecutor(
                context=SimpleNamespace(),
                gateway=gateway,
                reply_engine=reply,
                evolution_manager=SimpleNamespace(),
                config=gateway.config,
            )
            return executor, gateway, reply

        yield build
    for name in _ISOLATED_MODULES:
        assert (name in sys.modules) == (name in snapshot), name
        if name in snapshot:
            assert sys.modules[name] is snapshot[name], name


@pytest.mark.asyncio
async def test_strong_wait_signal_uses_visible_ack_and_unwraps_signal(_executor):
    executor, _, reply = _executor("[SYSTEM_WAIT_SIGNAL]")
    event = _Event()
    event.set_extra("astrmai_is_strong_wakeup", True)

    result = await executor.execute(event, "prompt", "system", tools=[object()])

    assert result == "收到，我在看。"
    assert reply.calls == [(event.unified_msg_origin, "收到，我在看。")]
    assert event.get_extra("astrmai_wait_signal_observed") is True
    assert event.get_extra("astrmai_wait_policy_decision") == "fallback"
    assert event.get_extra("astrmai_execution_signal") != "wait"
    assert event.get_extra("astrmai_execution_status") == "sent"


@pytest.mark.asyncio
async def test_ordinary_wait_signal_remains_skipped_wait(_executor):
    executor, _, reply = _executor("[SYSTEM_WAIT_SIGNAL]")
    event = _Event()

    result = await executor.execute(event, "prompt", "system", tools=[object()])

    assert result is None
    assert reply.calls == []
    assert event.get_extra("astrmai_execution_signal") == "wait"
    assert event.get_extra("astrmai_execution_status") == "skipped_wait"
    assert event.get_extra("astrmai_wait_policy_decision") == "allow_wait"


@pytest.mark.asyncio
async def test_strong_wait_false_receipt_is_not_success(_executor):
    reply = _Reply(result=False)
    executor, _, _ = _executor("[SYSTEM_WAIT_SIGNAL]", reply)
    event = _Event()
    event.set_extra("astrmai_is_strong_wakeup", True)

    result = await executor.execute(event, "prompt", "system", tools=[object()])

    assert result is None
    assert event.get_extra("astrmai_execution_status") == "send_failed"
    assert event.get_extra("astrmai_reply_sent", False) is False


@pytest.mark.asyncio
async def test_strong_wait_cancel_propagates_without_success(_executor):
    reply = _Reply(error=asyncio.CancelledError())
    executor, _, _ = _executor("[SYSTEM_WAIT_SIGNAL]", reply)
    event = _Event()
    event.set_extra("astrmai_is_strong_wakeup", True)

    with pytest.raises(asyncio.CancelledError):
        await executor.execute(event, "prompt", "system", tools=[object()])

    assert event.get_extra("astrmai_reply_sent", False) is False


@pytest.mark.asyncio
async def test_strong_wait_does_not_duplicate_consumed_output_claim(_executor):
    executor, _, reply = _executor("[SYSTEM_WAIT_SIGNAL]")
    event = _Event()
    event.set_extra("astrmai_is_strong_wakeup", True)
    event.set_extra("astrmai_reply_sent", True)

    result = await executor.execute(event, "prompt", "system", tools=[object()])

    assert result is None
    assert reply.calls == []
    assert event.get_extra("astrmai_wait_policy_reason") == "send_claim_consumed"


@pytest.mark.asyncio
async def test_wait_target_keeps_strong_wakeup_deferred(_executor):
    executor, _, reply = _executor("[SYSTEM_WAIT_SIGNAL]")
    event = _Event()
    event.set_extra("astrmai_is_strong_wakeup", True)
    event.set_extra("astrmai_wait_targets", [{"kind": "tool", "id": "job-1"}])

    result = await executor.execute(event, "prompt", "system", tools=[object()])

    assert result is None
    assert reply.calls == []
    assert event.get_extra("astrmai_wait_policy_reason") == "wait_target_present"
    assert event.get_extra("astrmai_execution_status") == "skipped_wait"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"astrmai_reply_wakeup": True},
        {"astrmai_group_direct_wakeup": True, "astrmai_image_urls": ["img-1"]},
        {"astrmai_risk_flags": ["danger"]},
    ],
)
async def test_structured_direct_or_risk_wakeup_never_silently_waits(extra, _executor):
    executor, _, reply = _executor("[SYSTEM_WAIT_SIGNAL]")
    event = _Event()
    for key, value in extra.items():
        event.set_extra(key, value)

    result = await executor.execute(event, "prompt", "system", tools=[object()])

    assert result == "收到，我在看。"
    assert reply.calls == [(event.unified_msg_origin, "收到，我在看。")]
    assert "[SYSTEM_WAIT_SIGNAL]" not in reply.calls[0][1]
    assert event.get_extra("astrmai_execution_status") == "sent"


@pytest.mark.asyncio
async def test_turn_context_perception_is_structured_policy_input(_executor):
    from astrmai.conversation.contracts.turn_context import PerceptionSnapshot, TurnContext

    executor, _, reply = _executor("[SYSTEM_WAIT_SIGNAL]")
    event = _Event()
    event.set_extra(
        "astrmai_turn_context",
        TurnContext(perception=PerceptionSnapshot(is_strong_wakeup=True)),
    )

    result = await executor.execute(event, "@not-a-signal", "system", tools=[object()])

    assert result == "收到，我在看。"
    assert reply.calls[0][1] == "收到，我在看。"
    assert event.get_extra("astrmai_wait_policy_reason") == "strong_wakeup_requires_visible_ack"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["stale_drop", "superseded", "shutdown_rejected", "cancelled"])
async def test_wait_signal_does_not_fallback_after_terminal_guard(status, _executor):
    executor, _, reply = _executor("[SYSTEM_WAIT_SIGNAL]")
    event = _Event()
    event.set_extra("astrmai_is_strong_wakeup", True)
    event.set_extra("astrmai_execution_status", status)

    result = await executor.execute(event, "prompt", "system", tools=[object()])

    assert result is None
    assert reply.calls == []
    assert event.get_extra("astrmai_execution_status") == status
    assert event.get_extra("astrmai_wait_policy_reason") == status


@pytest.mark.asyncio
async def test_fallback_receipt_and_history_boundary_only_use_visible_text(_executor):
    executor, _, reply = _executor("[SYSTEM_WAIT_SIGNAL]")
    event = _Event()
    event.set_extra("astrmai_is_strong_wakeup", True)
    event.set_extra("astrmai_feedback_messages", [])
    event.set_extra("astrmai_turn_ledger", [])

    result = await executor.execute(event, "prompt", "system", tools=[object()])

    assert result == "收到，我在看。"
    assert event.get_extra("astrmai_reply_sent") is True
    assert all("[SYSTEM_WAIT_SIGNAL]" not in str(value) for value in event.get_extra("astrmai_feedback_messages"))
    assert all("[SYSTEM_WAIT_SIGNAL]" not in str(value) for value in event.get_extra("astrmai_turn_ledger"))


@pytest.mark.asyncio
async def test_strong_fallback_uses_send_guard_and_terminal_outcome(monkeypatch, _executor):
    executor, _, reply = _executor("[SYSTEM_WAIT_SIGNAL]")
    event = _Event()
    event.set_extra("astrmai_is_strong_wakeup", True)
    module = sys.modules["astrmai.conversation.execution.executor"]
    monkeypatch.setattr(module, "outbound_send_allowed", lambda _event: False)

    result = await executor.execute(event, "prompt", "system", tools=[object()])

    assert result is None
    assert reply.calls == []
    assert event.get_extra("astrmai_execution_status") == "shutdown_rejected"
    assert event.get_extra("astrmai_reply_sent", False) is False


@pytest.mark.asyncio
async def test_stale_runtime_generation_rejects_visible_fallback(_executor):
    executor, _, reply = _executor("[SYSTEM_WAIT_SIGNAL]")
    event = _Event()
    event.set_extra("astrmai_is_strong_wakeup", True)

    class _StaleCoordinator:
        def __init__(self):
            self.freshness_calls = 0

        async def try_acquire_executor(self, *args, **kwargs):
            return SimpleNamespace(token="lease-1")

        async def release_executor(self, *args, **kwargs):
            return True

        async def evaluate_reply_freshness(self, *args, **kwargs):
            from astrmai.conversation.contracts.focus_context import FreshnessState

            self.freshness_calls += 1
            if self.freshness_calls <= 3:
                return FreshnessState.FRESH, ""
            return FreshnessState.EXPIRED, "stale_generation"

    executor.runtime_coordinator = _StaleCoordinator()
    result = await executor.execute(event, "prompt", "system", tools=[object()])

    assert result is None
    assert reply.calls == []
    assert event.get_extra("astrmai_execution_status") == "stale_drop"
    assert event.get_extra("astrmai_wait_policy_reason") == "stale_generation"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["generation_advanced", "turn_task_replaced"])
async def test_known_generation_replacement_is_superseded(source):
    from tests.unit.attention.test_atwake_supersession import Event, Harness

    harness = Harness()
    try:
        event = Event("replacement", strong=True)
        await harness.ingress(event)
        event.set_extra("astrmai_cancel_source", source)
        event.set_extra("astrmai_execution_status", "cancelled")
        decision = harness.gate._deferred_replay_status({"event": event})
        assert decision.status == "superseded"
        assert decision.reason == f"cancel_source_{source}"
        assert decision.terminal is True
    finally:
        await harness.close()

