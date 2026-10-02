import asyncio
from types import SimpleNamespace

from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin
from astrmai.conversation.planning.prompt_refiner import PromptRefiner
from astrmai.infrastructure.runtime.lane_history import LaneHistoryMixin


class _Planner(PlannerPromptContextMixin):
    pass


def _event():
    class Event:
        message_str = "Alice: latest question"
        unified_msg_origin = "default:GroupMessage:group-1"

        def __init__(self):
            self.extras = {"retrieve_keys": []}

        def get_extra(self, key, default=None):
            return self.extras.get(key, default)

        def set_extra(self, key, value):
            self.extras[key] = value

    return Event()


def _refine(envelope):
    event = _event()
    refiner = PromptRefiner(memory_engine=None)
    system, rendered = asyncio.run(
        refiner.refine_prompt(
            event=event,
            system_prompt="stable system prompt",
            prompt="",
            context={"disable_rag_injection": True},
            prompt_envelope=envelope,
        )
    )
    return system, rendered, envelope


def test_nonempty_warm_bundle_keeps_recent_real_tail():
    planner = _Planner()
    include, reason = planner._should_include_recent_transcript(
        "Alice: latest question",
        SimpleNamespace(
            summary_text="older summary",
            quote_text="Bot: older answer",
            has_latest_assistant=True,
        ),
        "Alice: latest question\nBot: latest answer",
    )
    assert include is True
    assert reason == "warm_with_recent_minimum"


def test_budget_trims_warm_before_recent_and_preserves_latest_pair():
    refiner = PromptRefiner(memory_engine=None)
    result = refiner._apply_flexible_context_budget(
        focus_text="Alice: current",
        direct_text="",
        warm_text="W" * 1400,
        warm_meta={"warm_summary": "W" * 1400, "warm_quotes": ""},
        recent_text="Alice: latest question\nBot: latest answer",
        memory_text="",
        memory_meta={},
        soft_background_text="",
    )
    assert result["recent_text"] == "Alice: latest question\nBot: latest answer"
    assert result["warm_text"]
    assert len(result["warm_text"]) + len(result["recent_text"]) <= refiner.FLEX_CONTEXT_BUDGET_CHARS


def test_recent_and_warm_duplicate_lines_prefer_recent_and_keep_distinct_history():
    refiner = PromptRefiner(memory_engine=None)
    envelope = PromptEnvelope(
        raw_user_text="Alice: current",
        focus_message_text="Alice: current",
        recent_transcript="Alice: shared event\nBot: recent answer\nAlice: distinct recent",
        warm_zone_summary="Alice: shared event\nAlice: older distinct",
        warm_zone_quotes="Bot: recent answer\nBot: older answer",
        recent_transcript_source="lane",
        warm_zone_transcript_source="store",
    )
    system, rendered, envelope = _refine(envelope)
    assert system == "stable system prompt"
    assert rendered.count("Alice: shared event") == 1
    assert rendered.count("Bot: recent answer") == 1
    assert "Alice: distinct recent" in rendered
    assert "Alice: older distinct" in rendered
    assert "Bot: older answer" in rendered
    assert rendered.index("---对话记录") < rendered.index("---近期对话脉络")
    assert "Alice: shared event" in envelope.recent_transcript
    assert "Alice: shared event" not in envelope.warm_zone_transcript


def test_anchor_and_bridge_runtime_sections_do_not_change_stable_system_prompt():
    refiner = PromptRefiner(memory_engine=None)
    envelope = PromptEnvelope(
        raw_user_text="Alice: continue",
        focus_message_text="Alice: continue",
        recent_transcript="Alice: continue\nBot: answer",
        planner_runtime_instruction_block=(
            '<untrusted_context type="derived" source="topic_attention_anchor">anchor</untrusted_context>\n'
            '<untrusted_context type="derived" source="cross_topic_bridge">bridge</untrusted_context>'
        ),
    )
    system, rendered, _ = _refine(envelope)
    assert system == "stable system prompt"
    assert "anchor" in rendered and "bridge" in rendered
    assert "stable system prompt" not in rendered


