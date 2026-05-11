import asyncio
from typing import Any, List, Optional

from astrbot.api import logger
from astrbot.api.star import Context

from ...shared.constants.defaults import GatewaySettings, build_infrastructure_settings
from ..runtime.lane_manager import LaneManager
from .gateway_call import GatewayCallMixin
from .gateway_exceptions import LLMCascadeFailureException
from .gateway_lane import GatewayLaneMixin
from .gateway_policy import GatewayPolicyMixin
from .gateway_result import GatewayResultMixin
from .gateway_tasks import GatewayTaskMixin
from .model_router import ModelRouter


class GlobalModelGateway(
    GatewayPolicyMixin,
    GatewayResultMixin,
    GatewayCallMixin,
    GatewayLaneMixin,
    GatewayTaskMixin,
):
    """Lane-aware gateway facade for plain chat, tool chat, and structured tasks."""

    def __init__(self, context: Context, config: Any, settings: GatewaySettings | None = None):
        self.context = context
        self.config = config
        self.settings = settings or build_infrastructure_settings(config).gateway
        self.router = ModelRouter()
        self.lane_manager: Optional[LaneManager] = None
        self._global_semaphore = asyncio.Semaphore(self.settings.max_concurrent_llm_calls)
        logger.info(
            f"[Gateway] global concurrency limiter ready, max={self.settings.max_concurrent_llm_calls}"
        )

    def get_models_for_task(self, pool_name: str, models: List[str]) -> List[str]:
        return self.router.get_ranked_models(pool_name, models)

    def set_lane_manager(self, lane_manager: LaneManager) -> None:
        self.lane_manager = lane_manager

    def _fallback_models(self) -> List[str]:
        return list(self.settings.fallback_models)

    def _task_models(self) -> List[str]:
        return list(self.settings.task_models)

    def _agent_models(self) -> List[str]:
        return list(self.settings.agent_models)

    def _vision_models(self) -> List[str]:
        return list(self.settings.vision_models)

    def _max_retries(self) -> int:
        return self.settings.llm_retries

    def _backoff_factor(self) -> float:
        return self.settings.backoff_factor

    def _api_timeout(self) -> float:
        return self.settings.api_timeout

    def _debug_mode(self) -> bool:
        return self.settings.debug_mode


__all__ = ["GlobalModelGateway", "LLMCascadeFailureException"]