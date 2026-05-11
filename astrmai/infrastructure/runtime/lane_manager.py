import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional

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
        self._remote_sessions: Dict[str, str] = {}
        self._lane_locks: Dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

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

    def _should_rotate(
        self,
        lane_umo: str,
        prompt_version: str,
        prefix_hash: str,
        model_id: str,
        persona_id: str,
    ) -> bool:
        meta = self._runtime_meta.get(lane_umo)
        if not meta:
            return False
        return any(
            [
                meta.get("prompt_version") != prompt_version,
                meta.get("prefix_hash") != prefix_hash,
                meta.get("persona_id") != persona_id,
            ]
        )

    def _build_title(self, lane_key: LaneKey) -> str:
        return f"AstrMai {lane_key.subsystem}/{lane_key.task_family}"

    def get_remote_session_id(self, lane_umo: str, provider_family: str) -> str:
        key = f"{provider_family}:{lane_umo}"
        if key not in self._remote_sessions:
            self._remote_sessions[key] = lane_umo
        return self._remote_sessions[key]