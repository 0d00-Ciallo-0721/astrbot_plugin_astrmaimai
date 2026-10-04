import asyncio
import time
from types import SimpleNamespace

import pytest

from astrmai.conversation.attention.gate import AttentionGate
from astrmai.conversation.contracts.turn_identity import build_turn_send_key
from astrmai.conversation.contracts.turn_outcome import claim_text_output, record_text_sent
from astrmai.conversation.execution.system2_runner import System2Runner
from astrmai.infrastructure.runtime.chat_runtime_coordinator import ChatRuntimeCoordinator
from astrmai.infrastructure.runtime.trace_runtime import debug_trace


class Event:
    def __init__(self, message_id, *, strong=False, sender="user", chat="group", components=None):
        self.extra = {}
        self.message_str = "original request" if strong else "followup"
        self.message_obj = SimpleNamespace(message_id=message_id, message=components or [])
        self.unified_msg_origin = chat
        self.timestamp = time.time()
        self.strong = strong
        self.sender = sender

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_sender_id(self):
        return self.sender

    def get_sender_name(self):
        return self.sender

    def get_message_str(self):
        return self.message_str

    def is_private_chat(self):
        return str(self.unified_msg_origin).startswith("private")

    def get_group_id(self):
        return self.unified_msg_origin

    def get_self_id(self):
        return "bot"


