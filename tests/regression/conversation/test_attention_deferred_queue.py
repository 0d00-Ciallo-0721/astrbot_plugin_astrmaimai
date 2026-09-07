from __future__ import annotations

import asyncio
import importlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs
from tests.helpers.attention_stubs import install_attention_stubs
from astrmai.infrastructure.persistence.attention_deferred_outbox import (
    AttentionDeferredOutboxStore,
)
from astrmai.infrastructure.runtime.turn_call_ledger import (
    configure_turn_budget,
    detach_turn_telemetry,
    deferred_replay_budget_scope,
    remaining_turn_budget,
)


class _Event:
    unified_msg_origin = "default:GroupMessage:deferred"

    def __init__(self):
        self._extra = {}
        self.terminal_event = asyncio.Event()

    def set_extra(self, key, value):
        self._extra[key] = value
        if key == "deferred_terminal_status":
            self.terminal_event.set()

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)


class _SendEvent(_Event):
    def __init__(self):
        super().__init__()
        self.sent = []

    def plain_result(self, text):
        return text

    async def send(self, payload):
        self.sent.append(payload)


class AttentionDeferredQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        install_attention_stubs()
        gate_mod = importlib.import_module("astrmai.conversation.attention.gate")
        self.gate_mod = importlib.reload(gate_mod)
        config = SimpleNamespace(
            attention=SimpleNamespace(
                attention_background_slot_wait_timeout_sec=0.01,
                attention_deferred_queue_max=8,
                attention_deferred_per_chat_max=4,
                attention_deferred_backoff_sec=0.2,
                attention_deferred_ttl_sec=1.0,
                attention_deferred_max_attempts=3,
            ),
            system1=SimpleNamespace(wakeup_words=[], nicknames=[]),
            global_settings=SimpleNamespace(debug_mode=False),
            timing=SimpleNamespace(attention_background_slot_wait_timeout_sec=0.01),
        )
        self.gate = self.gate_mod.AttentionGate(
            state_engine=SimpleNamespace(config=config),
            judge=SimpleNamespace(),
            sensors=SimpleNamespace(),
            system2_callback=None,
            config=config,
        )
        self.gate._background_task_semaphore = asyncio.Semaphore(1)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_budget_queue_timeout_is_deferred_and_replayed(self):
        async def run():
            budget_mod = importlib.import_module(
                "astrmai.infrastructure.runtime.background_task_budget"
            )
            budget = budget_mod.BackgroundTaskBudget(
                1,
                max_queue=1,
                wait_timeout_sec=0.01,
            )
            self.gate.background_task_budget = budget
            release = asyncio.Event()
            blocker_started = asyncio.Event()
            calls = []

            async def blocker():
                blocker_started.set()
                await release.wait()

            async def work():
                calls.append("run")
                event.terminal_event.set()

            blocker_task = asyncio.create_task(
                budget.run(blocker, task_name="test.blocker", scope_id="global")
            )
            await blocker_started.wait()
            event = _Event()
            await self.gate._run_background_task(
                asyncio.sleep(0),
                event,
                task_name="attention.system2",
                retry_factory=work,
            )
            self.assertEqual(event.get_extra("attention_deferred"), True)
            release.set()
            await blocker_task
            await asyncio.wait_for(event.terminal_event.wait(), timeout=1.5)
            await self.gate.shutdown_workers()
            return calls, event.get_extra("deferred_terminal_status")

        calls, status = asyncio.run(run())
        self.assertEqual(calls, ["run"])
        self.assertEqual(status, "replayed")

    def test_replay_uses_independent_budget_when_original_turn_expired(self):
        async def run():
            event = _Event()
            telemetry = configure_turn_budget(
                event,
                total_budget_sec=1.0,
                main_reply_reserve_sec=0.0,
            )
            telemetry.deadline_monotonic = time.monotonic() - 1.0
            expired_deadline = telemetry.deadline_monotonic
            calls = []
            observed = {}

            async def work():
                calls.append("started")
                observed["event"] = remaining_turn_budget(event)
                observed["none"] = remaining_turn_budget(None)
                await asyncio.sleep(0)

            self.gate._deferred_attention_work["expired-turn"] = {
                "work_id": "expired-turn",
                "chat_id": event.unified_msg_origin,
                "task_name": "attention.system2",
                "retry_factory": work,
                "event": event,
                "enqueued_at": time.time(),
                "next_retry_at": 0.0,
                "expires_at": time.time() + 5.0,
                "attempts": 0,
                "max_attempts": 3,
                "shutdown_generation": self.gate._shutdown_generation,
                "_terminal_status": None,
            }
            self.gate._ensure_deferred_attention_dispatcher()
            await asyncio.wait_for(event.terminal_event.wait(), timeout=2.0)
            status = event.get_extra("deferred_terminal_status")
            await self.gate.shutdown_workers()
            return telemetry.deadline_monotonic, expired_deadline, calls, observed, status

        deadline, expired_deadline, calls, observed, status = asyncio.run(run())
        self.assertEqual(calls, ["started"])
        self.assertGreater(observed["event"], 0.0)
        self.assertGreater(observed["none"], 0.0)
        self.assertEqual(deadline, expired_deadline)
        self.assertEqual(status, "replayed")

    def test_replay_budget_scopes_are_isolated_and_reset(self):
        async def observe(deadline):
            with deferred_replay_budget_scope(deadline, work_id=str(deadline)):
                first = remaining_turn_budget(None)
                await asyncio.sleep(0)
                second = remaining_turn_budget(None)
            return first, second, remaining_turn_budget(None)

        async def run():
            now = time.monotonic()
            return await asyncio.gather(
                observe(now + 1.0),
                observe(now + 2.0),
            )

        values = asyncio.run(run())
        self.assertEqual(len(values), 2)
        self.assertGreater(values[0][0], 0.0)
        self.assertGreater(values[1][0], values[0][0])
        self.assertGreater(values[0][1], 0.0)
        self.assertGreater(values[1][1], 0.0)
        self.assertIsNone(values[0][2])
        self.assertIsNone(values[1][2])

    def test_replay_provider_failure_is_not_marked_replayed_or_fallback(self):
        async def run():
            event = _Event()

            async def work():
                raise RuntimeError("provider probe")

            self.gate._deferred_attention_work["provider-failure"] = {
                "work_id": "provider-failure",
                "chat_id": event.unified_msg_origin,
                "task_name": "attention.compaction",
                "retry_factory": work,
                "event": event,
                "enqueued_at": time.time(),
                "next_retry_at": 0.0,
                "expires_at": time.time() + 5.0,
                "attempts": 0,
                "max_attempts": 1,
                "shutdown_generation": self.gate._shutdown_generation,
                "_terminal_status": None,
            }
            self.gate._ensure_deferred_attention_dispatcher()
            await asyncio.wait_for(event.terminal_event.wait(), timeout=2.0)
            status = self.gate.describe_status()
            await self.gate.shutdown_workers()
            return event.get_extra("deferred_terminal_status"), status

        terminal, status = asyncio.run(run())
        self.assertEqual(terminal, "failed")
        self.assertEqual(status["attention_deferred_replayed_total"], 0)
        self.assertGreaterEqual(status["attention_deferred_task_started_total"], 1)
        self.assertEqual(status["attention_deferred_last_failure_kind"], "task_internal_error")

    def test_compaction_replay_completes_after_original_deadline_expired(self):
        async def run():
            event = _Event()
            telemetry = configure_turn_budget(
                event,
                total_budget_sec=1.0,
                main_reply_reserve_sec=0.0,
            )
            telemetry.deadline_monotonic = time.monotonic() - 1.0
            observed = []

            async def work():
                observed.append(remaining_turn_budget(event))

            self.gate._deferred_attention_work["compaction-replay"] = {
                "work_id": "compaction-replay",
                "chat_id": event.unified_msg_origin,
                "task_name": "attention.compaction",
                "retry_factory": work,
                "event": event,
                "enqueued_at": time.time(),
                "next_retry_at": 0.0,
                "expires_at": time.time() + 5.0,
                "attempts": 0,
                "max_attempts": 1,
                "shutdown_generation": self.gate._shutdown_generation,
                "_terminal_status": None,
            }
            self.gate._ensure_deferred_attention_dispatcher()
            await asyncio.wait_for(event.terminal_event.wait(), timeout=2.0)
            terminal = event.get_extra("deferred_terminal_status")
            await self.gate.shutdown_workers()
            return terminal, observed

        terminal, observed = asyncio.run(run())
        self.assertEqual(terminal, "replayed")
        self.assertEqual(len(observed), 1)
        self.assertGreater(observed[0], 0.0)

    def test_replay_budget_reply_reserve_is_task_specific(self):
        async def observe(reserve):
            with deferred_replay_budget_scope(
                time.monotonic() + 2.0,
                reply_reserve_sec=reserve,
                work_id="reserve",
            ):
                return remaining_turn_budget(None), remaining_turn_budget(
                    None, reserve_for_reply=True
                )

        execution, reserved = asyncio.run(observe(0.5))
        self.assertGreater(execution, reserved)
        self.assertGreater(reserved, 0.0)

    def test_replay_child_task_cannot_use_budget_after_scope_exit(self):
        async def run():
            result = {}

            async def child():
                await asyncio.sleep(0.01)
                result["remaining"] = remaining_turn_budget(None)

            with deferred_replay_budget_scope(
                time.monotonic() + 2.0,
                work_id="child-scope",
            ):
                task = asyncio.create_task(child())
            await task
            return result["remaining"]

        self.assertIsNone(asyncio.run(run()))

    def test_detached_child_does_not_invalidate_parent_replay_budget(self):
        async def run():
            result = {}
            scope_exited = asyncio.Event()

            async def detached_child():
                detach_turn_telemetry()
                result["detached_child"] = remaining_turn_budget(None)

            async def inherited_child():
                await scope_exited.wait()
                result["inherited_child_after_scope"] = remaining_turn_budget(None)

            with deferred_replay_budget_scope(
                time.monotonic() + 2.0,
                work_id="detached-child",
            ):
                detached_task = asyncio.create_task(detached_child())
                inherited_task = asyncio.create_task(inherited_child())
                await detached_task
                result["parent"] = remaining_turn_budget(None)
            scope_exited.set()
            await inherited_task
            return result

        result = asyncio.run(run())
        self.assertIsNone(result["detached_child"])
        self.assertGreater(result["parent"], 0.0)
        self.assertIsNone(result["inherited_child_after_scope"])

    def test_local_attention_slot_timeout_replays_without_fallback(self):
        async def run():
            await self.gate._background_task_semaphore.acquire()
            event = _SendEvent()
            formal_calls = []

            async def formal_work():
                formal_calls.append("formal")
                await event.send(event.plain_result("formal"))

            started_at = time.monotonic()
            await self.gate._run_background_task(
                asyncio.sleep(0),
                event,
                task_name="attention.system2",
                retry_factory=formal_work,
            )
            elapsed = time.monotonic() - started_at
            self.assertLess(elapsed, 0.2)
            self.assertEqual(event.sent, [])
            self.assertEqual(event.get_extra("attention_deferred"), True)
            self.assertNotEqual(event.get_extra("astrmai_system2_failure_handled"), True)
            self.gate._background_task_semaphore.release()
            await asyncio.wait_for(event.terminal_event.wait(), timeout=1.5)
            await self.gate.shutdown_workers()
            return formal_calls, event.sent, event.get_extra("deferred_terminal_status")

        calls, sent, status = asyncio.run(run())
        self.assertEqual(calls, ["formal"])
        self.assertEqual(sent, ["formal"])
        self.assertEqual(status, "replayed")

    def test_budget_queue_full_is_deferred(self):
        async def run():
            budget_mod = importlib.import_module("astrmai.infrastructure.runtime.background_task_budget")
            budget = budget_mod.BackgroundTaskBudget(1, max_queue=0, wait_timeout_sec=0.01)
            self.gate.background_task_budget = budget
            release = asyncio.Event()
            started = asyncio.Event()
            async def blocker():
                started.set(); await release.wait()
            async def work():
                return "ok"
            blocker_task = asyncio.create_task(budget.run(blocker, task_name="test.blocker"))
            await started.wait()
            event = _Event()
            await self.gate._run_background_task(asyncio.sleep(0), event, task_name="attention.system2", retry_factory=work)
            release.set(); await blocker_task
            await self.gate.shutdown_workers()
            return event.get_extra("deferred_reason"), event.get_extra("deferred_terminal_status")
        reason, status = asyncio.run(run())
        self.assertEqual(reason, "queue_full")
        self.assertEqual(status, "shutdown")

    def test_queue_full_replay_does_not_send_fallback_or_duplicate(self):
        async def run():
            budget_mod = importlib.import_module("astrmai.infrastructure.runtime.background_task_budget")
            budget = budget_mod.BackgroundTaskBudget(1, max_queue=0, wait_timeout_sec=0.01)
            self.gate.background_task_budget = budget
            release = asyncio.Event()
            started = asyncio.Event()
            event = _SendEvent()
            formal_calls = []

            async def blocker():
                started.set()
                await release.wait()

            async def formal_work():
                formal_calls.append("formal")
                await event.send(event.plain_result("formal"))

            blocker_task = asyncio.create_task(budget.run(blocker, task_name="test.blocker"))
            await started.wait()
            await self.gate._run_background_task(
                    asyncio.sleep(0),
                    event,
                    task_name="attention.system2",
                    retry_factory=formal_work,
                )
            self.assertEqual(event.sent, [])
            self.assertEqual(event.get_extra("deferred_reason"), "queue_full")
            release.set()
            await blocker_task
            await asyncio.wait_for(event.terminal_event.wait(), timeout=1.5)
            await self.gate.shutdown_workers()
            return formal_calls, event.sent, event.get_extra("deferred_terminal_status")

        calls, sent, status = asyncio.run(run())
        self.assertEqual(calls, ["formal"])
        self.assertEqual(sent, ["formal"])
        self.assertEqual(status, "replayed")

    def test_fallback_sent_event_is_not_replayed(self):
        async def run():
            event = _SendEvent()
            event.set_extra("astrmai_reply_sent", True)
            event.set_extra("astrmai_system2_failure_handled", True)
            self.gate._deferred_attention_work["fallback"] = {
                "work_id": "fallback",
                "chat_id": event.unified_msg_origin,
                "retry_factory": lambda: asyncio.sleep(0),
                "event": event,
                "enqueued_at": 0.0,
                "next_retry_at": 0.0,
                "expires_at": time.time() + 5.0,
                "attempts": 0,
                "max_attempts": 3,
            }
            self.gate._ensure_deferred_attention_dispatcher()
            await asyncio.wait_for(event.terminal_event.wait(), timeout=1.5)
            await self.gate.shutdown_workers()
            return event.get_extra("deferred_terminal_status"), event.sent

        status, sent = asyncio.run(run())
        self.assertEqual(status, "skipped_already_terminal")
        self.assertEqual(sent, [])

    def test_replay_preflight_rejection_preserves_reason(self):
        async def run():
            event = _Event()
            event.set_extra("astrmai_system2_failure_handled", True)
            self.gate._deferred_attention_work["preflight-reason"] = {
                "work_id": "preflight-reason",
                "chat_id": event.unified_msg_origin,
                "task_name": "attention.system2",
                "retry_factory": lambda: asyncio.sleep(0),
                "event": event,
                "enqueued_at": time.time(),
                "next_retry_at": 0.0,
                "expires_at": time.time() + 5.0,
                "attempts": 0,
                "max_attempts": 3,
                "_terminal_status": None,
            }
            self.gate._ensure_deferred_attention_dispatcher()
            await asyncio.wait_for(event.terminal_event.wait(), timeout=1.5)
            return (
                event.get_extra("deferred_terminal_status"),
                event.get_extra("deferred_terminal_reason"),
                self.gate.describe_status(),
            )

        status, reason, diagnostics = asyncio.run(run())
        self.assertEqual(status, "skipped_already_terminal")
        self.assertEqual(reason, "turn_already_handled")
        self.assertEqual(diagnostics["attention_deferred_last_failure_stage"], "")

    def test_malformed_replay_preflight_is_failed_with_reason_and_stage(self):
        async def run():
            event = _Event()
            event.set_extra("astrmai_turn_outcome", "corrupt")
            self.gate._deferred_attention_work["preflight-malformed"] = {
                "work_id": "preflight-malformed",
                "chat_id": event.unified_msg_origin,
                "task_name": "attention.system2",
                "retry_factory": lambda: asyncio.sleep(0),
                "event": event,
                "enqueued_at": time.time(),
                "next_retry_at": 0.0,
                "expires_at": time.time() + 5.0,
                "attempts": 0,
                "max_attempts": 3,
                "_terminal_status": None,
            }
            self.gate._ensure_deferred_attention_dispatcher()
            await asyncio.wait_for(event.terminal_event.wait(), timeout=1.5)
            return (
                event.get_extra("deferred_terminal_status"),
                event.get_extra("deferred_terminal_reason"),
                self.gate.describe_status(),
            )

        status, reason, diagnostics = asyncio.run(run())
        self.assertEqual(status, "failed")
        self.assertEqual(reason, "malformed_turn_outcome")
        self.assertEqual(diagnostics["attention_deferred_last_failure_stage"], "replay_preflight")
        self.assertEqual(diagnostics["attention_deferred_last_failure_kind"], "malformed_turn_outcome")

    def test_output_claim_preflight_waits_for_retry_instead_of_failing(self):
        async def run():
            event = _Event()
            event.set_extra(
                "astrmai_turn_outcome",
                {"terminal_status": "active", "output_claim": "reply"},
            )
            item = {
                "work_id": "preflight-claim",
                "chat_id": event.unified_msg_origin,
                "task_name": "attention.system2",
                "retry_factory": lambda: asyncio.sleep(0),
                "event": event,
                "enqueued_at": time.time(),
                "next_retry_at": 0.0,
                "expires_at": time.time() + 5.0,
                "attempts": 0,
                "max_attempts": 3,
                "_terminal_status": None,
            }
            self.gate._deferred_attention_work[item["work_id"]] = item
            status = self.gate._deferred_replay_status(item)
            return status

        status = asyncio.run(run())
        self.assertEqual(status, ("retry", "output_claim_exists"))
        self.assertEqual(status.decision_stage, "replay_preflight")
        self.assertEqual(status.decision_kind, "output_claim_conflict")
        self.assertTrue(status.retryable)
        self.assertFalse(status.terminal)

    def test_dispatcher_does_not_terminal_fail_before_retry_preflight(self):
        async def run():
            event = _Event()
            event.set_extra(
                "astrmai_turn_outcome",
                {"terminal_status": "active", "output_claim": "reply"},
            )
            calls = []

            async def work():
                calls.append("run")

            self.gate._deferred_attention_work["preflight-delay"] = {
                "work_id": "preflight-delay",
                "chat_id": event.unified_msg_origin,
                "task_name": "attention.system2",
                "retry_factory": work,
                "event": event,
                "enqueued_at": time.time(),
                "next_retry_at": 0.0,
                "expires_at": time.time() + 5.0,
                "attempts": 0,
                "max_attempts": 3,
                "_terminal_status": None,
            }
            self.gate._ensure_deferred_attention_dispatcher()
            await asyncio.sleep(0.05)
            self.assertIsNone(event.get_extra("deferred_terminal_status"))
            event.set_extra("astrmai_turn_outcome", {"terminal_status": "active"})
            await asyncio.wait_for(event.terminal_event.wait(), timeout=2.0)
            await self.gate.shutdown_workers()
            return calls, event.get_extra("deferred_terminal_status")

        calls, status = asyncio.run(run())
        self.assertEqual(calls, ["run"])
        self.assertEqual(status, "replayed")

    def test_queue_timeout_marker_remains_replayable(self):
        async def run():
            event = _Event()
            event.set_extra("astrmai_execution_status", "queue_timeout")
            event.set_extra("astrmai_queue_timeout_stage", "system2.energy_prepare")
            item = {
                "work_id": "queue-timeout-replayable",
                "chat_id": event.unified_msg_origin,
                "task_name": "attention.system2",
                "retry_factory": lambda: asyncio.sleep(0),
                "event": event,
                "turn_generation": 0,
                "enqueued_at": time.time(),
                "next_retry_at": 0.0,
                "expires_at": time.time() + 5.0,
                "attempts": 0,
                "max_attempts": 3,
                "_terminal_status": None,
            }
            return self.gate._deferred_replay_status(item)

        status = asyncio.run(run())
        self.assertEqual(status, (None, ""))
        self.assertEqual(status.decision_stage, "replay_preflight")
        self.assertEqual(status.decision_kind, "ready")
        self.assertFalse(status.retryable)
        self.assertFalse(status.terminal)

    def test_started_execution_timeout_is_not_replayed(self):
        async def run():
            calls = []

            async def work():
                calls.append("run")
                raise asyncio.TimeoutError

            result = await self.gate._run_background_task(
                work(),
                _Event(),
                task_name="attention.system2",
                retry_factory=work,
            )
            return result, calls

        result, calls = asyncio.run(run())
        self.assertIsNone(result)
        self.assertEqual(calls, ["run"])
        self.assertEqual(self.gate.describe_status()["attention_deferred_current"], 0)

    def test_shutdown_clears_deferred_work(self):
        async def run():
            await self.gate._background_task_semaphore.acquire()
            event = _Event()

            async def work():
                return "ok"

            await self.gate._run_background_task(
                asyncio.sleep(0),
                event,
                task_name="attention.system2",
                retry_factory=work,
            )
            self.assertEqual(len(self.gate._deferred_attention_work), 1)
            await self.gate.shutdown_workers()
            return self.gate.describe_status()

        status = asyncio.run(run())
        self.assertEqual(status["attention_deferred_current"], 0)
        self.assertEqual(status["attention_deferred_shutdown_total"], 1)

    def test_deferred_queue_rejection_is_reported_on_event(self):
        async def run():
            self.gate.config.attention.attention_deferred_queue_max = 1
            self.gate._deferred_attention_work["existing"] = {
                "work_id": "existing",
                "chat_id": "other",
                "retry_factory": lambda: asyncio.sleep(0),
                "event": _Event(),
                "enqueued_at": 0.0,
                "next_retry_at": 9999999999.0,
                "expires_at": 9999999999.0,
                "attempts": 0,
                "max_attempts": 3,
            }
            event = _Event()
            accepted = self.gate._defer_attention_work(
                event=event,
                task_name="attention.system2",
                retry_factory=lambda: asyncio.sleep(0),
                reason="queue_timeout",
            )
            return accepted, event.get_extra("deferred_terminal_status")

        accepted, status = asyncio.run(run())
        self.assertFalse(accepted)
        self.assertEqual(status, "rejected")
        self.assertEqual(self.gate.describe_status()["attention_deferred_rejected_total"], 1)

    def test_deferred_enqueue_persists_and_shutdown_keeps_retryable_record(self):
        async def run():
            db_path = Path(self.temp_dir.name) / "attention.db"
            store = AttentionDeferredOutboxStore(db_path)
            self.gate._deferred_attention_outbox = store
            self.gate.config.attention.attention_deferred_backoff_sec = 30.0
            event = _Event()
            accepted = self.gate._defer_attention_work(
                event=event,
                task_name="attention.system2",
                retry_factory=lambda: asyncio.sleep(0),
                reason="queue_timeout",
            )
            self.assertTrue(accepted)
            await asyncio.sleep(0)
            pending = list(self.gate._deferred_persist_tasks)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            before = await store.describe()
            self.assertEqual(before["queued"], 1)
            await self.gate.shutdown_workers()
            after = await store.describe()
            return before, after

        before, after = asyncio.run(run())
        self.assertEqual(before["queued"], 1)
        self.assertEqual(after["queued"], 1)

    def test_deferred_restore_rehydrates_future_work_and_turn_identity(self):
        async def run():
            db_path = Path(self.temp_dir.name) / "attention-restore.db"
            store = AttentionDeferredOutboxStore(db_path)
            now = time.time()
            await store.enqueue(
                {
                    "work_id": "attention-deferred-restore-1",
                    "chat_id": "default:GroupMessage:deferred",
                    "task_name": "attention.system2",
                    "reason": "queue_timeout",
                    "turn_thread_id": "thread-restore",
                    "turn_generation": 7,
                    "worker_generation": 2,
                    "attempts": 1,
                    "max_attempts": 3,
                    "next_retry_at_wall": now + 30.0,
                    "expires_at": now + 300.0,
                },
                event_data={
                    "message_str": "restored",
                    "unified_msg_origin": "default:GroupMessage:deferred",
                },
            )
            self.gate._deferred_attention_outbox = store
            await self.gate._restore_deferred_attention()
            item = self.gate._deferred_attention_work["attention-deferred-restore-1"]
            identity = item["event"].get_extra("astrmai_turn_identity")
            self.assertEqual(identity.thread_id, "thread-restore")
            self.assertEqual(identity.generation, 7)
            self.assertEqual(item["attempts"], 1)
            await self.gate.shutdown_workers()

        asyncio.run(run())

    def test_deferred_restore_rebuilds_compaction_without_process_event(self):
        async def run():
            db_path = Path(self.temp_dir.name) / "compaction-restore.db"
            store = AttentionDeferredOutboxStore(db_path)
            calls = []

            class Compaction:
                async def schedule_compaction_evaluation(self, chat_id, focus_context=None, message_source=None):
                    calls.append((chat_id, focus_context, message_source))

            self.gate.context_compaction = Compaction()
            self.gate.process_event = mock.AsyncMock(side_effect=AssertionError("must not process event"))
            now = time.time()
            await store.enqueue(
                {
                    "work_id": "compaction-restore-1",
                    "chat_id": "default:GroupMessage:compact",
                    "task_name": "attention.compaction",
                    "reason": "queue_timeout",
                    "attempts": 0,
                    "max_attempts": 1,
                    "next_retry_at_wall": now,
                    "expires_at": now + 10.0,
                    "diagnostics": {
                        "replay_metadata": {
                            "chat_id": "default:GroupMessage:compact",
                            "focus_context_version": "v1",
                        }
                    },
                },
                event_data={"unified_msg_origin": "default:GroupMessage:compact"},
            )
            self.gate._deferred_attention_outbox = store
            await self.gate._restore_deferred_attention()
            item = self.gate._deferred_attention_work["compaction-restore-1"]
            await item["retry_factory"]()
            await self.gate.shutdown_workers()
            return calls

        calls = asyncio.run(run())
        self.assertEqual(calls, [("default:GroupMessage:compact", None, "user")])

    def test_deferred_restore_unknown_task_is_blocked_without_generic_fallback(self):
        async def run():
            db_path = Path(self.temp_dir.name) / "unknown-restore.db"
            store = AttentionDeferredOutboxStore(db_path)
            self.gate.process_event = mock.AsyncMock(
                side_effect=AssertionError("unknown task must not process event")
            )
            now = time.time()
            await store.enqueue(
                {
                    "work_id": "unknown-restore-1",
                    "chat_id": "default:GroupMessage:unknown",
                    "task_name": "attention.unknown",
                    "reason": "queue_timeout",
                    "next_retry_at_wall": now,
                    "expires_at": now + 30.0,
                },
                event_data={
                    "message_str": "unknown",
                    "unified_msg_origin": "default:GroupMessage:unknown",
                },
            )
            self.gate._deferred_attention_outbox = store
            await self.gate._restore_deferred_attention()
            await asyncio.sleep(0)
            description = await store.describe()
            await self.gate.shutdown_workers()
            return description

        self.assertEqual(asyncio.run(run())["total"], 0)

    def test_active_replay_is_visible_to_runtime_diagnostics(self):
        async def run():
            event = _Event()
            started = asyncio.Event()
            release = asyncio.Event()

            async def work():
                started.set()
                await release.wait()

            self.gate._deferred_attention_work["active-replay"] = {
                "work_id": "active-replay",
                "chat_id": event.unified_msg_origin,
                "task_name": "attention.compaction",
                "retry_factory": work,
                "event": event,
                "enqueued_at": time.time(),
                "next_retry_at": 0.0,
                "expires_at": time.time() + 10.0,
                "attempts": 0,
                "max_attempts": 1,
                "shutdown_generation": self.gate._shutdown_generation,
                "_terminal_status": None,
            }
            self.gate._ensure_deferred_attention_dispatcher()
            await asyncio.wait_for(started.wait(), timeout=1.0)
            active = self.gate.describe_status()
            release.set()
            await asyncio.wait_for(event.terminal_event.wait(), timeout=1.0)
            await self.gate.shutdown_workers()
            return active

        status = asyncio.run(run())
        self.assertTrue(status["attention_deferred_replay_budget_active"])
        self.assertIn("active-replay", status["attention_deferred_replay_active_work_ids"])

    def test_system2_replay_admission_deadline_excludes_reply_reserve(self):
        async def run():
            observed = {}

            async def fake_slot(awaitable_factory, event=None, *, admission_deadline=None):
                observed["deadline"] = admission_deadline
                return await awaitable_factory()

            original = self.gate._run_background_slot
            self.gate._run_background_slot = fake_slot
            try:
                hard_deadline = time.monotonic() + 2.0
                with deferred_replay_budget_scope(
                    hard_deadline,
                    work_id="reserve-boundary",
                    reply_reserve_sec=0.5,
                ):
                    await self.gate._run_background_task(
                        asyncio.sleep(0),
                        _Event(),
                        task_name="attention.system2",
                        _deferred_replay=True,
                    )
            finally:
                self.gate._run_background_slot = original
            return hard_deadline, observed["deadline"]

        hard_deadline, admission_deadline = asyncio.run(run())
        self.assertLess(admission_deadline, hard_deadline - 0.35)

    def test_deferred_terminal_transition_removes_persisted_record(self):
        async def run():
            db_path = Path(self.temp_dir.name) / "attention-terminal.db"
            store = AttentionDeferredOutboxStore(db_path)
            self.gate._deferred_attention_outbox = store
            self.gate.config.attention.attention_deferred_backoff_sec = 30.0
            event = _Event()
            self.assertTrue(
                self.gate._defer_attention_work(
                    event=event,
                    task_name="attention.system2",
                    retry_factory=lambda: asyncio.sleep(0),
                    reason="queue_timeout",
                )
            )
            await asyncio.sleep(0)
            pending = list(self.gate._deferred_persist_tasks)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            item = next(iter(self.gate._deferred_attention_work.values()))
            self.gate._set_deferred_terminal(item, "expired", reason="ttl_expired")
            pending = list(self.gate._deferred_persist_tasks)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            description = await store.describe()
            await self.gate.shutdown_workers()
            return description

        self.assertEqual(asyncio.run(run())["total"], 0)

    def test_deferred_enqueue_failure_is_retained_and_retried(self):
        async def run():
            class FlakyStore:
                db_path = "memory"

                def __init__(self):
                    self.calls = 0

                async def enqueue(self, item, *, event_data):
                    self.calls += 1
                    if self.calls == 1:
                        raise TimeoutError("sqlite busy")
                    return True

            store = FlakyStore()
            self.gate._deferred_attention_outbox = store
            item = {
                "work_id": "persist-failure-1",
                "chat_id": "chat-1",
                "task_name": "attention.system2",
                "reason": "queue_timeout",
                "event": _Event(),
                "next_retry_at": time.monotonic(),
                "expires_at": time.time() + 60.0,
            }
            self.gate._schedule_deferred_persist(item)
            await asyncio.gather(*list(self.gate._deferred_persist_tasks), return_exceptions=True)
            self.assertEqual(len(self.gate._deferred_pending_persistence), 1)
            self.gate.mark_runtime_started()
            retry_tasks = [task for task in self.gate._background_tasks if task not in self.gate._deferred_persist_tasks]
            if retry_tasks:
                await asyncio.gather(*retry_tasks, return_exceptions=True)
            self.assertEqual(store.calls, 2)
            self.assertEqual(self.gate._deferred_pending_persistence, {})

        asyncio.run(run())

    def test_deferred_finish_failure_is_retained_and_retried(self):
        async def run():
            class FlakyStore:
                db_path = "memory"

                def __init__(self):
                    self.calls = 0

                async def finish(self, *args, **kwargs):
                    self.calls += 1
                    if self.calls == 1:
                        raise TimeoutError("sqlite busy")
                    return True

            store = FlakyStore()
            self.gate._deferred_attention_outbox = store
            item = {
                "work_id": "finish-failure-1",
                "chat_id": "chat-1",
                "attempts": 1,
                "_outbox_lease_token": "lease-1",
                "diagnostics": {
                    "last_failure_stage": "replay",
                    "last_failure_kind": "provider_timeout",
                },
            }
            self.gate._schedule_deferred_finish(item, "replayed", reason="ok")
            await asyncio.gather(*list(self.gate._deferred_persist_tasks), return_exceptions=True)
            self.assertEqual(len(self.gate._deferred_pending_persistence), 1)
            pending = self.gate._deferred_pending_persistence["finish-failure-1"]
            self.assertEqual(
                pending["diagnostics"],
                {
                    "last_failure_stage": "replay",
                    "last_failure_kind": "provider_timeout",
                },
            )
            received = []

            async def finish_with_capture(*args, **kwargs):
                store.calls += 1
                received.append(kwargs.get("diagnostics"))
                return True

            store.finish = finish_with_capture
            await self.gate._retry_pending_persistence()
            self.assertEqual(store.calls, 2)
            self.assertEqual(
                received,
                [
                    {
                        "last_failure_stage": "replay",
                        "last_failure_kind": "provider_timeout",
                    }
                ],
            )
            self.assertEqual(self.gate._deferred_pending_persistence, {})

        asyncio.run(run())

    def test_deferred_persistence_cancelled_during_shutdown_is_retained(self):
        async def run():
            class BlockingStore:
                db_path = "memory"

                async def enqueue(self, item, *, event_data):
                    await asyncio.Event().wait()

            self.gate._deferred_attention_outbox = BlockingStore()
            item = {
                "work_id": "persist-cancelled-1",
                "chat_id": "chat-1",
                "task_name": "attention.system2",
                "reason": "queue_timeout",
                "event": _Event(),
                "next_retry_at": time.monotonic(),
                "expires_at": time.time() + 60.0,
            }
            self.gate._schedule_deferred_persist(item)
            await asyncio.sleep(0)
            self.gate.request_shutdown()
            task = next(iter(self.gate._deferred_persist_tasks))
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertIn("persist-cancelled-1", self.gate._deferred_pending_persistence)

        asyncio.run(run())

    def test_deferred_ttl_expiry_is_terminal(self):
        async def run():
            event = _Event()
            self.gate._deferred_attention_work["expired"] = {
                "work_id": "expired",
                "chat_id": event.unified_msg_origin,
                "retry_factory": lambda: asyncio.sleep(0),
                "event": event,
                "enqueued_at": 0.0,
                "next_retry_at": 0.0,
                "expires_at": 0.0,
                "attempts": 0,
                "max_attempts": 3,
            }
            self.gate._ensure_deferred_attention_dispatcher()
            self.gate._deferred_attention_event.set()
            await asyncio.wait_for(event.terminal_event.wait(), timeout=1.5)
            await self.gate.shutdown_workers()
            return event.get_extra("deferred_terminal_status")

        self.assertEqual(asyncio.run(run()), "expired")
        self.assertEqual(self.gate.describe_status()["attention_deferred_expired_total"], 1)

    def test_dispatcher_failure_restarts_with_remaining_work(self):
        async def run():
            event = _Event()
            calls = []

            async def work():
                calls.append("run")
                event.terminal_event.set()

            self.gate._deferred_attention_work["pending"] = {
                "work_id": "pending",
                "chat_id": event.unified_msg_origin,
                "retry_factory": work,
                "event": event,
                "enqueued_at": 0.0,
                "next_retry_at": 0.0,
                "expires_at": time.time() + 5.0,
                "attempts": 0,
                "max_attempts": 3,
            }
            original = self.gate._dispatch_deferred_attention_work
            failed_once = False

            async def fail_once():
                nonlocal failed_once
                if not failed_once:
                    failed_once = True
                    raise RuntimeError("dispatcher probe")
                return await original()

            self.gate._dispatch_deferred_attention_work = fail_once
            self.gate._ensure_deferred_attention_dispatcher()
            await asyncio.wait_for(event.terminal_event.wait(), timeout=1.5)
            await self.gate.shutdown_workers()
            status = self.gate.describe_status()
            return calls, status["attention_deferred_dispatcher_failed_total"], status

        calls, failures, status = asyncio.run(run())
        self.assertEqual(calls, ["run"])
        self.assertEqual(failures, 1)
        self.assertGreaterEqual(status["attention_deferred_dispatcher_started_total"], 2)
        self.assertGreaterEqual(status["attention_deferred_dispatcher_restart_total"], 1)

    def test_deferred_observability_distinguishes_depth_from_cumulative_counts(self):
        async def run():
            event = _Event()

            async def work():
                return "ok"

            accepted = self.gate._defer_attention_work(
                event=event,
                task_name="attention.system2",
                retry_factory=work,
                reason="queue_timeout",
            )
            self.assertTrue(accepted)
            status = self.gate.describe_status()
            await self.gate.shutdown_workers()
            return status

        status = asyncio.run(run())
        self.assertEqual(status["attention_deferred_current"], 1)
        self.assertEqual(status["attention_deferred_enqueued_total"], 1)
        self.assertEqual(status["attention_deferred_total"], 1)
        self.assertEqual(status["attention_deferred_current_by_kind"], {"attention.system2": 1})
        self.assertTrue(status["attention_deferred_dispatcher_running"])
        self.assertGreater(status["attention_deferred_dispatcher_started_total"], 0)
        self.assertGreater(status["attention_deferred_last_enqueued_at"], 0)

    def test_deferred_terminal_transition_is_idempotent(self):
        event = _Event()
        item = {
            "work_id": "terminal-once",
            "task_name": "attention.system2",
            "event": event,
            "_terminal_status": None,
        }

        self.gate._set_deferred_terminal(item, "expired", reason="ttl_expired")
        self.gate._set_deferred_terminal(item, "expired", reason="duplicate")

        status = self.gate.describe_status()
        self.assertEqual(event.get_extra("deferred_terminal_status"), "expired")
        self.assertEqual(event.get_extra("deferred_terminal_reason"), "ttl_expired")
        self.assertEqual(status["attention_deferred_expired_total"], 1)
        self.assertGreater(status["attention_deferred_last_terminal_at"], 0)

    def test_replay_attempt_and_success_are_reported_separately(self):
        async def run():
            event = _Event()
            calls = []

            async def work():
                calls.append("run")

            self.gate._deferred_attention_work["success"] = {
                "work_id": "success",
                "chat_id": event.unified_msg_origin,
                "task_name": "attention.system2",
                "retry_factory": work,
                "event": event,
                "enqueued_at": time.time(),
                "next_retry_at": 0.0,
                "expires_at": time.time() + 5.0,
                "attempts": 0,
                "max_attempts": 3,
                "_terminal_status": None,
            }
            self.gate._ensure_deferred_attention_dispatcher()
            for _ in range(50):
                if calls:
                    break
                await asyncio.sleep(0.01)
            status = self.gate.describe_status()
            await self.gate.shutdown_workers()
            return calls, status

        calls, status = asyncio.run(run())
        self.assertEqual(calls, ["run"])
        self.assertEqual(status["attention_deferred_current"], 0)
        self.assertEqual(status["attention_deferred_replay_attempt_total"], 1)
        self.assertEqual(status["attention_deferred_replayed_total"], 1)
        self.assertEqual(status["attention_deferred_replay_succeeded_total"], 1)

    def test_replay_timeout_reaches_exhausted_terminal_state(self):
        async def run():
            event = _Event()

            async def work():
                raise self.gate_mod.BackgroundTaskQueueTimeout("still full")

            self.gate._deferred_attention_work["exhausted"] = {
                "work_id": "exhausted",
                "chat_id": event.unified_msg_origin,
                "task_name": "attention.system2",
                "retry_factory": work,
                "event": event,
                "enqueued_at": time.time(),
                "next_retry_at": 0.0,
                "expires_at": time.time() + 5.0,
                "attempts": 0,
                "max_attempts": 1,
                "_terminal_status": None,
            }
            self.gate._ensure_deferred_attention_dispatcher()
            await asyncio.wait_for(event.terminal_event.wait(), timeout=1.5)
            status = self.gate.describe_status()
            await self.gate.shutdown_workers()
            return event.get_extra("deferred_terminal_status"), status

        terminal, status = asyncio.run(run())
        self.assertEqual(terminal, "exhausted")
        self.assertEqual(status["attention_deferred_replay_attempt_total"], 1)
        self.assertEqual(status["attention_deferred_exhausted_total"], 1)

    def test_startup_warmup_skips_ambient_group_background_task(self):
        async def run():
            self.gate.config.attention.startup_warmup_sec = 120.0
            self.gate.mark_runtime_started()
            event = _Event()
            calls = []

            async def work():
                calls.append("run")

            result = await self.gate._run_background_task(
                asyncio.sleep(0), event, task_name="attention.system2", retry_factory=work
            )
            return result, calls, event.get_extra("astrmai_execution_status")

        result, calls, status = asyncio.run(run())
        self.assertIsNone(result)
        self.assertEqual(calls, [])
        self.assertEqual(status, "startup_warmup_skipped")

    def test_startup_warmup_keeps_direct_group_background_task(self):
        async def run():
            self.gate.config.attention.startup_warmup_sec = 120.0
            self.gate.mark_runtime_started()
            event = _Event()
            event.set_extra("astrmai_group_direct_wakeup", True)
            calls = []

            async def work():
                calls.append("run")
                return "ok"

            result = await self.gate._run_background_task(
                work(), event, task_name="attention.system2", retry_factory=work
            )
            return result, calls

        result, calls = asyncio.run(run())
        self.assertEqual(result, "ok")
        self.assertEqual(calls, ["run"])

    def test_queue_admission_errors_are_warning_only(self):
        async def run():
            async def fail():
                raise self.gate_mod.BackgroundTaskQueueTimeout("probe")

            task = asyncio.create_task(fail())
            try:
                await task
            except self.gate_mod.BackgroundTaskQueueTimeout:
                pass
            task._astrmai_task_name = "attention.system2"
            with mock.patch.object(self.gate_mod.logger, "warning") as warning, mock.patch.object(
                self.gate_mod.logger, "error"
            ) as error:
                self.gate._handle_task_result(task)
            return warning.call_count, error.call_count

        warnings, errors = asyncio.run(run())
        self.assertEqual(warnings, 1)
        self.assertEqual(errors, 0)


if __name__ == "__main__":
    unittest.main()
