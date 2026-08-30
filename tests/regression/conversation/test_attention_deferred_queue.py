from __future__ import annotations

import asyncio
import importlib
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs
from tests.helpers.attention_stubs import install_attention_stubs


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
