from importlib import import_module

_EXPORTS = {
    "ChatRuntimeCoordinator": ".chat_runtime_coordinator",
    "EventBus": ".event_bus",
    "FailureKind": ".runtime_contracts",
    "HostBridge": ".host_bridge",
    "LLMCallResult": ".runtime_contracts",
    "LaneKey": ".lane_manager",
    "LaneManager": ".lane_manager",
    "SocialTranscriptTurn": ".runtime_contracts",
    "REVERSE_SESSION_TAG": ".reverse_session",
    "append_reverse_session_block": ".reverse_session",
    "maybe_attach_reverse_session_block": ".reverse_session",
    "parse_reverse_session_block": ".reverse_session",
    "provider_is_gemini_reverse": ".reverse_session",
    "render_reverse_session_block": ".reverse_session",
    "strip_reverse_session_block": ".reverse_session",
    "append_trace_stage": ".trace_runtime",
    "debug_trace": ".trace_runtime",
    "ensure_trace_id": ".trace_runtime",
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
