from __future__ import annotations

import asyncio
import json
import os
import random
import re
from dataclasses import dataclass
from typing import Any

import aiohttp


@dataclass
class LiveCompletionResult:
    text: str
    usage_input_tokens: int = 0
    usage_input_cached: int = 0
    usage_output_tokens: int = 0
    cached_usage_supported: bool = False
    request_hint_payload: dict[str, Any] | None = None
    request_hint_kind: str = ""
    request_session_id: str = ""
    raw_provider_family: str = ""
    raw_model_id: str = ""
    raw_response: dict[str, Any] | None = None


class LiveProviderRequestError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, retry_after: float = 0.0, retryable: bool = True):
        super().__init__(message)
        self.status = int(status or 0)
        self.retry_after = max(0.0, float(retry_after or 0.0))
        self.retryable = bool(retryable)


class BaseLiveProviderClient:
    provider_family = ""
    supports_usage_reporting = False
    supports_cache_hint = False
    supports_session_id = False
    DEFAULT_MAX_RETRIES = 3
    DEFAULT_RETRY_BACKOFF_SECONDS = 1.25

    def __init__(self, *, api_key: str, model: str, timeout: float):
        self.api_key_pool = self._split_api_keys(api_key)
        self.api_key = self.api_key_pool[0] if self.api_key_pool else ""
        self.model = str(model or "").strip()
        self.timeout = float(timeout or 20.0)
        self._session: aiohttp.ClientSession | None = None
        self.max_retries = max(1, int(str(os.getenv("MAIN_REPLY_LIVE_MAX_RETRIES", self.DEFAULT_MAX_RETRIES) or self.DEFAULT_MAX_RETRIES)))
        self.retry_backoff_seconds = max(
            0.1,
            float(str(os.getenv("MAIN_REPLY_LIVE_RETRY_BACKOFF_SECONDS", self.DEFAULT_RETRY_BACKOFF_SECONDS) or self.DEFAULT_RETRY_BACKOFF_SECONDS)),
        )

    @staticmethod
    def _split_api_keys(raw: str) -> list[str]:
        normalized = str(raw or "").strip()
        if not normalized:
            return []
        return [token for token in re.split(r"[\s,;]+", normalized) if token]

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            trust_env=True,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._session is not None:
            await self._session.close()

    async def complete(self, system_prompt: str, prompt: str, *, request_label: str, tools_notice: str = "") -> LiveCompletionResult:
        raise NotImplementedError

    @staticmethod
    def _parse_retry_after(value: Any) -> float:
        try:
            return max(0.0, float(str(value or "").strip()))
        except (TypeError, ValueError):
            return 0.0

    def _compute_retry_delay(self, attempt_index: int, retry_after: float = 0.0) -> float:
        exponential_delay = self.retry_backoff_seconds * (2 ** max(0, int(attempt_index)))
        jitter = random.uniform(0.0, 0.25)
        return max(float(retry_after or 0.0), exponential_delay) + jitter

    @staticmethod
    def _error_message_from_payload(payload: Any, fallback: str = "") -> str:
        if isinstance(payload, dict):
            if isinstance(payload.get("error"), dict):
                error = payload.get("error") or {}
                return str(error.get("message") or error.get("status") or fallback or "").strip()
            if payload.get("message"):
                return str(payload.get("message") or "").strip()
        return str(fallback or "").strip()

    async def _post_json(self, url: str, *, headers: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError(f"{self.__class__.__name__} session is not open")
        async with self._session.post(url, headers=headers, json=body) as resp:
            raw_text = await resp.text()
            try:
                payload = json.loads(raw_text) if raw_text else {}
            except Exception:
                payload = {"raw_text": raw_text}
            if 200 <= resp.status < 300:
                return payload if isinstance(payload, dict) else {"value": payload}
            retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
            message = self._error_message_from_payload(payload, raw_text)
            retryable = resp.status in {408, 409, 425, 429, 500, 502, 503, 504}
            raise LiveProviderRequestError(
                f"{self.provider_family or 'provider'} request failed with status {resp.status}: {message[:240]}",
                status=resp.status,
                retry_after=retry_after,
                retryable=retryable,
            )

    async def _request_with_retries(
        self,
        request_label: str,
        attempt_coro,
        *,
        rotate_api_keys: bool = False,
    ):
        key_candidates = list(self.api_key_pool or ([self.api_key] if self.api_key else []))
        if not key_candidates:
            key_candidates = [""]
        max_attempts = max(len(key_candidates), self.max_retries) if rotate_api_keys else self.max_retries
        last_error: Exception | None = None
        for attempt_index in range(max_attempts):
            current_key = key_candidates[attempt_index % len(key_candidates)]
            self.api_key = current_key
            try:
                return await attempt_coro(current_key, attempt_index)
            except LiveProviderRequestError as exc:
                last_error = exc
                if attempt_index >= max_attempts - 1 or not exc.retryable:
                    raise
                await asyncio.sleep(self._compute_retry_delay(attempt_index, exc.retry_after))
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt_index >= max_attempts - 1:
                    raise LiveProviderRequestError(
                        f"{self.provider_family or 'provider'} request failed after retries during {request_label}: {type(exc).__name__}",
                        retryable=True,
                    ) from exc
                await asyncio.sleep(self._compute_retry_delay(attempt_index, 0.0))
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"{self.provider_family or 'provider'} request failed without attempts")


