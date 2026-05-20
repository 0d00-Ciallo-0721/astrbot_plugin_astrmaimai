# astrmai/infra/model_router.py
"""
智能模型路由器（Model Router）。

职责：将模型轮询调度、健康度评分、故障冷却隔离从 gateway 主流程中拆出。

核心策略：
1. 每个模型维护一个 [-10, +10] 的健康分。
2. 触发 429 / timeout 等致命错误时进入冷却期。
3. 健康分相同时退化为 Round-Robin，避免长期偏压。
4. 所有模型都在冷却时，优先返回最早解除冷却的模型。
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List

from astrbot.api import logger


@dataclass
class ModelState:
    """单个模型的运行时状态。"""

    health_score: int = 5
    cooldown_until: float = 0.0
    total_calls: int = 0
    total_failures: int = 0
    consecutive_failures: int = 0


@dataclass
class PoolState:
    """单个模型池的状态机。"""

    cursor: int = 0
    models: Dict[str, ModelState] = field(default_factory=dict)


# ------------------------------------------------------------------
# 配置常量
# ------------------------------------------------------------------
HEALTH_MIN = -10
HEALTH_MAX = 10
HEALTH_INIT = 5
SUCCESS_REWARD = 1
FAILURE_PENALTY = -2
FATAL_PENALTY = -4
BASE_COOLDOWN_SEC = 30.0
MAX_COOLDOWN_SEC = 120.0


class ModelRouter:
    """
    智能模型路由器。

    使用方式：
        router = ModelRouter()
        ranked = router.get_ranked_models("task", ["model-a", "model-b", "model-c"])
        router.report_success("task", "model-a")
        router.report_failure("task", "model-b", is_fatal=True)
    """

    def __init__(self):
        self._pools: Dict[str, PoolState] = {}
        self._sticky_primary: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------
    def get_ranked_models(
        self,
        pool_name: str,
        models: List[str],
        sticky_key: str = "",
        sticky_preferred: str = "",
    ) -> List[str]:
        """
        获取按健康度排序的模型列表。

        算法：
        1. 清洗并去重输入模型列表。
        2. 为每个模型初始化或获取 ModelState。
        3. 分离“可用模型”和“冷却中模型”。
        4. 可用模型按健康分降序排序，同分时按轮询游标打散。
        5. 冷却中模型按最早解冻时间追加到队尾。
        6. 推进游标，避免同分时长期偏压。
        """
        clean_models = [m.strip() for m in models if m and m.strip()]
        if not clean_models:
            return []

        # 去重并保序
        unique_models = list(dict.fromkeys(clean_models))

        # 确保模型池和模型状态存在
        pool = self._ensure_pool(pool_name)
        for mid in unique_models:
            if mid not in pool.models:
                pool.models[mid] = ModelState()

        now = time.time()

        # 分离可用模型与冷却中模型
        available = []
        cooling = []
        for mid in unique_models:
            state = pool.models[mid]
            if state.cooldown_until > now:
                cooling.append((mid, state))
            else:
                available.append((mid, state))

        # 可用模型：健康分降序，同分时参考轮询游标
        cursor = pool.cursor % len(unique_models) if unique_models else 0
        model_index = {mid: i for i, mid in enumerate(unique_models)}

        def sort_key(item):
            mid, state = item
            idx = model_index.get(mid, 0)
            relative_pos = (idx - cursor) % len(unique_models)
            return (-state.health_score, relative_pos)

        available.sort(key=sort_key)
        sticky_primary = self._resolve_sticky_primary(
            pool_name=pool_name,
            unique_models=unique_models,
            sticky_key=sticky_key,
            sticky_preferred=sticky_preferred,
        )
        if sticky_primary:
            pinned = None
            tail = []
            for item in available:
                if item[0] == sticky_primary:
                    pinned = item
                else:
                    tail.append(item)
            if pinned is not None:
                available = [pinned] + tail

        # 冷却中的模型：按最早解冻时间升序
        cooling.sort(key=lambda x: x[1].cooldown_until)

        # 合并结果：可用模型优先，冷却中模型兜底
        ranked = [mid for mid, _ in available] + [mid for mid, _ in cooling]

        # 推进游标
        pool.cursor = (cursor + 1) % len(unique_models)

        if cooling:
            cooling_ids = [mid for mid, _ in cooling]
            logger.debug(f"[ModelRouter] pool={pool_name}: cooling models {cooling_ids} moved to the tail")

        return ranked

    def report_success(self, pool_name: str, model_id: str):
        """上报一次成功调用。"""
        pool = self._ensure_pool(pool_name)
        state = pool.models.get(model_id)
        if not state:
            return

        state.total_calls += 1
        state.consecutive_failures = 0
        state.health_score = min(HEALTH_MAX, state.health_score + SUCCESS_REWARD)

        # 如果模型在冷却中但成功返回，则提前解除冷却
        if state.cooldown_until > 0:
            state.cooldown_until = 0.0
            logger.info(f"[ModelRouter] model {model_id} succeeded during cooldown; isolation cleared early")

    def report_failure(self, pool_name: str, model_id: str, is_fatal: bool = False):
        """
        上报一次调用失败。

        is_fatal=True: 429 / timeout / ratelimit 等致命错误，触发冷却隔离。
        is_fatal=False: 普通错误，仅扣分，不进入隔离。
        """
        pool = self._ensure_pool(pool_name)
        state = pool.models.get(model_id)
        if not state:
            return

        state.total_calls += 1
        state.total_failures += 1
        state.consecutive_failures += 1

        penalty = FATAL_PENALTY if is_fatal else FAILURE_PENALTY
        state.health_score = max(HEALTH_MIN, state.health_score + penalty)

        if is_fatal:
            # 自适应冷却：连续失败越多，冷却时间越长
            cooldown = min(
                BASE_COOLDOWN_SEC * state.consecutive_failures,
                MAX_COOLDOWN_SEC,
            )
            state.cooldown_until = time.time() + cooldown
            logger.warning(
                f"[ModelRouter] model {model_id} hit a fatal error; "
                f"enter cooldown for {cooldown:.0f}s "
                f"(health={state.health_score}, consecutive_failures={state.consecutive_failures})"
            )
        else:
            logger.debug(
                f"[ModelRouter] model {model_id} failed normally; "
                f"health={state.health_score}"
            )

    def get_stats(self) -> Dict[str, dict]:
        """返回所有模型池的运行快照，用于调试和监控。"""
        now = time.time()
        stats = {}
        for pool_name, pool in self._pools.items():
            pool_stats = {}
            for mid, state in pool.models.items():
                pool_stats[mid] = {
                    "health": state.health_score,
                    "calls": state.total_calls,
                    "failures": state.total_failures,
                    "cooling": state.cooldown_until > now,
                    "cooldown_remaining": max(0, state.cooldown_until - now),
                }
            stats[pool_name] = {
                "cursor": pool.cursor,
                "models": pool_stats,
            }
        return stats

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------
    def _ensure_pool(self, pool_name: str) -> PoolState:
        """确保模型池状态存在。"""
        if pool_name not in self._pools:
            self._pools[pool_name] = PoolState()
        return self._pools[pool_name]

    def _resolve_sticky_primary(
        self,
        *,
        pool_name: str,
        unique_models: List[str],
        sticky_key: str,
        sticky_preferred: str,
    ) -> str:
        if not sticky_key or not unique_models:
            return ""
        sticky_slot = f"{pool_name}:{sticky_key}"
        current = self._sticky_primary.get(sticky_slot, "")
        if current in unique_models:
            return current
        preferred = sticky_preferred if sticky_preferred in unique_models else unique_models[0]
        self._sticky_primary[sticky_slot] = preferred
        return preferred
