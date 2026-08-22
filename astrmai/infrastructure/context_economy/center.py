from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import replace
from typing import Optional

from astrbot.api import logger

from .models import WorkloadFamily, WorkloadMetrics, WorkloadPolicy, WorkloadRequest, WorkloadTrace
from .prompt_templates import PromptEnvelope, PromptTemplateRegistry
from ..runtime.lane_manager import LaneKey, LaneManager


class ContextEconomyCenter:
    CACHE_PRIORITY_FAMILIES = frozenset(
        {
            WorkloadFamily.VISION,
            WorkloadFamily.MEMORY_TOPIC_SUMMARY,
            WorkloadFamily.MEMORY_GLOBAL_SUMMARY,
            WorkloadFamily.PROACTIVE_GENERATION,
            WorkloadFamily.PROFILE_GENERATION,
            WorkloadFamily.PERSONA_SUMMARY,
            WorkloadFamily.DREAM_GENERATION,
            WorkloadFamily.COMPACTION_SUMMARY,
        }
    )
    GLOBAL_SCOPE_DIAGNOSTIC_FAMILIES = frozenset(
        {
            WorkloadFamily.MEMORY_TOPIC_SUMMARY,
            WorkloadFamily.MEMORY_GLOBAL_SUMMARY,
            WorkloadFamily.DREAM_GENERATION,
        }
    )

    def __init__(self) -> None:
        self._metrics: dict[str, WorkloadMetrics] = defaultdict(WorkloadMetrics)
        self._template_metrics: dict[str, WorkloadMetrics] = defaultdict(WorkloadMetrics)
        self._lane_identity_state: dict[str, dict[str, str]] = {}
        self.templates = PromptTemplateRegistry()

    def build_request(
        self,
        *,
        family: WorkloadFamily,
        pool_name: str,
        prompt: str,
        system_prompt: str,
        models: list[str] | tuple[str, ...],
        lane_key: Optional[LaneKey] = None,
        base_origin: str = "",
        prefix_hash: str = "",
        persona_id: str = "",
        is_json: bool = False,
        scope_id: str = "",
        scope_kind: str = "chat",
        tool_mode: bool = False,
        template_id: str = "",
        template_version: str = "v1",
        schema_id: str = "",
        persona_core_version: str = "",
        stable_prefix_text: str = "",
        dynamic_payload_text: str = "",
        template_envelope: Optional[PromptEnvelope] = None,
    ) -> WorkloadRequest:
        envelope = template_envelope
        resolved_template_id = str(template_id or (envelope.template_id if envelope else "") or family.value)
        resolved_template_version = str(
            template_version or (envelope.template_version if envelope else "") or getattr(lane_key, "prompt_version", "v1") or "v1"
        )
        resolved_schema_id = str(schema_id or (envelope.schema_id if envelope else "") or ("json" if is_json else "text"))
        resolved_stable_prefix = str(stable_prefix_text or (envelope.stable_prefix_text if envelope else "") or system_prompt or "")
        resolved_dynamic_payload = str(dynamic_payload_text or (envelope.dynamic_payload_text if envelope else "") or prompt or "")
        return WorkloadRequest(
            family=family,
            pool_name=pool_name,
            prompt=str(prompt or ""),
            system_prompt=str(system_prompt or ""),
            models=tuple(models or ()),
            lane_key=lane_key,
            base_origin=base_origin,
            prefix_hash=str(prefix_hash or ""),
            persona_id=str(persona_id or ""),
            is_json=bool(is_json),
            scope_id=str(scope_id or ""),
            scope_kind=str(scope_kind or "chat"),
            tool_mode=bool(tool_mode),
            template_id=resolved_template_id,
            template_version=resolved_template_version,
            schema_id=resolved_schema_id,
            persona_core_version=str(persona_core_version or persona_id or ""),
            stable_prefix_text=resolved_stable_prefix,
            dynamic_payload_text=resolved_dynamic_payload,
            template_envelope=envelope,
        )

    def infer_workload_family(
        self,
        *,
        lane_key: Optional[LaneKey] = None,
        pool_name: str = "",
        tool_mode: bool = False,
    ) -> WorkloadFamily:
        if lane_key:
            if (lane_key.subsystem, lane_key.task_family) == ("sys2", "dialog"):
                return WorkloadFamily.CHAT_TOOLS if tool_mode else WorkloadFamily.CHAT_DIALOG
            if (lane_key.subsystem, lane_key.task_family) == ("sys1", "vision"):
                return WorkloadFamily.VISION
            # ponytail: M15 — added missing subsystem mappings
            if (lane_key.subsystem, lane_key.task_family) == ("sys1", "judge"):
                return WorkloadFamily.JUDGE
            if (lane_key.subsystem, lane_key.task_family) == ("bg", "judge_validation"):
                return WorkloadFamily.JUDGE
            if (lane_key.subsystem, lane_key.task_family) == ("sys1", "mood"):
                return WorkloadFamily.MOOD
            if (lane_key.subsystem, lane_key.task_family) == ("sys3", "direct"):
                return WorkloadFamily.CHAT_TOOLS
            if (lane_key.subsystem, lane_key.task_family) in {("bg", "reflect"), ("bg", "memory")}:
                return WorkloadFamily.MEMORY_GLOBAL_SUMMARY
            if (lane_key.subsystem, lane_key.task_family) in {("sys2", "followup"), ("sys2", "goal"), ("sys2", "expression"), ("sys2", "retrieval")}:
                return WorkloadFamily.CHAT_DIALOG
            if (lane_key.subsystem, lane_key.task_family) == ("sys2", "persona"):
                return WorkloadFamily.PERSONA_SUMMARY
            if (lane_key.subsystem, lane_key.task_family) in {("bg", "proactive"), ("bg", "wakeup"), ("bg", "diary"), ("bg", "group_signin")}:
                return WorkloadFamily.PROACTIVE_GENERATION
            if (lane_key.subsystem, lane_key.task_family) == ("bg", "profile"):
                return WorkloadFamily.PROFILE_GENERATION
            if (lane_key.subsystem, lane_key.task_family) == ("bg", "dream"):
                return WorkloadFamily.DREAM_GENERATION
            if (lane_key.subsystem, lane_key.task_family) == ("bg", "memory"):
                return WorkloadFamily.MEMORY_GLOBAL_SUMMARY
            if (lane_key.subsystem, lane_key.task_family) == ("bg", "compaction"):
                return WorkloadFamily.COMPACTION_SUMMARY
        if pool_name == "vision":
            return WorkloadFamily.VISION
        if pool_name == "task":
            return WorkloadFamily.DATA_PROCESS
        if pool_name == "agent":
            return WorkloadFamily.CHAT_TOOLS
        return WorkloadFamily.CHAT_DIALOG

    def resolve_policy(self, request: WorkloadRequest) -> WorkloadPolicy:
        family = request.family
        cache_priority = family in self.CACHE_PRIORITY_FAMILIES
        freshness_priority = family in {WorkloadFamily.CHAT_DIALOG, WorkloadFamily.CHAT_TOOLS}
        lane_scope_id = self._resolve_scope_id(request)
        lane_scope_kind = request.lane_key.scope_kind if request.lane_key else request.scope_kind
        stable_prefix_text = str(request.stable_prefix_text or request.system_prompt or "")
        dynamic_payload_text = str(request.dynamic_payload_text or request.prompt or "")
        stable_prefix_hash = self._hash_prefix(
            template_id=request.template_id,
            template_version=request.template_version,
            schema_id=request.schema_id,
            persona_core_version=request.persona_core_version,
            stable_prefix_text=stable_prefix_text,
        )
        effective_prefix_hash = stable_prefix_hash if cache_priority else (request.prefix_hash or stable_prefix_hash)
        primary_model = request.models[0] if request.models else ""
        lane_prompt_identity = self._lane_prompt_identity(request)
        rotation_scope_key = self._rotation_scope_key(family, lane_scope_kind, lane_scope_id)
        synthetic_lane_rotate_reason = (
            self._synthetic_lane_rotate_reason(rotation_scope_key, request, lane_prompt_identity)
            if cache_priority
            else ""
        )
        lane_key = self._normalize_lane_key(request, lane_scope_id, lane_scope_kind, lane_prompt_identity)
        sticky_key = f"{family.value}:{lane_scope_kind}:{lane_scope_id}:{request.template_id}:{request.template_version}"
        cache_affinity_enabled = bool(cache_priority and stable_prefix_text and primary_model)
        cache_affinity_reason = self._cache_affinity_reason(
            family=family,
            cache_affinity_enabled=cache_affinity_enabled,
            freshness_priority=freshness_priority,
            lane_scope_id=lane_scope_id,
            lane_scope_kind=lane_scope_kind,
            template_id=request.template_id,
        )
        provider_cache_affinity_class = "cache_priority" if cache_priority else "freshness_priority"
        return WorkloadPolicy(
            family=family,
            pool_name=request.pool_name,
            cache_priority=cache_priority,
            freshness_priority=freshness_priority,
            sticky_model=cache_priority,
            use_provider_session=lane_key is not None,
            use_cache_hint=bool(stable_prefix_text),
            lane_key=lane_key,
            lane_scope_id=lane_scope_id,
            lane_scope_kind=lane_scope_kind,
            template_id=request.template_id,
            template_version=request.template_version,
            schema_id=request.schema_id,
            persona_core_version=request.persona_core_version,
            stable_prefix_text=stable_prefix_text,
            dynamic_payload_text=dynamic_payload_text,
            stable_prefix_hash=stable_prefix_hash,
            effective_prefix_hash=effective_prefix_hash,
            lane_prompt_identity=lane_prompt_identity,
            primary_model=primary_model,
            sticky_key=sticky_key,
            cache_affinity_enabled=cache_affinity_enabled,
            cache_affinity_reason=cache_affinity_reason,
            provider_cache_affinity_class=provider_cache_affinity_class,
            rotation_scope_key=rotation_scope_key,
            synthetic_lane_rotated=bool(synthetic_lane_rotate_reason),
            synthetic_lane_rotate_reason=synthetic_lane_rotate_reason,
        )

    def build_provider_session_id(
        self,
        *,
        lane_manager: Optional[LaneManager],
        lane_umo: str,
        provider_family: str,
        policy: WorkloadPolicy,
    ) -> str:
        if not lane_manager or not policy.use_provider_session:
            return ""
        return lane_manager.get_remote_session_id(lane_umo, provider_family)

    def build_trace(
        self,
        *,
        policy: WorkloadPolicy,
        lane_umo: str = "",
        actual_model: str = "",
        fallback_used: bool = False,
        lane_rotated: bool = False,
        lane_rotate_reason: str = "",
        provider_family: str = "",
        provider_session_id: str = "",
        provider_session_enabled: bool = False,
        provider_cache_hint_enabled: bool = False,
    ) -> WorkloadTrace:
        combined_lane_rotated = bool(lane_rotated or policy.synthetic_lane_rotated)
        combined_lane_rotate_reason = self._merge_rotate_reasons(
            lane_rotate_reason,
            policy.synthetic_lane_rotate_reason,
        )
        return WorkloadTrace(
            workload_family=policy.family.value,
            lane_umo=lane_umo,
            lane_scope_id=policy.lane_scope_id,
            prefix_hash=policy.stable_prefix_hash,
            template_id=policy.template_id,
            template_version=policy.template_version,
            schema_id=policy.schema_id,
            primary_model=policy.primary_model,
            actual_model=actual_model,
            fallback_used=bool(fallback_used),
            lane_rotated=combined_lane_rotated,
            lane_rotate_reason=combined_lane_rotate_reason,
            provider_family=str(provider_family or ""),
            provider_session_enabled=bool(provider_session_enabled),
            provider_session_id=str(provider_session_id or ""),
            provider_cache_hint_enabled=bool(provider_cache_hint_enabled),
            provider_cache_affinity_class=policy.provider_cache_affinity_class,
            cache_affinity_enabled=policy.cache_affinity_enabled,
            cache_affinity_reason=policy.cache_affinity_reason,
            stable_prefix_length=len(policy.stable_prefix_text),
            dynamic_payload_length=len(policy.dynamic_payload_text),
            template_schema_id=policy.schema_id,
            rotation_scope_key=policy.rotation_scope_key,
            lane_prompt_identity=policy.lane_prompt_identity,
            persona_core_version=policy.persona_core_version,
        )

    def record_trace(self, trace: WorkloadTrace) -> None:
        metrics = self._metrics[trace.workload_family]
        self._accumulate_metrics(metrics, trace)
        template_key = self._template_metric_key(trace)
        self._accumulate_metrics(self._template_metrics[template_key], trace)
        if trace.rotation_scope_key:
            self._lane_identity_state[trace.rotation_scope_key] = {
                "template_id": trace.template_id,
                "template_version": trace.template_version,
                "schema_id": trace.schema_id,
                "persona_core_version": trace.persona_core_version,
                "lane_prompt_identity": trace.lane_prompt_identity,
            }

    def snapshot_metrics(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for family, metrics in self._metrics.items():
            result[family] = self._snapshot_metric(metrics)
        result["_templates"] = {
            key: self._snapshot_metric(metrics)
            for key, metrics in sorted(self._template_metrics.items())
        }
        return result

    def _resolve_scope_id(self, request: WorkloadRequest) -> str:
        if request.family == WorkloadFamily.PERSONA_SUMMARY:
            return request.persona_id or request.scope_id or (request.lane_key.scope_id if request.lane_key else "") or "global"
        if request.family in {
            WorkloadFamily.MEMORY_TOPIC_SUMMARY,
            WorkloadFamily.MEMORY_GLOBAL_SUMMARY,
            WorkloadFamily.DREAM_GENERATION,
        }:
            return request.scope_id or (request.lane_key.scope_id if request.lane_key else "") or "global"
        if request.family == WorkloadFamily.PROFILE_GENERATION:
            return request.scope_id or request.persona_id or (request.lane_key.scope_id if request.lane_key else "") or "global"
        if request.family == WorkloadFamily.PROACTIVE_GENERATION:
            return request.scope_id or (request.lane_key.scope_id if request.lane_key else "") or "global"
        if request.family == WorkloadFamily.COMPACTION_SUMMARY:
            return request.scope_id or (request.lane_key.scope_id if request.lane_key else "") or "global"
        if request.family == WorkloadFamily.VISION:
            return request.scope_id or (request.lane_key.scope_id if request.lane_key else "") or "global"
        if request.lane_key:
            return request.lane_key.scope_id
        return request.scope_id or "global"

    def _normalize_lane_key(
        self,
        request: WorkloadRequest,
        lane_scope_id: str,
        lane_scope_kind: str,
        lane_prompt_identity: str,
    ) -> Optional[LaneKey]:
        if request.lane_key is None:
            return None
        normalized_prompt_version = (
            lane_prompt_identity
            if request.family in self.CACHE_PRIORITY_FAMILIES
            else request.lane_key.prompt_version
        )
        return replace(
            request.lane_key,
            scope_id=lane_scope_id,
            scope_kind=lane_scope_kind,
            prompt_version=normalized_prompt_version,
        )

    @staticmethod
    def _lane_prompt_identity(request: WorkloadRequest) -> str:
        base = f"{request.template_id}:{request.template_version}:{request.schema_id or 'text'}"
        if request.family == WorkloadFamily.PERSONA_SUMMARY and request.persona_core_version:
            return f"{base}:{request.persona_core_version}"
        return base

    @staticmethod
    def _template_metric_key(trace: WorkloadTrace) -> str:
        return f"{trace.template_id}@{trace.template_version}"

    def _cache_affinity_reason(
        self,
        *,
        family: WorkloadFamily,
        cache_affinity_enabled: bool,
        freshness_priority: bool,
        lane_scope_id: str,
        lane_scope_kind: str,
        template_id: str,
    ) -> str:
        if family in self.GLOBAL_SCOPE_DIAGNOSTIC_FAMILIES and lane_scope_id == "global":
            logger.warning(
                "[ContextEconomy] cache-priority workload %s fell back to global scope; "
                "session affinity may be degraded (scope_kind=%s template=%s)",
                family.value,
                lane_scope_kind or "global",
                template_id or "unknown",
            )
            return "global_scope_fallback"
        if cache_affinity_enabled:
            return "cache_priority_stable_shell"
        if freshness_priority:
            return "freshness_priority"
        return "dynamic_or_model_missing"

    @staticmethod
    def _accumulate_metrics(metrics: WorkloadMetrics, trace: WorkloadTrace) -> None:
        metrics.call_count += 1
        metrics.lane_rotate_count += int(bool(trace.lane_rotated))
        metrics.fallback_count += int(bool(trace.fallback_used))
        metrics.primary_hits += int(bool(trace.actual_model and trace.actual_model == trace.primary_model))
        metrics.provider_session_uses += int(bool(trace.provider_session_enabled))
        if trace.provider_session_id:
            if trace.provider_session_id in metrics.seen_provider_session_ids:
                metrics.provider_session_reused += 1
            else:
                metrics.seen_provider_session_ids.add(trace.provider_session_id)
        metrics.cache_affinity_ready += int(bool(trace.cache_affinity_enabled))
        metrics.stable_prefix_length_total += int(trace.stable_prefix_length or 0)
        metrics.dynamic_payload_length_total += int(trace.dynamic_payload_length or 0)
        metrics.workload_families[trace.workload_family] = metrics.workload_families.get(trace.workload_family, 0) + 1
        if trace.actual_model:
            metrics.actual_models[trace.actual_model] = metrics.actual_models.get(trace.actual_model, 0) + 1
        if trace.lane_rotate_reason:
            for reason in ContextEconomyCenter._split_rotate_reasons(trace.lane_rotate_reason):
                metrics.rotate_reasons[reason] = metrics.rotate_reasons.get(reason, 0) + 1

    @staticmethod
    def _snapshot_metric(metrics: WorkloadMetrics) -> dict:
        call_count = max(metrics.call_count, 1)
        return {
            "call_count": metrics.call_count,
            "lane_rotate_count": metrics.lane_rotate_count,
            "fallback_count": metrics.fallback_count,
            "primary_hit_rate": round(metrics.primary_hits / call_count, 4),
            "provider_session_usage_rate": round(metrics.provider_session_uses / call_count, 4),
            "provider_session_reuse_rate": round(metrics.provider_session_reused / call_count, 4),
            "cache_affinity_ready_rate": round(metrics.cache_affinity_ready / call_count, 4),
            "avg_stable_prefix_length": round(metrics.stable_prefix_length_total / call_count, 2),
            "avg_dynamic_payload_length": round(metrics.dynamic_payload_length_total / call_count, 2),
            "actual_models": dict(metrics.actual_models),
            "rotate_reasons": dict(metrics.rotate_reasons),
            "workload_families": dict(metrics.workload_families),
        }

    @staticmethod
    def _rotation_scope_key(
        family: WorkloadFamily,
        lane_scope_kind: str,
        lane_scope_id: str,
    ) -> str:
        return f"{family.value}:{lane_scope_kind}:{lane_scope_id}"

    def _synthetic_lane_rotate_reason(
        self,
        rotation_scope_key: str,
        request: WorkloadRequest,
        lane_prompt_identity: str,
    ) -> str:
        previous = self._lane_identity_state.get(rotation_scope_key)
        if not previous:
            return ""
        if previous.get("lane_prompt_identity") == lane_prompt_identity:
            return ""
        reasons: list[str] = []
        if previous.get("template_id") != request.template_id:
            reasons.append("template_changed")
        if previous.get("template_version") != request.template_version:
            reasons.append("template_version_changed")
        if previous.get("schema_id") != request.schema_id:
            reasons.append("schema_changed")
        if previous.get("persona_core_version") != request.persona_core_version:
            reasons.append("persona_core_version_changed")
        return ",".join(reasons or ["template_version_changed"])

    @staticmethod
    def _merge_rotate_reasons(*reasons: str) -> str:
        merged: list[str] = []
        for raw in reasons:
            for part in ContextEconomyCenter._split_rotate_reasons(raw):
                if part not in merged:
                    merged.append(part)
        return ",".join(merged)

    @staticmethod
    def _split_rotate_reasons(raw: str) -> list[str]:
        parts: list[str] = []
        for part in str(raw or "").split(","):
            clean = part.strip()
            if clean and clean not in parts:
                parts.append(clean)
        return parts

    @staticmethod
    def _hash_prefix(
        *,
        template_id: str,
        template_version: str,
        schema_id: str,
        persona_core_version: str,
        stable_prefix_text: str,
    ) -> str:
        payload = "\n".join(
            [
                str(template_id or ""),
                str(template_version or ""),
                str(schema_id or ""),
                str(persona_core_version or ""),
                str(stable_prefix_text or ""),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
