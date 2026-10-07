import asyncio
import time
from types import SimpleNamespace

import pytest

from astrmai.conversation.planning.planning_input_loader import PlanningInputLoader
from astrmai.infrastructure.runtime.turn_call_ledger import ensure_turn_telemetry
from astrmai.conversation.execution.system2_runner import System2Runner


class Event:
    def __init__(self, strong=True):
        self.extra = {"astrmai_at_bot_wakeup": strong, "astrmai_attempt_id": "attempt"}
        self.unified_msg_origin = "chat"

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_sender_id(self):
        return "user"


def loader_with_feedback(callback):
    return PlanningInputLoader(SimpleNamespace(_load_memory_feedback_summary=callback))


@pytest.mark.asyncio
async def test_strong_side_input_uses_existing_reply_reserve():
    event = Event()
    telemetry = ensure_turn_telemetry(event)
    telemetry.deadline_monotonic = time.monotonic() + 0.1
    telemetry.main_reply_reserve_sec = 0.08
    cancelled = asyncio.Event()

    async def slow(_):
        try:
            await asyncio.sleep(0.15)
            return "late"
        finally:
            cancelled.set()

    assert await loader_with_feedback(slow).load_memory_feedback(event, "chat", 2) == ""
    assert cancelled.is_set()
    timing = event.get_extra("astrmai_side_input_timings")[-1]
    assert timing["outcome"] == "side_input_timeout"
    assert timing["degraded"] is True
    assert event.get_extra("astrmai_execution_status") is None


