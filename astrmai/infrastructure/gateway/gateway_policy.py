import asyncio
import re
import time
from time import monotonic
from typing import Any, Callable, Dict, List, Optional

from ..context_economy import WorkloadPolicy

from ..runtime.runtime_contracts import FailureKind


class GatewayPolicyMixin:
    def _cooldown_key(self, pool_name: str, model_id: str) -> tuple[str, str]:
        return str(pool_name or ""), str(model_id or "")

    @staticmethod
    def _provider_id(model_id: str) -> str:
        normalized = str(model_id or "").strip()
        return normalized.split("/", 1)[0] if "/" in normalized else normalized

    def _cleanup_model_cooldowns(self) -> None:
        now = monotonic()
        cooldowns = getattr(self, "_model_cooldowns", {})
        for key, meta in list(cooldowns.items()):
            if float(meta.get("until", 0.0) or 0.0) <= now:
                cooldowns.pop(key, None)
        health = getattr(self, "_model_health", {})
        for key, meta in list(health.items()):
            cooldown_until = float(meta.get("cooldown_until", 0.0) or 0.0)
            if cooldown_until and cooldown_until <= now and meta.get("status") == "cooldown":
                meta["status"] = "half_open"
                meta["half_open_probe"] = False

    def _model_cooldown_meta(self, pool_name: str, model_id: str) -> Dict[str, Any]:
        self._cleanup_model_cooldowns()
        cooldowns = getattr(self, "_model_cooldowns", {})
        exact = cooldowns.get(self._cooldown_key(pool_name, model_id), {})
        provider = cooldowns.get(self._cooldown_key("__provider__", self._provider_id(model_id)), {})
        candidates = [item for item in (exact, provider) if item]
        if not candidates:
            return {}
        return dict(max(candidates, key=lambda item: float(item.get("until", 0.0) or 0.0)))

    @staticmethod
    def _extract_http_status(error: Any = None) -> int | None:
        """Prefer a structured provider status over text matching."""
        candidates: List[Any] = []
        if isinstance(error, dict):
            candidates.extend(error.get(key) for key in ("status_code", "http_status", "status"))
            response = error.get("response")
            if isinstance(response, dict):
                candidates.extend(response.get(key) for key in ("status_code", "status"))
        else:
            candidates.extend(getattr(error, key, None) for key in ("status_code", "http_status", "status"))
            response = getattr(error, "response", None)
            if isinstance(response, dict):
                candidates.extend(response.get(key) for key in ("status_code", "status"))
            elif response is not None:
                candidates.extend(getattr(response, key, None) for key in ("status_code", "status"))
        for value in candidates:
            try:
                status = int(value)
            except (TypeError, ValueError):
                continue
            if 100 <= status <= 599:
                return status
        return None

    def _classify_cooldown_reason(self, error_message: str, error: Any = None) -> str:
        status = self._extract_http_status(error)
        text_status_match = re.search(
            r"(?:http\s*(?:status|code)?|status|error\s*code)\s*[:=]?\s*(401|403|404|408|429|500|502|503|504)\b",
            str(error_message or ""),
            re.IGNORECASE,
        )
        text_status = int(text_status_match.group(1)) if text_status_match else None
        if status is not None:
            if 500 <= status <= 599:
                return "server_error"
            if status == 429:
                return "rate_limit"
            if status in (401, 403, 404):
                return "provider_permission_denied"
        elif text_status is not None:
            if 500 <= text_status <= 599:
                return "server_error"
            if text_status == 429:
                return "rate_limit"
            if text_status == 401:
                return "provider_permission_denied"
        lowered = str(error_message or "").lower()
        if any(keyword in lowered for keyword in ("internal server error", "bad gateway", "service unavailable", "gateway timeout")):
            return "server_error"
        if any(keyword in lowered for keyword in ("429", "rate limit", "ratelimit", "too many requests")):
            return "rate_limit"
        if any(keyword in lowered for keyword in ("usage limit", "quota", "billing cycle")):
            return "quota_exhausted"
        if text_status in (403, 404):
            return "provider_permission_denied"
        if any(keyword in lowered for keyword in ("unauthorized", "authentication failed", "invalid api key", "permissiondenied", "permission denied")):
            return "provider_permission_denied"
        return ""

    def _server_error_status(self, pool_name: str, model_id: str) -> Dict[str, Any]:
        self._cleanup_model_cooldowns()
        health = getattr(self, "_model_health", {})
        return health.setdefault(
            self._cooldown_key(pool_name, model_id),
            {
                "provider_id": self._provider_id(model_id),
                "consecutive_5xx": 0,
                "last_5xx_at": 0.0,
                "status": "healthy",
                "half_open_probe": False,
                "last_error_type": "",
                "skipped_requests": 0,
            },
        )

    def _record_server_error(self, pool_name: str, model_id: str) -> Dict[str, Any]:
        if not model_id:
            return {}
        meta = self._server_error_status(pool_name, model_id)
        now = monotonic()
        window = max(1, int(getattr(self.settings, "server_error_window_sec", 60) or 60))
        if now - float(meta.get("last_5xx_at", 0.0) or 0.0) > window:
            meta["consecutive_5xx"] = 0
        meta["consecutive_5xx"] = int(meta.get("consecutive_5xx", 0) or 0) + 1
        meta["last_5xx_at"] = now
        meta["last_error_type"] = "server_error"
        meta["cooldown_source"] = "server_error_health"
        threshold = max(1, int(getattr(self.settings, "server_error_failure_threshold", 2) or 2))
        if meta["consecutive_5xx"] >= threshold:
            duration = max(0, int(getattr(self.settings, "server_error_model_cooldown_sec", 300) or 300))
            if duration > 0:
                meta.update(
                    {
                        "status": "cooldown",
                        "cooldown_until": now + duration,
                        "cooldown_reason": "server_error",
                        "duration_sec": duration,
                        "half_open_probe": False,
                    }
                )
        return dict(meta)

    def _record_model_success(self, pool_name: str, model_id: str) -> None:
        health = getattr(self, "_model_health", {})
        meta = health.get(self._cooldown_key(pool_name, model_id))
        if meta:
            meta.update({"consecutive_5xx": 0, "status": "healthy", "cooldown_until": 0.0, "half_open_probe": False})

    def _open_model_cooldown(self, pool_name: str, model_id: str, error_message: str, error: Any = None) -> Dict[str, Any]:
        reason = self._classify_cooldown_reason(error_message, error=error)
        if not reason or not model_id:
            return {}
        if reason == "server_error":
            return self._record_server_error(pool_name, model_id)
        if reason == "rate_limit":
            duration = int(getattr(self.settings, "rate_limit_model_cooldown_sec", 120) or 120)
        else:
            duration = int(getattr(self.settings, "quota_model_cooldown_sec", 1800) or 1800)
        if duration <= 0:
            return {}
        meta = {
            "until": monotonic() + duration,
            "reason": reason,
            "duration_sec": duration,
            "provider_id": self._provider_id(model_id),
        }
        cooldowns = getattr(self, "_model_cooldowns", {})
        cooldowns[self._cooldown_key(pool_name, model_id)] = meta
        provider_id = self._provider_id(model_id)
        if provider_id:
            cooldowns[self._cooldown_key("__provider__", provider_id)] = dict(meta)
        return dict(meta)

    def _is_model_cooldown(self, pool_name: str, model_id: str) -> bool:
        """查询模型是否处于冷却期（由 GatewayPolicy._model_cooldowns 统一管理）。"""
        health = self._server_error_status(pool_name, model_id)
        if health.get("status") == "cooldown":
            return True
        meta = self._model_cooldown_meta(pool_name, model_id)
        return bool(meta)

    def _filter_cooldown_attempt_queue(
        self,
        pool_name: str,
        primary_models: List[str],
        attempt_queue: List[str],
        *,
        allow_override: bool = True,
    ) -> tuple[List[str], List[Dict[str, Any]], bool]:
        self._cleanup_model_cooldowns()
        available: List[str] = []
        skipped: List[Dict[str, Any]] = []
        for model_id in attempt_queue:
            report_pool = pool_name if model_id in primary_models else "fallback"
            health = self._server_error_status(report_pool, model_id)
            if health.get("status") == "cooldown":
                health["skipped_requests"] = int(health.get("skipped_requests", 0) or 0) + 1
                skipped.append({"pool_name": report_pool, "model_id": model_id, "cooldown_until": health.get("cooldown_until", 0.0), "cooldown_reason": "server_error"})
                continue
            if health.get("status") == "half_open":
                if health.get("half_open_probe"):
                    health["skipped_requests"] = int(health.get("skipped_requests", 0) or 0) + 1
                    skipped.append({"pool_name": report_pool, "model_id": model_id, "cooldown_until": health.get("cooldown_until", 0.0), "cooldown_reason": "server_error_half_open_busy"})
                    continue
                health["half_open_probe"] = True
                available.append(model_id)
                continue
            meta = self._model_cooldown_meta(report_pool, model_id)
            if meta:
                health["skipped_requests"] = int(health.get("skipped_requests", 0) or 0) + 1
                skipped.append(
                    {
                        "pool_name": report_pool,
                        "model_id": model_id,
                        "cooldown_until": meta.get("until", 0.0),
                        "cooldown_reason": meta.get("reason", ""),
                    }
                )
            else:
                available.append(model_id)
        if available or not attempt_queue:
            return available, skipped, False
        if not allow_override:
            return [], skipped, False
        if skipped and all(
            str(item.get("cooldown_reason", "") or "").startswith("server_error")
            for item in skipped
        ):
            return [], skipped, False
        earliest = min(skipped, key=lambda item: float(item.get("cooldown_until", 0.0) or 0.0))
        return [str(earliest.get("model_id", "") or attempt_queue[0])], skipped, True

    def _build_attempt_queue(
        self,
        pool_name: str,
        models: List[str],
        use_fallback: bool,
        workload_policy: Optional[WorkloadPolicy] = None,
    ) -> tuple[List[str], List[str]]:
        sticky_key = workload_policy.sticky_key if workload_policy and workload_policy.sticky_model else ""
        sticky_preferred = workload_policy.primary_model if workload_policy else ""
        primary_models = self.router.get_ranked_models(
            pool_name,
            models,
            sticky_key=sticky_key,
            sticky_preferred=sticky_preferred,
            cooldown_checker=self._is_model_cooldown,
        )
        attempt_queue = primary_models.copy()
        if use_fallback:
            fallback_models = self.router.get_ranked_models("fallback", self._fallback_models())
            attempt_queue.extend(model_id for model_id in fallback_models if model_id not in attempt_queue)
        return primary_models, attempt_queue

    def _build_request_kwargs(
        self,
        *,
        model_id: str,
        system_prompt: str,
        image_urls: Optional[List[str]],
        request_kwargs: Optional[Dict[str, Any]],
        request_kwargs_factory: Optional[Callable[[str], Dict[str, Any]]],
    ) -> Dict[str, Any]:
        llm_kwargs = dict(request_kwargs or {})
        if request_kwargs_factory:
            llm_kwargs.update(request_kwargs_factory(model_id) or {})
        if system_prompt:
            llm_kwargs["system_prompt"] = system_prompt
        if image_urls:
            llm_kwargs["image_urls"] = list(image_urls)
        return llm_kwargs

    def _classify_failure_kind(self, error_message: str, error: Exception | None = None) -> FailureKind:
        if error is not None and isinstance(error, asyncio.TimeoutError):
            if "turn_deadline_exhausted" in str(error_message or ""):
                return FailureKind.TURN_BUDGET_EXHAUSTED
            return FailureKind.TIMEOUT
        lowered = str(error_message).lower()
        if "empty_response" in lowered:
            return FailureKind.EMPTY_RESPONSE
        if "provider_failure_text" in lowered:
            return FailureKind.PROVIDER_FAILURE_TEXT
        if "unsafe_or_empty_text" in lowered:
            return FailureKind.UNSAFE_OR_EMPTY_TEXT
        if "prompt_scaffold_text" in lowered:
            return FailureKind.PROMPT_SCAFFOLD_TEXT
        if "tool_protocol_text" in lowered:
            return FailureKind.TOOL_PROTOCOL_TEXT
        if "json" in lowered:
            return FailureKind.JSON_DECODE_ERROR
        if "timeout" in lowered:
            return FailureKind.TIMEOUT
        if "turn_deadline_exhausted" in lowered or "turn_budget_exhausted" in lowered:
            return FailureKind.TURN_BUDGET_EXHAUSTED
        if "payload" in lowered or "validation error" in lowered:
            return FailureKind.BAD_PAYLOAD
        return FailureKind.UNKNOWN

    def _is_fatal_failure(self, error_message: str, error: Exception | None = None) -> bool:
        if error is not None and isinstance(error, asyncio.TimeoutError):
            return False  # 客户端超时不致命，应重试
        status = self._extract_http_status(error)
        if status is not None:
            return status in (401, 403, 404, 408, 429)
        lowered = str(error_message).lower()
        if re.search(
            r"(?:http\s*(?:status|code)?|status|error\s*code)\s*[:=]?\s*(401|403|404|408|429)\b",
            lowered,
            re.IGNORECASE,
        ):
            return True
        fatal_keywords = (
            "unauthorized",
            "authentication failed",
            "invalid api key",
            "ratelimit",
            "rate limit",
            "too many requests",
            "permissiondenied",
            "permission denied",
            "usage limit",
            "quota",
            "billing cycle",
            "invalid_request_error",
            "apitimeouterror",
            "request timed out",
            "timed out",
            # OPT-09/TG-02: 配置漂移的 provider（生产实证 openai/deepseek-v4-pro
            # 不存在）永远不可能成功，必须单次尝试即切下一模型，不得 backoff 空转
            "没有找到",
            "providernotfounderror",
            "provider not found",
            "not found",
            "unknown provider",
        )
        return any(keyword in lowered for keyword in fatal_keywords) or "content=none" in lowered