def test_shared_event_ids_remove_warm_quotes_without_dropping_distinct_legacy_lines():
    envelope = PromptEnvelope(
        raw_user_text="Alice: current",
        focus_message_text="Alice: current",
        recent_transcript="Alice: recent wording\nBot: recent response",
        recent_transcript_event_ids=["canonical-1", "reply-1"],
        topic_attention_anchor_block="topic anchor projection",
        topic_attention_anchor_event_ids=["anchor-1"],
        cross_topic_bridge_block="accepted bridge projection",
        cross_topic_bridge_event_ids=["bridge-1"],
        warm_zone_summary="summary of earlier context",
        warm_zone_quotes=(
            "Alice: stale rendering of same event\n"
            "Bot: duplicate anchor body\n"
            "Alice: duplicate bridge body\n"
            "Bob: distinct old event\n"
            "Carol: old history without id"
        ),
        warm_zone_quote_event_ids=["canonical-1", "anchor-1", "bridge-1", "other-1", ""],
    )
    system, rendered, envelope = _refine(envelope)
    assert system == "stable system prompt"
    assert "Alice: stale rendering of same event" not in rendered
    assert "Bot: duplicate anchor body" not in rendered
    assert "Alice: duplicate bridge body" not in rendered
    assert "Bob: distinct old event" in rendered
    assert "Carol: old history without id" in rendered
    assert rendered.index("---对话记录") < rendered.index("---话题注意力锚点")
    assert rendered.index("---话题注意力锚点") < rendered.index("---跨话题桥接")
    assert rendered.index("---跨话题桥接") < rendered.index("---近期对话脉络")


def test_multiline_warm_quote_keeps_event_identity_alignment():
    envelope = PromptEnvelope(
        raw_user_text="Alice: current",
        focus_message_text="Alice: current",
        recent_transcript="Alice: recent wording",
        recent_transcript_event_ids=["event-1"],
        warm_zone_summary="earlier summary",
        warm_zone_quotes="Alice: old wording\ncontinued old wording\nBob: separate quote",
        warm_zone_quote_event_ids=["event-1", "event-2"],
        warm_zone_quote_entries=[
            ("event-1", "Alice: old wording\ncontinued old wording"),
            ("event-2", "Bob: separate quote"),
        ],
    )
    _, rendered, _ = _refine(envelope)
    assert "old wording" not in rendered
    assert "Bob: separate quote" in rendered


def test_shared_budget_keeps_latest_pair_and_bounds_all_auxiliary_sources():
    refiner = PromptRefiner(memory_engine=None)
    recent = "\n".join(
        f"Alice: question {index} " + "U" * 120 + "\nBot: answer " + "A" * 120
        for index in range(8)
    )
    result = refiner._apply_flexible_context_budget(
        focus_text="Alice: current input",
        direct_text="",
        warm_text="W" * 1400,
        warm_meta={"warm_summary": "W" * 1400, "warm_quotes": ""},
        recent_text=recent,
        memory_text="M" * 400,
        memory_meta={},
        soft_background_text="S" * 400,
        anchor_text="T" * 900,
        bridge_text="B" * 900,
    )
    assert "Alice: question 7" in result["recent_text"]
    assert "Bot: answer " in result["recent_text"]
    assert "Alice: question 0" not in result["recent_text"]
    assert "soft_background" in result["trimmed_sections"]
    assert result["warm_text"] == ""
    assert sum(len(result[key]) for key in (
        "recent_text", "anchor_text", "bridge_text", "warm_text", "memory_text", "soft_background_text"
    )) <= refiner.FLEX_CONTEXT_BUDGET_CHARS


def test_lane_transcript_excludes_rotation_summary_and_returns_aligned_ids():
    class Lane(LaneHistoryMixin):
        settings = SimpleNamespace(nicknames=["Bot"], debug_mode=False)

        async def _ensure_lane_with_timeout(self, **kwargs):
            return "lane", "conversation", [
                {"role": "assistant", "content": "历史上下文摘要（轮换桥接，非真实回复）： old", "event_id": "summary"},
                {"role": "user", "content": "real question", "sender_name": "Alice", "event_id": "canonical-1"},
                {"role": "user", "content": "duplicated old rendering", "sender_name": "Alice", "event_id": "canonical-1"},
                {"role": "assistant", "content": "real answer", "message_id": "reply-1"},
            ], ""

        def _message_timestamp(self, message):
            return 1.0

    lane = Lane()
    transcript, ids = asyncio.run(lane.get_recent_transcript("lane", "chat", include_event_ids=True))
    assert transcript.splitlines() == ["Alice: duplicated old rendering", "Bot: real answer"]
    assert ids == ["canonical-1", "reply-1"]
    assert asyncio.run(lane.get_recent_transcript("lane", "chat")) == transcript


def test_empty_history_and_missing_ids_do_not_create_synthetic_turns():
    envelope = PromptEnvelope(raw_user_text="Alice: current", focus_message_text="Alice: current")
    _, rendered, _ = _refine(envelope)
    assert "---对话记录" not in rendered
    assert "---近期对话脉络" not in rendered
    assert "Alice: current" in rendered
