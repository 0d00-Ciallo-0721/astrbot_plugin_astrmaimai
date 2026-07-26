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


def _lookup_provider(context: Any, provider_id: str) -> Any:
    for accessor_name in ("get_provider_by_id", "get_provider"):
        accessor = getattr(context, accessor_name, None) if context is not None else None
        if not callable(accessor):
            continue
        try:
            provider = accessor(provider_id)
        except Exception:
            provider = None
        if provider is not None:
            return provider
    return None


def resolve_provider_capabilities(context: Any, provider_id: str) -> ProviderCapabilities:
    # OPT-09/RT-08: 网关传入的是完整模型 ID（如 code2/deepseek-v4-flash），旧实现
    # 直接拿它当 provider type 匹配家族表必然 unknown（线上 1005/1005 全 unknown，
    # cache_control/远程会话特性形同虚设）。按 '/' 前缀降级查 provider 对象，
    # 字符串回退也用前缀（gemini/xx → gemini 家族）。
    normalized_id = str(provider_id or "").strip()
    prefix = normalized_id.split("/", 1)[0] if "/" in normalized_id else ""
    for candidate in filter(None, (normalized_id, prefix)):
        provider = _lookup_provider(context, candidate)
        if provider is not None:
            return infer_provider_capabilities(provider)
    all_getter = getattr(context, "get_all_providers", None) if context is not None else None
    if callable(all_getter):
        try:
            providers = list(all_getter() or [])
        except Exception:
            providers = []
        for provider in providers:
            meta = _provider_meta(provider)
            registered_id = str(
                getattr(meta, "id", None) or getattr(provider, "id", "") or ""
            ).strip()
            if registered_id and (
                registered_id == normalized_id or normalized_id.startswith(f"{registered_id}/")
            ):
                return infer_provider_capabilities(provider)
    return infer_provider_capabilities(prefix or normalized_id)
