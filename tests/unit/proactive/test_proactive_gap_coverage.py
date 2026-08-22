import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.helpers.astrbot_stubs import install_astrbot_stubs


def _config():
    return SimpleNamespace(
        life=SimpleNamespace(
            proactive_quiet_hours=[],
            wakeup_min_energy=0.0,
        ),
        reply=SimpleNamespace(base_frequency=0.7),
        persona=SimpleNamespace(name="Mai", persona_id="global"),
    )


class ProactiveGapCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        for module_name in (
            "astrmai.proactive.dispatcher",
            "astrmai.proactive.wakeup_service",
            "astrmai.proactive.heartflow.manager",
            "astrmai.proactive.proactive_task",
        ):
            sys.modules.pop(module_name, None)
        self.dispatcher_mod = importlib.import_module("astrmai.proactive.dispatcher")
        self.wakeup_mod = importlib.import_module("astrmai.proactive.wakeup_service")
        self.heartflow_mod = importlib.import_module("astrmai.proactive.heartflow.manager")
        self.task_mod = importlib.import_module("astrmai.proactive.proactive_task")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_dispatcher_blocks_second_intent_during_completion_cooldown(self):
        class _AttentionGate:
            async def inject_external_event(self, chat_id, event_data):
                return True

        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=_AttentionGate(),
            runtime_coordinator=SimpleNamespace(
                get_activity_snapshot=lambda chat_id: asyncio.sleep(
                    0,
                    result={
                        "latest_activity_ts": time.time(),
                        "wait_targets": [],
                        "executor_pending": 0,
                    },
                )
            ),
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(
                    0,
                    result=SimpleNamespace(energy=1.0),
                )
            ),
            config=_config(),
        )

        async def _run():
            first = await dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-1",
                    source="wakeup",
                    reason="first",
                    guidance="say one line",
                    cooldown=60,
                )
            )
            await dispatcher.complete(first.intent_id, reply_sent=True)
            second = await dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-1",
                    source="wakeup",
                    reason="second",
                    guidance="say another line",
                    cooldown=60,
                )
            )
            return first, second

        first, second = asyncio.run(_run())

        self.assertTrue(first.allowed)
        self.assertFalse(second.allowed)
        self.assertEqual(second.blocked_reason, "cooldown")
        self.assertGreater(dispatcher._cooldowns["chat-1"], time.time())
        self.assertLessEqual(dispatcher._cooldowns["chat-1"], time.time() + 60)

    def test_dispatcher_allows_intent_after_epoch_cooldown_expires(self):
        class _AttentionGate:
            async def inject_external_event(self, chat_id, event_data):
                return True

        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=_AttentionGate(),
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(
                    0,
                    result=SimpleNamespace(energy=1.0),
                )
            ),
            config=_config(),
        )
        dispatcher._cooldowns["chat-expired"] = time.time() - 1

        decision = asyncio.run(
            dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-expired",
                    source="wakeup",
                    reason="cooldown expired",
                    guidance="say one line",
                )
            )
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.blocked_reason, "")
        self.assertNotIn("chat-expired", dispatcher._cooldowns)

    def test_allowed_dispatch_records_safety_and_completion_stages(self):
        class _AttentionGate:
            def __init__(self):
                self.event_data = None

            async def inject_external_event(self, chat_id, event_data):
                self.event_data = event_data
                return True

        attention_gate = _AttentionGate()
        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=attention_gate,
            runtime_coordinator=SimpleNamespace(
                get_activity_snapshot=lambda chat_id: asyncio.sleep(
                    0,
                    result={"latest_activity_ts": time.time(), "wait_targets": [], "executor_pending": 0},
                )
            ),
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=_config(),
        )

        async def _run():
            decision = await dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-stages",
                    source="wakeup",
                    reason="stage coverage",
                    guidance="say one line",
                )
            )
            callback = attention_gate.event_data["extra"]["astrmai_proactive_completion_callback"]
            await callback(True, "done")
            return dispatcher.list_intents(limit=1)[0]

        history = asyncio.run(_run())
        stages = history["decision"]["stage_ledger"]
        self.assertTrue(any(item["stage"] == "proactive.safety_check" and item["status"] == "success" for item in stages))
        self.assertTrue(any(item["stage"] == "proactive.reply_commit" for item in stages))
        self.assertTrue(any(item["stage"] == "proactive.state_settle" and item["status"] == "success" for item in stages))

    def test_completion_watchdog_settles_stuck_queued_intent(self):
        class _AttentionGate:
            def __init__(self):
                self.event_data = None

            async def inject_external_event(self, chat_id, event_data):
                self.event_data = event_data
                return True

        attention_gate = _AttentionGate()
        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=attention_gate,
            runtime_coordinator=SimpleNamespace(
                get_activity_snapshot=lambda chat_id: asyncio.sleep(
                    0,
                    result={"latest_activity_ts": time.time(), "wait_targets": [], "executor_pending": 0},
                )
            ),
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=_config(),
        )

        async def _run():
            decision = await dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-watchdog",
                    source="wakeup",
                    reason="stuck completion",
                    guidance="say one line",
                )
            )
            callback = attention_gate.event_data["extra"]["astrmai_proactive_completion_callback"]
            await dispatcher._watch_completion(decision.intent_id, 0.01, decision, callback)
            return dispatcher.list_intents(limit=1)[0]

        history = asyncio.run(_run())
        self.assertEqual(history["decision"]["status"], "timeout")
        self.assertEqual(history["decision"]["blocked_reason"], "completion_timeout")
        stages = history["decision"]["stage_ledger"]
        self.assertTrue(any(item["stage"] == "proactive.completion_watchdog" and item["status"] == "timeout" for item in stages))
        self.assertTrue(any(item["stage"] == "proactive.reply_commit" and item["status"] == "timeout" for item in stages))
        self.assertTrue(any(item["stage"] == "proactive.state_settle" for item in stages))

    def test_gate_early_terminal_status_still_settles_external_callback(self):
        callback_results = []

        class _AttentionGate:
            async def inject_external_event(self, chat_id, event_data):
                decision = event_data["extra"]["astrmai_proactive_dispatch_decision"]
                decision.status = "skipped"
                decision.blocked_reason = "duplicate_event"
                callback = event_data["extra"]["astrmai_proactive_completion_callback"]
                await callback(False, "")
                return "DUPLICATED"

        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=_AttentionGate(),
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=_config(),
        )

        async def _run():
            decision = await dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-duplicate",
                    source="wakeup",
                    reason="duplicate",
                    guidance="say one line",
                ),
                on_complete=lambda sent, preview: callback_results.append((sent, preview)),
            )
            await asyncio.sleep(0)
            return decision

        decision = asyncio.run(_run())
        history = dispatcher.list_intents(limit=1)[0]["decision"]
        self.assertEqual(callback_results, [(False, "")])
        self.assertEqual(history["status"], "skipped")
        self.assertEqual(history["blocked_reason"], "duplicate_event")
        self.assertFalse(history["synthetic_event_queued"])
        self.assertEqual(dispatcher.describe_status()["completion_watchdogs"], 0)

    def test_dispatcher_shutdown_settles_queued_claim_and_ignores_late_callback(self):
        class _AttentionGate:
            def __init__(self):
                self.event_data = None

            async def inject_external_event(self, chat_id, event_data):
                self.event_data = event_data
                return True

        callback_results = []
        attention_gate = _AttentionGate()
        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=attention_gate,
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=_config(),
        )

        async def _run():
            decision = await dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-shutdown",
                    source="wakeup",
                    reason="shutdown test",
                    guidance="say one line",
                ),
                on_complete=lambda sent, preview: callback_results.append((sent, preview)),
            )
            callback = attention_gate.event_data["extra"]["astrmai_proactive_completion_callback"]
            await dispatcher.shutdown()
            await callback(True, "late reply")
            return decision, dispatcher.list_intents(limit=1)[0]

        decision, history = asyncio.run(_run())
        self.assertEqual(history["decision"]["status"], "shutdown")
        self.assertEqual(history["decision"]["blocked_reason"], "dispatcher_shutdown")
        self.assertEqual(callback_results, [(False, "")])
        self.assertEqual(dispatcher.describe_status()["completion_watchdogs"], 0)
        self.assertTrue(
            any(
                entry["stage"] == "proactive.dispatcher_shutdown"
                for entry in history["decision"]["stage_ledger"]
            )
        )

    def test_dispatch_rejection_records_stable_reason(self):
        class _AttentionGate:
            async def inject_external_event(self, chat_id, event_data):
                return False

        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=_AttentionGate(),
            runtime_coordinator=SimpleNamespace(
                get_activity_snapshot=lambda chat_id: asyncio.sleep(
                    0,
                    result={"latest_activity_ts": time.time(), "wait_targets": [], "executor_pending": 0},
                )
            ),
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=_config(),
        )

        decision = asyncio.run(
            dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-rejected",
                    source="wakeup",
                    reason="rejected injection",
                    guidance="say one line",
                )
            )
        )

        self.assertFalse(decision.synthetic_event_queued)
        self.assertIn(decision.blocked_reason, {"event_enqueue_rejected", "dispatch_rejected"})
        self.assertTrue(any(item["stage"] == "proactive.event_enqueue" for item in decision.stage_ledger))

    def test_truthy_gate_rejection_is_not_marked_as_queued_and_settles_callback(self):
        callback_results = []

        class _AttentionGate:
            async def inject_external_event(self, chat_id, event_data):
                return "FILTERED"

        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=_AttentionGate(),
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=_config(),
        )
        decision = asyncio.run(
            dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-filtered",
                    source="wakeup",
                    reason="filtered injection",
                    guidance="say one line",
                ),
                on_complete=lambda sent, preview: callback_results.append((sent, preview)),
            )
        )

        self.assertFalse(decision.synthetic_event_queued)
        self.assertEqual(callback_results, [(False, "")])
        self.assertEqual(len(dispatcher._callbacks), 0)

    def test_external_completion_callback_failure_remains_retryable(self):
        attempts = []
        first = True

        class _AttentionGate:
            def __init__(self):
                self.event_data = None

            async def inject_external_event(self, chat_id, event_data):
                self.event_data = event_data
                return "BUFFERED"

        attention_gate = _AttentionGate()

        def external_callback(sent, preview):
            nonlocal first
            attempts.append((sent, preview))
            if first:
                first = False
                raise RuntimeError("settle unavailable")

        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=attention_gate,
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=_config(),
        )

        async def _run():
            decision = await dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-retry",
                    source="wakeup",
                    reason="retry completion",
                    guidance="say one line",
                ),
                on_complete=external_callback,
            )
            callback = attention_gate.event_data["extra"]["astrmai_proactive_completion_callback"]
            with_error = False
            try:
                await callback(False, "")
            except RuntimeError:
                with_error = True
            await callback(False, "")
            return decision, with_error

        decision, with_error = asyncio.run(_run())
        self.assertTrue(with_error)
        self.assertEqual(attempts, [(False, ""), (False, "")])
        self.assertEqual(len(dispatcher._callbacks), 0)
        self.assertEqual(dispatcher.list_intents(limit=1)[0]["decision"]["status"], "skipped")

    def test_external_completion_failure_has_bounded_automatic_retry(self):
        attempts = []
        failures_left = 1

        class _AttentionGate:
            def __init__(self):
                self.event_data = None

            async def inject_external_event(self, chat_id, event_data):
                self.event_data = event_data
                return "BUFFERED"

        def external_callback(sent, preview):
            nonlocal failures_left
            attempts.append((sent, preview))
            if failures_left:
                failures_left -= 1
                raise RuntimeError("transient settle failure")

        attention_gate = _AttentionGate()
        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=attention_gate,
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=_config(),
        )

        async def _run():
            decision = await dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-auto-retry",
                    source="wakeup",
                    reason="automatic retry",
                    guidance="say one line",
                ),
                on_complete=external_callback,
            )
            callback = attention_gate.event_data["extra"]["astrmai_proactive_completion_callback"]
            with self.assertRaises(RuntimeError):
                await callback(False, "")
            await asyncio.sleep(0.12)
            return decision

        decision = asyncio.run(_run())
        self.assertEqual(attempts, [(False, ""), (False, "")])
        self.assertEqual(dispatcher._settlement_state[decision.intent_id], "settled")
        self.assertNotIn(decision.intent_id, dispatcher._callbacks)

    def test_continuous_completion_failure_reaches_retry_exhaustion(self):
        attempts = []

        class _AttentionGate:
            async def inject_external_event(self, chat_id, event_data):
                self.event_data = event_data
                return "BUFFERED"

        def external_callback(sent, preview):
            attempts.append((sent, preview))
            raise RuntimeError("persistent settle failure")

        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=_AttentionGate(),
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=_config(),
        )

        async def _run():
            decision = await dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-retry-exhausted",
                    source="wakeup",
                    reason="persistent failure",
                    guidance="say one line",
                ),
                on_complete=external_callback,
            )
            callback = dispatcher.attention_gate.event_data["extra"]["astrmai_proactive_completion_callback"]
            with self.assertRaises(RuntimeError):
                await callback(False, "")
            await asyncio.sleep(0.5)
            return decision

        decision = asyncio.run(_run())
        self.assertEqual(len(attempts), 4)
        self.assertEqual(dispatcher._settlement_state.get(decision.intent_id), "exhausted")
        self.assertNotIn(decision.intent_id, dispatcher._callbacks)
        self.assertEqual(len(dispatcher._completion_retry_tasks), 0)
        self.assertTrue(
            any(
                item.get("status") == "retry_exhausted"
                for item in dispatcher.list_intents(limit=1)[0]["decision"]["stage_ledger"]
            )
        )

    def test_exhausted_completion_ignores_late_callback(self):
        attempts = []

        class _AttentionGate:
            def __init__(self):
                self.event_data = None

            async def inject_external_event(self, chat_id, event_data):
                self.event_data = event_data
                return "BUFFERED"

        def external_callback(sent, preview):
            attempts.append((sent, preview))
            raise RuntimeError("persistent settle failure")

        attention_gate = _AttentionGate()
        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=attention_gate,
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=_config(),
        )

        async def _run():
            decision = await dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-late-callback",
                    source="wakeup",
                    reason="late callback",
                    guidance="say one line",
                ),
                on_complete=external_callback,
            )
            callback = attention_gate.event_data["extra"]["astrmai_proactive_completion_callback"]
            with self.assertRaises(RuntimeError):
                await callback(False, "")
            await asyncio.sleep(0.5)
            before = dispatcher.list_intents(limit=1)[0]["decision"]
            await callback(True, "late reply")
            after = dispatcher.list_intents(limit=1)[0]["decision"]
            return decision, before, after

        decision, before, after = asyncio.run(_run())
        self.assertEqual(dispatcher._settlement_state.get(decision.intent_id), "exhausted")
        self.assertEqual(attempts, [(False, "")] * 4)
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["reply_sent"], before["reply_sent"])
        self.assertEqual(dispatcher.describe_status()["settlement_exhausted"], 1)

    def test_complete_cannot_mutate_exhausted_intent(self):
        class _AttentionGate:
            def __init__(self):
                self.event_data = None

            async def inject_external_event(self, chat_id, event_data):
                self.event_data = event_data
                return "BUFFERED"

        def external_callback(sent, preview):
            raise RuntimeError("persistent settle failure")

        attention_gate = _AttentionGate()
        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=attention_gate,
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=_config(),
        )

        async def _run():
            decision = await dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-complete-exhausted",
                    source="wakeup",
                    reason="direct complete",
                    guidance="say one line",
                    cooldown=60,
                ),
                on_complete=external_callback,
            )
            callback = attention_gate.event_data["extra"]["astrmai_proactive_completion_callback"]
            with self.assertRaises(RuntimeError):
                await callback(False, "")
            await asyncio.sleep(0.5)
            before = dispatcher.list_intents(limit=1)[0]["decision"]
            cooldowns_before = dict(dispatcher._cooldowns)
            await dispatcher.complete(decision.intent_id, reply_sent=True, reply_preview="late")
            after = dispatcher.list_intents(limit=1)[0]["decision"]
            return before, after, cooldowns_before

        before, after, cooldowns_before = asyncio.run(_run())
        self.assertEqual(after, before)
        self.assertEqual(dispatcher._cooldowns, cooldowns_before)

    def test_shutdown_rejects_dispatch_while_settling(self):
        shutdown_started = asyncio.Event()
        release_callback = asyncio.Event()

        class _AttentionGate:
            async def inject_external_event(self, chat_id, event_data):
                return "BUFFERED"

        async def external_callback(sent, preview):
            shutdown_started.set()
            await release_callback.wait()

        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=_AttentionGate(),
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=_config(),
        )

        async def _run():
            queued = await dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-shutdown-race",
                    source="wakeup",
                    reason="shutdown race",
                    guidance="say one line",
                ),
                on_complete=external_callback,
            )
            shutdown_task = asyncio.create_task(dispatcher.shutdown())
            await shutdown_started.wait()
            rejected = await dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-after-shutdown",
                    source="wakeup",
                    reason="late dispatch",
                    guidance="say one line",
                )
            )
            release_callback.set()
            await shutdown_task
            return queued, rejected

        queued, rejected = asyncio.run(_run())
        self.assertEqual(rejected.blocked_reason, "dispatcher_shutdown")
        self.assertFalse(rejected.allowed)
        status = dispatcher.describe_status()
        self.assertEqual(status["completion_watchdogs"], 0)
        self.assertEqual(status["settlement_pending"], 0)
        self.assertEqual(len(dispatcher._callbacks), 0)

    def test_shutdown_waits_for_inflight_gate_injection(self):
        inject_started = asyncio.Event()
        release_injection = asyncio.Event()

        class _AttentionGate:
            async def inject_external_event(self, chat_id, event_data):
                inject_started.set()
                await release_injection.wait()
                return "BUFFERED"

        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=_AttentionGate(),
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=_config(),
        )

        async def _run():
            dispatch_task = asyncio.create_task(
                dispatcher.dispatch(
                    self.dispatcher_mod.ProactiveMessageIntent(
                        chat_id="chat-inflight-shutdown",
                        source="wakeup",
                        reason="inflight gate",
                        guidance="say one line",
                    ),
                    on_complete=lambda sent, preview: None,
                )
            )
            await inject_started.wait()
            shutdown_task = asyncio.create_task(dispatcher.shutdown())
            await asyncio.sleep(0)
            self.assertFalse(shutdown_task.done())
            release_injection.set()
            decision = await dispatch_task
            await shutdown_task
            return decision

        decision = asyncio.run(_run())
        status = dispatcher.describe_status()
        self.assertNotEqual(decision.status, "queued")
        self.assertNotEqual(dispatcher.list_intents(limit=1)[0]["decision"]["status"], "queued")
        self.assertEqual(status["completion_watchdogs"], 0)
        self.assertEqual(status["settlement_pending"], 0)
        self.assertEqual(len(dispatcher._callbacks), 0)

    def test_resume_reopens_dispatch_after_stop(self):
        class _AttentionGate:
            async def inject_external_event(self, chat_id, event_data):
                return "FILTERED"

        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=_AttentionGate(),
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=_config(),
        )
        dispatcher._shutting_down = True
        self.assertTrue(dispatcher.resume())
        decision = asyncio.run(
            dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-resumed",
                    source="wakeup",
                    reason="reinitialize",
                    guidance="say one line",
                )
            )
        )
        self.assertIn(decision.blocked_reason, {"event_enqueue_rejected", "dispatch_rejected"})
        self.assertFalse(decision.synthetic_event_queued)

    def test_shutdown_isolates_callback_failure_and_settles_intent(self):
        attempts = []

        class _AttentionGate:
            async def inject_external_event(self, chat_id, event_data):
                self.event_data = event_data
                return "BUFFERED"

        def external_callback(sent, preview):
            attempts.append((sent, preview))
            raise RuntimeError("shutdown callback failure")

        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=_AttentionGate(),
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=_config(),
        )

        async def _run():
            decision = await dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-shutdown-failure",
                    source="wakeup",
                    reason="shutdown failure",
                    guidance="say one line",
                ),
                on_complete=external_callback,
            )
            await dispatcher.shutdown()
            return decision

        decision = asyncio.run(_run())
        self.assertEqual(attempts, [(False, "")])
        self.assertEqual(dispatcher._settlement_state.get(decision.intent_id), "shutdown")
        self.assertNotIn(decision.intent_id, dispatcher._callbacks)
        shutdown_entries = [
            item
            for item in dispatcher.list_intents(limit=1)[0]["decision"]["stage_ledger"]
            if item.get("stage") == "proactive.dispatcher_shutdown"
        ]
        self.assertEqual(shutdown_entries[-1]["status"], "settle_failed")

    def test_settlement_state_tables_follow_bounded_tombstones(self):
        class _AttentionGate:
            async def inject_external_event(self, chat_id, event_data):
                return "BUFFERED"

        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=_AttentionGate(),
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=_config(),
        )

        async def _run():
            for index in range(260):
                decision = await dispatcher.dispatch(
                    self.dispatcher_mod.ProactiveMessageIntent(
                        chat_id=f"chat-{index}",
                        source="wakeup",
                        reason=f"bounded-{index}",
                        guidance="say one line",
                    ),
                    on_complete=lambda sent, preview: None,
                )
                callback = dispatcher._history[-1]["decision"]
                await dispatcher.complete(decision.intent_id, reply_sent=False)

        asyncio.run(_run())
        self.assertLessEqual(len(dispatcher._settlement_locks), dispatcher.TERMINAL_TOMBSTONE_LIMIT)
        self.assertLessEqual(len(dispatcher._settlement_state), dispatcher.TERMINAL_TOMBSTONE_LIMIT)
        self.assertLessEqual(len(dispatcher._terminal_intents), dispatcher.TERMINAL_TOMBSTONE_LIMIT)
        self.assertLessEqual(
            dispatcher.describe_status()["terminal_tombstones"],
            dispatcher.TERMINAL_TOMBSTONE_LIMIT,
        )

    def test_concurrent_completion_retries_run_callback_once(self):
        attempts = []

        class _AttentionGate:
            def __init__(self):
                self.event_data = None

            async def inject_external_event(self, chat_id, event_data):
                self.event_data = event_data
                return "BUFFERED"

        async def external_callback(sent, preview):
            attempts.append((sent, preview))
            await asyncio.sleep(0.02)

        attention_gate = _AttentionGate()
        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=attention_gate,
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=_config(),
        )

        async def _run():
            decision = await dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-settlement-lock",
                    source="wakeup",
                    reason="single intent lock",
                    guidance="say one line",
                ),
                on_complete=external_callback,
            )
            callback = attention_gate.event_data["extra"]["astrmai_proactive_completion_callback"]
            await asyncio.gather(callback(False, ""), callback(False, ""))
            return decision

        decision = asyncio.run(_run())
        self.assertEqual(attempts, [(False, "")])
        self.assertEqual(dispatcher._settlement_state[decision.intent_id], "settled")

    def test_dispatcher_blocks_conservatively_on_dirty_pending_snapshot(self):
        class _AttentionGate:
            async def inject_external_event(self, chat_id, event_data):
                raise AssertionError("dirty pending state must not dispatch")

        dispatcher = self.dispatcher_mod.ProactiveDispatcher(
            attention_gate=_AttentionGate(),
            runtime_coordinator=SimpleNamespace(
                get_activity_snapshot=lambda chat_id: asyncio.sleep(
                    0,
                    result={
                        "latest_activity_ts": time.time(),
                        "wait_targets": 7,
                        "executor_pending": "invalid",
                    },
                )
            ),
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(
                    0,
                    result=SimpleNamespace(energy=1.0),
                )
            ),
            config=_config(),
        )

        decision = asyncio.run(
            dispatcher.dispatch(
                self.dispatcher_mod.ProactiveMessageIntent(
                    chat_id="chat-dirty",
                    source="wakeup",
                    reason="dirty snapshot",
                    guidance="say one line",
                )
            )
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.blocked_reason, "user_waiting")

        history = dispatcher.list_intents(limit=1)[0]
        stages = history["decision"]["stage_ledger"]
        self.assertEqual([item["stage"] for item in stages], [
            "proactive.candidate",
            "proactive.safety_check",
            "proactive.dispatch",
        ])
        self.assertEqual(stages[-1]["reason"], "user_waiting")

    def test_wakeup_intent_preserves_dispatch_contract(self):
        service = self.wakeup_mod.WakeupService(
            context=SimpleNamespace(),
            state_engine=SimpleNamespace(),
            persistence=SimpleNamespace(load_persona_cache=lambda: {}),
            call_background_lane=None,
            config=_config(),
        )

        async def _opening(chat_id):
            return "Continue the conversation gently."

        service.generate_opening_line = _opening
        state = SimpleNamespace(chat_id="group:10001")

        intent = asyncio.run(service.build_wakeup_intent(state, 5, 90))

        self.assertEqual(intent.chat_id, "group:10001")
        self.assertEqual(intent.source, "wakeup")
        self.assertEqual(intent.cost, 5.0)
        self.assertEqual(intent.cooldown, 90.0)
        self.assertEqual(
            intent.metadata,
            {
                "chat_kind": "group",
                "captured_generation": 0,
                "claim_token": "",
                "group_id": "group:10001",
            },
        )

    def test_wakeup_intent_rejects_blank_guidance(self):
        service = self.wakeup_mod.WakeupService(
            context=SimpleNamespace(),
            state_engine=SimpleNamespace(),
            persistence=SimpleNamespace(load_persona_cache=lambda: {}),
            call_background_lane=None,
            config=_config(),
        )

        async def _opening(chat_id):
            return "   "

        service.generate_opening_line = _opening

        intent = asyncio.run(
            service.build_wakeup_intent(
                SimpleNamespace(chat_id="group:10001"),
                5,
                90,
            )
        )

        self.assertIsNone(intent)

    def test_tick_chat_degrades_when_runtime_snapshot_lookup_fails(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                raise RuntimeError("runtime unavailable")

        manager = self.heartflow_mod.HeartflowManager(
            runtime_coordinator=_Coordinator(),
        )

        payload = asyncio.run(manager.tick_chat("chat-2"))

        self.assertFalse(payload["eligible"])
        self.assertEqual(payload["blocked_reason"], "snapshot_unavailable")

    def test_tick_chat_tolerates_dirty_snapshot_counters(self):
        manager = self.heartflow_mod.HeartflowManager(
            state_engine=SimpleNamespace(
                get_state=lambda chat_id: asyncio.sleep(
                    0,
                    result=SimpleNamespace(energy=0.8, mood=0.1),
                )
            ),
        )
        now = time.time()
        snapshot = {
            "latest_activity_ts": now - 20,
            "recent_activity_count": "invalid",
            "recent_activity_count_60s": "invalid",
            "recent_direct_count": "invalid",
            "recent_bot_reply_count": "invalid",
            "latest_activity_preview": "hello",
            "wait_targets": 7,
            "executor_pending": "invalid",
            "cooldown_tags": 7,
        }

        payload = asyncio.run(
            manager.tick_chat(
                "chat-3",
                snapshot=snapshot,
                now=now,
            )
        )

        self.assertTrue(payload["performed"])
        self.assertEqual(payload["action_type"], "wait")
        self.assertEqual(payload["blocked_reason"], "user_waiting")
        self.assertIsNotNone(manager.get_state("chat-3"))
        self.assertIsNotNone(manager.get_session("chat-3"))

    def test_proactive_task_maintenance_cycle_isolates_subservice_failures(self):
        async def _run():
            calls = []

            class _Decay:
                async def run_once(self):
                    calls.append("decay")
                    raise RuntimeError("decay failed")

            class _GroupSignin:
                async def run_once(self):
                    calls.append("signin")

            class _Digest:
                async def run_once(self, manager):
                    calls.append(("digest", manager.name))

            task = self.task_mod.ProactiveTask.__new__(self.task_mod.ProactiveTask)
            task.decay_service = _Decay()
            task.group_signin_service = _GroupSignin()
            task.heartflow_topic_digest_service = _Digest()
            task.heartflow_manager = SimpleNamespace(name="heartflow")
            task._background_tasks = set()
            task._handle_task_result = lambda background_task: task._background_tasks.discard(background_task)
            task._fire_background_task = self.task_mod.ProactiveTask._fire_background_task.__get__(task, self.task_mod.ProactiveTask)

            await task._run_maintenance_cycle()
            await asyncio.gather(*list(task._background_tasks))

            self.assertEqual(calls, ["decay", "signin", ("digest", "heartflow")])
            self.assertEqual(task._background_tasks, set())

        asyncio.run(_run())

    def test_proactive_task_stop_cancels_loop_and_background_tasks(self):
        async def _run():
            task = self.task_mod.ProactiveTask.__new__(self.task_mod.ProactiveTask)
            task._is_running = True

            async def _sleep_forever():
                await asyncio.sleep(60)

            loop_task = asyncio.create_task(_sleep_forever())
            background_task = asyncio.create_task(_sleep_forever())
            task._task = loop_task
            task._background_tasks = {background_task}

            await task.stop()

            self.assertFalse(task._is_running)
            self.assertIsNone(task._task)
            self.assertTrue(loop_task.cancelled())
            self.assertTrue(background_task.cancelled())
            self.assertEqual(task._background_tasks, set())

        asyncio.run(_run())

    def test_proactive_task_scheduler_poll_modes_set_expected_intervals(self):
        task = self.task_mod.ProactiveTask.__new__(self.task_mod.ProactiveTask)

        task._set_scheduler_poll_mode("FAST")
        self.assertEqual(task._scheduler_poll_mode, "FAST")
        self.assertEqual(
            task._scheduler_poll_interval_seconds,
            task.FAST_POLL_INTERVAL_SECONDS,
        )

        task._set_scheduler_poll_mode("NORMAL")
        self.assertEqual(task._scheduler_poll_mode, "NORMAL")
        self.assertEqual(
            task._scheduler_poll_interval_seconds,
            task.NORMAL_POLL_INTERVAL_SECONDS,
        )

        task._set_scheduler_poll_mode("unexpected")
        self.assertEqual(task._scheduler_poll_mode, "IDLE")
        self.assertEqual(
            task._scheduler_poll_interval_seconds,
            task.IDLE_POLL_INTERVAL_SECONDS,
        )

    def test_proactive_task_loads_selected_persona_summary_from_sync_cache(self):
        task = self.task_mod.ProactiveTask.__new__(self.task_mod.ProactiveTask)
        task.config = SimpleNamespace(persona=SimpleNamespace(persona_id="persona-1"))
        task.persistence = SimpleNamespace(
            load_persona_cache=lambda: {
                "persona-1": {"summary": "  A calm and curious persona.  "},
                "global": {"summary": "not selected"},
            }
        )

        summary = asyncio.run(task._load_persona_summary())

        self.assertEqual(summary, "A calm and curious persona.")

    def test_proactive_task_binds_dream_dependencies(self):
        captured = {}

        class _DreamAgent:
            def __init__(self, **kwargs):
                captured["agent_kwargs"] = kwargs

        class _PromotionEngine:
            def __init__(self, memory_engine):
                captured["promotion_engine_memory"] = memory_engine

        class _DreamScheduler:
            def bind_dependencies(self, *args, **kwargs):
                captured["bind_args"] = args
                captured["bind_kwargs"] = kwargs

        task = self.task_mod.ProactiveTask.__new__(self.task_mod.ProactiveTask)
        task.gateway = object()
        task._db_service = object()
        task.memory_engine = object()
        task.config = _config()
        task.dream_generator = object()
        task.dream_scheduler = _DreamScheduler()

        with (
            patch.object(self.task_mod, "DreamAgent", _DreamAgent),
            patch.object(self.task_mod, "MemoryPromotionEngine", _PromotionEngine),
        ):
            task._bind_dream_dependencies()

        self.assertIsInstance(task.dream_agent, _DreamAgent)
        self.assertIs(captured["agent_kwargs"]["gateway"], task.gateway)
        self.assertIs(captured["agent_kwargs"]["db_service"], task._db_service)
        self.assertIs(captured["agent_kwargs"]["memory_engine"], task.memory_engine)
        self.assertIs(captured["promotion_engine_memory"], task.memory_engine)
        self.assertIs(captured["bind_args"][0], task.dream_agent)
        self.assertIs(captured["bind_args"][1], task.dream_generator)
        self.assertIs(captured["bind_kwargs"]["db_service"], task._db_service)
        self.assertIsInstance(captured["bind_kwargs"]["promotion_engine"], _PromotionEngine)

    def test_proactive_task_generates_and_persists_persona_analysis(self):
        async def _run():
            saved = []
            task = self.task_mod.ProactiveTask.__new__(self.task_mod.ProactiveTask)
            task.state_engine = SimpleNamespace()
            task.prompt_registry = None
            task.profile_generator = SimpleNamespace(
                build_prompt=lambda profile, summary: f"profile:{profile.name}:{summary}",
                parse_result=lambda result: {
                    "analysis": "thoughtful",
                    "tags": ["curious"],
                    "memory_points": [{"category": "identity", "content": "likes puzzles"}],
                },
                categorize_memory_points=lambda points: {
                    "identity_points": ["likes puzzles"],
                    "preference_points": [],
                    "relationship_points": [],
                    "speech_style_points": [],
                },
            )

            async def _load_summary():
                return "persona summary"

            async def _call_lane(*args, **kwargs):
                self.assertEqual(args[2], "profile:Alice:persona summary")
                return "analysis response"

            async def _save(profile):
                saved.append(profile)

            task._load_persona_summary = _load_summary
            task._call_background_lane = _call_lane
            task._save_user_profile = _save
            profile = SimpleNamespace(
                user_id="user-1",
                name="Alice",
                message_count_for_profiling=12,
            )

            await task._generate_persona_analysis(profile)

            self.assertEqual(profile.persona_analysis, "thoughtful")
            self.assertEqual(profile.tags, ["curious"])
            self.assertEqual(profile.identity_points, ["likes puzzles"])
            self.assertEqual(profile.message_count_for_profiling, 0)
            self.assertTrue(profile.is_dirty)
            self.assertEqual(saved, [profile])

        asyncio.run(_run())

    def test_proactive_task_generates_and_persists_nickname(self):
        async def _run():
            saved = []
            task = self.task_mod.ProactiveTask.__new__(self.task_mod.ProactiveTask)
            task.state_engine = SimpleNamespace()
            task.prompt_registry = None
            task.nickname_generator = SimpleNamespace(
                build_prompt=lambda profile, summary: f"nickname:{profile.name}:{summary}",
                parse_result=lambda result: ("小艾", "friendly"),
                choose=lambda name, preferred: preferred,
            )

            async def _load_summary():
                return "persona summary"

            async def _call_lane(*args, **kwargs):
                self.assertEqual(args[2], "nickname:Alice:persona summary")
                return "nickname response"

            async def _save(profile):
                saved.append(profile)

            task._load_persona_summary = _load_summary
            task._call_background_lane = _call_lane
            task._save_user_profile = _save
            profile = SimpleNamespace(
                user_id="user-1",
                name="Alice",
                is_known=False,
            )

            await task._generate_nickname(profile)

            self.assertEqual(profile.nickname, "小艾")
            self.assertEqual(profile.nickname_reason, "friendly")
            self.assertTrue(profile.is_known)
            self.assertTrue(profile.is_dirty)
            self.assertEqual(saved, [profile])

        asyncio.run(_run())

    def test_proactive_task_runs_reflection_pipeline_for_active_chats(self):
        async def _run():
            calls = []

            class _Reflector:
                async def reflect_batch(self, chat_id):
                    calls.append(("reflect", chat_id))

                async def auto_audit(self, chat_id):
                    calls.append(("audit", chat_id))

            class _AutoCheck:
                async def run_once(self, chat_id):
                    calls.append(("check", chat_id))

            class _ReviewDispatcher:
                async def dispatch_pending(self):
                    calls.append(("dispatch", None))

            task = self.task_mod.ProactiveTask.__new__(self.task_mod.ProactiveTask)
            task.config = SimpleNamespace(
                evolution=SimpleNamespace(enable_expression_mining=True)
            )
            task.reflector = _Reflector()
            task.auto_check_task = _AutoCheck()
            task.review_dispatcher = _ReviewDispatcher()
            task.state_engine = SimpleNamespace(
                get_active_states=lambda: [
                    SimpleNamespace(chat_id=""),
                    SimpleNamespace(chat_id="chat-1"),
                ]
            )

            await task._run_reflection_tasks()

            self.assertEqual(
                calls,
                [
                    ("reflect", "chat-1"),
                    ("audit", "chat-1"),
                    ("check", "chat-1"),
                    ("dispatch", None),
                ],
            )

        asyncio.run(_run())

    def test_proactive_task_runs_group_profile_learning_and_generation(self):
        async def _run():
            calls = []

            async def _record_touch(user_id, **kwargs):
                calls.append(("touch", user_id, kwargs))

            async def _select_target(chat_id):
                calls.append(("select", chat_id))
                return "user-1", "Alice", 4

            async def _generate_nickname(profile):
                calls.append(("nickname", profile.name))

            async def _generate_analysis(profile):
                calls.append(("analysis", profile.name))

            profile = SimpleNamespace(
                name="Alice",
                know_times=3,
                is_known=False,
                message_count_for_profiling=5,
            )
            task = self.task_mod.ProactiveTask.__new__(self.task_mod.ProactiveTask)
            task._profile_semaphore = asyncio.Semaphore(1)
            task.state_engine = SimpleNamespace(
                get_active_states=lambda: [
                    SimpleNamespace(chat_id=""),
                    SimpleNamespace(chat_id="FriendMessage:user-2"),
                    SimpleNamespace(chat_id="GroupMessage:group-1"),
                ],
                get_active_profiles=lambda: [profile],
                record_profile_learning_touch=_record_touch,
            )
            task.config = SimpleNamespace(
                life=SimpleNamespace(profiling_msg_threshold=5)
            )
            task._select_group_profile_target = _select_target
            task._generate_nickname = _generate_nickname
            task._generate_persona_analysis = _generate_analysis

            await task._run_profiling_task()

            self.assertEqual(
                calls,
                [
                    ("select", "GroupMessage:group-1"),
                    (
                        "touch",
                        "user-1",
                        {
                            "chat_id": "GroupMessage:group-1",
                            "source": "group_periodic",
                            "weight": 4,
                            "sender_name": "Alice",
                            "increment_know_times": True,
                        },
                    ),
                    ("nickname", "Alice"),
                    ("analysis", "Alice"),
                ],
            )

        asyncio.run(_run())

    def test_profiling_respects_persisted_user_cooldown(self):
        async def _run():
            calls = []
            profile = SimpleNamespace(
                user_id="user-1",
                name="Alice",
                know_times=3,
                is_known=True,
                message_count_for_profiling=100,
                last_persona_gen_time=time.time() - 60,
            )
            task = self.task_mod.ProactiveTask.__new__(self.task_mod.ProactiveTask)
            task._profile_semaphore = asyncio.Semaphore(1)
            task._profiling_user_ids = set()
            task.state_engine = SimpleNamespace(
                get_active_states=lambda: [],
                get_active_profiles=lambda: [profile],
            )
            task.config = SimpleNamespace(
                life=SimpleNamespace(
                    profiling_msg_threshold=5,
                    profiling_user_cooldown_sec=21600,
                )
            )

            async def _generate_analysis(_profile):
                calls.append("analysis")

            task._generate_persona_analysis = _generate_analysis
            task._generate_nickname = lambda _profile: None
            await task._run_profiling_task()
            self.assertEqual(calls, [])
            self.assertEqual(task._profiling_user_ids, set())

        asyncio.run(_run())

    def test_preview_chat_uses_epoch_clock_for_stale_snapshot(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {
                    "latest_activity_ts": time.time() - 1900,
                    "recent_activity_count": 4,
                    "recent_activity_count_60s": 2,
                    "latest_activity_preview": "old conversation",
                    "wait_targets": [],
                    "executor_pending": 0,
                }

        manager = self.heartflow_mod.HeartflowManager(
            runtime_coordinator=_Coordinator(),
        )

        payload = asyncio.run(manager.preview_chat("chat-stale"))

        self.assertFalse(payload["eligible"])
        self.assertEqual(payload["action_type"], "")


if __name__ == "__main__":
    unittest.main()