class Harness:
    def __init__(self):
        self.coordinator = ChatRuntimeCoordinator()
        self.entered = asyncio.Event()
        self.sent = []
        self.attempts = []
        self.replay_entered = asyncio.Event()
        self.block_replay = False
        self.send_before_block = False
        self.successor_sends = True
        self.runner = System2Runner(SimpleNamespace())
        self.runner.get_sys2_lock = self.get_lock
        self.runner._prepare_system2_runtime = self.noop
        self.runner._finalize_followups = self.noop
        self.runner._execute_planner = self.execute
        self.gate = AttentionGate(
            SimpleNamespace(config=SimpleNamespace()), None, SimpleNamespace(), self.runner.run,
            config=SimpleNamespace(conversation=SimpleNamespace(group_thread_wait_enabled=False)),
            runtime_coordinator=self.coordinator,
        )
        self.gate._resolve_wakeup_flags = lambda event, *_: (event.strong, event.strong, False, False)

    async def noop(self, *args):
        pass

    async def get_lock(self, *args):
        return asyncio.Lock()

    async def execute(self, event, *args):
        self.attempts.append((event, event.get_extra("astrmai_attempt_id")))
        if event.get_extra("astrmai_atwake_replay_count", 0) and self.block_replay:
            self.replay_entered.set()
            await asyncio.Event().wait()
        if event.message_obj.message_id == "old" and not event.get_extra("astrmai_atwake_replay_count", 0):
            if self.send_before_block:
                turn = event.get_extra("astrmai_turn_identity")
                assert claim_text_output(event).allowed
                key = build_turn_send_key(turn)
                assert await self.coordinator.claim_send(turn.chat_id, key)
                self.sent.append(event.message_obj.message_id)
                await self.coordinator.commit_send(turn.chat_id, key)
                record_text_sent(event, segments=1)
            self.entered.set()
            await asyncio.Event().wait()
        if (event.message_obj.message_id != "old" and not self.successor_sends):
            return False
        turn = event.get_extra("astrmai_turn_identity")
        if not await self.coordinator.is_current_turn(turn):
            event.set_extra("astrmai_execution_status", "stale_drop")
            return False
        if not claim_text_output(event).allowed:
            return False
        key = build_turn_send_key(turn)
        if not await self.coordinator.claim_send(turn.chat_id, key):
            return False
        self.sent.append(event.message_obj.message_id)
        await self.coordinator.commit_send(turn.chat_id, key)
        record_text_sent(event, segments=1)
        return True

    async def ingress(self, event):
        debug_trace(event, "fixture.ingress")
        await self.gate._ensure_turn_identity(event, event.unified_msg_origin, False)

    async def start(self, event):
        await self.ingress(event)
        task = self.gate._fire_priority_task(self.runner.run(event), event)
        await self.entered.wait()
        return task

    async def drain(self):
        for _ in range(50):
            tasks = [task for task in self.gate._background_tasks if not task.done()]
            if not tasks:
                return
            await asyncio.sleep(0.002)
        pytest.fail("managed work did not finish")

    async def close(self):
        self.gate.request_shutdown()
        tasks = list(self.gate._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", ["user", "other"])
@pytest.mark.parametrize("status", ["skipped_passive_group_image", "skipped_ignore"])
async def test_strong_wakeup_replays_once_after_non_reply_successor(sender, status):
    h = Harness()
    old = Event("old", strong=True)
    try:
        task = await h.start(old)
        successor = Event("next", sender=sender)
        await h.ingress(successor)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert old.get_extra("astrmai_terminal_outcome") == "superseded"
        await h.gate._finalize_pre_planner_turn(successor, "group", status=status)
        await h.drain()
        assert h.sent == ["old"], old.get_extra("astrmai_trace_log")
        assert old.get_extra("astrmai_atwake_replay_count") == 1
        assert len({attempt for event, attempt in h.attempts if event is old}) == 2
        terminals = [row for row in old.get_extra("astrmai_trace_log") if row["stage"] == "turn.terminal"]
        assert [row["status"] for row in terminals] == ["superseded", "reply_sent"]
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "visible_successor", "new_strong", "already_sent", "expired", "shutdown",
    "send_claim", "committed_claim", "successor_claim", "output_claim",
    "generation", "chat", "thread", "replay_count",
])
async def test_handoff_rejects_unsafe_or_unnecessary_replay(case):
    h = Harness()
    old = Event("old", strong=True)
    try:
        task = await h.start(old)
        old_turn = old.get_extra("astrmai_turn_identity")
        successor = Event("next", strong=case == "new_strong")
        await h.ingress(successor)
        with pytest.raises(asyncio.CancelledError):
            await task
        key = (old_turn.chat_id, old_turn.thread_id)
        if case == "visible_successor":
            claim_text_output(successor)
            record_text_sent(successor, segments=1)
        elif case == "already_sent":
            old.set_extra("astrmai_reply_sent", True)
        elif case == "expired":
            h.gate._strong_wakeup_pending[key].expires_at = time.monotonic() - 1
        elif case == "shutdown":
            h.gate.request_shutdown()
        elif case in {"send_claim", "committed_claim"}:
            await h.coordinator.claim_send(old_turn.chat_id, build_turn_send_key(old_turn))
            if case == "committed_claim":
                await h.coordinator.commit_send(old_turn.chat_id, build_turn_send_key(old_turn))
        elif case == "successor_claim":
            turn = successor.get_extra("astrmai_turn_identity")
            await h.coordinator.claim_send(turn.chat_id, build_turn_send_key(turn))
        elif case == "output_claim":
            outcome = old.get_extra("astrmai_turn_outcome")
            old.set_extra("astrmai_turn_outcome", {**outcome, "output_claim": "reply"})
        elif case == "replay_count":
            old.set_extra("astrmai_atwake_replay_count", 1)
        elif case in {"chat", "thread"}:
            from dataclasses import replace
            pending = h.gate._strong_wakeup_pending.pop(key)
            turn = successor.get_extra("astrmai_turn_identity")
            mismatched = replace(turn, **{f"{case}_id": "another"})
            await h.gate._replay_strong_wakeup(pending, mismatched)
        elif case == "generation":
            await h.coordinator.advance_generation(*key)
        await h.gate._finalize_pre_planner_turn(successor, "group", status="skipped_ignore")
        await h.drain()
        assert h.sent == []
        assert len(h.attempts) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_visible_system2_successor_finishes_without_old_replay():
    h = Harness()
    try:
        old = Event("old", strong=True)
        task = await h.start(old)
        successor = Event("next")
        await h.ingress(successor)
        await asyncio.gather(task, return_exceptions=True)
        await h.gate._fire_priority_task(h.runner.run(successor), successor)
        await h.drain()
        assert h.sent == ["next"]
        assert not old.get_extra("astrmai_atwake_replay_count", 0)
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_ordinary_original_preemption_remains_cancelled_without_replay():
    h = Harness()
    try:
        old = Event("old")
        task = await h.start(old)
        successor = Event("next")
        await h.ingress(successor)
        with pytest.raises(asyncio.CancelledError):
            await task
        await h.gate._finalize_pre_planner_turn(successor, "group", status="skipped_ignore")
        await h.drain()
        assert h.sent == []
        assert old.get_extra("astrmai_terminal_outcome") == "superseded"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_new_strong_successor_only_answers_latest_request():
    h = Harness()
    try:
        old = Event("old", strong=True)
        task = await h.start(old)
        successor = Event("next", strong=True)
        await h.ingress(successor)
        await asyncio.gather(task, return_exceptions=True)
        await h.gate._fire_priority_task(h.runner.run(successor), successor)
        await h.drain()
        assert h.sent == ["next"]
        assert not old.get_extra("astrmai_atwake_replay_count", 0)
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_kind", ["generation", "external", "shutdown"])
async def test_replay_cancellation_propagates_without_second_replay(cancel_kind):
    h = Harness()
    h.block_replay = True
    try:
        old = Event("old", strong=True)
        task = await h.start(old)
        successor = Event("next")
        await h.ingress(successor)
        await asyncio.gather(task, return_exceptions=True)
        await h.gate._finalize_pre_planner_turn(successor, "group", status="skipped_ignore")
        await asyncio.wait_for(h.replay_entered.wait(), 1)
        replay = next(task for task in h.gate._background_tasks if not task.done())
        if cancel_kind == "generation":
            latest = Event("latest")
            await h.ingress(latest)
        else:
            if cancel_kind == "shutdown":
                h.gate.request_shutdown()
            replay.cancel()
        with pytest.raises(asyncio.CancelledError):
            await replay
        if cancel_kind == "generation":
            await h.gate._finalize_pre_planner_turn(latest, "group", status="skipped_ignore")
        await h.drain()
        assert len(h.attempts) == 2
        assert h.sent == []
        assert old.get_extra("astrmai_atwake_replay_count") == 1
        assert old.get_extra("astrmai_terminal_outcome") == (
            "superseded" if cancel_kind == "generation" else "cancelled"
        )
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("successor_kind", ["image", "throttled", "filtered"])
async def test_real_components_through_gate_ingress_replay(successor_kind):
    import astrbot.api.message_components as Comp

    h = Harness()
    # Persistence and mood are outside this in-memory replay; all scheduling,
    # perception, generation, task registration and terminals remain real.
    h.gate._resolve_wakeup_flags = AttentionGate._resolve_wakeup_flags.__get__(h.gate)
    h.gate._append_dialogue_segment = h.noop
    h.gate.config.vision = SimpleNamespace(enable_vision=False)
    try:
        old = Event("old", strong=True)
        old.message_str = "hi"
        old.message_obj.message = [Comp.At(qq="bot"), Comp.Plain(text="hi")]
        assert await h.gate.process_event(old) == "ENGAGED"
        await asyncio.wait_for(h.entered.wait(), 1)
        original_task = next(task for task in h.gate._background_tasks if not task.done())
        successor = Event("next", sender="other")
        if successor_kind == "image":
            successor.message_str = ""
            successor.message_obj.message = [Comp.Image(file="https://example.invalid/image.png")]
            successor.set_extra("extracted_image_refs", ["image-ref"])
            expected = "IGNORED_IMAGE"
        elif successor_kind == "throttled":
            successor.message_obj.message = [Comp.Plain(text="followup")]
            h.gate.state_engine.get_state = lambda *_: SimpleNamespace(should_drop=True)
            expected = "THROTTLED"
        else:
            async def reject(*_):
                return False
            h.gate.sensors.should_process_message = reject
            expected = "FILTERED"
        assert await h.gate.process_event(successor) == expected
        with pytest.raises(asyncio.CancelledError):
            await original_task
        await h.drain()
        assert h.sent == ["old"]
        assert old.get_extra("astrmai_atwake_replay_count") == 1
        assert old.message_obj.message_id == "old"
        assert old.message_str == "hi"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_old_sent_then_cancelled_retains_single_visible_reply():
    h = Harness()
    h.send_before_block = True
    try:
        old = Event("old", strong=True)
        task = await h.start(old)
        successor = Event("next")
        await h.ingress(successor)
        with pytest.raises(asyncio.CancelledError):
            await task
        await h.gate._finalize_pre_planner_turn(successor, "group", status="skipped_ignore")
        await h.drain()
        assert h.sent == ["old"]
        assert len(h.attempts) == 1
        assert old.get_extra("astrmai_terminal_outcome") == "reply_sent"
        assert old.get_extra("astrmai_atwake_replay_count", 0) == 0
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_successive_passive_generations_transfer_original_once():
    h = Harness()
    try:
        old = Event("old", strong=True)
        timestamp = old.timestamp
        task = await h.start(old)
        first = Event("first")
        second = Event("second", sender="other")
        await h.ingress(first)
        await h.ingress(second)
        await asyncio.gather(task, return_exceptions=True)
        await h.gate._finalize_pre_planner_turn(first, "group", status="skipped_ignore")
        assert h.sent == []
        await h.gate._finalize_pre_planner_turn(second, "group", status="skipped_ignore")
        await h.drain()
        assert h.sent == ["old"]
        assert old.get_extra("astrmai_turn_identity").input_message_ids == ("old",)
        assert old.timestamp == timestamp
        await h.gate._finalize_pre_planner_turn(second, "group", status="skipped_ignore")
        await h.drain()
        assert len(h.attempts) == 2
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_other_chat_does_not_cancel_or_take_over_strong_wakeup():
    h = Harness()
    try:
        old = Event("old", strong=True)
        task = await h.start(old)
        other = Event("other", chat="another-group")
        await h.ingress(other)
        await h.gate._finalize_pre_planner_turn(other, "another-group", status="skipped_ignore")
        assert not task.done()
        assert not h.gate._strong_wakeup_pending
        assert h.sent == []
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_successor_failure_with_visible_fallback_does_not_replay():
    h = Harness()
    execute = h.runner._execute_planner

    async def fail_successor(event, *args):
        if event.message_obj.message_id == "next":
            raise RuntimeError("successor planner failed")
        return await execute(event, *args)

    async def visible_fallback(event, exc):
        assert isinstance(exc, RuntimeError)
        turn = event.get_extra("astrmai_turn_identity")
        assert claim_text_output(event).allowed
        key = build_turn_send_key(turn)
        assert await h.coordinator.claim_send(turn.chat_id, key)
        h.sent.append("fallback")
        await h.coordinator.commit_send(turn.chat_id, key)
        record_text_sent(event, segments=1)

    h.runner._execute_planner = fail_successor
    h.gate._handle_system2_failure = visible_fallback
    try:
        old = Event("old", strong=True)
        task = await h.start(old)
        successor = Event("next")
        await h.ingress(successor)
        await asyncio.gather(task, return_exceptions=True)
        await h.gate._fire_priority_task(h.runner.run(successor), successor)
        await h.drain()
        assert h.sent == ["fallback"]
        assert not h.gate._strong_wakeup_pending
        assert old.get_extra("astrmai_atwake_replay_count", 0) == 0
    finally:
        await h.close()


