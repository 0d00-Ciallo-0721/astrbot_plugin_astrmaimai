from types import SimpleNamespace

from astrmai.conversation.contracts.turn_context import (
    ToolDecisionTrace,
    TurnContext,
    build_turn_trace_summary,
    ensure_turn_context,
    get_turn_context,
)


class _Event:
    def __init__(self):
        self._extra = {}

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


def test_turn_context_attaches_once_and_keeps_layered_state():
    event = _Event()

    first = ensure_turn_context(event)
    first.perception.chat_id = "chat-1"
    first.cognitive.social_intent = "answer"
    second = ensure_turn_context(event)

    assert first is second
    assert get_turn_context(event) is first
    assert second.perception.chat_id == "chat-1"
    assert second.cognitive.social_intent == "answer"


def test_turn_context_is_not_created_from_unrelated_extra_value():
    event = _Event()
    event.set_extra("astrmai_turn_context", SimpleNamespace())

    context = ensure_turn_context(event)

    assert get_turn_context(event) is context
    assert isinstance(context, TurnContext)


def test_tool_decision_trace_records_filter_steps():
    trace = ToolDecisionTrace(requested_tier="chat", final_tier="chat")

    trace.initial_tools = ["proactive_meme", "message_reaction_action"]
    trace.record_step(
        "action_modifier.cooldown",
        ["proactive_meme", "message_reaction_action"],
        ["message_reaction_action"],
        "cooldown(meme)",
        category="cooldown",
    )

    assert trace.filter_reasons == ["cooldown(meme)"]
    assert trace.filter_steps[0]["removed"] == ["proactive_meme"]
    assert trace.filter_steps[0]["category"] == "cooldown"
    assert trace.removed_by_cooldown == ["proactive_meme"]


def test_turn_trace_summary_hides_inner_monologue_and_prompt_text():
    context = TurnContext()
    context.perception.chat_id = "chat-1"
    context.perception.sender_name = "Alice"
    context.perception.text = "hello there"
    context.cognitive.social_intent = "answer"
    context.cognitive.think_level = 2
    context.cognitive.think_reason = "deeper_reasoning"
    context.cognitive.think_signals = ["complexity_keyword"]
    context.cognitive.cognitive_loop_ran = False
    context.cognitive.cognitive_loop_skipped_reason = "cooldown_simple_turn"
    context.cognitive.cognitive_loop_skip_signals = ["sharp_reply"]
    context.cognitive.readonly_tools_allowed = False
    context.cognitive.readonly_tools_skip_reason = "think_level_2_blocks_readonly_tool"
    context.cognitive.inner_monologue = "secret private thought"
    context.continuity.current_topic = "Alice: hello there"
    context.continuity.current_goal = "answer Alice naturally"
    context.continuity.goal_status = "continuing"
    context.continuity.continuity_weight = "weak"
    context.continuity.turn_count = 2
    context.memory.policy = "deep"
    context.memory.source = "react"
    context.memory.retrieve_keys = ["timeline"]
    context.memory.injected = True
    context.memory.summary_preview = "secret memory detail " * 20
    context.follow_up.eligible = False
    context.follow_up.skipped_reason = "probability_gate"
    context.follow_up.signals = ["answer"]
    context.follow_up.probability = 0.08
    context.follow_up.llm_checked = False
    context.follow_up.followed = False
    context.follow_up.reason = "secret follow prompt"
    context.side_inputs.timings = [
        {"name": "memory_feedback", "elapsed_ms": 3.2, "ok": True},
        {"name": "expression_habits", "elapsed_ms": 1.0, "ok": False, "error": "RuntimeError: hidden"},
    ]
    context.tools.removed_by_energy = ["full_tier"]
    context.tools.removed_by_cooldown = ["proactive_meme"]
    context.prompt_envelope = SimpleNamespace(
        focus_message_text="Alice: hello there",
        system_prompt="secret system prompt",
        user_prompt="secret user prompt",
    )

    summary = build_turn_trace_summary(context, created_at=1.0, status="executed", reply_preview="ok")
    rendered = str(summary)

    assert summary["perception"]["sender_name"] == "Alice"
    assert summary["attention"]["focus_preview"] == "Alice: hello there"
    assert summary["continuity"]["current_topic_preview"] == "Alice: hello there"
    assert summary["continuity"]["current_goal_preview"] == "answer Alice naturally"
    assert summary["continuity"]["goal_status"] == "continuing"
    assert summary["continuity"]["continuity_weight"] == "weak"
    assert summary["continuity"]["turn_count"] == 2
    assert summary["memory"]["policy"] == "deep"
    assert summary["cognitive"]["think_level"] == 2
    assert summary["cognitive"]["think_reason"] == "deeper_reasoning"
    assert summary["cognitive"]["think_signals"] == ["complexity_keyword"]
    assert summary["cognitive"]["cognitive_loop_ran"] is False
    assert summary["cognitive"]["cognitive_loop_skipped_reason"] == "cooldown_simple_turn"
    assert summary["cognitive"]["cognitive_loop_skip_signals"] == ["sharp_reply"]
    assert summary["cognitive"]["readonly_tools_allowed"] is False
    assert summary["cognitive"]["readonly_tools_skip_reason"] == "think_level_2_blocks_readonly_tool"
    assert summary["memory"]["source"] == "react"
    assert summary["memory"]["retrieve_keys"] == ["timeline"]
    assert summary["memory"]["injected"] is True
    assert summary["memory"]["summary_preview"].endswith("...")
    assert summary["follow_up"]["skipped_reason"] == "probability_gate"
    assert summary["follow_up"]["probability"] == 0.08
    assert summary["follow_up"]["llm_checked"] is False
    assert summary["follow_up"]["followed"] is False
    assert summary["side_inputs"]["timings"][0]["name"] == "memory_feedback"
    assert summary["side_inputs"]["timings"][1]["ok"] is False
    assert summary["tools"]["removed_by_energy"] == ["full_tier"]
    assert summary["tools"]["removed_by_cooldown"] == ["proactive_meme"]
    assert "secret private thought" not in rendered
    assert "secret system prompt" not in rendered
    assert "secret user prompt" not in rendered


def test_turn_trace_summary_uses_ascii_truncation_marker():
    context = TurnContext()
    context.perception.text = "x" * 220

    summary = build_turn_trace_summary(context, created_at=1.0)

    assert summary["perception"]["text_preview"].endswith("...")
    assert "…" not in summary["perception"]["text_preview"]
