import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


def _install_workmode_stubs():
    tool_mod = types.ModuleType("astrbot.core.agent.tool")

    class _FunctionTool:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    class _ToolSet:
        def __init__(self, tools):
            self.tools = list(tools)

        def get_light_tool_set(self):
            return self

    tool_mod.ToolSet = _ToolSet
    tool_mod.FunctionTool = _FunctionTool
    tool_mod.ToolExecResult = str
    sys.modules["astrbot.core.agent.tool"] = tool_mod

    run_context_mod = types.ModuleType("astrbot.core.agent.run_context")

    class _ContextWrapper:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    run_context_mod.ContextWrapper = _ContextWrapper
    sys.modules["astrbot.core.agent.run_context"] = run_context_mod

    agent_ctx_mod = types.ModuleType("astrbot.core.astr_agent_context")
    agent_ctx_mod.AstrAgentContext = object
    sys.modules["astrbot.core.astr_agent_context"] = agent_ctx_mod

    cron_tools_mod = types.ModuleType("astrbot.core.tools.cron_tools")
    cron_tools_mod.CREATE_CRON_JOB_TOOL = SimpleNamespace(name="create")
    cron_tools_mod.DELETE_CRON_JOB_TOOL = SimpleNamespace(name="delete")
    cron_tools_mod.LIST_CRON_JOBS_TOOL = SimpleNamespace(name="list")
    sys.modules["astrbot.core.tools.cron_tools"] = cron_tools_mod


class WorkmodeRouterRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        _install_workmode_stubs()
        for mod in [
            "astrmai.workmode.router",
            "astrmai.workmode.tools.handoff_registry",
        ]:
            sys.modules.pop(mod, None)
        self.router_mod = importlib.import_module("astrmai.workmode.router")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_router_exposes_static_and_dynamic_agents(self):
        handoffs = [
            SimpleNamespace(name="dynamic_alpha", provider_id="p1"),
            SimpleNamespace(name="transfer_to_computer", provider_id="ignored"),
        ]
        context = SimpleNamespace(subagent_orchestrator=SimpleNamespace(handoffs=handoffs))
        router = self.router_mod.Sys3Router(SimpleNamespace(), context, db_service=None)

        async def _run():
            names = await router.list_agent_names()
            light = await router.get_light_tools_for_planner()
            return names, light

        names, light = asyncio.run(_run())
        self.assertIn("transfer_to_computer", names)
        self.assertIn("transfer_to_cron", names)
        self.assertIn("dynamic_alpha", names)
        self.assertEqual(len(light.tools), len(names))


if __name__ == "__main__":
    unittest.main()
