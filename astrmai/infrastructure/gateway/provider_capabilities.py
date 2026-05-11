from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCapabilities:
    provider_family: str
    supports_native_prompt_cache: bool
    supports_remote_session: bool
    supports_cache_control: bool


def infer_provider_capabilities(model_or_provider_id: str) -> ProviderCapabilities:
    key = (model_or_provider_id or "").lower()

    if any(token in key for token in ("claude", "anthropic")):
        return ProviderCapabilities(
            provider_family="anthropic",
            supports_native_prompt_cache=True,
            supports_remote_session=False,
            supports_cache_control=True,
        )
    if any(token in key for token in ("gemini", "vertex")):
        return ProviderCapabilities(
            provider_family="gemini",
            supports_native_prompt_cache=True,
            supports_remote_session=False,
            supports_cache_control=False,
        )
    if any(token in key for token in ("gpt", "openai", "deepseek", "openrouter", "groq", "xai", "zhipu")):
        return ProviderCapabilities(
            provider_family="native_chat",
            supports_native_prompt_cache=True,
            supports_remote_session=False,
            supports_cache_control=False,
        )
    if any(token in key for token in ("dify", "coze", "dashscope", "bailian")):
        return ProviderCapabilities(
            provider_family="runner",
            supports_native_prompt_cache=False,
            supports_remote_session=True,
            supports_cache_control=False,
        )

    return ProviderCapabilities(
        provider_family="unknown",
        supports_native_prompt_cache=True,
        supports_remote_session=False,
        supports_cache_control=False,
    )
