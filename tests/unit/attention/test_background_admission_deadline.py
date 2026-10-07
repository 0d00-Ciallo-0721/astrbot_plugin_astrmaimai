import asyncio
import time
from types import SimpleNamespace

import pytest

from astrmai.infrastructure.runtime.background_task_budget import BackgroundTaskBudget
from astrmai.infrastructure.runtime.turn_call_ledger import ensure_turn_telemetry
from tests.unit.attention.test_atwake_supersession import GroupIngressHarness, Harness, Event


def configure(h, admission=0.1, execution=1.0):
    h.gate.config.timing = SimpleNamespace(attention_background_slot_wait_timeout_sec=admission)
    h.gate.background_task_budget = BackgroundTaskBudget(1, execution_timeout_sec=execution)


def terminals(event):
    return [row for row in event.get_extra("astrmai_trace_log", []) if row["stage"] == "turn.terminal"]


@pytest.mark.asyncio
@pytest.mark.parametrize("admission,delay", [(0.1, 0.2), (15.0, 15.1)])
@pytest.mark.parametrize("route", ["BUFFERED", "ENGAGED"])
async def test_real_group_route_execution_outlives_admission(admission, delay, route):
    h = GroupIngressHarness()
    configure(h, admission, delay + 5)
    event = h.strong_event("long_planning")
    if route == "BUFFERED":
        event.message_str = "Please provide a detailed answer to this request"
    execute = h.runner._execute_planner

    async def slow(current, *args):
        await asyncio.sleep(delay)
        return await execute(current, *args)

    h.runner._execute_planner = slow
    try:
        assert await h.gate.process_event(event) == route
        await h.settle(timeout=delay + 5)
        assert h.sent == ["long_planning"]
        assert [row["status"] for row in terminals(event)] == ["reply_sent"]
        assert event.get_extra("astrmai_cancel_source", "") == ""
        assert not h.gate._deferred_attention_work
        assert not h.gate.background_task_budget._active_leases
        if route == "BUFFERED":
            assert event.get_extra("astrmai_background_execution_started") is True
            assert event.get_extra("astrmai_background_execution_elapsed_sec") >= delay
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["slot", "budget", "custom"])
@pytest.mark.parametrize("admission", [0.1, 30.0])
async def test_admission_timeout_never_starts_system2(kind, admission):
    h = Harness()
    configure(h, admission=admission, execution=admission + 5)
    event = Event("queued", strong=True)
    await h.ingress(event)
    release = asyncio.Event()
    acquired = asyncio.Event()
    holder = None

    async def hold():
        acquired.set()
        await release.wait()

    if kind == "slot":
        h.gate._background_task_semaphore = asyncio.Semaphore(0)
    elif kind == "budget":
        holder = asyncio.create_task(h.gate.background_task_budget.run(hold))
        await acquired.wait()
    else:
        class CustomBudget:
            async def run(self, factory, *, task_name):
                await release.wait()
                return await factory()
        h.gate.background_task_budget = CustomBudget()
    try:
        assert await h.gate._run_background_task(h.runner.run(event), event, task_name="attention.system2") is None
        assert not h.attempts and not h.sent
        assert event.get_extra("astrmai_execution_status") == "background_queue_timeout"
        assert event.get_extra("astrmai_cancel_source", "") == ""
        assert [row["status"] for row in terminals(event)] == ["queue_timeout"]
        assert not h.gate._deferred_attention_work
        assert h.gate._background_task_semaphore._value == (0 if kind == "slot" else h.gate.BACKGROUND_TASK_MAX_CONCURRENCY)
    finally:
        release.set()
        if holder:
            await holder
        await h.close()
    if kind != "custom":
        assert not h.gate.background_task_budget._active_leases


@pytest.mark.asyncio
async def test_custom_budget_factory_is_acquired_boundary():
    h = Harness()
    configure(h)
    event = Event("custom", strong=True)
    await h.ingress(event)

    class CustomBudget:
        async def run(self, factory, *, task_name):
            return await factory()

    h.gate.background_task_budget = CustomBudget()
    execute = h.runner._execute_planner

    async def slow(current, *args):
        await asyncio.sleep(0.2)
        return await execute(current, *args)

    h.runner._execute_planner = slow
    try:
        assert await h.gate._run_background_task(h.runner.run(event), event, task_name="attention.system2") is True
        assert h.sent == ["custom"]
        assert len(terminals(event)) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["execution", "total"])
