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
