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
        if self._loaded:
            return list(self._dynamic_agents)
        self._loaded = True
        orchestrator = self._find_orchestrator()
        if not orchestrator or not hasattr(orchestrator, "handoffs"):
            logger.warning("[Sys3Router] 鈿狅笍 鏈壘鍒?subagent_orchestrator锛屼繚鎸佺函闈欐€佹ā寮忋€?")
            return []

        for handoff in getattr(orchestrator, "handoffs", []) or []:
            agent_name = getattr(handoff, "name", "")
            if not agent_name or agent_name in static_names:
                continue
            self._dynamic_agents.append(handoff)
            provider = getattr(handoff, "provider_id", "璺熼殢鍏ㄥ眬")
            logger.info(
                f"[Sys3Router] 馃攲 婊¤鎸傝浇 WebUI 鍔ㄦ€?SubAgent: [{agent_name}] | 鐙珛 Provider: [{provider}]"
            )
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
