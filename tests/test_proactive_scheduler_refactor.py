import asyncio
import dataclasses
import importlib
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs
from tests.helpers.attention_stubs import install_attention_stubs


def _install_proactive_stubs():
    event_mod = sys.modules["astrbot.api.event"]

    class MessageChain:
        def __init__(self):
            self.chain = []

        def message(self, text):
            self.chain.append(text)
            return self

    event_mod.MessageChain = MessageChain

    legacy_mod = types.ModuleType("astrmai.evolution.proactive_task")

    class LegacyProactiveTask:
        def __init__(self, *args, **kwargs):
            self.auto_check_task = None
            self.reflect_tracker = None

        def set_db_service(self, db_service):
            self.db_service = db_service

        async def _run_profiling_task(self):
            return None

    legacy_mod.ProactiveTask = LegacyProactiveTask
    sys.modules["astrmai.evolution.proactive_task"] = legacy_mod

    dream_agent_mod = types.ModuleType("astrmai.memory.dream_agent")

    class DreamAgent:
        def __init__(self, *args, **kwargs):
            self._last_session_id = "group-1"
            self.MIN_EVENTS_TO_DREAM = 5

        async def run_dream_cycle(self):
            return "dream-log"

    dream_agent_mod.DreamAgent = DreamAgent
    sys.modules["astrmai.memory.dream_agent"] = dream_agent_mod

    dream_generator_mod = types.ModuleType("astrmai.memory.dream_generator")

    class DreamGenerator:
        def __init__(self, *args, **kwargs):
            pass

        async def generate(self, **kwargs):
            return "dream-text"

        def build_maintenance_result(self, dream_log, session_id=""):
            return {"summary": f"{dream_log}:{session_id}"}

    dream_generator_mod.DreamGenerator = DreamGenerator
    sys.modules["astrmai.memory.dream_generator"] = dream_generator_mod


class ProactiveSchedulerRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        _install_proactive_stubs()
        sys.modules.pop("astrmai.proactive.proactive_task", None)
        self.mod = importlib.import_module("astrmai.proactive.proactive_task")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_proactive_task_exposes_local_scheduler_status(self):
        gateway = SimpleNamespace(
            config=SimpleNamespace(
                life=SimpleNamespace(
                    dream_interval_min=1,
                    dream_time_ranges=[],
                    silence_threshold=10,
                    wakeup_min_energy=20,
                    wakeup_cost=5,
                    wakeup_cooldown=60,
                    dream_visible=False,
                ),
                persona=SimpleNamespace(persona_id="global", name="Mai"),
                evolution=SimpleNamespace(enable_expression_mining=False, enable_relationship_engine=False),
            ),
            call_proactive_task=None,
        )

        async def _call_proactive_task(**kwargs):
            return "ok"

        gateway.call_proactive_task = _call_proactive_task
        state_engine = SimpleNamespace(
            get_active_states=lambda: [],
            get_active_profiles=lambda: [],
            apply_natural_decay=lambda state: None,
        )
        persistence = SimpleNamespace(load_persona_cache=lambda: {})
        memory_engine = SimpleNamespace(add_memory=None)
        task = self.mod.ProactiveTask(
            context=SimpleNamespace(send_message=None),
            state_engine=state_engine,
            gateway=gateway,
            persistence=persistence,
            memory_engine=memory_engine,
            reflector=None,
            config=gateway.config,
        )
        task.set_db_service(SimpleNamespace())
        status = task.describe_status()
        self.assertIn("dream_ready", status)
        self.assertFalse(status["running"])
        self.assertIn("dream_scheduler", status)
        self.assertIn("group_signin", status)
        self.assertIn("heartflow", status)
        self.assertFalse(status["heartflow"]["enabled"])
        self.assertFalse(status["chat_loop_kernel_bound"])
        self.assertEqual(status["heartbeat_mode"], "kernel_mediated")
        self.assertTrue(status["private_wait_visible_in_heartbeat"])
        self.assertTrue(status["heartflow_preview_readonly"])
        self.assertEqual(status["dream_scope"], "global_throttle")
        self.assertEqual(status["scheduler_poll_mode"], "FAST")
        self.assertEqual(status["scheduler_poll_interval"], 5.0)
        self.assertEqual(status["global_maintenance_interval"], 60.0)

    def test_configure_accepts_deps_and_binds_planner_heartflow_manager(self):
        gateway = SimpleNamespace(
            config=SimpleNamespace(
                life=SimpleNamespace(
                    dream_interval_min=1,
                    dream_time_ranges=[],
                    silence_threshold=10,
                    wakeup_min_energy=20,
                    wakeup_cost=5,
                    wakeup_cooldown=60,
                    dream_visible=False,
                ),
                persona=SimpleNamespace(persona_id="global", name="Mai"),
                evolution=SimpleNamespace(enable_expression_mining=False, enable_relationship_engine=False),
            ),
            call_proactive_task=None,
        )

        async def _call_proactive_task(**kwargs):
            return "ok"

        gateway.call_proactive_task = _call_proactive_task
        task = self.mod.ProactiveTask(
            context=SimpleNamespace(send_message=None),
            state_engine=SimpleNamespace(get_active_states=lambda: [], get_active_profiles=lambda: [], apply_natural_decay=lambda state: None),
            gateway=gateway,
            persistence=SimpleNamespace(load_persona_cache=lambda: {}),
            memory_engine=SimpleNamespace(add_memory=None),
            reflector=None,
            config=gateway.config,
        )
        planner = SimpleNamespace(heartflow_manager=None)

        task.configure(self.mod.ProactiveDeps(dream_visible=True, planner=planner))

        self.assertTrue(task.dream_scheduler.dream_visible)
        self.assertIs(planner.heartflow_manager, task.heartflow_manager)

    def test_proactive_task_refresh_config_propagates_to_runtime_children(self):
        old_config = SimpleNamespace(
            life=SimpleNamespace(proactive_quiet_hours=["23:30-07:30"], silence_threshold=10, wakeup_min_energy=20, wakeup_cost=5, wakeup_cooldown=60, dream_visible=False),
            persona=SimpleNamespace(persona_id="global", name="Mai"),
            evolution=SimpleNamespace(enable_expression_mining=False, enable_relationship_engine=False),
        )
        new_config = SimpleNamespace(
            life=SimpleNamespace(proactive_quiet_hours=[], silence_threshold=20, wakeup_min_energy=10, wakeup_cost=1, wakeup_cooldown=30, dream_visible=False),
            persona=SimpleNamespace(persona_id="new-persona", name="Mai"),
            evolution=SimpleNamespace(enable_expression_mining=False, enable_relationship_engine=False),
        )
        gateway = SimpleNamespace(config=old_config, call_proactive_task=lambda **kwargs: asyncio.sleep(0, result="ok"))
        state_engine = SimpleNamespace(
            get_active_states=lambda: [],
            get_active_profiles=lambda: [],
            apply_natural_decay=lambda state: None,
        )
        persistence = SimpleNamespace(load_persona_cache=lambda: {})
        memory_engine = SimpleNamespace(add_memory=None)
        task = self.mod.ProactiveTask(
            context=SimpleNamespace(send_message=None),
            state_engine=state_engine,
            gateway=gateway,
            persistence=persistence,
            memory_engine=memory_engine,
            reflector=None,
            config=old_config,
        )
        task.dream_agent = SimpleNamespace(config=old_config)

        task.refresh_config(new_config)

        self.assertIs(task.config, new_config)
        self.assertIs(task.gateway.config, new_config)
        self.assertIs(task.proactive_dispatcher.config, new_config)
        self.assertIs(task.wakeup_service.config, new_config)
        self.assertIs(task.group_signin_service.config, new_config)
        self.assertIs(task.decay_service.config, new_config)
        self.assertIs(task.diary_service.config, new_config)
        self.assertIs(task.dream_scheduler.config, new_config)
        self.assertIs(task.heartflow_manager.config, new_config)
        self.assertIs(task.dream_generator.config, new_config)
        self.assertIs(task.dream_agent.config, new_config)

    def test_start_initializes_global_maintenance_clock(self):
        gateway = SimpleNamespace(
            config=SimpleNamespace(
                life=SimpleNamespace(
                    dream_interval_min=1,
                    dream_time_ranges=[],
                    silence_threshold=10,
                    wakeup_min_energy=20,
                    wakeup_cost=5,
                    wakeup_cooldown=60,
                    dream_visible=False,
                ),
                persona=SimpleNamespace(persona_id="global", name="Mai"),
                evolution=SimpleNamespace(enable_expression_mining=False, enable_relationship_engine=False),
            ),
            call_proactive_task=lambda **kwargs: asyncio.sleep(0, result="ok"),
        )
        task = self.mod.ProactiveTask(
            context=SimpleNamespace(send_message=None),
            state_engine=SimpleNamespace(
                get_active_states=lambda: [],
                get_active_profiles=lambda: [],
                apply_natural_decay=lambda state: None,
            ),
            gateway=gateway,
            persistence=SimpleNamespace(load_persona_cache=lambda: {}),
            memory_engine=SimpleNamespace(add_memory=None),
            reflector=None,
            config=gateway.config,
        )

        async def _run():
            await task.start()
            await task.stop()

        asyncio.run(_run())

        self.assertGreater(task._last_global_maintenance_run, 0.0)

    def test_loop_no_longer_uses_first_tick_continue_for_global_maintenance(self):
        path = Path(self.mod.__file__)
        content = path.read_text(encoding="utf-8")

        self.assertNotIn("if self._last_global_maintenance_run <= 0:", content)
        self.assertNotIn("self._last_global_maintenance_run = now\n                    continue", content)

    def test_chat_heartbeat_pass_routes_active_chats_through_kernel(self):
        kernel_mod = importlib.import_module("astrmai.conversation.loop.chat_loop_kernel")

        gateway = SimpleNamespace(
            config=SimpleNamespace(
                life=SimpleNamespace(
                    dream_interval_min=1,
                    dream_time_ranges=[],
                    silence_threshold=10,
                    wakeup_min_energy=20,
                    wakeup_cost=5,
                    wakeup_cooldown=60,
                    dream_visible=False,
                ),
                persona=SimpleNamespace(persona_id="global", name="Mai"),
                evolution=SimpleNamespace(enable_expression_mining=False, enable_relationship_engine=False),
            ),
        )

        async def _call_proactive_task(**kwargs):
            return "ok"

        gateway.call_proactive_task = _call_proactive_task

        class _Coordinator:
            async def list_active_chats(self):
                return ["chat-1", "chat-2"]

            async def get_activity_snapshot(self, chat_id):
                if chat_id == "chat-1":
                    return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}
                return {"chat_id": chat_id, "executor_pending": 1, "wait_targets": ["u1"]}

        task = self.mod.ProactiveTask(
            context=SimpleNamespace(send_message=None),
            state_engine=SimpleNamespace(
                get_active_states=lambda: [],
                get_active_profiles=lambda: [],
                apply_natural_decay=lambda state: None,
            ),
            gateway=gateway,
            persistence=SimpleNamespace(load_persona_cache=lambda: {}),
            memory_engine=SimpleNamespace(add_memory=None),
            reflector=None,
            config=gateway.config,
            runtime_coordinator=_Coordinator(),
        )
        kernel = kernel_mod.ChatLoopKernel(runtime_coordinator=task.runtime_coordinator)
        kernel.set_scheduler_profile_for_testing("maintenance_friendly")
        task.bind_chat_loop_kernel(kernel)

        async def _describe_due_selection(chat_ids, **kwargs):
            return {
                "selected": ["chat-1"],
                "skipped_not_due": ["chat-2"],
                "skipped_by_batch": [],
                "score_breakdown": {"chat-1": {"scheduler_score": 11.0, "due_rank": 1}},
                "due_phase_mix": {"MAINTENANCE": 1},
                "poll_mode": "NORMAL",
                "poll_mode_reason": "background_due_only",
                "promotion_candidates": ["chat-1"],
                "forced_promotions_selected": ["chat-1"],
                "dialogue_selected": [],
                "maintenance_selected": ["chat-1"],
                "batch_plan": {
                    "total_limit": 32,
                    "promotion_slots": 1,
                    "dialogue_slots": 12,
                    "maintenance_slots": 12,
                    "overflow_slots": 7,
                },
                "batch_fill_rate": 0.03125,
                "batch_pressure": {"busy_ratio": 0.0, "maintenance_backlog_ratio": 1.0, "retry_pressure_count": 0},
                "quota_skipped": {
                    "skipped_by_dialogue_quota": [],
                    "skipped_by_maintenance_quota": [],
                    "skipped_by_promotion_overflow": [],
                },
                "quota_skip_counts": {
                    "skipped_by_dialogue_quota": 0,
                    "skipped_by_maintenance_quota": 0,
                    "skipped_by_promotion_overflow": 0,
                },
                "maintenance_budget_total": 1,
                "maintenance_budget_used": 0,
                "maintenance_budget_remaining": 1,
                "maintenance_blocked_by_budget": [],
                "busy_backpressure_active": False,
                "maintenance_backpressure_active": True,
            }

        kernel.describe_due_selection = _describe_due_selection

        results = asyncio.run(task._run_chat_heartbeat_pass())

        self.assertEqual(results, [{"chat_id": "chat-1", "action": "NOOP", "reason": "no_signal_ready"}])
        status = task.describe_status()
        self.assertEqual(status["due_chat_count"], 1)
        self.assertEqual(status["skipped_not_due_count"], 1)
        self.assertEqual(status["scheduler_poll_mode"], "NORMAL")
        self.assertEqual(status["scheduler_poll_interval"], 10.0)
        self.assertEqual(status["due_phase_mix"], {"MAINTENANCE": 1})
        self.assertEqual(status["maintenance_budget_total"], 1)
        self.assertEqual(status["maintenance_budget_remaining"], 1)
        self.assertEqual(status["scheduler_batch_limit"], 32)
        self.assertEqual(status["scheduler_batch_plan"]["maintenance_slots"], 12)
        self.assertEqual(status["forced_promotion_count"], 1)
        self.assertEqual(status["last_selection_summary"]["maintenance_selected_count"], 1)
        self.assertTrue(status["maintenance_backpressure_active"])
        self.assertEqual(status["scheduler_policy"]["active_profile"], "maintenance_friendly")
        self.assertEqual(status["scheduler_policy"]["current"]["maintenance_batch_slots"], 6)
        self.assertIn("forced_promotion_count", status["kernel_due_selection_summary"])
        self.assertEqual(status["poll_mode_transition"]["previous"], "FAST")
        self.assertEqual(status["poll_mode_transition"]["current"], "NORMAL")

    def _build_heartbeat_task_for_due_tests(self, state_engine, runtime_coordinator, *, proactive_due_enabled=True):
        config = SimpleNamespace(
            life=SimpleNamespace(
                dream_interval_min=1,
                dream_time_ranges=[],
                silence_threshold=10,
                wakeup_min_energy=20,
                wakeup_cost=5,
                wakeup_cooldown=60,
                dream_visible=False,
            ),
            persona=SimpleNamespace(persona_id="global", name="Mai"),
            evolution=SimpleNamespace(enable_expression_mining=False, enable_relationship_engine=False),
            architecture_rollout=SimpleNamespace(proactive_due_enabled=proactive_due_enabled),
        )
        gateway = SimpleNamespace(config=config, call_proactive_task=lambda **_kwargs: asyncio.sleep(0, result="ok"))

        class _Kernel:
            def __init__(self):
                self.selection_args = None

            async def describe_due_selection(self, chat_ids, **kwargs):
                self.selection_args = (list(chat_ids), dict(kwargs))
                sources = dict(kwargs.get("candidate_sources", {}) or {})
                persistent_due_selected = [
                    chat_id for chat_id in chat_ids if sources.get(chat_id) == "persistent_due"
                ]
                return {
                    "selected": list(chat_ids),
                    "skipped_not_due": [],
                    "skipped_by_batch": [],
                    "due_phase_mix": {"ACTIVE": len(chat_ids)} if chat_ids else {},
                    "poll_mode": "FAST" if chat_ids else "IDLE",
                    "poll_mode_reason": "dialogue_pressure" if chat_ids else "idle_backoff",
                    "persistent_due_selected": persistent_due_selected,
                    "batch_plan": {"total_limit": 32},
                }

            async def tick(self, *, chat_id, trigger):
                return SimpleNamespace(decision=SimpleNamespace(action="NOOP", reason="no_signal_ready"))

        task = self.mod.ProactiveTask(
            context=SimpleNamespace(send_message=None),
            state_engine=state_engine,
            gateway=gateway,
            persistence=SimpleNamespace(load_persona_cache=lambda: {}),
            memory_engine=SimpleNamespace(add_memory=None),
            reflector=None,
            config=config,
            runtime_coordinator=runtime_coordinator,
        )
        kernel = _Kernel()
        task.bind_chat_loop_kernel(kernel)
        return task, kernel

    def test_chat_heartbeat_recovers_persisted_due_chat_after_empty_runtime_cache(self):
        class _Coordinator:
            async def list_active_chats(self):
                return []

        class _StateEngine:
            async def list_due_proactive_chat_ids(self, **_kwargs):
                return ["group:restart", "group:restart"]

        task, kernel = self._build_heartbeat_task_for_due_tests(_StateEngine(), _Coordinator())

        results = asyncio.run(task._run_chat_heartbeat_pass())

        self.assertEqual(results, [{"chat_id": "group:restart", "action": "NOOP", "reason": "no_signal_ready"}])
        self.assertEqual(kernel.selection_args[0], ["group:restart"])
        self.assertEqual(kernel.selection_args[1]["candidate_sources"], {"group:restart": "persistent_due"})
        status = task.describe_status()
        self.assertEqual(status["active_candidate_count"], 0)
        self.assertEqual(status["persistent_due_candidate_count"], 1)
        self.assertEqual(status["merged_candidate_count"], 1)
        self.assertEqual(status["selected_persistent_due_count"], 1)
        self.assertTrue(status["persistent_due_scan_enabled"])

    def test_chat_heartbeat_keeps_active_scan_when_persistent_due_scan_disabled_or_degraded(self):
        class _Coordinator:
            async def list_active_chats(self):
                return ["group:active"]

        class _DisabledStateEngine:
            async def list_due_proactive_chat_ids(self, **_kwargs):
                raise AssertionError("disabled due scan must not query persistence")

        disabled_task, disabled_kernel = self._build_heartbeat_task_for_due_tests(
            _DisabledStateEngine(),
            _Coordinator(),
            proactive_due_enabled=False,
        )
        asyncio.run(disabled_task._run_chat_heartbeat_pass())
        self.assertEqual(disabled_kernel.selection_args[0], ["group:active"])
        self.assertFalse(disabled_task.describe_status()["persistent_due_scan_enabled"])

        class _FailingStateEngine:
            async def list_due_proactive_chat_ids(self, **_kwargs):
                raise RuntimeError("database temporarily unavailable")

        degraded_task, degraded_kernel = self._build_heartbeat_task_for_due_tests(
            _FailingStateEngine(),
            _Coordinator(),
        )
        asyncio.run(degraded_task._run_chat_heartbeat_pass())
        self.assertEqual(degraded_kernel.selection_args[0], ["group:active"])
        self.assertEqual(degraded_task.describe_status()["persistent_due_scan_degraded_reason"], "RuntimeError")

    def test_handle_chat_heartbeat_marks_observe_only_dispatch_mode(self):
        gateway = SimpleNamespace(
            config=SimpleNamespace(
                life=SimpleNamespace(
                    dream_interval_min=1,
                    dream_time_ranges=[],
                    silence_threshold=10,
                    wakeup_min_energy=20,
                    wakeup_cost=5,
                    wakeup_cooldown=60,
                    dream_visible=False,
                ),
                persona=SimpleNamespace(persona_id="global", name="Mai"),
                evolution=SimpleNamespace(enable_expression_mining=False, enable_relationship_engine=False),
            ),
        )

        async def _call_proactive_task(**kwargs):
            return "ok"

        gateway.call_proactive_task = _call_proactive_task
        task = self.mod.ProactiveTask(
            context=SimpleNamespace(send_message=None),
            state_engine=SimpleNamespace(
                get_active_states=lambda: [],
                get_active_profiles=lambda: [],
                apply_natural_decay=lambda state: None,
            ),
            gateway=gateway,
            persistence=SimpleNamespace(load_persona_cache=lambda: {}),
            memory_engine=SimpleNamespace(add_memory=None),
            reflector=None,
            config=gateway.config,
        )

        decision = SimpleNamespace(action="NOOP", reason="no_signal_ready", metadata={})
        snapshot = SimpleNamespace(executor_pending=0, wait_targets=[])

        result = asyncio.run(task.handle_chat_heartbeat("chat-1", snapshot, decision))

        self.assertEqual(decision.metadata["dispatch_mode"], "observe_only")
        self.assertEqual(result["dispatch_mode"], "observe_only")

    def test_bind_chat_loop_kernel_registers_signal_sources_and_bridges(self):
        kernel_mod = importlib.import_module("astrmai.conversation.loop.chat_loop_kernel")

        gateway = SimpleNamespace(
            config=SimpleNamespace(
                life=SimpleNamespace(
                    dream_interval_min=1,
                    dream_time_ranges=[],
                    silence_threshold=10,
                    wakeup_min_energy=20,
                    wakeup_cost=5,
                    wakeup_cooldown=60,
                    dream_visible=False,
                ),
                persona=SimpleNamespace(persona_id="global", name="Mai"),
                evolution=SimpleNamespace(enable_expression_mining=False, enable_relationship_engine=False),
            ),
        )

        async def _call_proactive_task(**kwargs):
            return "ok"

        gateway.call_proactive_task = _call_proactive_task
        state_engine = SimpleNamespace(
            get_active_states=lambda: [],
            get_active_profiles=lambda: [],
            apply_natural_decay=lambda state: None,
            context_compaction=SimpleNamespace(),
        )
        task = self.mod.ProactiveTask(
            context=SimpleNamespace(send_message=None),
            state_engine=state_engine,
            gateway=gateway,
            persistence=SimpleNamespace(load_persona_cache=lambda: {}),
            memory_engine=SimpleNamespace(add_memory=None),
            reflector=None,
            config=gateway.config,
            runtime_coordinator=SimpleNamespace(),
        )
        kernel = kernel_mod.ChatLoopKernel(runtime_coordinator=task.runtime_coordinator)

        task.bind_chat_loop_kernel(kernel)
        status = kernel.describe_status_sync()

        self.assertTrue(status["dispatch_bridges"]["PROACTIVE_WAKEUP"])
        self.assertTrue(status["dispatch_bridges"]["HEARTFLOW_EVALUATE"])
        self.assertTrue(status["dispatch_bridges"]["DREAM_MAINTENANCE"])
        self.assertTrue(status["dispatch_bridges"]["MEMORY_MAINTENANCE"])
        self.assertTrue(status["dispatch_bridges"]["COMPACTION_EVALUATE"])

    def test_bridge_handlers_are_kernel_mediated(self):
        gateway = SimpleNamespace(
            config=SimpleNamespace(
                life=SimpleNamespace(
                    dream_interval_min=1,
                    dream_time_ranges=[],
                    silence_threshold=10,
                    wakeup_min_energy=20,
                    wakeup_cost=5,
                    wakeup_cooldown=60,
                    dream_visible=False,
                ),
                persona=SimpleNamespace(persona_id="global", name="Mai"),
                evolution=SimpleNamespace(enable_expression_mining=False, enable_relationship_engine=False),
            ),
        )

        async def _call_proactive_task(**kwargs):
            return "ok"

        gateway.call_proactive_task = _call_proactive_task
        wakeup_calls = []
        heartflow_calls = []
        task = self.mod.ProactiveTask(
            context=SimpleNamespace(send_message=None),
            state_engine=SimpleNamespace(
                get_active_states=lambda: [],
                get_active_profiles=lambda: [],
                apply_natural_decay=lambda state: None,
                context_compaction=SimpleNamespace(maybe_compact=lambda chat_id: asyncio.sleep(0, result={"chat_id": chat_id, "performed": True})),
            ),
            gateway=gateway,
            persistence=SimpleNamespace(load_persona_cache=lambda: {}),
            memory_engine=SimpleNamespace(add_memory=None),
            reflector=None,
            config=gateway.config,
        )
        task.wakeup_service = SimpleNamespace(run_for_chat=lambda chat_id: asyncio.sleep(0, result={"chat_id": chat_id, "performed": True}))
        task.heartflow_manager = SimpleNamespace(tick_chat=lambda chat_id, snapshot=None: asyncio.sleep(0, result={"chat_id": chat_id, "performed": True}))
        task.memory_engine = SimpleNamespace(
            memory_pipeline=SimpleNamespace(
                run_maintenance_for_session=lambda chat_id: asyncio.sleep(0, result={"chat_id": chat_id, "performed": True, "reason": "summarized"})
            )
        )
        task.dream_scheduler = SimpleNamespace(run_once_for_session=lambda chat_id: asyncio.sleep(0, result={"chat_id": chat_id, "performed": True}))
        snapshot = SimpleNamespace(latest_activity={}, executor_pending=0, wait_targets=[])

        async def _run():
            wake = await task.handle_wakeup_signal("chat-1", snapshot, SimpleNamespace(action="PROACTIVE_WAKEUP", reason="wakeup_signal", metadata={}))
            heart = await task.handle_heartflow_signal("chat-1", snapshot, SimpleNamespace(action="HEARTFLOW_EVALUATE", reason="heartflow_signal", metadata={}))
            memory = await task.handle_memory_signal("chat-1", snapshot, SimpleNamespace(action="MEMORY_MAINTENANCE", reason="memory_signal", metadata={}))
            dream = await task.handle_dream_signal("chat-1", snapshot, SimpleNamespace(action="DREAM_MAINTENANCE", reason="dream_signal", metadata={}))
            comp = await task.handle_compaction_signal("chat-1", snapshot, SimpleNamespace(action="COMPACTION_EVALUATE", reason="compaction_signal", metadata={}))
            return wake, heart, memory, dream, comp

        wake, heart, memory, dream, comp = asyncio.run(_run())

        self.assertEqual(wake["dispatch_mode"], "kernel_mediated")
        self.assertEqual(heart["dispatch_mode"], "kernel_mediated")
        self.assertEqual(memory["dispatch_mode"], "kernel_mediated")
        self.assertEqual(dream["dispatch_mode"], "kernel_mediated")
        self.assertEqual(comp["dispatch_mode"], "kernel_mediated")
        self.assertEqual(memory["bridge"], "MEMORY_MAINTENANCE")
        self.assertTrue(memory["result"]["performed"])
        self.assertEqual(dream["result"]["throttle_scope"], "global")

    def test_heartflow_cooldown_requires_visible_dispatch(self):
        gateway = SimpleNamespace(
            config=SimpleNamespace(
                life=SimpleNamespace(
                    dream_interval_min=1,
                    dream_time_ranges=[],
                    silence_threshold=10,
                    wakeup_min_energy=20,
                    wakeup_cost=5,
                    wakeup_cooldown=60,
                    dream_visible=False,
                ),
                persona=SimpleNamespace(persona_id="global", name="Mai"),
                evolution=SimpleNamespace(enable_expression_mining=False, enable_relationship_engine=False),
            ),
        )

        async def _call_proactive_task(**kwargs):
            return "ok"

        gateway.call_proactive_task = _call_proactive_task
        cooldown_calls = []
        task = self.mod.ProactiveTask(
            context=SimpleNamespace(send_message=None),
            state_engine=SimpleNamespace(
                get_active_states=lambda: [],
                get_active_profiles=lambda: [],
                apply_natural_decay=lambda state: None,
            ),
            gateway=gateway,
            persistence=SimpleNamespace(load_persona_cache=lambda: {}),
            memory_engine=SimpleNamespace(add_memory=None),
            reflector=None,
            config=gateway.config,
        )
        task.chat_loop_kernel = SimpleNamespace(
            set_cooldown=lambda chat_id, action, until_ts, reason="": asyncio.sleep(
                0,
                result=cooldown_calls.append((chat_id, action, reason)),
            )
        )
        snapshot = SimpleNamespace(latest_activity={}, executor_pending=0, wait_targets=[])

        async def _run():
            task.heartflow_manager = SimpleNamespace(
                VISIBLE_CANDIDATE_COOLDOWN_SECONDS=900,
                tick_chat=lambda chat_id, snapshot=None: asyncio.sleep(
                    0,
                    result={
                        "chat_id": chat_id,
                        "performed": True,
                        "synthetic_event_queued": False,
                        "visible_dispatch_performed": False,
                    },
                ),
            )
            hidden = await task.handle_heartflow_signal(
                "chat-1",
                snapshot,
                SimpleNamespace(action="HEARTFLOW_EVALUATE", reason="heartflow_signal", metadata={}),
            )
            task.heartflow_manager = SimpleNamespace(
                VISIBLE_CANDIDATE_COOLDOWN_SECONDS=900,
                tick_chat=lambda chat_id, snapshot=None: asyncio.sleep(
                    0,
                    result={
                        "chat_id": chat_id,
                        "performed": True,
                        "synthetic_event_queued": True,
                        "visible_dispatch_performed": True,
                    },
                ),
            )
            visible = await task.handle_heartflow_signal(
                "chat-1",
                snapshot,
                SimpleNamespace(action="HEARTFLOW_EVALUATE", reason="heartflow_signal", metadata={}),
            )
            return hidden, visible

        hidden, visible = asyncio.run(_run())

        self.assertEqual(hidden["cooldown_until"], 0.0)
        self.assertFalse(hidden["visible_dispatch_performed"])
        self.assertGreater(visible["cooldown_until"], 0.0)
        self.assertTrue(visible["visible_dispatch_performed"])
        self.assertEqual(cooldown_calls, [("chat-1", "heartflow", "heartflow_dispatch")])

    def test_heartflow_preview_is_readonly(self):
        manager_mod = importlib.import_module("astrmai.proactive.heartflow.manager")
        models_mod = importlib.import_module("astrmai.proactive.heartflow.models")

        manager = manager_mod.HeartflowManager(
            runtime_coordinator=None,
            state_engine=None,
            memory_engine=None,
            semaphore=None,
            dispatcher=None,
            config=SimpleNamespace(),
        )
        manager._sessions["chat-1"] = models_mod.HeartflowSessionState(
            chat_id="chat-1",
            started_at=10.0,
            last_activity_ts=100.0,
            last_tick_ts=120.0,
            expires_at=400.0,
            tick_count=3,
            topic_heat=0.4,
            talk_frequency_adjust=1.2,
        )
        manager._pulses_by_chat["chat-1"] = []
        manager._impulse_decisions_by_chat["chat-1"] = []
        manager._action_decisions_by_chat["chat-1"] = []
        snapshot = {
            "latest_activity_ts": 100.0,
            "recent_activity_count": 2,
            "recent_activity_count_60s": 1,
            "latest_activity_preview": "hello",
        }
        before_session = dataclasses.asdict(manager._sessions["chat-1"])
        before_pulses = list(manager._pulses_by_chat["chat-1"])
        before_impulses = list(manager._impulse_decisions_by_chat["chat-1"])
        before_actions = list(manager._action_decisions_by_chat["chat-1"])

        asyncio.run(manager.preview_chat("chat-1", snapshot=snapshot, now=180.0))

        after_session = dataclasses.asdict(manager._sessions["chat-1"])
        self.assertEqual(after_session, before_session)
        self.assertEqual(manager._pulses_by_chat["chat-1"], before_pulses)
        self.assertEqual(manager._impulse_decisions_by_chat["chat-1"], before_impulses)
        self.assertEqual(manager._action_decisions_by_chat["chat-1"], before_actions)

    def test_dream_scheduler_reports_global_throttle(self):
        dream_mod = importlib.import_module("astrmai.proactive.dream_scheduler")
        scheduler = dream_mod.DreamScheduler(
            context=SimpleNamespace(send_message=None),
            memory_engine=None,
            config=SimpleNamespace(life=SimpleNamespace(dream_interval_min=1, dream_time_ranges=[])),
            semaphore=asyncio.Semaphore(1),
            dream_visible=False,
        )
        scheduler.dream_agent = object()
        scheduler.dream_generator = object()
        scheduler._last_dream_time = time.time()

        eligibility = scheduler.describe_session_eligibility("chat-2", time.time())
        result = asyncio.run(scheduler.run_once_for_session("chat-2"))

        self.assertFalse(eligibility["eligible"])
        self.assertEqual(eligibility["reason"], "dream_global_cooldown")
        self.assertEqual(eligibility["throttle_scope"], "global")
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], "dream_global_cooldown")
        self.assertEqual(result["throttle_scope"], "global")

    def test_dream_scheduler_backs_off_sessions_without_enough_events(self):
        dream_mod = importlib.import_module("astrmai.proactive.dream_scheduler")

        class _DreamAgent:
            MIN_EVENTS_TO_DREAM = 5

            def __init__(self):
                self.count_calls = 0

            async def count_session_events(self, _session_id):
                self.count_calls += 1
                return 2

            async def run_dream_cycle(self, **_kwargs):
                raise AssertionError("dream cycle should not start below the event threshold")

        agent = _DreamAgent()
        scheduler = dream_mod.DreamScheduler(
            context=SimpleNamespace(send_message=None),
            memory_engine=None,
            config=SimpleNamespace(
                life=SimpleNamespace(
                    dream_interval_min=30,
                    dream_time_ranges=[],
                    min_memory_events_to_dream=5,
                )
            ),
            semaphore=asyncio.Semaphore(1),
        )
        scheduler.bind_dependencies(agent, object())

        first = asyncio.run(scheduler.run_once_for_session("chat-small"))
        second = asyncio.run(scheduler.run_once_for_session("chat-small"))

        self.assertEqual(first["reason"], "insufficient_memory_events")
        self.assertEqual(first["throttle_scope"], "session")
        self.assertEqual(second["reason"], "dream_session_backoff")
        self.assertEqual(agent.count_calls, 1)

    def test_dream_scheduler_backs_off_after_empty_dream_result(self):
        dream_mod = importlib.import_module("astrmai.proactive.dream_scheduler")

        class _DreamAgent:
            MIN_EVENTS_TO_DREAM = 5

            def __init__(self):
                self.run_calls = 0

            async def count_session_events(self, _session_id):
                return 5

            async def run_dream_cycle(self, **_kwargs):
                self.run_calls += 1
                return None

        agent = _DreamAgent()
        scheduler = dream_mod.DreamScheduler(
            context=SimpleNamespace(send_message=None),
            memory_engine=None,
            config=SimpleNamespace(
                life=SimpleNamespace(
                    dream_interval_min=30,
                    dream_time_ranges=[],
                    min_memory_events_to_dream=5,
                )
            ),
            semaphore=asyncio.Semaphore(1),
        )
        scheduler.bind_dependencies(agent, object())

        first = asyncio.run(scheduler.run_once_for_session("chat-empty"))
        second = asyncio.run(scheduler.run_once_for_session("chat-empty"))

        self.assertEqual(first["reason"], "no_dream_log")
        self.assertEqual(second["reason"], "dream_session_backoff")
        self.assertEqual(agent.run_calls, 1)

    def test_dream_scheduler_restores_cooldowns_after_reload(self):
        dream_mod = importlib.import_module("astrmai.proactive.dream_scheduler")
        cache_dir = Path(self.temp_dir.name) / "cache"
        db_service = SimpleNamespace(persistence=SimpleNamespace(cache_dir=cache_dir))
        config = SimpleNamespace(life=SimpleNamespace(dream_interval_min=30, dream_time_ranges=[]))

        first = dream_mod.DreamScheduler(
            context=SimpleNamespace(send_message=None),
            memory_engine=None,
            config=config,
            semaphore=asyncio.Semaphore(1),
        )
        first.bind_dependencies(object(), object(), db_service=db_service)
        first._last_dream_time = time.time()
        first._last_attempt_by_session["chat-1"] = time.time()
        asyncio.run(first._persist_runtime_state())

        restored = dream_mod.DreamScheduler(
            context=SimpleNamespace(send_message=None),
            memory_engine=None,
            config=config,
            semaphore=asyncio.Semaphore(1),
        )
        restored.bind_dependencies(object(), object(), db_service=db_service)

        self.assertGreater(restored._last_dream_time, 0)
        self.assertIn("chat-1", restored._last_attempt_by_session)
        self.assertEqual(
            restored.describe_session_eligibility("chat-2", time.time())["reason"],
            "dream_global_cooldown",
        )

    def test_diary_jitter_cancellation_does_not_commit_daily_marker(self):
        task = self.mod.ProactiveTask.__new__(self.mod.ProactiveTask)
        task._last_diary_date = ""
        task._diary_pending_date = "2026-07-14"
        task.state_engine = SimpleNamespace(get_active_states=lambda: [])
        task.diary_service = SimpleNamespace(
            run_once=lambda *_args, **_kwargs: asyncio.sleep(0, result={"failed": 0})
        )
        original_sleep = self.mod.asyncio.sleep

        async def _cancel(_delay, **_kwargs):
            raise asyncio.CancelledError

        self.mod.asyncio.sleep = _cancel
        try:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(task._run_daily_diary_task_with_jitter("2026-07-14"))
        finally:
            self.mod.asyncio.sleep = original_sleep

        self.assertEqual(task._last_diary_date, "")
        self.assertEqual(task._diary_pending_date, "")

    def test_group_profile_target_prefers_top_non_self_speaker(self):
        task = self.mod.ProactiveTask.__new__(self.mod.ProactiveTask)
        task._db_service = SimpleNamespace(
            get_recent_message_logs_async=lambda chat_id, limit=80, max_age_seconds=3600, include_processed=True: asyncio.sleep(
                0,
                result=[
                    SimpleNamespace(sender_id="user-1", sender_name="Alice", content="hello"),
                    SimpleNamespace(sender_id="user-2", sender_name="Bob", content="yo"),
                    SimpleNamespace(sender_id="user-1", sender_name="Alice", content="still here"),
                    SimpleNamespace(sender_id="bot-1", sender_name="SELF", content="reply"),
                ],
            )
        )
        task.state_engine = SimpleNamespace(bot_id="bot-1")

        result = asyncio.run(task._select_group_profile_target("group-1"))

        self.assertEqual(result, ("user-1", "Alice", 2))

    def test_proactive_task_no_longer_imports_legacy_proactive_helper(self):
        path = Path(__file__).resolve().parents[1] / "astrmai" / "proactive" / "proactive_task.py"
        content = path.read_text(encoding="utf-8")
        self.assertNotIn("LegacyProactiveTask", content)

    def test_scheduler_loop_no_longer_directly_runs_chat_services(self):
        path = Path(__file__).resolve().parents[1] / "astrmai" / "proactive" / "proactive_task.py"
        content = path.read_text(encoding="utf-8")
        self.assertNotIn("await self.wakeup_service.run_once()", content)
        self.assertNotIn("await self.heartflow_manager.tick()", content)
        self.assertNotIn("self._fire_background_task(self.dream_scheduler.run_once())", content)

    def test_wakeup_routes_intent_through_dispatcher_before_energy_cost(self):
        wakeup_mod = importlib.import_module("astrmai.proactive.wakeup_service")
        now = time.time()
        state = SimpleNamespace(
            chat_id="group:10001",
            last_reply_time=now - 1200,
            energy=80,
            next_wakeup_timestamp=0,
        )

        class _StateEngine:
            def __init__(self):
                self.energy_calls = []

            def get_active_states(self):
                return [state]

            async def consume_energy(self, chat_id, amount=None):
                self.energy_calls.append((chat_id, amount))

        class _Dispatcher:
            def __init__(self):
                self.intents = []
                self.callback = None

            async def dispatch(self, intent, *, on_complete=None):
                self.intents.append(intent)
                self.callback = on_complete
                return SimpleNamespace(allowed=True, blocked_reason="")

        state_engine = _StateEngine()
        dispatcher = _Dispatcher()
        context = SimpleNamespace(send_message=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("direct send forbidden")))
        config = SimpleNamespace(
            life=SimpleNamespace(
                silence_threshold=10,
                wakeup_min_energy=20,
                wakeup_cost=5,
                wakeup_cooldown=60,
            ),
            persona=SimpleNamespace(persona_id="global"),
        )
        service = wakeup_mod.WakeupService(
            context=context,
            state_engine=state_engine,
            persistence=SimpleNamespace(load_persona_cache=lambda: {}, save_chat_state=lambda chat_id, state: asyncio.sleep(0)),
            call_background_lane=lambda *args, **kwargs: None,
            config=config,
            dispatcher=dispatcher,
        )

        asyncio.run(service.run_once())

        self.assertEqual(len(dispatcher.intents), 1)
        self.assertEqual(dispatcher.intents[0].source, "wakeup")
        self.assertEqual(dispatcher.intents[0].suggested_action_tier, "chat")
        self.assertEqual(state_engine.energy_calls, [])

        asyncio.run(dispatcher.callback(True, "hello"))

        self.assertEqual(state_engine.energy_calls, [("group:10001", 5)])
        self.assertGreater(state.next_wakeup_timestamp, now)

    def test_dispatcher_blocks_wakeup_and_heartflow_during_quiet_hours(self):
        dispatcher_mod = importlib.import_module("astrmai.proactive.dispatcher")
        quiet_ts = time.mktime((2026, 5, 11, 23, 45, 0, 0, 0, -1))
        original_time = dispatcher_mod.time.time

        class _AttentionGate:
            async def inject_external_event(self, chat_id, event_data):
                raise AssertionError("quiet hours should not inject proactive events")

        class _StateEngine:
            async def get_state(self, chat_id):
                return SimpleNamespace(energy=0.9)

        dispatcher = dispatcher_mod.ProactiveDispatcher(
            attention_gate=_AttentionGate(),
            state_engine=_StateEngine(),
            config=SimpleNamespace(
                life=SimpleNamespace(proactive_quiet_hours=["23:30-07:30"], wakeup_min_energy=0.6),
                reply=SimpleNamespace(base_frequency=0.7),
                persona=SimpleNamespace(name="Mai"),
            ),
        )

        async def _run():
            dispatcher_mod.time.time = lambda: quiet_ts
            try:
                results = []
                for source in ("wakeup", "heartflow"):
                    results.append(
                        await dispatcher.dispatch(
                            dispatcher_mod.ProactiveMessageIntent(
                                chat_id="group:10001",
                                source=source,
                                reason="quiet test",
                                guidance="say one short line",
                                metadata={"group_id": "group:10001", "talk_willingness": 0.8},
                            )
                        )
                    )
                return results
            finally:
                dispatcher_mod.time.time = original_time

        decisions = asyncio.run(_run())

        self.assertEqual([item.blocked_reason for item in decisions], ["quiet_hours", "quiet_hours"])
        self.assertTrue(all(item.safety_checks["quiet_hours"] for item in decisions))

    def test_dispatcher_injects_proactive_events_through_kernel_bound_gate(self):
        install_attention_stubs()
        sys.modules.pop("astrmai.conversation.attention.gate", None)
        gate_mod = importlib.import_module("astrmai.conversation.attention.gate")
        gate_mod = importlib.reload(gate_mod)
        calls = []

        class _Kernel:
            async def tick(self, *, chat_id, trigger, event=None):
                calls.append(
                    (
                        chat_id,
                        trigger,
                        event.get_extra("astrmai_loop_source"),
                        event.get_extra("astrmai_is_proactive_event"),
                        event.get_extra("astrmai_proactive_candidate"),
                        event.get_extra("astrmai_force_engage", False),
                        event.message_str,
                    )
                )
                return SimpleNamespace(dispatch_result="BUFFERED")

        attention_gate = gate_mod.AttentionGate(
            state_engine=SimpleNamespace(
                config=SimpleNamespace(
                    attention=SimpleNamespace(),
                    system1=SimpleNamespace(wakeup_words=[], nicknames=["AstrMai"]),
                    global_settings=SimpleNamespace(debug_mode=False),
                )
            ),
            judge=SimpleNamespace(),
            sensors=SimpleNamespace(),
            system2_callback=None,
        )
        attention_gate.bind_chat_loop_kernel(_Kernel())
        dispatcher_mod = importlib.import_module("astrmai.proactive.dispatcher")
        original_eval = dispatcher_mod.evaluate_proactive_rhythm
        dispatcher_mod.evaluate_proactive_rhythm = lambda config, now=None: SimpleNamespace(
            quiet_hours=False,
            time_bucket="day",
            quiet_ranges=[],
            base_frequency=0.7,
            base_frequency_factor=1.0,
        )
        dispatcher = dispatcher_mod.ProactiveDispatcher(
            attention_gate=attention_gate,
            state_engine=SimpleNamespace(get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=0.9))),
            runtime_coordinator=SimpleNamespace(get_activity_snapshot=lambda chat_id: asyncio.sleep(0, result={"latest_activity_ts": time.time(), "wait_targets": [], "executor_pending": 0})),
            config=SimpleNamespace(
                life=SimpleNamespace(proactive_quiet_hours=[], wakeup_min_energy=0.6),
                reply=SimpleNamespace(base_frequency=0.7),
                persona=SimpleNamespace(name="Mai"),
            ),
        )

        async def _run():
            return await dispatcher.dispatch(
                dispatcher_mod.ProactiveMessageIntent(
                    chat_id="group:10001",
                    source="wakeup",
                    reason="kernel route",
                    guidance="say one short line",
                    metadata={"group_id": "group:10001"},
                )
            )

        try:
            decision = asyncio.run(_run())
        finally:
            dispatcher_mod.evaluate_proactive_rhythm = original_eval

        self.assertTrue(decision.allowed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], ("group:10001", "external", "proactive_dispatcher", True))
        self.assertTrue(calls[0][4])
        self.assertFalse(calls[0][5])
        self.assertIn("主动开口候选", calls[0][6])

    def test_dispatcher_blocks_wakeup_after_other_bot_command_noise(self):
        dispatcher_mod = importlib.import_module("astrmai.proactive.dispatcher")

        class _AttentionGate:
            async def inject_external_event(self, chat_id, event_data):
                raise AssertionError("noisy other-bot command must not inject proactive event")

        dispatcher = dispatcher_mod.ProactiveDispatcher(
            attention_gate=_AttentionGate(),
            runtime_coordinator=SimpleNamespace(
                get_activity_snapshot=lambda chat_id: asyncio.sleep(
                    0,
                    result={
                        "latest_activity_ts": time.time(),
                        "latest_activity_preview": "@丛雨丸(3889060937) /抽卡",
                        "recent_activity_count_60s": 1,
                        "wait_targets": [],
                        "executor_pending": 0,
                    },
                )
            ),
            state_engine=SimpleNamespace(
                bot_id="2715245266",
                get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=1.0)),
            ),
            config=SimpleNamespace(
                life=SimpleNamespace(proactive_quiet_hours=[], wakeup_min_energy=0.6),
                reply=SimpleNamespace(base_frequency=0.7),
                persona=SimpleNamespace(name="Mai"),
            ),
        )

        decision = asyncio.run(
            dispatcher.dispatch(
                dispatcher_mod.ProactiveMessageIntent(
                    chat_id="group:10001",
                    source="wakeup",
                    reason="silence_threshold_reached",
                    guidance="say one short line",
                    metadata={"group_id": "group:10001"},
                )
            )
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.blocked_reason, "recent_other_bot_command")
        self.assertEqual(decision.safety_checks["proactive_noise_block"], "recent_other_bot_command")

    def test_wakeup_quiet_hours_block_does_not_consume_energy_or_cooldown(self):
        wakeup_mod = importlib.import_module("astrmai.proactive.wakeup_service")
        now = time.time()
        state = SimpleNamespace(
            chat_id="group:10001",
            last_reply_time=now - 1200,
            energy=80,
            next_wakeup_timestamp=0,
        )

        class _StateEngine:
            def __init__(self):
                self.energy_calls = []

            def get_active_states(self):
                return [state]

            async def consume_energy(self, chat_id, amount=None):
                self.energy_calls.append((chat_id, amount))

        class _Dispatcher:
            async def dispatch(self, intent, *, on_complete=None):
                return SimpleNamespace(allowed=False, blocked_reason="quiet_hours")

        state_engine = _StateEngine()
        service = wakeup_mod.WakeupService(
            context=SimpleNamespace(send_message=None),
            state_engine=state_engine,
            persistence=SimpleNamespace(load_persona_cache=lambda: {}),
            call_background_lane=lambda *args, **kwargs: None,
            config=SimpleNamespace(
                life=SimpleNamespace(
                    silence_threshold=10,
                    wakeup_min_energy=20,
                    wakeup_cost=5,
                    wakeup_cooldown=60,
                    proactive_quiet_hours=["23:30-07:30"],
                ),
                persona=SimpleNamespace(persona_id="global"),
                reply=SimpleNamespace(base_frequency=0.7),
            ),
            dispatcher=_Dispatcher(),
        )

        asyncio.run(service.run_once())

        self.assertEqual(state_engine.energy_calls, [])
        self.assertEqual(state.next_wakeup_timestamp, 0)

    def test_wakeup_guidance_is_human_low_pressure(self):
        wakeup_mod = importlib.import_module("astrmai.proactive.wakeup_service")
        service = wakeup_mod.WakeupService(
            context=SimpleNamespace(send_message=None),
            state_engine=SimpleNamespace(get_active_states=lambda: []),
            persistence=SimpleNamespace(load_persona_cache=lambda: {"global": {"summary": "soft and concise"}}),
            call_background_lane=lambda *args, **kwargs: None,
            config=SimpleNamespace(
                life=SimpleNamespace(proactive_quiet_hours=["23:30-07:30"]),
                persona=SimpleNamespace(persona_id="global"),
                reply=SimpleNamespace(base_frequency=0.7),
            ),
            dispatcher=None,
        )

        guidance = asyncio.run(service.generate_opening_line("group:10001"))

        self.assertIn("one short natural line", guidance)
        self.assertNotIn("threshold", guidance.lower())
        self.assertNotIn("system", guidance.lower())
        self.assertNotIn("anyone here", guidance.lower())

    def test_wakeup_build_signal_uses_life_defaults_when_config_missing(self):
        wakeup_mod = importlib.import_module("astrmai.proactive.wakeup_service")
        config_mod = importlib.import_module("config")
        defaults = config_mod.LifeConfig()
        now = time.time()
        state = SimpleNamespace(
            chat_id="group:10001",
            last_reply_time=now - (defaults.silence_threshold + 5) * 60,
            energy=defaults.wakeup_min_energy + 0.2,
            next_wakeup_timestamp=0,
        )
        service = wakeup_mod.WakeupService(
            context=SimpleNamespace(send_message=None),
            state_engine=SimpleNamespace(get_state=lambda chat_id: state),
            persistence=SimpleNamespace(load_persona_cache=lambda: {}),
            call_background_lane=lambda *args, **kwargs: None,
            config=None,
            dispatcher=None,
        )

        signal = asyncio.run(service.build_signal("group:10001", now=now))

        self.assertTrue(signal["eligible"])
        self.assertEqual(signal["reason"], "silence_threshold_reached")
        self.assertEqual(signal["wakeup_cost"], float(defaults.wakeup_cost))
        self.assertEqual(signal["wakeup_cooldown"], float(defaults.wakeup_cooldown))

    def test_wakeup_run_for_chat_falls_back_when_life_config_missing(self):
        wakeup_mod = importlib.import_module("astrmai.proactive.wakeup_service")
        config_mod = importlib.import_module("config")
        defaults = config_mod.LifeConfig()
        now = time.time()
        state = SimpleNamespace(
            chat_id="group:10001",
            last_reply_time=now - (defaults.silence_threshold + 5) * 60,
            energy=defaults.wakeup_min_energy + 0.2,
            next_wakeup_timestamp=0,
        )

        class _StateEngine:
            def __init__(self):
                self.energy_calls = []

            def get_active_states(self):
                return [state]

            async def consume_energy(self, chat_id, amount=None):
                self.energy_calls.append((chat_id, amount))

        class _Dispatcher:
            def __init__(self):
                self.intents = []
                self.callback = None

            async def dispatch(self, intent, *, on_complete=None):
                self.intents.append(intent)
                self.callback = on_complete
                return SimpleNamespace(allowed=True, blocked_reason="")

        state_engine = _StateEngine()
        dispatcher = _Dispatcher()
        service = wakeup_mod.WakeupService(
            context=SimpleNamespace(send_message=None),
            state_engine=state_engine,
            persistence=SimpleNamespace(load_persona_cache=lambda: {}, save_chat_state=lambda chat_id, target_state: asyncio.sleep(0)),
            call_background_lane=lambda *args, **kwargs: None,
            config=SimpleNamespace(
                persona=SimpleNamespace(persona_id="global"),
                reply=SimpleNamespace(base_frequency=0.7),
            ),
            dispatcher=dispatcher,
        )

        asyncio.run(service.run_once())

        self.assertEqual(len(dispatcher.intents), 1)
        self.assertEqual(dispatcher.intents[0].cost, float(defaults.wakeup_cost))
        self.assertEqual(dispatcher.intents[0].cooldown, float(defaults.wakeup_cooldown))

        asyncio.run(dispatcher.callback(True, "hello"))

        self.assertEqual(state_engine.energy_calls, [("group:10001", defaults.wakeup_cost)])
        self.assertGreater(state.next_wakeup_timestamp, now)

    def test_wakeup_partial_life_config_merges_missing_defaults(self):
        wakeup_mod = importlib.import_module("astrmai.proactive.wakeup_service")
        config_mod = importlib.import_module("config")
        defaults = config_mod.LifeConfig()
        now = time.time()
        state = SimpleNamespace(
            chat_id="group:10001",
            last_reply_time=now - 15 * 60,
            energy=defaults.wakeup_min_energy + 0.2,
            next_wakeup_timestamp=0,
        )
        service = wakeup_mod.WakeupService(
            context=SimpleNamespace(send_message=None),
            state_engine=SimpleNamespace(get_state=lambda chat_id: state),
            persistence=SimpleNamespace(load_persona_cache=lambda: {}),
            call_background_lane=lambda *args, **kwargs: None,
            config=SimpleNamespace(
                life=SimpleNamespace(silence_threshold=10),
                persona=SimpleNamespace(persona_id="global"),
                reply=SimpleNamespace(base_frequency=0.7),
            ),
            dispatcher=None,
        )

        signal = asyncio.run(service.build_signal("group:10001", now=now))

        self.assertTrue(signal["eligible"])
        self.assertEqual(signal["wakeup_cost"], float(defaults.wakeup_cost))
        self.assertEqual(signal["wakeup_cooldown"], float(defaults.wakeup_cooldown))

    def test_wakeup_run_for_chat_uses_safe_life_fallback_when_signal_omits_cost(self):
        wakeup_mod = importlib.import_module("astrmai.proactive.wakeup_service")
        config_mod = importlib.import_module("config")
        defaults = config_mod.LifeConfig()
        state = SimpleNamespace(
            chat_id="group:10001",
            last_reply_time=time.time() - 1200,
            energy=80,
            next_wakeup_timestamp=0,
        )

        class _StateEngine:
            def __init__(self):
                self.energy_calls = []

            async def consume_energy(self, chat_id, amount=None):
                self.energy_calls.append((chat_id, amount))

        class _Dispatcher:
            def __init__(self):
                self.intents = []
                self.callback = None

            async def dispatch(self, intent, *, on_complete=None):
                self.intents.append(intent)
                self.callback = on_complete
                return SimpleNamespace(allowed=True, blocked_reason="")

        state_engine = _StateEngine()
        dispatcher = _Dispatcher()
        service = wakeup_mod.WakeupService(
            context=SimpleNamespace(send_message=None),
            state_engine=state_engine,
            persistence=SimpleNamespace(load_persona_cache=lambda: {}, save_chat_state=lambda chat_id, target_state: asyncio.sleep(0)),
            call_background_lane=lambda *args, **kwargs: None,
            config=SimpleNamespace(
                life=SimpleNamespace(silence_threshold=10, wakeup_min_energy=20, wakeup_cost=7),
                persona=SimpleNamespace(persona_id="global"),
                reply=SimpleNamespace(base_frequency=0.7),
            ),
            dispatcher=dispatcher,
        )

        result = asyncio.run(
            service.run_for_chat(
                "group:10001",
                signal={"eligible": True, "state": state, "reason": "silence_threshold_reached"},
            )
        )

        self.assertTrue(result["performed"])
        self.assertEqual(len(dispatcher.intents), 1)
        self.assertEqual(dispatcher.intents[0].cost, 7.0)
        self.assertEqual(dispatcher.intents[0].cooldown, float(defaults.wakeup_cooldown))

        asyncio.run(dispatcher.callback(True, "hello"))

        self.assertEqual(state_engine.energy_calls, [("group:10001", 7.0)])


    def test_dispatcher_history_reflects_queued_before_async_completion(self):
        dispatcher_mod = importlib.import_module("astrmai.proactive.dispatcher")
        stored_callback = []

        class _AttentionGate:
            async def inject_external_event(self, chat_id, event_data):
                stored_callback.append(event_data["extra"]["astrmai_proactive_completion_callback"])
                return "BUFFERED"

        dispatcher = dispatcher_mod.ProactiveDispatcher(
            attention_gate=_AttentionGate(),
            state_engine=SimpleNamespace(get_state=lambda chat_id: asyncio.sleep(0, result=SimpleNamespace(energy=0.9))),
            runtime_coordinator=SimpleNamespace(
                get_activity_snapshot=lambda chat_id: asyncio.sleep(
                    0,
                    result={"latest_activity_ts": time.time(), "wait_targets": [], "executor_pending": 0},
                )
            ),
            config=SimpleNamespace(
                life=SimpleNamespace(proactive_quiet_hours=[], wakeup_min_energy=0.0),
                reply=SimpleNamespace(base_frequency=0.7),
                persona=SimpleNamespace(name="Mai"),
            ),
        )

        decision = asyncio.run(
            dispatcher.dispatch(
                dispatcher_mod.ProactiveMessageIntent(
                    chat_id="group:10001",
                    source="wakeup",
                    reason="async completion",
                    guidance="say one short line",
                    metadata={"group_id": "group:10001"},
                )
            )
        )

        self.assertTrue(decision.synthetic_event_queued)
        self.assertFalse(decision.reply_sent)
        self.assertEqual(decision.status, "queued")

        history = dispatcher.list_intents(limit=1)[0]
        self.assertTrue(history["decision"]["synthetic_event_queued"])
        self.assertFalse(history["decision"]["reply_sent"])
        self.assertEqual(history["decision"]["status"], "queued")
        self.assertEqual(history["status"], "queued")

        callback = stored_callback[0]
        asyncio.run(callback(True, "hello world"))

        history_after = dispatcher.list_intents(limit=1)[0]
        self.assertTrue(history_after["decision"]["reply_sent"])
        self.assertEqual(history_after["decision"]["reply_preview"], "hello world")
        self.assertEqual(history_after["decision"]["status"], "sent")
        self.assertEqual(history_after["status"], "sent")


if __name__ == "__main__":
    unittest.main()
