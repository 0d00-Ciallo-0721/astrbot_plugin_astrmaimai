import asyncio
from types import SimpleNamespace

import pytest

from astrmai.conversation.planning.planning_input_loader import PlanningInputLoader
from astrmai.infrastructure.runtime.trace_runtime import record_terminal_outcome


class Event:
    def __init__(self):
        self.extra = {}
        self.unified_msg_origin = "fixture"

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value


def test_terminal_outcome_is_bounded_and_traceable():
    event = Event()
    record_terminal_outcome(event, "not-a-status", stage="system2", reason="fixture")
    assert event.get_extra("astrmai_terminal_outcome") == "error"
    trace = event.get_extra("astrmai_trace_log")[-1]
    assert trace["stage"] == "turn.terminal"
    assert trace["status"] == "error"


@pytest.mark.asyncio
async def test_side_input_cancellation_records_source_and_propagates():
    event = Event()
    loader = PlanningInputLoader(SimpleNamespace())
    blocker = asyncio.Event()

    async def load():
        await blocker.wait()

    task = asyncio.create_task(loader._run_timed(event, "fixture", load, {}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    trace = event.get_extra("astrmai_trace_log")[-1]
    assert trace["stage"] == "planning.side_input"
    assert trace["error"] == "CancelledError"
    assert trace["cancel_source"] == "external_or_superseded"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,exception,release_failure,expected", [
    ("skipped_wait", None, False, "skipped_wait"),
    ("", RuntimeError("business"), False, "error"),
    ("", asyncio.CancelledError(), False, "cancelled"),
    ("", BaseException("abort"), False, "error"),
    ("", None, True, "error"),
    ("", asyncio.CancelledError(), True, "cancelled"),
    ("", None, False, "no_visible_reply"),
    ("reply_sent", None, False, "reply_sent"),
    ("queue_timeout", asyncio.CancelledError(), False, "cancelled"),
    ("queue_timeout", RuntimeError("current error"), False, "error"),
])
async def test_runner_terminal_preserves_errors(status, exception, release_failure, expected):
    from astrmai.conversation.execution.system2_runner import System2Runner

    persisted = []

    class Store:
        async def append_many(self, chat_id, rows):
            persisted.extend(rows)

    runner = System2Runner(SimpleNamespace(system2_planner=SimpleNamespace(raw_trace_store=Store())))
    event = Event()

    async def get_lock(*args):
        return asyncio.Lock()

    async def prepare(*args):
        pass

    async def execute(*args):
        event.set_extra("astrmai_execution_status", status)
        if status == "reply_sent":
            event.set_extra("astrmai_reply_sent", True)
        if exception is not None:
            raise exception
        return False

    async def release(*args):
        raise RuntimeError("release")

    runner.get_sys2_lock = get_lock
    runner._prepare_system2_runtime = prepare
    runner._execute_planner = execute
    runner._finalize_followups = prepare
    if release_failure:
        runner._release_lock = release
    if exception is not None or release_failure:
        with pytest.raises(type(exception) if exception is not None else RuntimeError) as caught:
            await runner.run(event)
        if exception is not None:
            assert caught.value is exception
    else:
        assert await runner.run(event) is False
    assert event.get_extra("astrmai_terminal_outcome") == expected
    assert persisted[-1]["status"] == expected
    assert len([row for row in event.get_extra("astrmai_trace_log") if row["stage"] == "turn.terminal"]) == 1