class MoonshotLiveClient(BaseLiveProviderClient):
    provider_family = "kimi"
    supports_usage_reporting = False
    supports_cache_hint = False
    supports_session_id = False

    def __init__(self, *, api_key: str, model: str, timeout: float):
        super().__init__(api_key=api_key, model=model, timeout=timeout)
        self._client = None

    async def __aenter__(self):
        from tests.manual.kimi_replay_acceptance import KimiClient

        self._client = KimiClient(
            api_key=self.api_key,
            model=self.model,
            timeout=self.timeout,
            max_calls=200,
            max_tokens=160,
            temperature=0.0,
            rpm_limit=20,
            rpm_safety_margin=1,
            concurrency_limit=1,
        )
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._client is not None:
            await self._client.__aexit__(exc_type, exc, tb)

    async def complete(self, system_prompt: str, prompt: str, *, request_label: str, tools_notice: str = "") -> LiveCompletionResult:
        payload = await self._client.chat_completion(
            messages=[
                {"role": "system", "content": system_prompt or "You are AstrMai."},
                {"role": "user", "content": (prompt or "Reply briefly.") + (tools_notice or "")},
            ],
            request_label=request_label,
        )
        choice = (((payload or {}).get("choices") or [{}])[0] or {}).get("message") or {}
        return LiveCompletionResult(
            text=str(choice.get("content") or "").strip(),
            usage_input_tokens=0,
            usage_input_cached=0,
            usage_output_tokens=0,
            cached_usage_supported=False,
            raw_provider_family=self.provider_family,
            raw_model_id=self.model,
            raw_response=payload if isinstance(payload, dict) else {},
        )


