import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from ...shared.constants.defaults import LaneRuntimeSettings, build_infrastructure_settings
from .lane_history import LaneHistoryMixin
from .lane_storage import LaneStorageMixin


@dataclass(frozen=True)
class LaneKey:
    subsystem: str
    task_family: str
    scope_id: str
    prompt_version: str = "v1"
    scope_kind: str = "chat"

    def as_suffix(self) -> str:
        return f"{self.subsystem}:{self.task_family}:{self.prompt_version}"

    def as_log_key(self) -> str:
        return f"{self.scope_kind}:{self.scope_id}:{self.as_suffix()}"


@dataclass(frozen=True)
class LanePolicy:
    store_mode: str
    max_raw_turns: int
    summarize_threshold_tokens: int = 0
    ttl_seconds: int = 86400


class LaneManager(LaneHistoryMixin, LaneStorageMixin):
    """Lane runtime coordinator for isolated model conversations."""

    DEFAULT_POLICIES: Dict[tuple[str, str], LanePolicy] = {
        ("sys1", "judge"): LanePolicy(store_mode="structured", max_raw_turns=6),
        ("sys1", "mood"): LanePolicy(store_mode="structured", max_raw_turns=6),
        ("sys1", "vision"): LanePolicy(store_mode="structured", max_raw_turns=2),
        ("sys2", "dialog"): LanePolicy(store_mode="full", max_raw_turns=12),
        ("sys2", "followup"): LanePolicy(store_mode="structured", max_raw_turns=4),
        ("sys2", "goal"): LanePolicy(store_mode="structured", max_raw_turns=4),
        ("sys2", "expression"): LanePolicy(store_mode="structured", max_raw_turns=4),
        ("sys2", "persona"): LanePolicy(store_mode="structured", max_raw_turns=4),
        ("sys2", "retrieval"): LanePolicy(store_mode="structured", max_raw_turns=4),
        ("sys3", "direct"): LanePolicy(store_mode="full", max_raw_turns=8),
        ("bg", "memory"): LanePolicy(store_mode="summary_only", max_raw_turns=3),
        ("bg", "compaction"): LanePolicy(store_mode="summary_only", max_raw_turns=3),
        ("bg", "dream"): LanePolicy(store_mode="summary_only", max_raw_turns=3),
        ("bg", "reflect"): LanePolicy(store_mode="summary_only", max_raw_turns=3),
        ("bg", "proactive"): LanePolicy(store_mode="summary_only", max_raw_turns=3),
        ("bg", "profile"): LanePolicy(store_mode="summary_only", max_raw_turns=3),
    }

    def __init__(
        self,
        conversation_manager: Any,
        config: Any = None,
        settings: LaneRuntimeSettings | None = None,
    ):
        self.conversation_manager = conversation_manager
        self.config = config
        self.settings = settings or build_infrastructure_settings(config).lane
        self._runtime_meta: Dict[str, Dict[str, Any]] = {}
        self._remote_sessions: Dict[str, Tuple[str, float]] = {}
        self._remote_sessions_ttl: float = 3600.0
        self._remote_sessions_last_cleanup: float = 0.0
        self._lane_locks: Dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()
        self._meta_lock = asyncio.Lock()

    def get_policy(self, lane_key: LaneKey) -> LanePolicy:
        return self.DEFAULT_POLICIES.get(
            (lane_key.subsystem, lane_key.task_family),
            LanePolicy(store_mode="structured", max_raw_turns=6),
        )

    def resolve_lane_umo(self, base_origin: Optional[str], lane_key: LaneKey) -> str:
        root = base_origin or f"astrmai_bg:OtherMessage:{lane_key.scope_id}"
        return f"{root}@@astrmai:{lane_key.as_suffix()}"

    async def _get_lane_lock(self, lane_umo: str) -> asyncio.Lock:
        async with self._lock:
            if lane_umo not in self._lane_locks:
                self._lane_locks[lane_umo] = asyncio.Lock()
            return self._lane_locks[lane_umo]

    async def _should_rotate(
        self,
        lane_umo: str,
        prompt_version: str,
        prefix_hash: str,
        model_id: str,
        persona_id: str,
        template_id: str = "",
        schema_id: str = "",
        persona_core_version: str = "",
    ) -> bool:
        return bool(
            await self._rotation_reason(
                lane_umo=lane_umo,
                prompt_version=prompt_version,
                prefix_hash=prefix_hash,
                model_id=model_id,
                persona_id=persona_id,
                template_id=template_id,
                schema_id=schema_id,
                persona_core_version=persona_core_version,
            )
        )

    async def _rotation_reason(
        self,
        lane_umo: str,
        prompt_version: str,
        prefix_hash: str,
        model_id: str,
        persona_id: str,
        template_id: str = "",
        schema_id: str = "",
        persona_core_version: str = "",
    ) -> str:
        async with self._meta_lock:
            meta = self._runtime_meta.get(lane_umo)
        # 锁外读取安全：写操作是整体替换（= {...}）而非原地修改，
        # meta 持有旧 dict 引用，后续 .get() 读取的是一致快照。
        # 此处无 await 点，asyncio 不会切换协程，无 TOCTOU 窗口。
        if not meta:
            return ""
        reasons = []
        if meta.get("template_id") != template_id:
            reasons.append("template_changed")
        if meta.get("prompt_version") != prompt_version:
            reasons.append("template_version_changed")
        if meta.get("schema_id") != schema_id:
            reasons.append("schema_changed")
        if meta.get("prefix_hash") != prefix_hash:
            reasons.append("prefix_hash")
        if meta.get("persona_core_version") != persona_core_version:
            reasons.append("persona_core_version_changed")
        if meta.get("persona_id") != persona_id:
            reasons.append("persona_id")
        return ",".join(reasons)

    def _build_title(self, lane_key: LaneKey) -> str:
        return f"AstrMai {lane_key.subsystem}/{lane_key.task_family}"

    def get_remote_session_id(self, lane_umo: str, provider_family: str) -> str:
        key = f"{provider_family}:{lane_umo}"
        now = time.time()
        # 惰性清理：每 300s 最多执行一次全量扫描
        if now - self._remote_sessions_last_cleanup > 300.0:
            self._cleanup_remote_sessions(now)
        if key not in self._remote_sessions:
            self._remote_sessions[key] = (lane_umo, now)
        else:
            # 刷新时间戳，防止活跃条目被误淘汰
            self._remote_sessions[key] = (self._remote_sessions[key][0], now)
        return self._remote_sessions[key][0]

    def _cleanup_remote_sessions(self, now: float) -> None:
        expired = [
            k for k, (_, ts) in self._remote_sessions.items()
            if now - ts > self._remote_sessions_ttl
        ]
        for k in expired:
            del self._remote_sessions[k]
        self._remote_sessions_last_cleanup = now

    def expire_remote_sessions_for_lane(self, lane_umo: str) -> int:
        """lane 旋转时过期该 lane 的所有 remote session 映射，返回过期数量。"""
        removed = 0
        for key in list(self._remote_sessions.keys()):
            if key.endswith(f":{lane_umo}"):
                del self._remote_sessions[key]
                removed += 1
        return removed

    async def get_runtime_meta(self, lane_umo: str) -> Dict[str, Any]:
        async with self._meta_lock:
            return dict(self._runtime_meta.get(lane_umo, {}) or {})