class GroupStateEngine:
    config = SimpleNamespace()

    def get_state(self, *args):
        return SimpleNamespace(should_drop=False)


class GroupSensors:
    async def should_process_message(self, *args):
        return True

    def is_wakeup_signal(self, *args):
        return False


class GroupIngressHarness(Harness):
    """Full production-shaped group ingress.

    process_event -> BUFFERED -> session worker -> background task -> successor
    settlement -> replay decision -> replay task -> send guard -> terminal.  Only
    the inbound message predicates and the planner execution boundary are stubbed;
    scheduling, generation, settlement, replay, claims and terminals are the real
    implementations.
    """

    def __init__(self):
        super().__init__()
        self.gate = AttentionGate(
            GroupStateEngine(), None, GroupSensors(), self.bridge,
            config=SimpleNamespace(conversation=SimpleNamespace(group_thread_wait_enabled=False)),
            runtime_coordinator=self.coordinator,
        )
        self.gate._append_dialogue_segment = self.noop
        self.gate.config.vision = SimpleNamespace(enable_vision=False)
        # Timing knob only: the accumulation window never gates a settlement decision.
        self.gate._compute_debounce_delay = lambda *args: 0.0

    async def bridge(self, main_event, events_to_process=None):
        return await self.runner.run(main_event, events_to_process)

    def strong_event(self, message_id="old"):
        import astrbot.api.message_components as Comp

        event = Event(message_id, strong=True, components=[Comp.At(qq="bot"), Comp.Plain(text="hi")])
        event.message_str = "hi"
        return event

    def ordinary_event(self, message_id="next", sender="other"):
        import astrbot.api.message_components as Comp

        return Event(message_id, sender=sender, components=[Comp.Plain(text="followup")])

    async def settle(self, timeout=5.0):
        """Deterministic drain: wait on the gate's own task sets until quiescent."""
        deadline = time.monotonic() + timeout
        idle = 0
        while time.monotonic() < deadline:
            pending = [task for task in self.gate._session_tasks if not task.done()]
            pending += [task for task in self.gate._background_tasks if not task.done()]
            if not pending:
                idle += 1
                if idle >= 3:
                    return
                await asyncio.sleep(0)
                continue
            idle = 0
            await asyncio.wait(pending, timeout=max(0.001, deadline - time.monotonic()))
        pytest.fail("real ingress chain did not reach quiescence")

    async def close(self):
        self.gate.request_shutdown()
        tasks = list(self.gate._background_tasks) + list(self.gate._session_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def handoff_reasons(event):
    return [row.get("reason") for row in (event.get_extra("astrmai_trace_log") or [])
            if row.get("stage") == "atwake.handoff"]


def terminals_by_attempt(*events):
    grouped = {}
    for event in events:
        for row in event.get_extra("astrmai_trace_log") or []:
            if row.get("stage") == "turn.terminal":
                grouped.setdefault(row.get("attempt_id"), []).append(row.get("status"))
    return grouped


@pytest.mark.asyncio
async def test_real_group_ingress_silent_successor_replays_strong_wakeup_once():
    h = GroupIngressHarness()
    h.successor_sends = False
    try:
        old = h.strong_event()
        assert await h.gate.process_event(old) == "ENGAGED"
        await asyncio.wait_for(h.entered.wait(), 2)
        old_attempt = old.get_extra("astrmai_attempt_id")
        successor = h.ordinary_event()
        assert await h.gate.process_event(successor) == "BUFFERED"
        await h.settle()

        assert h.sent == ["old"]
        assert [event.message_obj.message_id for event, _ in h.attempts] == ["old", "next", "old"]
        assert old.get_extra("astrmai_atwake_replay_count") == 1
        assert "superseded_pending" in handoff_reasons(old)
        assert "replay_started" in handoff_reasons(old)
        assert successor.get_extra("astrmai_terminal_outcome") == "no_visible_reply"
        assert not h.gate._strong_wakeup_pending
        assert not h.gate._strong_wakeup_running
        replay = next(attempt for event, attempt in h.attempts
                      if event is old and attempt != old_attempt)
        assert {attempt for event, attempt in h.attempts if event is old} == {old_attempt, replay}
        assert terminals_by_attempt(old, successor) == {
            old_attempt: ["superseded"],
            successor.get_extra("astrmai_attempt_id"): ["no_visible_reply"],
            replay: ["reply_sent"],
        }
        assert old.get_extra("astrmai_turn_identity").input_message_ids == ("old",)
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_real_group_ingress_visible_successor_releases_handoff_and_never_replays():
    h = GroupIngressHarness()
    try:
        old = h.strong_event()
        assert await h.gate.process_event(old) == "ENGAGED"
        await asyncio.wait_for(h.entered.wait(), 2)
        successor = h.ordinary_event("next")
        assert await h.gate.process_event(successor) == "BUFFERED"
        await h.settle()

        assert h.sent == ["next"]
        assert old.get_extra("astrmai_terminal_outcome") == "superseded"
        assert "successor_visible_reply" in handoff_reasons(old)
        assert not h.gate._strong_wakeup_pending
        assert not h.gate._strong_wakeup_running

        later = h.ordinary_event("later", sender="third")
        assert await h.gate.process_event(later) == "BUFFERED"
        await h.settle()
        assert [event.message_obj.message_id for event, _ in h.attempts].count("old") == 1
        assert h.sent == ["next", "later"]
        assert not old.get_extra("astrmai_atwake_replay_count", 0)
        assert "replay_started" not in handoff_reasons(old)
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_real_group_ingress_replay_is_bounded_to_one_attempt():
    h = GroupIngressHarness()
    h.successor_sends = False
    try:
        old = h.strong_event()
        assert await h.gate.process_event(old) == "ENGAGED"
        await asyncio.wait_for(h.entered.wait(), 2)
        first = h.ordinary_event("first")
        assert await h.gate.process_event(first) == "BUFFERED"
        await h.settle()

        assert h.sent == ["old"]
        assert len(h.attempts) == 3
        assert handoff_reasons(old).count("replay_started") == 1

        second = h.ordinary_event("second", sender="third")
        assert await h.gate.process_event(second) == "BUFFERED"
        await h.settle()
        assert len(h.attempts) == 4
        assert h.sent == ["old"]
        assert old.get_extra("astrmai_atwake_replay_count") == 1
        assert handoff_reasons(old).count("replay_started") == 1
        assert not h.gate._strong_wakeup_pending
        assert all(len(statuses) == 1 for statuses in terminals_by_attempt(
            old, first, second).values())
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_real_group_ingress_replay_cancellation_does_not_retry():
    h = GroupIngressHarness()
    h.block_replay = True
    h.successor_sends = False
    try:
        old = h.strong_event()
        assert await h.gate.process_event(old) == "ENGAGED"
        await asyncio.wait_for(h.entered.wait(), 2)
        successor = h.ordinary_event()
        assert await h.gate.process_event(successor) == "BUFFERED"
        await asyncio.wait_for(h.replay_entered.wait(), 5)

        replay = next(task for task in h.gate._background_tasks
                      if not task.done()
                      and getattr(task, "_astrmai_task_name", "") == "attention.priority")
        h.gate.request_shutdown()
        replay.cancel()
        with pytest.raises(asyncio.CancelledError):
            await replay
        await h.settle(timeout=2.0)

        assert [event.message_obj.message_id for event, _ in h.attempts] == ["old", "next", "old"]
        assert h.sent == []
        assert old.get_extra("astrmai_atwake_replay_count") == 1
        assert "replay_cancelled" in handoff_reasons(old)
        assert not h.gate._strong_wakeup_pending
        assert not h.gate._strong_wakeup_running
    finally:
        await h.close()