class AnthropicLiveClient(BaseLiveProviderClient):
    provider_family = "anthropic"
    supports_usage_reporting = True
    supports_cache_hint = True
    supports_session_id = False
    BASE_URL = "https://api.anthropic.com/v1/messages"

    @staticmethod
    def _build_result_from_payload(payload: dict[str, Any], model: str) -> LiveCompletionResult:
        usage = dict((payload or {}).get("usage", {}) or {})
        text_parts = [
            str(item.get("text") or "")
            for item in list((payload or {}).get("content", []) or [])
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return LiveCompletionResult(
            text="".join(text_parts).strip(),
            usage_input_tokens=int(usage.get("input_tokens", 0) or 0),
            usage_input_cached=int(usage.get("cache_read_input_tokens", 0) or 0),
            usage_output_tokens=int(usage.get("output_tokens", 0) or 0),
            cached_usage_supported=True,
            request_hint_payload={"type": "ephemeral"},
            request_hint_kind="anthropic_cache_control",
            raw_provider_family="anthropic",
            raw_model_id=str((payload or {}).get("model", "") or model),
            raw_response=payload if isinstance(payload, dict) else {},
        )

    async def complete(self, system_prompt: str, prompt: str, *, request_label: str, tools_notice: str = "") -> LiveCompletionResult:
        if not self.api_key:
            raise RuntimeError("AnthropicLiveClient requires api_key")

        async def _attempt(current_key: str, _attempt_index: int) -> LiveCompletionResult:
            body = {
                "model": self.model,
                "max_tokens": 160,
                "system": [
                    {
                        "type": "text",
                        "text": system_prompt or "You are AstrMai.",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [{"role": "user", "content": (prompt or "Reply briefly.") + (tools_notice or "")}],
            }
            headers = {
                "x-api-key": current_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            payload = await self._post_json(self.BASE_URL, headers=headers, body=body)
            result = self._build_result_from_payload(payload if isinstance(payload, dict) else {}, self.model)
            result.request_hint_payload = {"type": "ephemeral"}
            return result

        return await self._request_with_retries(request_label, _attempt)


class GeminiLiveClient(BaseLiveProviderClient):
    provider_family = "gemini"
    supports_usage_reporting = False
    supports_cache_hint = False
    supports_session_id = False

    @staticmethod
    def _build_result_from_payload(payload: dict[str, Any], model: str) -> LiveCompletionResult:
        usage = dict((payload or {}).get("usageMetadata", {}) or {})
        text = ""
        candidates = list((payload or {}).get("candidates", []) or [])
        if candidates:
            parts = list((((candidates[0] or {}).get("content") or {}).get("parts", []) or []))
            text = "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict)).strip()
        cached = usage.get("cachedContentTokenCount", None)
        return LiveCompletionResult(
            text=text,
            usage_input_tokens=int(usage.get("promptTokenCount", 0) or 0),
            usage_input_cached=int(cached or 0),
            usage_output_tokens=int(usage.get("candidatesTokenCount", 0) or 0),
            cached_usage_supported=cached is not None,
            request_hint_payload={},
            request_hint_kind="",
            raw_provider_family="gemini",
            raw_model_id=model,
            raw_response=payload if isinstance(payload, dict) else {},
        )

    async def complete(self, system_prompt: str, prompt: str, *, request_label: str, tools_notice: str = "") -> LiveCompletionResult:
        if not self.api_key:
            raise RuntimeError("GeminiLiveClient requires api_key")

        async def _attempt(current_key: str, _attempt_index: int) -> LiveCompletionResult:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={current_key}"
            body = {
                "systemInstruction": {"parts": [{"text": system_prompt or "You are AstrMai."}]},
                "contents": [{"role": "user", "parts": [{"text": (prompt or 'Reply briefly.') + (tools_notice or '')}]}],
            }
            payload = await self._post_json(url, body=body)
            return self._build_result_from_payload(payload if isinstance(payload, dict) else {}, self.model)

        return await self._request_with_retries(
            request_label,
            _attempt,
            rotate_api_keys=True,
        )


class OpenAICompatibleLiveClient(BaseLiveProviderClient):
    provider_family = "native_chat"
    supports_usage_reporting = False
    supports_cache_hint = True
    supports_session_id = False

    def __init__(self, *, api_key: str, model: str, timeout: float, base_url: str, prompt_cache_key: str = "", prompt_cache_retention: str = ""):
        super().__init__(api_key=api_key, model=model, timeout=timeout)
        self.base_url = self._normalize_base_url(base_url)
        self.prompt_cache_key = str(prompt_cache_key or "").strip()
        self.prompt_cache_retention = str(prompt_cache_retention or "").strip()

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        cleaned = str(base_url or "").rstrip("/")
        if not cleaned:
            return ""
        return cleaned if cleaned.endswith("/v1") else cleaned + "/v1"

    @staticmethod
    def _build_result_from_payload(
        payload: dict[str, Any],
        model: str,
        *,
        hint_payload: dict[str, Any] | None = None,
    ) -> LiveCompletionResult:
        usage = dict((payload or {}).get("usage", {}) or {})
        prompt_details = dict(usage.get("prompt_tokens_details", {}) or {})
        cached_tokens = (
            prompt_details.get("cached_tokens")
            if prompt_details.get("cached_tokens") is not None
            else usage.get("input_cached")
            if usage.get("input_cached") is not None
            else usage.get("cached_tokens")
        )
        choice = (((payload or {}).get("choices") or [{}])[0] or {}).get("message") or {}
        return LiveCompletionResult(
            text=str(choice.get("content") or "").strip(),
            usage_input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            usage_input_cached=int(cached_tokens or 0),
            usage_output_tokens=int(usage.get("completion_tokens", 0) or 0),
            cached_usage_supported=cached_tokens is not None,
            request_hint_payload=hint_payload or {},
            request_hint_kind="openai_compatible_prompt_cache" if hint_payload else "",
            raw_provider_family="native_chat",
            raw_model_id=str((payload or {}).get("model", "") or model),
            raw_response=payload if isinstance(payload, dict) else {},
        )

    async def complete(self, system_prompt: str, prompt: str, *, request_label: str, tools_notice: str = "") -> LiveCompletionResult:
        if not self.api_key:
            raise RuntimeError("OpenAICompatibleLiveClient requires api_key")
        if not self.base_url:
            raise RuntimeError("OpenAICompatibleLiveClient requires base_url")

        async def _attempt(current_key: str, _attempt_index: int) -> LiveCompletionResult:
            url = f"{self.base_url}/chat/completions"
            body: dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt or "You are AstrMai."},
                    {"role": "user", "content": (prompt or "Reply briefly.") + (tools_notice or "")},
                ],
                "temperature": 0.0,
                "max_tokens": 160,
            }
            hint_payload = {}
            if self.prompt_cache_key:
                hint_payload["prompt_cache_key"] = self.prompt_cache_key
            if self.prompt_cache_retention:
                hint_payload["prompt_cache_retention"] = self.prompt_cache_retention
            if hint_payload:
                body.update(hint_payload)
            headers = {
                "Authorization": f"Bearer {current_key}",
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
            }
            payload = await self._post_json(url, headers=headers, body=body)
            return self._build_result_from_payload(
                payload if isinstance(payload, dict) else {},
                self.model,
                hint_payload=hint_payload,
            )

        return await self._request_with_retries(request_label, _attempt)


async def build_live_provider_client(
    provider_family: str,
    *,
    api_key: str,
    model: str,
    timeout: float,
    base_url: str = "",
    prompt_cache_key: str = "",
    prompt_cache_retention: str = "",
) -> BaseLiveProviderClient:
    family = str(provider_family or "").strip().lower()
    if family == "kimi":
        return MoonshotLiveClient(api_key=api_key, model=model, timeout=timeout)
    if family == "anthropic":
        return AnthropicLiveClient(api_key=api_key, model=model, timeout=timeout)
    if family == "gemini":
        return GeminiLiveClient(api_key=api_key, model=model, timeout=timeout)
    if family == "native_chat":
        return OpenAICompatibleLiveClient(
            api_key=api_key,
            model=model,
            timeout=timeout,
            base_url=base_url,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_retention=prompt_cache_retention,
        )
    raise RuntimeError(f"Unsupported MAIN_REPLY_LIVE_PROVIDER_FAMILY={provider_family!r}")