async def test_execution_and_total_timeout_are_not_queue_or_external_cancel(limit):
    h = Harness()
    configure(h, admission=0.5, execution=0.1 if limit == "execution" else 1.0)
    event = Event("timed", strong=True)
    event.set_extra("astrmai_at_bot_wakeup", True)
    await h.ingress(event)
    ensure_turn_telemetry(event)
    if limit == "total":
        ensure_turn_telemetry(event).deadline_monotonic = time.monotonic() + 0.15

    async def blocked(*args):
        await asyncio.Event().wait()

    h.runner._execute_planner = blocked
    try:
        await h.gate._run_background_task(h.runner.run(event), event, task_name="attention.system2")
        assert h.sent == []
        assert [row["status"] for row in terminals(event)] == ["timeout"]
        assert event.get_extra("astrmai_execution_status") == ("execution_timeout" if limit == "execution" else "budget_exhausted")
        planning_stage = next(row for row in event.get_extra("astrmai_stage_ledger") if row["stage"] == "system2.planner")
        assert planning_stage["status"] == "timeout"
        assert planning_stage["reason"] == ("execution_timeout" if limit == "execution" else "budget_exhausted")
        assert not h.gate._deferred_attention_work
        assert not h.gate.background_task_budget._active_leases
        assert h.gate._background_task_semaphore._value == h.gate.BACKGROUND_TASK_MAX_CONCURRENCY
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["generation", "shutdown"])
async def test_background_cancel_preserves_producer_and_releases_resources(source):
    h = Harness()
    configure(h)
    event = Event("old", strong=True)
    task = None
    try:
        await h.ingress(event)
        task = h.gate._fire_background_task(h.runner.run(event), event, task_name="attention.system2")
        await asyncio.wait_for(h.entered.wait(), 2)
        if source == "generation":
            turn = event.get_extra("astrmai_turn_identity")
            await h.coordinator.advance_generation(turn.chat_id, turn.thread_id)
        else:
            h.gate.request_shutdown()
            event.set_extra("astrmai_cancel_source", "shutdown")
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert h.sent == []
        assert [row["status"] for row in terminals(event)] == ["superseded" if source == "generation" else "cancelled"]
        assert not h.gate._deferred_attention_work
        assert not h.gate.background_task_budget._active_leases
        assert h.gate._background_task_semaphore._value == h.gate.BACKGROUND_TASK_MAX_CONCURRENCY
    finally:
        await h.close()


def test_explicit_admission_config_is_not_overridden_by_default():
    from config import TimingConfig
    from pydantic import ValidationError

    assert TimingConfig().attention_background_slot_wait_timeout_sec == 30.0
    assert TimingConfig(attention_background_slot_wait_timeout_sec=15.0).attention_background_slot_wait_timeout_sec == 15.0
    with pytest.raises(ValidationError):
        TimingConfig(attention_background_slot_wait_timeout_sec="invalid")


@pytest.mark.asyncio
async def test_budget_admission_respects_remaining_reply_reserve_and_flush_order():
    h = Harness()
    configure(h, admission=30)
    event = Event("reserve", strong=True)
    await h.ingress(event)
    telemetry = ensure_turn_telemetry(event)
    telemetry.deadline_monotonic = time.monotonic() + 0.2
    telemetry.main_reply_reserve_sec = 0.18
    release = asyncio.Event()
    acquired = asyncio.Event()
    persisted = []

    async def hold():
        acquired.set()
        await release.wait()

    async def flush(*args, **kwargs):
        persisted.extend(terminals(event))

    h.gate.turn_trace_callback = flush
    holder = asyncio.create_task(h.gate.background_task_budget.run(hold))
    await acquired.wait()
    try:
        await h.gate._run_background_task(h.runner.run(event), event, task_name="attention.system2")
        assert len(persisted) == 1 and persisted[0]["status"] == "queue_timeout"
        assert event.get_extra("astrmai_queue_timeout_stage") == "attention.background_budget_wait"
        assert not h.attempts and not h.sent
        assert telemetry.deadline_monotonic - time.monotonic() > 0.1
    finally:
        release.set()
        await holder
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["no_reply", "sent_then_timeout"])
async def test_admitted_result_matches_visible_send_without_replay(outcome):
    h = Harness()
    configure(h, admission=0.1, execution=0.3)
    event = Event("result", strong=True)
    await h.ingress(event)
    execute = h.runner._execute_planner

    async def finish(current, *args):
        if outcome == "no_reply":
            await asyncio.sleep(0.15)
            return False
        await execute(current, *args)
        await asyncio.Event().wait()

    h.runner._execute_planner = finish
    try:
        await h.gate._run_background_task(h.runner.run(event), event, task_name="attention.system2",
                                          retry_factory=lambda: h.runner.run(event))
        assert h.sent == ([] if outcome == "no_reply" else ["result"])
        assert [row["status"] for row in terminals(event)] == ["no_visible_reply" if outcome == "no_reply" else "reply_sent"]
        assert not h.gate._deferred_attention_work
        assert not h.gate.background_task_budget._active_leases
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["slot", "budget"])
async def test_cancel_while_queued_cleans_admission_without_starting(kind):
    h = Harness()
    configure(h, admission=30)
    event = Event("queued_cancel", strong=True)
    await h.ingress(event)
    ensure_turn_telemetry(event)
    holder = None
    release = asyncio.Event()
    acquired = asyncio.Event()

    async def hold():
        acquired.set()
        await release.wait()

    if kind == "slot":
        h.gate._background_task_semaphore = asyncio.Semaphore(0)
    else:
        holder = asyncio.create_task(h.gate.background_task_budget.run(hold))
        await acquired.wait()
    task = h.gate._fire_background_task(h.runner.run(event), event, task_name="attention.system2")
    try:
        async def waiting():
            stage = "attention.background_slot_wait" if kind == "slot" else "attention.background_budget_wait"
            while not any(row["stage"] == stage for row in event.get_extra("astrmai_stage_ledger", [])):
                await asyncio.sleep(0)
        await asyncio.wait_for(waiting(), 2)
        event.set_extra("astrmai_cancel_source", "shutdown")
        h.gate.request_shutdown()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not h.attempts and not h.sent
        assert [row["status"] for row in terminals(event)] == ["cancelled"]
        assert not h.gate._deferred_attention_work
        assert not h.gate.background_task_budget._waiters
    finally:
        release.set()
        if holder:
            await holder
        await h.close()
    assert not h.gate.background_task_budget._active_leases
