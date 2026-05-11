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
        self._static_agents = [
            CronAgent(db_service=db_service),
            ComputerAgent(),
        ]
        self._handoff_registry = HandoffRegistry(context)
        logger.info("[Sys3Router] 馃殾 Refactoring-side Sys3 router initialized.")

    async def get_all_agents(self) -> list:
        static_names = {getattr(agent, "name", "") for agent in self._static_agents}
        dynamic_agents = await self._handoff_registry.discover(static_names)
        return [*self._static_agents, *dynamic_agents]

    async def get_light_tools_for_planner(self) -> ToolSet:
        full_set = ToolSet(await self.get_all_agents())
        return full_set.get_light_tool_set()

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
