import asyncio
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _PluginApi:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def get_proactive_task(self):
        return getattr(self, "proactive_task", None)

    def get_state_engine(self):
        return getattr(self, "state_engine", None)

    def get_runtime_config(self):
        return getattr(self, "runtime_config", SimpleNamespace(life=SimpleNamespace()))

    def has_bound_facade(self):
        return bool(getattr(self, "bound", False))

    def get_reflector(self):
        return getattr(self, "reflector", None)

    def get_auto_check_task(self):
        return getattr(self, "auto_check_task", None)

    def get_runtime_coordinator(self):
        return getattr(self, "coordinator", None)

    def get_heartflow_manager(self):
        return getattr(self, "heartflow_manager", None)

    def get_memory_engine(self):
        return getattr(self, "memory_engine", None)


class ChatRuntimeServiceGapCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_status_endpoints_degrade_without_bound_runtime(self):
        from astrmai.webui.backend.services.chatruntimeservice import ChatRuntimeService

        service = ChatRuntimeService(_PluginApi(bound=False))

        self.assertEqual(asyncio.run(service.proactive_status())["data"], {"running": False})
        self.assertFalse(asyncio.run(service.dream_status())["runtime_bound"])
        self.assertFalse(asyncio.run(service.diary_status())["data"]["available"])
        self.assertEqual(asyncio.run(service.run_dream_once())["status"], "error")
        self.assertEqual(asyncio.run(service.run_diary_once())["status"], "error")

    def test_chat_runtime_clear_removes_coordinator_and_heartflow_state(self):
        from astrmai.webui.backend.services.chatruntimeservice import ChatRuntimeService

        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "last": 10}

            async def get_wait_target_name(self, chat_id):
                return "Alice"

            async def clear_runtime_state(self, chat_id):
                self.cleared = chat_id
                return True

        class _Heartflow:
            def __init__(self):
                self._pulses_by_chat = {"chat-1": object()}
                self._impulse_decisions_by_chat = {"chat-1": object()}
                self.state = SimpleNamespace(cooldown_tags=["old"])

            def get_state(self, chat_id):
                return self.state

        coordinator = _Coordinator()
        heartflow = _Heartflow()
        service = ChatRuntimeService(_PluginApi(bound=True, coordinator=coordinator, heartflow_manager=heartflow))

        runtime = asyncio.run(service.chat_runtime("chat-1"))
        cleared = asyncio.run(service.clear_chat_runtime("chat-1"))

        self.assertEqual(runtime["data"]["wait_target_name"], "Alice")
        self.assertTrue(cleared["changed"])
        self.assertEqual(coordinator.cleared, "chat-1")
        self.assertEqual(heartflow._pulses_by_chat, {})
        self.assertEqual(heartflow.state.cooldown_tags, [])

    def test_memory_feedback_lists_filters_sources_and_disables(self):
        from astrmai.webui.backend.services.chatruntimeservice import ChatRuntimeService

        signal_a = SimpleNamespace(chat_id="chat-1", source="planner", timestamp=10.2, summary="s1", guidance="g1")
        signal_b = SimpleNamespace(chat_id="chat-2", source="judge", timestamp=11.9, summary="s2", guidance="g2")

        class _Engine:
            def __init__(self):
                self._cognitive_feedback_cache = {"chat-1": [signal_a], "chat-2": [signal_b]}
                self.disabled = None

            async def get_cognitive_feedback(self, chat_id, limit=30, sources=None):
                return [item for item in self._cognitive_feedback_cache.get(chat_id, []) if sources is None or item.source in sources]

            def disable_cognitive_feedback(self, signal):
                self.disabled = signal

        engine = _Engine()
        service = ChatRuntimeService(_PluginApi(memory_engine=engine))

        filtered = asyncio.run(service.list_memory_feedback(chat_id="chat-1", source="planner"))
        all_sources = asyncio.run(service.memory_feedback_sources())
        feedback_id = filtered["items"][0]["id"]
        disabled = asyncio.run(service.disable_memory_feedback(feedback_id))

        self.assertEqual(filtered["total"], 1)
        self.assertEqual({item["source"] for item in all_sources["items"]}, {"planner", "judge"})
        self.assertTrue(disabled["changed"])
        self.assertIs(engine.disabled, signal_a)

    def test_memory_feedback_prefers_persisted_canonical_records(self):
        from astrmai.webui.backend.services.chatruntimeservice import ChatRuntimeService

        class _Engine:
            def __init__(self):
                self.list_call = None
                self.disabled_id = ""

            async def list_cognitive_feedback_records(self, **kwargs):
                self.list_call = kwargs
                return {
                    "items": [{"id": "mem_feedback_1", "source": "planner", "persisted": True}],
                    "total": 192,
                    "limit": kwargs["limit"],
                    "offset": kwargs["offset"],
                }

            async def disable_cognitive_feedback_record(self, memory_id):
                self.disabled_id = memory_id
                return True

        engine = _Engine()
        service = ChatRuntimeService(_PluginApi(memory_engine=engine))

        result = asyncio.run(service.list_memory_feedback(source="planner", limit=20, offset=40))
        disabled = asyncio.run(service.disable_memory_feedback("mem_feedback_1"))

        self.assertEqual(result["total"], 192)
        self.assertEqual(engine.list_call["offset"], 40)
        self.assertEqual(engine.disabled_id, "mem_feedback_1")
        self.assertTrue(disabled["persisted"])


if __name__ == "__main__":
    unittest.main()
