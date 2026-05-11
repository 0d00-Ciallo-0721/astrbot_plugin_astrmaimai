import asyncio
import importlib
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


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
        self.assertIn("heartflow", status)
        self.assertFalse(status["heartflow"]["enabled"])

    def test_proactive_task_no_longer_imports_legacy_proactive_helper(self):
        path = Path(__file__).resolve().parents[1] / "astrmai" / "proactive" / "proactive_task.py"
        content = path.read_text(encoding="utf-8")
        self.assertNotIn("LegacyProactiveTask", content)

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
            persistence=SimpleNamespace(load_persona_cache=lambda: {}),
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


if __name__ == "__main__":
    unittest.main()
