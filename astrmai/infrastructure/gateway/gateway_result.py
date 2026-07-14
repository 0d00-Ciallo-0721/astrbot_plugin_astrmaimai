import hashlib
import json
import re
from typing import Any, Dict, List, Optional

from astrbot.api import logger

from ...conversation.contracts.reply_artifact import VisibleReplyArtifact
from ..runtime.runtime_contracts import FailureKind, LLMCallResult
from .output_guard import is_safe_visible_text
from .provider_capabilities import resolve_provider_capabilities

try:
    from json_repair import repair_json
except ImportError:
    repair_json = None


class GatewayResultMixin:
    def _provider_capabilities(self, provider_id: str):
        return resolve_provider_capabilities(getattr(self, "context", None), provider_id)

    @staticmethod
    def _stable_hash_text(text: Any) -> str:
        normalized = str(text or "")
        if not normalized:
            return ""
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def _read_usage_field(self, usage: Any, *names: str) -> int:
        if usage is None:
            return 0
        for name in names:
            value = getattr(usage, name, None)
            if value is None and isinstance(usage, dict):
                value = usage.get(name)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return 0
        return 0

    def _has_usage_field(self, usage: Any, *names: str) -> bool:
        if usage is None:
            return False
        for name in names:
            value = getattr(usage, name, None)
            if value is None and isinstance(usage, dict):
                value = usage.get(name)
            if value is not None:
                return True
        return False

    def _extract_usage(self, resp: Any) -> Dict[str, int]:
        usage = getattr(resp, "usage", None)
        input_tokens = self._read_usage_field(usage, "input", "input_tokens", "prompt_tokens")
        input_cached = self._read_usage_field(
            usage,
            "input_cached",
            "cached_tokens",
            "cache_read_input_tokens",
        )
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        if prompt_details is None and isinstance(usage, dict):
            prompt_details = usage.get("prompt_tokens_details")
        nested_cached_supported = self._has_usage_field(
            prompt_details,
            "cached_tokens",
            "cache_read_input_tokens",
        )
        if not input_cached and nested_cached_supported:
            input_cached = self._read_usage_field(
                prompt_details,
                "cached_tokens",
                "cache_read_input_tokens",
            )
        output_tokens = self._read_usage_field(usage, "output", "output_tokens", "completion_tokens")
        cached_usage_supported = self._has_usage_field(
            usage,
            "input_cached",
            "cached_tokens",
            "cache_read_input_tokens",
        ) or nested_cached_supported
        return {
            "input_tokens": input_tokens,
            "input_cached": input_cached,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cached_usage_supported": bool(cached_usage_supported or input_cached > 0),
        }

    def _enrich_cache_debug_meta(
        self,
        debug_meta: Optional[Dict[str, Any]],
        *,
        workload_policy: Any = None,
        usage: Optional[Dict[str, int]] = None,
        provider_visible_hash_stable: bool = False,
    ) -> Dict[str, Any]:
        meta = dict(debug_meta or {})
        if workload_policy is not None:
            meta.setdefault("cache_affinity_enabled", bool(getattr(workload_policy, "cache_affinity_enabled", False)))
        if usage is not None:
            meta.setdefault("cached_usage_supported", bool((usage or {}).get("cached_usage_supported", False)))
        if provider_visible_hash_stable:
            meta["provider_visible_hash_stable"] = True
        return meta

    def _build_cache_observation(
        self,
        usage: Dict[str, int],
        debug_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        debug_meta = debug_meta or {}
        cache_ready_reasons: list[str] = []
        if str(debug_meta.get("request_cache_control", "") or "").strip():
            cache_ready_reasons.append("explicit_cache_hint")
        if str(debug_meta.get("request_session_id", "") or "").strip():
            cache_ready_reasons.append("session_reuse")
        if bool(debug_meta.get("prefix_stable", False)):
            cache_ready_reasons.append("semantic_system_hash_stable")
        if bool(debug_meta.get("provider_visible_hash_stable", False)):
            cache_ready_reasons.append("provider_visible_hash_stable")
        if bool(debug_meta.get("cache_affinity_enabled", False)):
            cache_ready_reasons.append("cache_affinity_enabled")
        cache_ready_reasons = list(dict.fromkeys(cache_ready_reasons))
        input_cached = int((usage or {}).get("input_cached", 0) or 0)
        cached_usage_supported = bool(debug_meta.get("cached_usage_supported", False) or input_cached > 0)
        return {
            "cache_ready": bool(cache_ready_reasons),
            "cache_hit": bool(input_cached > 0),
            "cache_ready_reasons": cache_ready_reasons,
            "cache_hit_evidence_supported": cached_usage_supported,
        }

    def _log_usage(
        self,
        pool_name: str,
        model_id: str,
        usage: Dict[str, int],
        debug_meta: Optional[Dict[str, Any]] = None,
        latency_ms: float = 0.0,
    ) -> None:
        debug_meta = debug_meta or {}
        input_tokens = usage.get("input_tokens", 0)
        input_cached = usage.get("input_cached", 0)
        cache_rate = (input_cached / input_tokens) if input_tokens else 0.0
        cache_observation = self._build_cache_observation(usage, debug_meta)
        logger.info(
            "[GatewayUsage] pool=%s model=%s provider=%s latency_ms=%.0f lane_key=%s conversation_id=%s prefix_hash=%s input_tokens=%s input_cached=%s output_tokens=%s cache_rate=%.4f cache_ready=%s cache_hit=%s cache_ready_reasons=%s",
            pool_name,
            model_id,
            debug_meta.get("provider", model_id),
            latency_ms,
            debug_meta.get("lane_key", ""),
            debug_meta.get("conversation_id", ""),
            debug_meta.get("prefix_hash", ""),
            input_tokens,
            input_cached,
            usage.get("output_tokens", 0),
            cache_rate,
            cache_observation.get("cache_ready", False),
            cache_observation.get("cache_hit", False),
            ",".join(cache_observation.get("cache_ready_reasons", []) or []),
        )

    def _build_success_result(
        self,
        *,
        text: str = "",
        parsed_json: Any = None,
        model_id: str = "",
        usage: Optional[Dict[str, int]] = None,
        economy: Optional[Dict[str, Any]] = None,
        skipped_cooldown_models: Optional[List[Dict[str, Any]]] = None,
        cooldown_overridden: bool = False,
    ) -> LLMCallResult:
        capabilities = self._provider_capabilities(model_id) if model_id else None
        return LLMCallResult(
            ok=True,
            text=text,
            parsed_json=parsed_json,
            model_id=model_id,
            provider_family=getattr(capabilities, "provider_family", ""),
            usage=usage or {},
            raw_completion=text,
            economy=dict(economy or {}),
            skipped_cooldown_models=list(skipped_cooldown_models or []),
            cooldown_overridden=bool(cooldown_overridden),
        )

    @staticmethod
    def _bot_speaker_names(nicknames: list) -> List[str]:
        names: List[str] = ["Bot"]
        if isinstance(nicknames, list):
            names.extend(str(name).strip() for name in nicknames if str(name).strip())
        return list(dict.fromkeys(names))

    def _build_failure_result(
        self,
        *,
        error_kind: FailureKind,
        error_message: str,
        model_id: str = "",
        raw_completion: str = "",
        economy: Optional[Dict[str, Any]] = None,
    ) -> LLMCallResult:
        capabilities = self._provider_capabilities(model_id) if model_id else None
        return LLMCallResult(
            ok=False,
            error_kind=error_kind,
            error_message=error_message,
            model_id=model_id,
            provider_family=getattr(capabilities, "provider_family", ""),
            raw_completion=raw_completion,
            economy=dict(economy or {}),
        )

    def _extract_json(self, text: str) -> str:
        normalized = (text or "").strip()
        if not normalized:
            return ""
        try:
            json.loads(normalized)
            return normalized
        except Exception:
            logger.debug("[AstrMai-result] json extraction failed", exc_info=True)
        match = re.search(r"```(?:json)?\s*(.*?)```", normalized, re.DOTALL | re.IGNORECASE)
        if match:
            extracted = match.group(1).strip()
            try:
                json.loads(extracted)
                return extracted
            except Exception:
                logger.debug("[AstrMai-result] json extraction failed", exc_info=True)
        return normalized

    def _parse_json_completion(self, content: str) -> Any:
        raw_json_str = self._extract_json(content)
        try:
            return json.loads(raw_json_str)
        except json.JSONDecodeError:
            if repair_json:
                repaired = repair_json(raw_json_str, return_objects=False)
                if isinstance(repaired, str):
                    return json.loads(repaired)
                if isinstance(repaired, (dict, list)):
                    return repaired
            raise ValueError(f"json_decode_error: {raw_json_str[:120]}")

    def _build_lane_artifact(self, result: LLMCallResult, persistable_text: str) -> VisibleReplyArtifact:
        return VisibleReplyArtifact(
            visible_text=result.text,
            segments=[result.text] if result.text else [],
            persistable_text=persistable_text if isinstance(persistable_text, str) else "",
            blocked_reason="" if (persistable_text and is_safe_visible_text(persistable_text)) else "unsafe_or_empty_assistant",
        )

    def _history_roles_tail(self, history: Optional[List[Any]]) -> List[str]:
        tail: List[str] = []
        for item in (history or [])[-4:]:
            if isinstance(item, dict):
                tail.append(str(item.get("role", "")))
        return tail
