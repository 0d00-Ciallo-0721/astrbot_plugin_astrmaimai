from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderCapabilities:
    provider_family: str
    supports_native_prompt_cache: bool
    supports_remote_session: bool
    supports_cache_control: bool


def _capabilities_for_provider_type(provider_type: str) -> ProviderCapabilities:
    key = str(provider_type or "").strip().lower().replace("-", "_").replace(" ", "_")

    if key in {"anthropic", "anthropic_chat_completion"}:
        return ProviderCapabilities(
            provider_family="anthropic",
            supports_native_prompt_cache=True,
            supports_remote_session=False,
            supports_cache_control=True,
        )
    if key in {"gemini", "google_generative_ai", "vertex", "vertex_ai"}:
        return ProviderCapabilities(
            provider_family="gemini",
            supports_native_prompt_cache=True,
            supports_remote_session=False,
            supports_cache_control=False,
        )
    if key in {
        "openai",
        "openai_chat_completion",
        "native_chat",
        "deepseek",
        "openrouter",
        "groq",
        "xai",
        "zhipu",
    }:
        return ProviderCapabilities(
            provider_family="native_chat",
            supports_native_prompt_cache=True,
            supports_remote_session=False,
            supports_cache_control=False,
        )
    if key in {"dify", "coze", "dashscope", "bailian", "runner"}:
        return ProviderCapabilities(
            provider_family="runner",
            supports_native_prompt_cache=False,
            supports_remote_session=True,
            supports_cache_control=False,
        )

    return ProviderCapabilities(
        provider_family="unknown",
        supports_native_prompt_cache=False,
        supports_remote_session=False,
        supports_cache_control=False,
    )


def _provider_meta(provider: Any) -> Any:
    meta = getattr(provider, "meta", None)
    if callable(meta):
        try:
            return meta()
        except Exception:
            return None
    return meta


def infer_provider_capabilities(provider_or_type: Any) -> ProviderCapabilities:
    if provider_or_type is None or isinstance(provider_or_type, str):
        return _capabilities_for_provider_type(str(provider_or_type or ""))

    meta = _provider_meta(provider_or_type)
    provider_type = ""
    for source in (meta, provider_or_type):
        if source is None:
            continue
        for field in ("provider_family", "provider_type", "type"):
            value = getattr(source, field, None)
            if value:
                provider_type = str(value)
                break
        if provider_type:
            break

    inferred = _capabilities_for_provider_type(provider_type)
    values = {
        "supports_native_prompt_cache": inferred.supports_native_prompt_cache,
        "supports_remote_session": inferred.supports_remote_session,
        "supports_cache_control": inferred.supports_cache_control,
    }
    for field in values:
        for source in (meta, provider_or_type):
            explicit = getattr(source, field, None) if source is not None else None
            if isinstance(explicit, bool):
                values[field] = explicit
                break
    return ProviderCapabilities(
        provider_family=inferred.provider_family,
        **values,
    )


def resolve_provider_capabilities(context: Any, provider_id: str) -> ProviderCapabilities:
    provider = None
    for accessor_name in ("get_provider_by_id", "get_provider"):
        accessor = getattr(context, accessor_name, None) if context is not None else None
        if not callable(accessor):
            continue
        try:
            provider = accessor(provider_id)
        except Exception:
            provider = None
        if provider is not None:
            break
    if provider is not None:
        return infer_provider_capabilities(provider)
    return infer_provider_capabilities(provider_id)
