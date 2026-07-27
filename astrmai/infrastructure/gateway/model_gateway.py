import asyncio
from typing import Any, List, Optional

from astrbot.api import logger
from astrbot.api.star import Context

from ...shared.constants.defaults import GatewaySettings, build_infrastructure_settings
from ..context_economy import ContextEconomyCenter
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
        self.context_economy = ContextEconomyCenter()
        self.benchmark_sample_store = None
        self.lane_manager: Optional[LaneManager] = None
        self._model_cooldowns: dict[tuple[str, str], dict[str, Any]] = {}
        self._last_agent_model_selection: dict[str, Any] = {}
        self._global_semaphore = asyncio.Semaphore(max(1, int(self.settings.max_concurrent_llm_calls)))
        self._background_semaphore = self._build_background_semaphore()
        logger.info(
            f"[Gateway] global concurrency limiter ready, max={self.settings.max_concurrent_llm_calls}, "
            f"background_max={self._background_limit()}"
        )

    # G7/RT-11: 关键路径与后台调用此前共用一把全局信号量(默认3)，高峰期被忽略消息的
    # judge/mood 占满槽位、真实回复排队（skipped 轮 judge ledger elapsed 51.7s 而
    # attempt 仅数秒）。解法不是放大总并发（那会破坏 429 保护），而是给后台调用加一层
    # 子限流器：后台最多占 max-reserved 个槽，剩余槽位始终为关键路径保留。
    def _reserved_critical_slots(self) -> int:
        raw = getattr(self.settings, "critical_path_reserved_slots", 1)
        try:
            reserved = int(1 if raw is None else raw)
        except (TypeError, ValueError):
            reserved = 1
        total = max(1, int(self.settings.max_concurrent_llm_calls))
        # 至少给后台留 1 个槽，否则后台任务永远拿不到配额（total=1 时无法预留）
        return max(0, min(reserved, total - 1))

    def _background_limit(self) -> int:
        total = max(1, int(self.settings.max_concurrent_llm_calls))
        return max(1, total - self._reserved_critical_slots())

    def _build_background_semaphore(self) -> asyncio.Semaphore | None:
        total = max(1, int(self.settings.max_concurrent_llm_calls))
        limit = self._background_limit()
        return None if limit >= total else asyncio.Semaphore(limit)

    def refresh_config(self, config):
        """ponytail: hot-reload config into gateway"""
        self.config = config
        from ...shared.constants.defaults import build_infrastructure_settings
        old_limit = max(1, int(getattr(self.settings, "max_concurrent_llm_calls", 1) or 1))
        old_reserved = self._reserved_critical_slots()
        self.settings = build_infrastructure_settings(config).gateway
        new_limit = max(1, int(self.settings.max_concurrent_llm_calls))
        new_reserved = self._reserved_critical_slots()
        if new_limit != old_limit or new_reserved != old_reserved:
            self._global_semaphore = asyncio.Semaphore(new_limit)
            self._background_semaphore = self._build_background_semaphore()
            logger.info(
                f"[Gateway] concurrency limiter rebuilt, max={new_limit}, "
                f"background_max={self._background_limit()}"
            )

    def get_models_for_task(self, pool_name: str, models: List[str]) -> List[str]:
        return self.router.get_ranked_models(pool_name, models)

    def set_lane_manager(self, lane_manager: LaneManager) -> None:
        self.lane_manager = lane_manager

    def get_context_economy_stats(self) -> dict:
        return self.context_economy.snapshot_metrics()

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
