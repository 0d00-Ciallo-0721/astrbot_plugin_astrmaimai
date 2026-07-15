from importlib import import_module

_EXPORTS = {
    "FocusThreadContext": ".focus_context",
    "FreshnessState": ".focus_context",
    "ReplyFreshnessBudget": ".focus_context",
    "ReplyMode": ".focus_context",
    "VisionBundle": ".focus_context",
    "PromptEnvelope": ".prompt_envelope",
    "VisibleReplyArtifact": ".reply_artifact",
    "OutboundPolicy": ".reply_artifact",
    "TurnContext": ".turn_context",
    "PerceptionSnapshot": ".turn_context",
    "AttentionSnapshot": ".turn_context",
    "CognitiveSnapshot": ".turn_context",
    "ContinuitySnapshot": ".turn_context",
    "ToolDecisionTrace": ".turn_context",
    "ToolSnapshot": ".turn_context",
    "build_turn_trace_summary": ".turn_context",
    "ensure_turn_context": ".turn_context",
    "get_turn_context": ".turn_context",
    "TurnIdentity": ".turn_identity",
    "build_p0_thread_id": ".turn_identity",
    "build_turn_send_key": ".turn_identity",
    "PendingQQAction": ".qq_action",
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
