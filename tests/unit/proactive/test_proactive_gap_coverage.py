import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

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
        self.assertEqual(intent.metadata, {"group_id": "group:10001"})

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