@pytest.mark.asyncio
async def test_terminal_diagnostic_abort_preserves_original_cancel():
    from astrmai.conversation.execution.system2_runner import System2Runner

    class Store:
        async def append_many(self, *args):
            raise BaseException("diagnostic abort")

    runner = System2Runner(SimpleNamespace(system2_planner=SimpleNamespace(raw_trace_store=Store())))
    event = Event()
    original = asyncio.CancelledError("original")

    async def get_lock(*args):
        return asyncio.Lock()

    async def prepare(*args):
        pass

    async def execute(*args):
        raise original

    runner.get_sys2_lock = get_lock
    runner._prepare_system2_runtime = prepare
    runner._execute_planner = execute
    with pytest.raises(asyncio.CancelledError) as caught:
        await runner.run(event)
    assert caught.value is original


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", [False, True])
@pytest.mark.parametrize("waiting", [False, True])
async def test_generation_cancel_persists_terminal_without_entry_flush(tmp_path, sent, waiting):
    import json
    from astrmai.conversation.execution.system2_runner import System2Runner
    from astrmai.infrastructure.runtime.chat_runtime_coordinator import ChatRuntimeCoordinator
    from astrmai.infrastructure.runtime.raw_trace_store import RawTraceEventStore

    store = RawTraceEventStore(tmp_path)
    coordinator = ChatRuntimeCoordinator()
    runner = System2Runner(SimpleNamespace(system2_planner=SimpleNamespace(raw_trace_store=store)))
    event = Event()
    entered = asyncio.Event()

    async def get_lock(*args):
        return asyncio.Lock()

    async def prepare(*args):
        pass

    async def execute(*args):
        if waiting:
            from astrmai.conversation.planning.planner import Planner
            event.set_extra("astrmai_execution_status", "skipped_wait")
            event.set_extra("astrmai_defer_turn_trace_persist", True)
            await Planner._remember_turn_trace(object.__new__(Planner), "fixture", event, status="skipped_wait")
        event.set_extra("astrmai_reply_sent", sent)
        entered.set()
        await asyncio.Event().wait()

    runner.get_sys2_lock = get_lock
    runner._prepare_system2_runtime = prepare
    runner._execute_planner = execute
    generation = await coordinator.advance_generation("fixture", "thread")
    turn = SimpleNamespace(chat_id="fixture", thread_id="thread", generation=generation)
    task = asyncio.create_task(runner.run(event))
    task._astrmai_diagnostic_event = event
    assert await coordinator.register_turn_task(turn, task)
    await entered.wait()
    await coordinator.advance_generation("fixture", "thread")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert event.get_extra("astrmai_terminal_outcome") == ("reply_sent" if sent else "superseded")
    assert await store.flush()
    rows = [json.loads(line) for line in store.jsonl_path.read_text(encoding="utf-8").splitlines()]
    assert any(row["stage"] == "turn.terminal" and row["status"] == ("reply_sent" if sent else "superseded") for row in rows)
    terminal_rows = [row for row in rows if row["stage"] == "turn.terminal"]
    assert len(terminal_rows) == 1
    assert terminal_rows[0]["event_id"] == f"{event.get_extra('astrmai_attempt_id')}:turn.terminal"
    await store.close()


@pytest.mark.asyncio
async def test_real_lock_timeout_records_single_final_terminal():
    from astrmai.conversation.execution.system2_runner import System2Runner

    event = Event()
    observed = []

    async def record_turn_trace(*args, **kwargs):
        observed.append([row for row in event.get_extra("astrmai_trace_log") if row["stage"] == "turn.terminal"])

    runner = System2Runner(SimpleNamespace(system2_planner=SimpleNamespace(record_turn_trace=record_turn_trace)))
    lock = asyncio.Lock()
    await lock.acquire()

    async def get_lock(*args):
        return lock

    runner.get_sys2_lock = get_lock
    runner._lock_wait_timeout = lambda event: 0.1
    assert await runner.run(event) is False
    assert observed == [[]]
    assert len([row for row in event.get_extra("astrmai_trace_log") if row["stage"] == "turn.terminal"]) == 1
    assert event.get_extra("astrmai_queue_timeout_stage") == "system2.chat_lock_wait"
    lock.release()


@pytest.mark.asyncio
async def test_exhausted_real_turn_budget_is_queue_timeout_not_superseded():
    from astrmai.conversation.execution.system2_runner import System2Runner
    from astrmai.infrastructure.runtime.turn_call_ledger import configure_turn_budget

    event = Event()
    configure_turn_budget(event, total_budget_sec=1.0, main_reply_reserve_sec=1.0)
    runner = System2Runner(SimpleNamespace())
    assert await runner.run(event) is False
    assert event.get_extra("astrmai_terminal_outcome") == "queue_timeout"
    assert event.get_extra("astrmai_queue_timeout_stage") == "system2.chat_lock_resolve"


