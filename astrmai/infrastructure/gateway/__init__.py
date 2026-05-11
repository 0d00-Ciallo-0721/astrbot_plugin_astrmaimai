from importlib import import_module

_EXPORTS = {
    "GlobalModelGateway": ".model_gateway",
    "LLMCascadeFailureException": ".model_gateway",
    "ModelRouter": ".model_router",
    "ProviderCapabilities": ".provider_capabilities",
    "infer_provider_capabilities": ".provider_capabilities",
    "is_safe_visible_text": ".output_guard",
    "is_sendable_segment": ".output_guard",
    "looks_like_prompt_scaffold_text": ".output_guard",
    "looks_like_provider_failure_text": ".output_guard",
    "looks_like_tool_protocol_text": ".output_guard",
    "sanitize_visible_reply_text": ".output_guard",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    try:
        module_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(f"{__name__}{module_name}")
    value = getattr(module, name)
    globals()[name] = value
    return value
