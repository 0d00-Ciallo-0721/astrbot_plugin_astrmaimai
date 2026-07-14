"""Regression tests for the Sys3 planner FunctionTool contract."""

import asyncio
import importlib
import types
import sys
import unittest


def _install_sys3_stubs():
    """Install minimal stubs so that workmode.router can be imported."""
    # AstrBot core stubs
    tool_mod = types.ModuleType("astrbot.core.agent.tool")

    class _FunctionTool:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, name="", parameters=None, description="", handler=None):
            self.name = name
            self.parameters = parameters or {"type": "object", "properties": {}}
            self.description = description
            self.handler = handler
            self.active = True

        async def call(self, context, **kwargs):
            if self.handler is not None:
                return await self.handler(context, **kwargs)
            raise NotImplementedError(
                "FunctionTool.call() must be implemented by subclasses or set a handler."
            )

    class _ToolSet:
        def __init__(self, tools):
            self.tools = list(tools)

        def get_light_tool_set(self):
            # Simulate AstrBot's get_light_tool_set: returns bare FunctionTool with handler=None
            light_tools = []
            for tool in self.tools:
                light_tools.append(
                    _FunctionTool(
                        name=getattr(tool, "name", ""),
                        parameters={"type": "object", "properties": {}},
                        description=getattr(tool, "description", ""),
                        handler=None,  # ⚠ Bare handler — the bug we're fixing
                    )
                )
            return _ToolSet(light_tools)

    tool_mod.FunctionTool = _FunctionTool
    tool_mod.ToolSet = _ToolSet
    tool_mod.ToolExecResult = str
    sys.modules["astrbot.core.agent.tool"] = tool_mod

    # AstrBot run_context stub
    rc_mod = types.ModuleType("astrbot.core.agent.run_context")

    class _ContextWrapper:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    rc_mod.ContextWrapper = _ContextWrapper
    sys.modules["astrbot.core.agent.run_context"] = rc_mod

    # AstrBot astr_agent_context stub
    aac_mod = types.ModuleType("astrbot.core.astr_agent_context")

    class _AstrAgentContext:
        pass

    aac_mod.AstrAgentContext = _AstrAgentContext
    sys.modules["astrbot.core.astr_agent_context"] = aac_mod

    # astrbot.api logger stub
    api_mod = types.ModuleType("astrbot.api")
    api_mod.logger = type("Logger", (), {
        "info": lambda *a, **kw: None,
        "debug": lambda *a, **kw: None,
        "warning": lambda *a, **kw: None,
        "error": lambda *a, **kw: None,
        "exception": lambda *a, **kw: None,
    })
    sys.modules["astrbot.api"] = api_mod


class Sys3LightToolRegressionTests(unittest.TestCase):
    """Regression tests for R1: Sys3 light tool handler injection."""

    @classmethod
    def setUpClass(cls):
        _install_sys3_stubs()

    def test_planner_tools_keep_real_subagent_call_contract(self):
        """Planner tools must retain the real SubAgent call override and schema."""
        router_mod = importlib.import_module("astrmai.workmode.router")

        # Build a minimal Sys3Router with no dynamic agents
        plugin_config = type("Config", (), {
            "sys3": type("Sys3Cfg", (), {"computer_agent_sandbox_enabled": True}),
        })()
        context = object()
        router = router_mod.Sys3Router(plugin_config, context, db_service=None)

        async def _run():
            light_set = await router.get_light_tools_for_planner()
            return light_set.tools

        planner_tools = asyncio.run(_run())

        self.assertGreater(len(planner_tools), 0, "Expected at least one SubAgent")

        static_names = router.get_static_agent_names()
        for tool in planner_tools:
            name = getattr(tool, "name", "")
            if name in static_names:
                self.assertTrue(
                    hasattr(tool, "call") and callable(tool.call),
                    f"Planner tool '{name}' must preserve its SubAgent call override",
                )
                self.assertIn("query", getattr(tool, "parameters", {}).get("properties", {}))

    def test_raw_agent_map_is_populated_after_get_all_agents(self):
        """_raw_agent_map must be populated after get_all_agents() is called."""
        router_mod = importlib.import_module("astrmai.workmode.router")
        plugin_config = type("Config", (), {
            "sys3": type("Sys3Cfg", (), {"computer_agent_sandbox_enabled": True}),
        })()
        router = router_mod.Sys3Router(plugin_config, object(), db_service=None)

        async def _run():
            agents = await router.get_all_agents()
            return agents, router._raw_agent_map

        agents, agent_map = asyncio.run(_run())

        self.assertGreater(len(agents), 0)
        self.assertGreater(len(agent_map), 0)
        for agent in agents:
            name = getattr(agent, "name", "")
            self.assertIn(name, agent_map, f"Agent '{name}' must be in _raw_agent_map")

    def test_full_tools_still_return_real_agents(self):
        """get_full_tools_for_direct_entry() must still return actual SubAgent instances."""
        router_mod = importlib.import_module("astrmai.workmode.router")
        plugin_config = type("Config", (), {
            "sys3": type("Sys3Cfg", (), {"computer_agent_sandbox_enabled": True}),
        })()
        router = router_mod.Sys3Router(plugin_config, object(), db_service=None)

        async def _run():
            full_set = await router.get_full_tools_for_direct_entry()
            return full_set.tools

        full_tools = asyncio.run(_run())
        self.assertGreater(len(full_tools), 0)

        static_names = router.get_static_agent_names()
        for tool in full_tools:
            name = getattr(tool, "name", "?")
            if name in static_names:
                # Full SubAgent must have a call() method
                self.assertTrue(
                    hasattr(tool, "call") and callable(tool.call),
                    f"Full tool '{name}' must have callable call()",
                )


if __name__ == "__main__":
    unittest.main()