@pytest.mark.asyncio
async def test_executor_wait_signal_matches_planner_and_runner_terminal():
    from tests.helpers.atwake_stage00_fixtures import (
        FakeEvent, FakeEvolution, FakeGateway, FakeReplyService, executor_fixture,
    )
    from astrmai.conversation.execution.system2_runner import System2Runner
    from astrmai.conversation.planning.planner import Planner

    with executor_fixture() as executor_mod:
        gateway = FakeGateway(tool_responses={"model-a": "[SYSTEM_WAIT_SIGNAL]"})
        replies = FakeReplyService()
        executor = executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(), gateway=gateway, reply_engine=replies,
            evolution_manager=FakeEvolution(), config=gateway.config,
        )
        event = FakeEvent()
        event.set_extra("astrmai_defer_turn_trace_persist", True)
        planner = object.__new__(Planner)
        runner = System2Runner(SimpleNamespace(system2_planner=planner))

        async def get_lock(*args):
            return asyncio.Lock()

        async def prepare(*args):
            pass

        async def execute(*args):
            assert await executor.execute(event, "prompt", "system", tools=[object()]) is None
            await planner._remember_turn_trace(event.unified_msg_origin, event, status=event.get_extra("astrmai_execution_status"))
            return False

        runner.get_sys2_lock = get_lock
        runner._prepare_system2_runtime = prepare
        runner._execute_planner = execute
        runner._finalize_followups = prepare
        assert await runner.run(event) is False
        assert replies.calls == []
        assert event.get_extra("astrmai_deferred_turn_trace")["status"] == "skipped_wait"
        assert event.get_extra("astrmai_terminal_outcome") == "skipped_wait"
        assert all(row["status"] == "skipped_wait" for row in event.get_extra("astrmai_trace_log") if row["stage"] == "turn.terminal")
        assert len([row for row in event.get_extra("astrmai_trace_log") if row["stage"] == "turn.terminal"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["queue_timeout", "background_queue_timeout"])
@pytest.mark.parametrize("result", ["normal", "cancelled", "superseded", "error"])
async def test_stale_timeout_does_not_override_current_attempt(tmp_path, stale, result):
    import json
    from astrmai.conversation.execution.system2_runner import System2Runner
    from astrmai.conversation.planning.planner import Planner
    from astrmai.infrastructure.runtime.raw_trace_store import RawTraceEventStore

    event = Event()
    store = RawTraceEventStore(tmp_path)
    planner = object.__new__(Planner)
    planner.raw_trace_store = store
    runner = System2Runner(SimpleNamespace(system2_planner=planner))

    async def get_lock(*args):
        return asyncio.Lock()

    async def prepare(*args):
        pass

    async def first(*args):
        event.set_extra("astrmai_execution_status", stale)
        return False

    runner.get_sys2_lock = get_lock
    runner._prepare_system2_runtime = prepare
    runner._finalize_followups = prepare
    runner._execute_planner = first
    await runner.run(event)
    assert event.get_extra("astrmai_terminal_outcome") == "queue_timeout"
    original = RuntimeError("current") if result == "error" else asyncio.CancelledError("current")

    async def second(*args):
        if result == "superseded":
            event.set_extra("astrmai_cancel_source", "turn_task_replaced")
        if result != "normal":
            raise original
        return False

    runner._execute_planner = second
    if result == "normal":
        await runner.run(event)
    else:
        with pytest.raises(type(original)) as caught:
            await runner.run(event)
        assert caught.value is original
    expected = "no_visible_reply" if result == "normal" else result
    assert event.get_extra("astrmai_terminal_outcome") == expected
    rows = [row for row in event.get_extra("astrmai_trace_log") if row["stage"] == "turn.terminal"]
    assert len(rows) == 2
    assert rows[0]["attempt_id"] != rows[1]["attempt_id"]
    await store.append_many("fixture", planner._build_raw_trace_events("fixture", event))
    assert await store.flush()
    persisted = [json.loads(line) for line in store.jsonl_path.read_text(encoding="utf-8").splitlines()]
    terminals = [row for row in persisted if row["stage"] == "turn.terminal"]
    assert len(terminals) == 2
    assert {row["attempt_id"] for row in terminals} == {row["attempt_id"] for row in rows}
    assert terminals[-1]["outcome"] == expected
    await store.close()


def test_finalizer_is_idempotent_for_same_attempt():
    event = Event()
    event.set_extra("astrmai_attempt_id", "one")
    record_terminal_outcome(event, "skipped_wait")
    record_terminal_outcome(event, "superseded")
    rows = [row for row in event.get_extra("astrmai_trace_log") if row["stage"] == "turn.terminal"]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "skipped_wait"
