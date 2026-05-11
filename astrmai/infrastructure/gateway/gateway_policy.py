from typing import Any, Callable, Dict, List, Optional

from ..runtime.runtime_contracts import FailureKind


class GatewayPolicyMixin:
    def _build_attempt_queue(self, pool_name: str, models: List[str], use_fallback: bool) -> tuple[List[str], List[str]]:
        primary_models = self.router.get_ranked_models(pool_name, models)
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

    def _classify_failure_kind(self, error_message: str) -> FailureKind:
        lowered = str(error_message).lower()
        if "empty_response" in lowered:
            return FailureKind.EMPTY_RESPONSE
        if "provider_failure_text" in lowered:
            return FailureKind.PROVIDER_FAILURE_TEXT
        if "json" in lowered:
            return FailureKind.JSON_DECODE_ERROR
        if "timeout" in lowered:
            return FailureKind.TIMEOUT
        if "payload" in lowered or "validation error" in lowered:
            return FailureKind.BAD_PAYLOAD
        return FailureKind.UNKNOWN

    def _is_fatal_failure(self, error_message: str) -> bool:
        lowered = str(error_message).lower()
        fatal_keywords = (
            "429",
            "ratelimit",
            "too many requests",
            "invalid_request_error",
            "apitimeouterror",
            "request timed out",
            "timeout",
        )
        return any(keyword in lowered for keyword in fatal_keywords) or "content=none" in lowered