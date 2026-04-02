# astrmai/infra/model_router.py
"""
智能模型路由器 (Model Router)
职责: 将模型轮询调度、健康度评分、故障冷却隔离从 gateway.py 中独立出来。

核心算法:
1. 健康分优先调度 — 每个模型维护 [-10, +10] 的健康分，成功 +1、失败 -2
2. 故障冷却隔离 — 触发 429/timeout 等致命错误的模型进入 30s 冷却期
3. 轮询均匀分配 — 健康分相同时退化为 Round-Robin，保证负载均衡
4. 冷却穿透兜底 — 所有模型都冷却时，选最快解冻的那个（不死等）
"""
import time
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from astrbot.api import logger


@dataclass
class ModelState:
    """单个模型的运行时状态"""
    health_score: int = 5          # 健康分，初始中位值，区间 [-10, +10]
    cooldown_until: float = 0.0    # 冷却截止时间戳 (unix)，0 = 未冷却
    total_calls: int = 0           # 累计调用次数
    total_failures: int = 0        # 累计失败次数
    consecutive_failures: int = 0  # 连续失败计数（用于自适应冷却）


@dataclass
class PoolState:
    """单个模型池的状态机"""
    cursor: int = 0                                 # 轮询游标
    models: Dict[str, ModelState] = field(default_factory=dict)  # {model_id: ModelState}


# ──────────────────────────────────────────────────────────
# 配置常量
# ──────────────────────────────────────────────────────────
HEALTH_MIN = -10
HEALTH_MAX = 10
HEALTH_INIT = 5
SUCCESS_REWARD = 1       # 成功一次 +1
FAILURE_PENALTY = -2     # 普通失败 -2
FATAL_PENALTY = -4       # 致命失败 (429/timeout) -4
BASE_COOLDOWN_SEC = 30.0 # 基础冷却时间
MAX_COOLDOWN_SEC = 120.0 # 最大冷却时间 (连续失败自适应增长上限)


class ModelRouter:
    """
    智能模型路由器
    
    使用方式:
        router = ModelRouter()
        ranked = router.get_ranked_models("task", ["model-a", "model-b", "model-c"])
        # ... 调用 ranked[0] ...
        router.report_success("task", "model-a")   # 成功
        router.report_failure("task", "model-b", is_fatal=True)  # 429 致命失败
    """
    
    def __init__(self):
        self._pools: Dict[str, PoolState] = {}

    # ──────────────────────────────────────────────────────────
    # 公共接口
    # ──────────────────────────────────────────────────────────

    def get_ranked_models(self, pool_name: str, models: List[str]) -> List[str]:
        """
        获取按健康度排序的模型列表（替代原 get_models_for_task）
        
        算法:
        1. 清洗去重输入模型列表
        2. 为每个模型初始化/获取 ModelState
        3. 分离「可用模型」和「冷却中模型」
        4. 可用模型按 (健康分 DESC, 轮询游标) 排序
        5. 冷却中模型按解冻时间 ASC 追加到末尾（兜底）
        6. 推进游标
        """
        clean_models = [m.strip() for m in models if m and m.strip()]
        if not clean_models:
            return []
        
        # 去重保序
        unique_models = list(dict.fromkeys(clean_models))
        
        # 确保池和模型状态存在
        pool = self._ensure_pool(pool_name)
        for mid in unique_models:
            if mid not in pool.models:
                pool.models[mid] = ModelState()

        now = time.time()
        
        # 分离可用 / 冷却中
        available = []
        cooling = []
        for mid in unique_models:
            state = pool.models[mid]
            if state.cooldown_until > now:
                cooling.append((mid, state))
            else:
                available.append((mid, state))
        
        # 可用模型: 健康分降序排序，相同分数按轮询游标位置排序
        cursor = pool.cursor % len(unique_models) if unique_models else 0
        model_index = {mid: i for i, mid in enumerate(unique_models)}
        
        def sort_key(item):
            mid, state = item
            idx = model_index.get(mid, 0)
            # 基于游标的相对位置 (让游标指向的模型在同分中排首位)
            relative_pos = (idx - cursor) % len(unique_models)
            return (-state.health_score, relative_pos)
        
        available.sort(key=sort_key)
        
        # 冷却中模型: 按解冻时间升序 (最快解冻的排前面)
        cooling.sort(key=lambda x: x[1].cooldown_until)
        
        # 合并: 可用模型优先，冷却中兜底
        ranked = [mid for mid, _ in available] + [mid for mid, _ in cooling]
        
        # 推进游标
        pool.cursor = (cursor + 1) % len(unique_models)
        
        if cooling:
            cooling_ids = [mid for mid, _ in cooling]
            logger.debug(f"[ModelRouter] 池 {pool_name}: 冷却中模型 {cooling_ids}，已降至队尾")
        
        return ranked

    def report_success(self, pool_name: str, model_id: str):
        """上报调用成功"""
        pool = self._ensure_pool(pool_name)
        state = pool.models.get(model_id)
        if not state:
            return
        
        state.total_calls += 1
        state.consecutive_failures = 0  # 重置连续失败计数
        state.health_score = min(HEALTH_MAX, state.health_score + SUCCESS_REWARD)
        
        # 如果模型在冷却中但成功了（被兜底选中时），提前解除冷却
        if state.cooldown_until > 0:
            state.cooldown_until = 0.0
            logger.info(f"[ModelRouter] ✅ 模型 {model_id} 冷却期内成功响应，提前解除隔离")

    def report_failure(self, pool_name: str, model_id: str, is_fatal: bool = False):
        """
        上报调用失败
        
        is_fatal=True: 429/timeout/ratelimit 等确定性故障，触发冷却隔离
        is_fatal=False: 普通错误（幻觉、JSON 损坏），仅扣分不隔离
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
            # 自适应冷却: 连续失败次数越多，冷却时间越长（上限 120s）
            cooldown = min(
                BASE_COOLDOWN_SEC * state.consecutive_failures, 
                MAX_COOLDOWN_SEC
            )
            state.cooldown_until = time.time() + cooldown
            logger.warning(
                f"[ModelRouter] 🧊 模型 {model_id} 触发致命错误，"
                f"进入 {cooldown:.0f}s 冷却隔离 "
                f"(健康分: {state.health_score}, 连续失败: {state.consecutive_failures})"
            )
        else:
            logger.debug(
                f"[ModelRouter] ⚠️ 模型 {model_id} 普通失败，"
                f"健康分: {state.health_score}"
            )

    def get_stats(self) -> Dict[str, dict]:
        """返回所有池的健康状态快照 (用于 debug/监控)"""
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
                    "cooldown_remaining": max(0, state.cooldown_until - now)
                }
            stats[pool_name] = {
                "cursor": pool.cursor,
                "models": pool_stats
            }
        return stats

    # ──────────────────────────────────────────────────────────
    # 内部方法
    # ──────────────────────────────────────────────────────────

    def _ensure_pool(self, pool_name: str) -> PoolState:
        """确保池状态存在"""
        if pool_name not in self._pools:
            self._pools[pool_name] = PoolState()
        return self._pools[pool_name]
