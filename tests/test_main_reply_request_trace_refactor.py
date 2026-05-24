from types import SimpleNamespace

from astrmai.conversation.contracts.turn_context import TurnContext, build_turn_trace_summary


def test_turn_trace_summary_includes_provider_visible_request_trace_fields():
    context = TurnContext()
    context.continuity.semantic_system_hash = "semantic1111"
    context.continuity.semantic_system_length = 240
    context.continuity.gateway_system_hash = "gatewaysys0001"
    context.continuity.gateway_prompt_hash = "gatewayprompt0002"
    context.continuity.provider_visible_system_hash = "syshash1234"
    context.continuity.provider_visible_prompt_hash = "prompthash5678"
    context.continuity.post_hook_system_hash = "posthook9999"
    context.continuity.request_session_id = "session-1"
    context.continuity.request_cache_control = '{"type":"ephemeral"}'
    context.continuity.request_provider_family = "anthropic"
    context.continuity.request_model_id = "claude-3-5-sonnet"
    context.continuity.usage_input_tokens = 1200
    context.continuity.usage_input_cached = 800
    context.continuity.usage_output_tokens = 120
    context.continuity.cache_ready = True
    context.continuity.cache_hit = True
    context.continuity.cache_ready_reasons = ["explicit_cache_hint", "session_reuse"]
    context.continuity.cache_hit_evidence_supported = True
    context.prompt_envelope = SimpleNamespace(focus_message_text="Alice: hello")

    summary = build_turn_trace_summary(context, created_at=1.0)

    continuity = summary["continuity"]
    assert continuity["semantic_system_hash"] == "semantic1111"
    assert continuity["semantic_system_length"] == 240
    assert continuity["gateway_system_hash"] == "gatewaysys0001"
    assert continuity["gateway_prompt_hash"] == "gatewayprompt0002"
    assert continuity["provider_visible_system_hash"] == "syshash1234"
    assert continuity["provider_visible_prompt_hash"] == "prompthash5678"
    assert continuity["post_hook_system_hash"] == "posthook9999"
    assert continuity["request_session_id"] == "session-1"
    assert continuity["request_cache_control"] == '{"type":"ephemeral"}'
    assert continuity["request_provider_family"] == "anthropic"
    assert continuity["request_model_id"] == "claude-3-5-sonnet"
    assert continuity["usage_input_tokens"] == 1200
    assert continuity["usage_input_cached"] == 800
    assert continuity["usage_output_tokens"] == 120
    assert continuity["cache_ready"] is True
    assert continuity["cache_hit"] is True
    assert continuity["cache_ready_reasons"] == ["explicit_cache_hint", "session_reuse"]
    assert continuity["cache_hit_evidence_supported"] is True
