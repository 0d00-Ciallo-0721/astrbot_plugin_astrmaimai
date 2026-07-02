from __future__ import annotations

from astrbot.api import logger
from astrbot.core.agent.tool import ToolSet

from .subagents.computer_agent import ComputerAgent
from .subagents.cron_agent import CronAgent
from .tools.handoff_registry import HandoffRegistry


class Sys3Router:
    """Refactoring-side workmode router for static and dynamic subagents."""

    def __init__(self, plugin_config, context, db_service=None):
        self.plugin_config = plugin_config
        self.context = context
        self.db_service = db_service
        sandbox_enabled = bool(
            getattr(getattr(plugin_config, "sys3", None), "computer_agent_sandbox_enabled", False)
        )
        self._static_agents = [
            CronAgent(db_service=db_service),
            ComputerAgent(sandbox_enabled=sandbox_enabled),
        ]
        self._handoff_registry = HandoffRegistry(context)
        self._raw_agent_map: dict[str, object] = {}
        logger.info("[Sys3Router] 🚀 Refactoring-side Sys3 router initialized (sandbox=%s).", sandbox_enabled)

    async def get_all_agents(self) -> list:
        static_names = {getattr(agent, "name", "") for agent in self._static_agents}
        dynamic_agents = await self._handoff_registry.discover(static_names)
        agents = [*self._static_agents, *dynamic_agents]
        self._raw_agent_map = {getattr(a, "name", ""): a for a in agents if getattr(a, "name", "")}
        return agents

    async def get_light_tools_for_planner(self) -> ToolSet:
        full_set = ToolSet(await self.get_all_agents())
        light_set = full_set.get_light_tool_set()
        # ponytail: inject handler refs from raw agents so _execute_local won't raise ValueError.
        # get_light_tool_set() creates bare FunctionTool(handler=None) — without this injection,
        # tool_loop_agent → _execute_local MRO check fails because FunctionTool.call is not overridden.
        for light_tool in light_set.tools:
            name = getattr(light_tool, "name", "")
            raw_agent = self._raw_agent_map.get(name)
            if raw_agent is not None and hasattr(raw_agent, "call"):
                light_tool.handler = raw_agent.call
        return light_set

    async def get_full_tools_for_direct_entry(self) -> ToolSet:
        return ToolSet(await self.get_all_agents())

    def get_static_agent_names(self) -> list[str]:
        return [getattr(agent, "name", "") for agent in self._static_agents]

    async def list_agent_names(self) -> list[str]:
        return [getattr(agent, "name", "") for agent in await self.get_all_agents()]

    def get_cron_service(self):
        for agent in self._static_agents:
            if isinstance(agent, CronAgent):
                return agent
        return None

    async def describe_status(self) -> dict:
        loaded_names = []
        if hasattr(self._handoff_registry, "list_loaded_names"):
            loaded_names = list(self._handoff_registry.list_loaded_names())
        all_names = await self.list_agent_names()
        return {
            "static_agents": self.get_static_agent_names(),
            "all_agents": all_names,
            "dynamic_agents": [name for name in all_names if name not in self.get_static_agent_names()],
            "loaded_handoffs": loaded_names,
            "cron_available": self.get_cron_service() is not None,
        }


__all__ = ["Sys3Router"]
