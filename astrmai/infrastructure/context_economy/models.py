from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

from ..runtime.lane_manager import LaneKey
from .prompt_templates import PromptEnvelope


class WorkloadFamily(str, Enum):
    CHAT_DIALOG = "chat_dialog"
    CHAT_TOOLS = "chat_tools"
    VISION = "vision"
    MEMORY_TOPIC_SUMMARY = "memory_topic_summary"
    MEMORY_GLOBAL_SUMMARY = "memory_global_summary"
    PROACTIVE_GENERATION = "proactive_generation"
    PROFILE_GENERATION = "profile_generation"
    PERSONA_SUMMARY = "persona_summary"
    DREAM_GENERATION = "dream_generation"
    COMPACTION_SUMMARY = "compaction_summary"
    JUDGE = "judge"
    MOOD = "mood"
    DATA_PROCESS = "data_process"


@dataclass(frozen=True)
class WorkloadRequest:
    family: WorkloadFamily
    pool_name: str
    prompt: str
    system_prompt: str
    models: tuple[str, ...]
    lane_key: Optional[LaneKey] = None
    base_origin: str = ""
    prefix_hash: str = ""
    persona_id: str = ""
    is_json: bool = False
    scope_id: str = ""
    scope_kind: str = "chat"
    tool_mode: bool = False
    template_id: str = ""
    template_version: str = "v1"
    schema_id: str = ""
    persona_core_version: str = ""
    stable_prefix_text: str = ""
    dynamic_payload_text: str = ""
    template_envelope: Optional[PromptEnvelope] = None


@dataclass(frozen=True)
class WorkloadPolicy:
    family: WorkloadFamily
    pool_name: str
    cache_priority: bool
    freshness_priority: bool
    sticky_model: bool
    use_provider_session: bool
    use_cache_hint: bool
    lane_key: Optional[LaneKey]
    lane_scope_id: str
    lane_scope_kind: str
    template_id: str
    template_version: str
    schema_id: str
    persona_core_version: str
    stable_prefix_text: str
    dynamic_payload_text: str
    stable_prefix_hash: str
    effective_prefix_hash: str
    lane_prompt_identity: str
    primary_model: str
    sticky_key: str
    cache_affinity_enabled: bool
    cache_affinity_reason: str
    provider_cache_affinity_class: str
    rotation_scope_key: str = ""
    synthetic_lane_rotated: bool = False
    synthetic_lane_rotate_reason: str = ""


@dataclass
class WorkloadTrace:
    workload_family: str
    lane_umo: str = ""
    lane_scope_id: str = ""
    prefix_hash: str = ""
    template_id: str = ""
    template_version: str = ""
    schema_id: str = ""
    primary_model: str = ""
    actual_model: str = ""
    fallback_used: bool = False
    lane_rotated: bool = False
    lane_rotate_reason: str = ""
    provider_family: str = ""
    provider_session_enabled: bool = False
    provider_session_id: str = ""
    provider_cache_hint_enabled: bool = False
    provider_cache_affinity_class: str = ""
    cache_affinity_enabled: bool = False
    cache_affinity_reason: str = ""
    stable_prefix_length: int = 0
    dynamic_payload_length: int = 0
    template_schema_id: str = ""
    rotation_scope_key: str = ""
    lane_prompt_identity: str = ""
    persona_core_version: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkloadMetrics:
    call_count: int = 0
    lane_rotate_count: int = 0
    fallback_count: int = 0
    primary_hits: int = 0
    provider_session_uses: int = 0
    provider_session_reused: int = 0
    cache_affinity_ready: int = 0
    stable_prefix_length_total: int = 0
    dynamic_payload_length_total: int = 0
    actual_models: dict[str, int] = field(default_factory=dict)
    rotate_reasons: dict[str, int] = field(default_factory=dict)
    workload_families: dict[str, int] = field(default_factory=dict)
    seen_provider_session_ids: set[str] = field(default_factory=set)
