import asyncio
import importlib
import sys
import tempfile
import types
import unittest

from tests.helpers import install_astrbot_stubs


class _FunctionTool:
    def __class_getitem__(cls, _item):
        return cls


class _FakeEvent:
    def __init__(self):
        self._extra = {}

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_sender_id(self):
        return "sender-1"

    def get_sender_name(self):
        return "Alice"


class _FakeWrapper:
    def __init__(self, event):
        self.context = types.SimpleNamespace(
            event=event,
            context=types.SimpleNamespace(),
        )


class MemoryV2ToolInjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        run_context_mod = types.ModuleType("astrbot.core.agent.run_context")
        run_context_mod.ContextWrapper = type("ContextWrapper", (), {"__class_getitem__": classmethod(lambda cls, item: cls)})
        tool_mod = types.ModuleType("astrbot.core.agent.tool")
        tool_mod.FunctionTool = _FunctionTool
        astr_ctx_mod = types.ModuleType("astrbot.core.astr_agent_context")
        astr_ctx_mod.AstrAgentContext = type("AstrAgentContext", (), {})
        sys.modules["astrbot.core.agent.run_context"] = run_context_mod
        sys.modules["astrbot.core.agent.tool"] = tool_mod
        sys.modules["astrbot.core.astr_agent_context"] = astr_ctx_mod
        sys.modules.pop("astrmai.conversation.planning.tools.pfc_tools", None)
        self.pfc_tools = importlib.import_module("astrmai.conversation.planning.tools.pfc_tools")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_omni_tool_uses_memory_tool_service_before_legacy_recall(self):
        class LegacyEngine:
            def __init__(self):
                self.recall_calls = []

            async def recall(self, *args, **kwargs):
                self.recall_calls.append((args, kwargs))
                return "legacy"

        class ToolService:
            def __init__(self):
                self.calls = []

            async def omni_query(self, **kwargs):
                self.calls.append(kwargs)
                return "v2 result"

        async def run():
            event = _FakeEvent()
            engine = LegacyEngine()
            service = ToolService()
            tool = self.pfc_tools.OmniPerceptionTool(
                memory_engine=engine,
                memory_tool_service=service,
                chat_id="chat-1",
                current_sender_id="sender-1",
                current_sender_name="Alice",
            )
            result = await tool.call(_FakeWrapper(event), query="blue notebook")
            self.assertEqual(result, "v2 result")
            self.assertEqual(engine.recall_calls, [])
            self.assertEqual(service.calls[0]["query"], "blue notebook")
            self.assertEqual(service.calls[0]["chat_id"], "chat-1")
            self.assertIs(service.calls[0]["event"], event)

        asyncio.run(run())

    def test_omni_tool_legacy_recall_uses_keyword_query_and_session(self):
        class LegacyEngine:
            def __init__(self):
                self.calls = []

            async def recall(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return "legacy memory"

        async def run():
            engine = LegacyEngine()
            tool = self.pfc_tools.OmniPerceptionTool(memory_engine=engine, chat_id="chat-1")
            result = await tool.call(_FakeWrapper(_FakeEvent()), query="notebook")
            self.assertIn("legacy memory", result)
            self.assertEqual(engine.calls, [((), {"query": "notebook", "session_id": "chat-1"})])

        asyncio.run(run())

    def test_self_lore_tool_legacy_recall_uses_keyword_query_and_persona(self):
        class LegacyEngine:
            def __init__(self):
                self.calls = []

            async def recall_persona_lore(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return "persona fact"

        async def run():
            engine = LegacyEngine()
            tool = self.pfc_tools.SelfLoreQueryTool(memory_engine=engine, persona_id="persona-a")
            result = await tool.call(_FakeWrapper(_FakeEvent()), query="voice")
            self.assertEqual(result, "persona fact")
            self.assertEqual(engine.calls, [((), {"query": "voice", "persona_id": "persona-a"})])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
