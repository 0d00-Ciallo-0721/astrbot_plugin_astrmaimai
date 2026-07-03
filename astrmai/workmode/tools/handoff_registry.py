from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger


@dataclass(slots=True)
class HandoffRegistry:
    context: Any
    _dynamic_agents: list[Any] = field(default_factory=list)
    _loaded: bool = False

    async def discover(self, static_names: set[str]) -> list[Any]:
        # ponytail: re-scan every call to pick up newly registered SubAgents (R18)
        orchestrator = self._find_orchestrator()
        previous_names: set[str] = {getattr(a, "name", "") for a in self._dynamic_agents}
        if not orchestrator or not hasattr(orchestrator, "handoffs"):
            if not self._loaded:
                logger.warning("[Sys3Router] 未找到 subagent_orchestrator，保持纯静态模式。")
                self._loaded = True
            self._dynamic_agents = []
            return []

        next_agents: list[Any] = []
        existing_names: set[str] = set()
        for handoff in getattr(orchestrator, "handoffs", []) or []:
            agent_name = str(getattr(handoff, "name", "") or "").strip()
            if not agent_name or agent_name in static_names:
                continue
            if agent_name in existing_names:
                continue
            if not getattr(handoff, "active", True):
                logger.info(f"[Sys3Router] skip inactive dynamic agent: {agent_name}")
                continue
            next_agents.append(handoff)
            existing_names.add(agent_name)
            if agent_name in previous_names:
                continue
            provider = getattr(handoff, "provider_id", "璺熼殢鍏ㄥ眬")
            logger.info(
                f"[Sys3Router] 馃攲 婊¤鎸傝浇 WebUI 鍔ㄦ€?SubAgent: [{agent_name}] | 鐙珛 Provider: [{provider}]"
            )
        self._dynamic_agents = next_agents
        return list(self._dynamic_agents)

    def list_loaded_names(self) -> list[str]:
        return [getattr(agent, "name", "") for agent in self._dynamic_agents]

    def _find_orchestrator(self) -> Any:
        orchestrator = getattr(self.context, "subagent_orchestrator", None)
        if orchestrator:
            return orchestrator
        provider_manager = getattr(self.context, "provider_manager", None)
        if provider_manager:
            return getattr(provider_manager, "subagent_orchestrator", None)
        return None


__all__ = ["HandoffRegistry"]
