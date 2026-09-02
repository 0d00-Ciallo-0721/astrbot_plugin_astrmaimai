from importlib import import_module

_EXPORTS = {
    "AttentionTopicIdentity": ".attention_topic",
    "FocusThreadContext": ".focus_context",
    "FreshnessState": ".focus_context",
    "ReplyFreshnessBudget": ".focus_context",
    "ReplyMode": ".focus_context",
    "VisionBundle": ".focus_context",
    "PromptEnvelope": ".prompt_envelope",
    "VisibleReplyArtifact": ".reply_artifact",
    "OutboundPolicy": ".reply_artifact",
    "CommittedBotTurn": ".committed_reply",
    "ReplyCommitStatus": ".committed_reply",
    "ReplyPlan": ".committed_reply",
    "ReplySendReceipt": ".committed_reply",
    "ContextBlock": ".context_package",
    "ContextPackage": ".context_package",
    "escape_untrusted_text": ".context_package",
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
    "TurnTarget": ".turn_target",
    "TargetKind": ".turn_target",
    "ActorSet": ".turn_target",
    "build_p0_thread_id": ".turn_identity",
    "build_turn_send_key": ".turn_identity",
    "TURN_OUTCOME_EXTRA_KEY": ".turn_outcome",
    "TurnOutcome": ".turn_outcome",
    "TurnOutcomeStatus": ".turn_outcome",
    "TurnDecision": ".turn_outcome",
    "can_deferred_replay": ".turn_outcome",
    "claim_deferred_replay": ".turn_outcome",
    "can_retry": ".turn_outcome",
    "can_send_fallback": ".turn_outcome",
    "can_send_text": ".turn_outcome",
    "can_send_tool_action": ".turn_outcome",
    "claim_tool_action": ".turn_outcome",
    "claim_completion_callback": ".turn_outcome",
    "claim_text_output": ".turn_outcome",
    "ensure_turn_outcome": ".turn_outcome",
    "get_turn_outcome": ".turn_outcome",
    "mark_deferred_replayed": ".turn_outcome",
    "mark_system2_handled": ".turn_outcome",
    "mark_terminal": ".turn_outcome",
    "record_text_failed": ".turn_outcome",
    "record_text_sent": ".turn_outcome",
    "record_tool_action_result": ".turn_outcome",
    "release_tool_action": ".turn_outcome",
    "release_text_output": ".turn_outcome",
    "release_deferred_replay": ".turn_outcome",
    "settle_completion_callback": ".turn_outcome",
    "PendingQQAction": ".qq_action",
    "VisionCandidate": ".vision_candidate",
    "load_vision_candidates": ".vision_candidate",
    "SocialFeedbackDecision": ".social_feedback",
    "SocialFeedbackEvidence": ".social_feedback",
    "SocialFeedbackObservation": ".social_feedback",
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