@pytest.mark.asyncio
async def test_child_cancel_propagates_from_pre_budget_gather():
    event = Event()
    loader = PlanningInputLoader(SimpleNamespace())

    async def cancelled(_):
        raise asyncio.CancelledError()

    loader._continuity_snapshot = cancelled
    with pytest.raises(asyncio.CancelledError):
        await loader.load_pre_budget(event, "chat")
    assert event.get_extra("astrmai_side_input_timings")[-1]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_child_cancel_settles_sibling_side_inputs_before_return():
    loader = PlanningInputLoader(SimpleNamespace())
    entered = asyncio.Event()
    settled = asyncio.Event()

    async def sibling(*_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            settled.set()

    async def cancelled(*_):
        await entered.wait()
        raise asyncio.CancelledError()

    loader._agency_snapshot = sibling
    loader._continuity_snapshot = cancelled
    with pytest.raises(asyncio.CancelledError):
        await loader.load_pre_budget(Event(), "chat")
    assert settled.is_set()


@pytest.mark.asyncio
async def test_tool_state_child_cancellation_is_not_context_data():
    async def cancelled(*_):
        raise asyncio.CancelledError()

    loader = PlanningInputLoader(SimpleNamespace(state_engine=SimpleNamespace(get_state=cancelled)))
    with pytest.raises(asyncio.CancelledError):
        await loader.load_prompt_inputs(Event(), "chat", None, [], 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("strong", [False, True])
async def test_side_input_success_has_attempt_local_diagnostics(strong):
    event = Event(strong)

    async def successful(_):
        return "summary"

    assert await loader_with_feedback(successful).load_memory_feedback(event, "chat", 2) == "summary"
    timing = event.get_extra("astrmai_side_input_timings")[-1]
    assert timing["outcome"] == "success"
    assert timing["attempt_id"] == "attempt"
    assert timing["degraded"] is False


@pytest.mark.asyncio
async def test_side_input_error_diagnostics_do_not_include_exception_body():
    event = Event()

    async def failed(_):
        raise ValueError("private provider config")

    assert await loader_with_feedback(failed).load_memory_feedback(event, "chat", 2) == ""
    timing = event.get_extra("astrmai_side_input_timings")[-1]
    assert timing["outcome"] == "side_input_error"
    assert timing["error"] == "ValueError"
    assert "private provider config" not in str(event.extra)


@pytest.mark.asyncio
async def test_ordinary_side_input_is_not_promoted_or_budget_clamped():
    event = Event(False)
    ensure_turn_telemetry(event).deadline_monotonic = time.monotonic() - 1

    async def successful(_):
        await asyncio.sleep(0)
        return "ordinary"

    assert await loader_with_feedback(successful).load_memory_feedback(event, "chat", 2) == "ordinary"
    assert event.get_extra("astrmai_at_bot_wakeup") is False


def runner_with_planner(callback):
    runner = System2Runner(SimpleNamespace())

    async def lock(*_):
        return asyncio.Lock()

    async def noop(*_):
        pass

    runner.get_sys2_lock = lock
    runner._prepare_system2_runtime = noop
    runner._finalize_followups = noop
    runner._execute_planner = callback
    return runner


@pytest.mark.asyncio
async def test_total_budget_exhaustion_is_not_no_visible_reply():
    event = Event()
    ensure_turn_telemetry(event).deadline_monotonic = time.monotonic() + 0.03

    async def slow(*_):
        await asyncio.sleep(0.12)
        return False

    assert await runner_with_planner(slow).run(event) is False
    assert event.get_extra("astrmai_terminal_outcome") == "timeout"
    assert event.get_extra("astrmai_execution_status") == "budget_exhausted"
    assert event.get_extra("astrmai_budget_timeout_stage") == "system2.planner"


@pytest.mark.asyncio
async def test_side_input_degradation_continues_main_reply():
    event = Event()
    telemetry = ensure_turn_telemetry(event)
    telemetry.deadline_monotonic = time.monotonic() + 0.15
    telemetry.main_reply_reserve_sec = 0.12

    async def slow(_):
        await asyncio.Event().wait()

    async def planner(current, *_):
        assert await loader_with_feedback(slow).load_memory_feedback(current, "chat", 2) == ""
        current.set_extra("astrmai_reply_sent", True)
        return True

    assert await runner_with_planner(planner).run(event) is True
    assert event.get_extra("astrmai_terminal_outcome") == "reply_sent"


@pytest.mark.asyncio
@pytest.mark.parametrize("source,expected", [("external", "cancelled"), ("generation_advanced", "superseded"), ("shutdown", "cancelled")])
async def test_cancellation_remains_distinct_from_budget_timeout(source, expected):
    event = Event()
    ensure_turn_telemetry(event).deadline_monotonic = time.monotonic() + 10

    async def cancelled(current, *_):
        current.set_extra("astrmai_cancel_source", source)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await runner_with_planner(cancelled).run(event)
    assert event.get_extra("astrmai_terminal_outcome") == expected


@pytest.mark.asyncio
async def test_budget_after_visible_send_does_not_replace_reply_terminal():
    event = Event()
    ensure_turn_telemetry(event).deadline_monotonic = time.monotonic() + 0.03

    async def sent(current, *_):
        current.set_extra("astrmai_reply_sent", True)
        await asyncio.sleep(0.1)

    await runner_with_planner(sent).run(event)
    assert event.get_extra("astrmai_terminal_outcome") == "reply_sent"


@pytest.mark.asyncio
async def test_next_attempt_does_not_inherit_side_input_budget_diagnostics():
    event = Event()

    async def planner(current, *_):
        async def successful(_):
            return "summary"
        await loader_with_feedback(successful).load_memory_feedback(current, "chat", 2)
        return False

    runner = runner_with_planner(planner)
    await runner.run(event)
    first = event.get_extra("astrmai_attempt_id")
    await runner.run(event)
    second = event.get_extra("astrmai_attempt_id")
    assert first != second
    assert [item["attempt_id"] for item in event.get_extra("astrmai_side_input_timings")] == [second]
    rows = [row for row in event.get_extra("astrmai_trace_log") if row["stage"] == "turn.terminal"]
    assert [row["attempt_id"] for row in rows] == [first, second]


@pytest.mark.asyncio
@pytest.mark.parametrize("replay_fails", [False, True])
async def test_strong_pre_planner_queue_timeout_replays_at_most_once(replay_fails):
    from tests.unit.attention.test_atwake_supersession import Harness, Event as GateEvent

    h = Harness()
    event = GateEvent("budget", strong=True)
    event.set_extra("astrmai_at_bot_wakeup", True)
    await h.ingress(event)
    blocked = asyncio.Lock()
    await blocked.acquire()
    calls = 0

    async def get_lock(*_):
        nonlocal calls
        calls += 1
        return blocked if calls == 1 or replay_fails else asyncio.Lock()

    h.runner.get_sys2_lock = get_lock
    h.runner._lock_wait_timeout = lambda _: 0.01
    h.gate.config.attention = SimpleNamespace(attention_deferred_backoff_sec=0.1)
    try:
        await h.gate._run_background_task(h.runner.run(event), event,
                                        task_name="attention.system2", retry_factory=lambda: h.runner.run(event))
        dispatcher = h.gate._deferred_attention_dispatcher
        assert dispatcher is not None
        async def settled():
            while h.gate._deferred_attention_work or h.gate._deferred_attention_replay_active:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(settled(), timeout=4)
        assert calls == 2
        assert event.get_extra("astrmai_atwake_replay_count") == 1
        assert h.sent == ([] if replay_fails else ["budget"])
        terminals = [row for row in event.get_extra("astrmai_trace_log") if row["stage"] == "turn.terminal"]
        assert len(terminals) == 2
        assert terminals[0]["status"] == "queue_timeout"
        assert terminals[1]["status"] == ("queue_timeout" if replay_fails else "reply_sent")
    finally:
        dispatcher = h.gate._deferred_attention_dispatcher
        if dispatcher is not None:
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_background_slot_timeout_has_distinct_wait_identity():
    from tests.unit.attention.test_atwake_supersession import Harness, Event as GateEvent
    from astrmai.infrastructure.runtime.background_task_budget import BackgroundTaskQueueTimeout

    h = Harness()
    event = GateEvent("slot", strong=True)
    await h.ingress(event)
    h.gate._background_task_semaphore = asyncio.Semaphore(0)
    h.gate.config.timing = SimpleNamespace(attention_background_slot_wait_timeout_sec=0.1)
    started = False

    async def work():
        nonlocal started
        started = True

    try:
        with pytest.raises(BackgroundTaskQueueTimeout):
            await h.gate._run_background_slot(work, event)
        assert not started
        assert event.get_extra("astrmai_queue_timeout_stage") == "attention.background_slot_wait"
        assert event.get_extra("astrmai_execution_status") == "background_queue_timeout"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_background_admission_deadline_does_not_cancel_admitted_system2():
    from tests.unit.attention.test_atwake_supersession import Harness, Event as GateEvent
    from astrmai.infrastructure.runtime.background_task_budget import BackgroundTaskBudget

    h = Harness()
    event = GateEvent("admitted", strong=True)
    await h.ingress(event)
    h.gate.config.timing = SimpleNamespace(
        attention_background_slot_wait_timeout_sec=0.1,
        turn_total_budget_sec=1.0,
        main_reply_reserve_sec=0.0,
    )
    h.gate.background_task_budget = BackgroundTaskBudget(
        1,
        wait_timeout_sec=0.1,
        execution_timeout_sec=1.0,
    )
    execute = h.runner._execute_planner

    async def slow_execute(current, *args):
        await asyncio.sleep(0.2)
        return await execute(current, *args)

    h.runner._execute_planner = slow_execute
    try:
        result = await h.gate._run_background_task(
            h.runner.run(event),
            event,
            task_name="attention.system2",
        )
        assert result is True
        assert h.sent == ["admitted"]
        assert event.get_extra("astrmai_terminal_outcome") == "reply_sent"
        assert event.get_extra("astrmai_background_budget_acquired_at")
        assert event.get_extra("astrmai_background_execution_started") is True
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_real_executor_lock_timeout_reaches_runner_terminal():
    from astrmai.conversation.execution.executor import ConcurrentExecutor
    from tests.unit.attention.test_atwake_supersession import Harness, Event as GateEvent

    h = Harness()
    event = GateEvent("executor", strong=True)
    event.set_extra("astrmai_at_bot_wakeup", True)
    await h.ingress(event)
    turn = event.get_extra("astrmai_turn_identity")
    lease = await h.coordinator.try_acquire_executor(turn.chat_id, thread_id=turn.thread_id)
    executor = ConcurrentExecutor.__new__(ConcurrentExecutor)
    executor.config = SimpleNamespace(timing=SimpleNamespace(executor_lock_wait_timeout_sec=0.1))
    executor.runtime_coordinator = h.coordinator

    async def execute(current, *_):
        return bool(await executor.execute(current, "prompt", "system"))

    h.runner._execute_planner = execute
    try:
        assert await h.runner.run(event) is False
        assert event.get_extra("astrmai_terminal_outcome") == "queue_timeout"
        assert event.get_extra("astrmai_queue_timeout_stage") == "executor.chat_lock_wait"
        assert h.sent == []
        rows = event.get_extra("astrmai_stage_ledger")
        row = next(row for row in rows if row["stage"] == "executor.chat_lock_wait")
        assert row["status"] == "timeout"
        assert row["metadata"]["budget_kind"] == "executor_lock"
    finally:
        await h.coordinator.release_executor(turn.chat_id, lease=lease)
        await h.close()


@pytest.mark.asyncio
async def test_cancelled_budget_replay_does_not_restart():
    from tests.unit.attention.test_atwake_supersession import Harness, Event as GateEvent

    h = Harness()
    h.block_replay = True
    event = GateEvent("cancel_replay", strong=True)
    event.set_extra("astrmai_at_bot_wakeup", True)
    await h.ingress(event)
    blocked = asyncio.Lock()
    await blocked.acquire()
    calls = 0

    async def get_lock(*_):
        nonlocal calls
        calls += 1
        return blocked if calls == 1 else asyncio.Lock()

    h.runner.get_sys2_lock = get_lock
    h.runner._lock_wait_timeout = lambda _: 0.01
    h.gate.config.attention = SimpleNamespace(attention_deferred_backoff_sec=0.1)
    try:
        await h.gate._run_background_task(h.runner.run(event), event,
                                        task_name="attention.system2", retry_factory=lambda: h.runner.run(event))
        await asyncio.wait_for(h.replay_entered.wait(), timeout=4)
        dispatcher = h.gate._deferred_attention_dispatcher
        dispatcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dispatcher
        assert event.get_extra("astrmai_terminal_outcome") == "cancelled"
        assert event.get_extra("astrmai_atwake_replay_count") == 1
        restarted = asyncio.create_task(h.gate._dispatch_deferred_attention_work())
        h.gate._deferred_attention_dispatcher = restarted

        async def settled():
            while h.gate._deferred_attention_work:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(settled(), timeout=4)
        assert calls == 2
        assert h.sent == []
        terminals = [row for row in event.get_extra("astrmai_trace_log") if row["stage"] == "turn.terminal"]
        assert [row["status"] for row in terminals] == ["queue_timeout", "cancelled"]
    finally:
        dispatcher = h.gate._deferred_attention_dispatcher
        if dispatcher is not None:
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "error"])
async def test_real_group_ingress_degrades_side_input_and_sends_once(failure):
    from tests.unit.attention.test_atwake_supersession import GroupIngressHarness

    h = GroupIngressHarness()
    execute = h.runner._execute_planner

    async def optional_input(_):
        if failure == "error":
            raise ValueError("private context")
        await asyncio.Event().wait()

    async def planner(current, *args):
        telemetry = ensure_turn_telemetry(current)
        telemetry.deadline_monotonic = time.monotonic() + 0.2
        telemetry.main_reply_reserve_sec = 0.17
        assert await loader_with_feedback(optional_input).load_memory_feedback(current, "group", 2) == ""
        return await execute(current, *args)

    h.runner._execute_planner = planner
    try:
        event = h.strong_event("degraded")
        assert await h.gate.process_event(event) == "ENGAGED"
        await h.settle()
        assert h.sent == ["degraded"]
        timing = event.get_extra("astrmai_side_input_timings")[-1]
        assert timing["outcome"] == "side_input_" + failure
        assert timing["degraded"] is True
        assert event.get_extra("astrmai_terminal_outcome") == "reply_sent"
        assert event.get_extra("astrmai_atwake_replay_count", 0) == 0
        terminals = [row for row in event.get_extra("astrmai_trace_log") if row["stage"] == "turn.terminal"]
        assert len(terminals) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_committed_send_followed_by_budget_timeout_never_replays():
    from tests.unit.attention.test_atwake_supersession import Harness, Event as GateEvent

    h = Harness()
    event = GateEvent("sent_budget", strong=True)
    event.set_extra("astrmai_at_bot_wakeup", True)
    await h.ingress(event)
    ensure_turn_telemetry(event).deadline_monotonic = time.monotonic() + 0.05
    execute = h.runner._execute_planner

    async def sent_then_blocked(current, *args):
        assert await execute(current, *args) is True
        await asyncio.Event().wait()

    h.runner._execute_planner = sent_then_blocked
    try:
        assert await h.gate._run_background_task(
            h.runner.run(event), event, task_name="attention.system2",
            retry_factory=lambda: h.runner.run(event),
        ) is True
        assert h.sent == ["sent_budget"]
        assert event.get_extra("astrmai_terminal_outcome") == "reply_sent"
        assert event.get_extra("astrmai_execution_status") == "budget_exhausted"
        assert event.get_extra("astrmai_atwake_replay_count", 0) == 0
        assert not h.gate._deferred_attention_work
        assert len([row for row in event.get_extra("astrmai_trace_log") if row["stage"] == "turn.terminal"]) == 1
    finally:
        await h.close()
