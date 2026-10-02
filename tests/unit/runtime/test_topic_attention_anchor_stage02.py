import asyncio
from types import SimpleNamespace
import json

from astrmai.conversation.planning.conversation_continuity import ConversationContinuityStore
from astrmai.conversation.planning.planning_input_loader import PlanningInputLoader
from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
from astrmai.conversation.contracts.topic_attention_anchor import TopicAttentionAnchor
from astrmai.conversation.contracts.turn_context import ensure_turn_context
from astrmai.conversation.planning.prompt_refiner import PromptRefiner


def _event(event_id="evt-1", *, text="讨论项目计划", actor_id="user-1", epoch=1, reply_id=""):
    return SimpleNamespace(
        event_id=event_id,
        chat_id="group-1",
        actor_id=actor_id,
        actor_name="Alice",
        visible_text=text,
        topic_epoch=epoch,
        reply_target_event_id=reply_id,
        quote_event_id=reply_id,
        source_event_ids=(event_id,),
    )


def test_anchor_creation_is_bounded_and_auditable():
    store = ConversationContinuityStore()
    store.update_topic_anchor(
        "chat-1",
        event=_event(text="项目计划"),
        subject_preview="项目计划",
        now=1000,
    )
    anchor = store.snapshot("chat-1", now=1001)["topic_anchor"]
    assert anchor["topic_epoch"] == 1
    assert anchor["participants"] == ["user-1"]
    assert anchor["subject_preview"] == "项目计划"
    assert anchor["recent_event_ids"] == ["evt-1"]
    assert anchor["source"] == ["user_message"]
    assert 0.0 < anchor["confidence"] <= 1.0


def test_quote_and_reply_target_are_recorded_as_canonical_evidence():
    store = ConversationContinuityStore()
    store.update_topic_anchor(
        "chat-1",
        event=_event("evt-2", reply_id="evt-1"),
        subject_preview="项目计划",
        now=1000,
    )
    anchor = store.snapshot("chat-1", now=1000)["topic_anchor"]
    assert anchor["recent_event_ids"] == ["evt-2", "evt-1"]
    assert "quote" in anchor["source"]
    assert "reply_target" in anchor["source"]


def test_anchor_updates_same_topic_deduplicates_events_and_keeps_participants():
    store = ConversationContinuityStore()
    store.update_topic_anchor("chat-1", event=_event("evt-1"), subject_preview="项目计划", now=1000)
    store.update_topic_anchor(
        "chat-1",
        event=_event("evt-2", text="项目计划的时间", actor_id="user-2"),
        subject_preview="项目计划的时间",
        now=1001,
    )
    store.update_topic_anchor("chat-1", event=_event("evt-2"), subject_preview="项目计划的时间", now=1002)
    anchor = store.snapshot("chat-1", now=1002)["topic_anchor"]
    assert anchor["participants"] == ["user-1", "user-2"]
    assert anchor["recent_event_ids"] == ["evt-1", "evt-2"]


def test_duplicate_event_does_not_refresh_anchor_or_expand_source_values():
    store = ConversationContinuityStore()
    store.update_topic_anchor(
        "chat-1", event=_event("evt-1"), subject_preview="项目计划", source=("untrusted-body",), now=1000
    )
    store.update_topic_anchor(
        "chat-1", event=_event("evt-1"), subject_preview="项目计划", source=("untrusted-body",), now=2000
    )
    anchor = store.snapshot("chat-1", now=2000)["topic_anchor"]
    assert anchor["updated_at"] == 1000
    assert "untrusted-body" not in anchor["source"]


def test_topic_epoch_change_replaces_anchor_even_when_subject_text_matches():
    store = ConversationContinuityStore()
    store.update_topic_anchor("chat-1", event=_event("evt-1", epoch=1), subject_preview="项目计划", now=1000)
    store.update_topic_anchor(
        "chat-1", event=_event("evt-2", epoch=2, actor_id="user-2"), subject_preview="项目计划", now=1001
    )
    anchor = store.snapshot("chat-1", now=1001)["topic_anchor"]
    assert anchor["topic_epoch"] == 2
    assert anchor["participants"] == ["user-2"]
    assert anchor["recent_event_ids"] == ["evt-2"]
    assert anchor["source"] == ["topic_transition", "user_message"]


def test_anchor_open_loop_closes_when_user_answers_and_records_question_source():
    store = ConversationContinuityStore()
    store.update_topic_anchor(
        "chat-1", event=_event("evt-1"), subject_preview="项目计划", reply_text="你周五有空吗？", now=1000
    )
    assert store.snapshot("chat-1", now=1000)["topic_anchor"]["open_loop"]
    store.update_topic_anchor(
        "chat-1", event=_event("evt-2", text="我周五有空"), subject_preview="项目计划", now=1001
    )
    anchor = store.snapshot("chat-1", now=1001)["topic_anchor"]
    assert anchor["open_loop"] == ""
    assert "explicit_question" in anchor["source"]


def test_non_question_assistant_reply_closes_existing_open_loop():
    store = ConversationContinuityStore()
    store.update_topic_anchor(
        "chat-1", event=_event("evt-1"), subject_preview="项目计划", reply_text="周五你有空吗？", now=1000
    )
    store.update_topic_anchor(
        "chat-1", event=_event("evt-2"), subject_preview="项目计划", reply_text="那我们定在周五。", now=1001
    )
    assert store.snapshot("chat-1", now=1001)["topic_anchor"]["open_loop"] == ""


def test_anchor_replaces_subject_on_new_topic_without_old_open_loop():
    store = ConversationContinuityStore()
    store.update_topic_anchor(
        "chat-1", event=_event("evt-1"), subject_preview="项目计划", reply_text="要继续吗？", now=1000
    )
    store.update_topic_anchor(
        "chat-1", event=_event("evt-2", text="天气怎么样"), subject_preview="明天天气", now=1001
    )
    anchor = store.snapshot("chat-1", now=1001)["topic_anchor"]
    assert anchor["subject_preview"] == "明天天气"
    assert anchor["open_loop"] == ""
    assert anchor["recent_event_ids"] == ["evt-2"]
    assert "topic_transition" in anchor["source"]


def test_anchor_limits_participants_subject_events_and_prompt_size():
    store = ConversationContinuityStore()
    for index in range(20):
        store.update_topic_anchor(
            "chat-1",
            event=_event(f"evt-{index}", actor_id=f"user-{index}"),
            subject_preview="x" * 1000,
            now=1000 + index,
        )
    anchor = store.snapshot("chat-1", now=1020)["topic_anchor"]
    assert len(anchor["participants"]) <= store.ANCHOR_MAX_PARTICIPANTS
    assert len(anchor["subject_preview"]) <= store.ANCHOR_MAX_SUBJECT_CHARS
    assert len(anchor["recent_event_ids"]) <= store.ANCHOR_MAX_EVENT_IDS
    assert len(store.topic_anchor_prompt("chat-1", now=1020)) <= store.ANCHOR_MAX_PROMPT_CHARS


def test_anchor_isolated_by_chat_and_expires_using_existing_turn_ttl():
    store = ConversationContinuityStore()
    store.update_topic_anchor("chat-1", event=_event(), subject_preview="项目计划", now=1000)
    assert store.snapshot("chat-2", now=1001)["topic_anchor"]["subject_preview"] == ""
    assert store.topic_anchor_view("chat-1", now=1000 + store.TURN_TTL_SECONDS + 1) == {}
    store.update_topic_anchor(
        "chat-1",
        event=_event("evt-2", actor_id="user-2"),
        subject_preview="项目计划",
        now=1000 + store.TURN_TTL_SECONDS + 2,
    )
    refreshed = store.snapshot("chat-1", now=1000 + store.TURN_TTL_SECONDS + 2)["topic_anchor"]
    assert refreshed["participants"] == ["user-2"]
    assert refreshed["recent_event_ids"] == ["evt-2"]


def test_anchor_snapshot_restore_is_backward_compatible_and_localizes_bad_fields():
    store = ConversationContinuityStore()
    store.restore_snapshot(
        "chat-1",
        {
            "current_topic": "旧话题",
            "topic_anchor": {
                "subject_preview": "恢复话题",
                "participants": "bad",
                "recent_event_ids": ["evt-1", 4, "evt-1"],
                "confidence": "bad",
                "updated_at": 1000,
            },
        },
    )
    anchor = store.snapshot("chat-1", now=1001)["topic_anchor"]
    assert anchor["subject_preview"] == "恢复话题"
    assert anchor["participants"] == []
    assert anchor["recent_event_ids"] == ["evt-1"]
    assert anchor["confidence"] == 0.0
    assert store.snapshot("chat-1", now=1001)["current_topic"] == "旧话题"


def test_anchor_restore_applies_the_same_bounds_as_live_updates():
    store = ConversationContinuityStore()
    store.restore_snapshot(
        "chat-1",
        {
            "topic_anchor": {
                "subject_preview": "s" * 1000,
                "open_loop": "q" * 1000,
                "participants": [f"actor-{index}-" + "x" * 200 for index in range(30)],
                "recent_event_ids": [f"event-{index}-" + "x" * 200 for index in range(30)],
                "source": ["source-" + "x" * 200 for _ in range(30)],
                "updated_at": 1000,
            }
        },
    )
    anchor = store.snapshot("chat-1", now=1001)["topic_anchor"]
    assert len(anchor["participants"]) <= store.ANCHOR_MAX_PARTICIPANTS
    assert len(anchor["recent_event_ids"]) <= store.ANCHOR_MAX_EVENT_IDS
    assert len(anchor["subject_preview"]) <= store.ANCHOR_MAX_SUBJECT_CHARS
    assert len(anchor["open_loop"]) <= store.ANCHOR_MAX_OPEN_LOOP_CHARS
    assert len(json.dumps(anchor, ensure_ascii=False)) <= store.ANCHOR_MAX_SERIALIZED_CHARS


def test_record_prefers_canonical_identity_and_records_reply_source():
    store = ConversationContinuityStore()
    store.record(
        chat_id="chat-1",
        focus_preview="项目计划",
        reply_preview="周五可以吗？",
        sender_id="user-1",
        source_event_id="platform-id",
        anchor_event=_event("canonical-id", actor_id="canonical-user"),
        now=1000,
    )
    anchor = store.snapshot("chat-1", now=1000)["topic_anchor"]
    assert anchor["recent_event_ids"] == ["canonical-id"]
    assert anchor["participants"] == ["canonical-user"]
    assert "assistant_reply" in anchor["source"]
    assert "explicit_question" in anchor["source"]


def test_anchor_prompt_is_dynamic_and_does_not_touch_stable_system_prompt():
    store = ConversationContinuityStore()
    store.update_topic_anchor("chat-1", event=_event(), subject_preview="项目计划", now=1000)
    block = store.topic_anchor_prompt("chat-1", now=1001)
    assert "话题注意力锚点" in block
    assert "source=" in block and "age_seconds=" in block and "confidence=" in block
    assert "system_prompt" not in block


def test_empty_or_expired_anchor_does_not_create_synthetic_prompt_content():
    store = ConversationContinuityStore()
    assert store.topic_anchor_prompt("chat-1", now=1000) == ""
    store.update_topic_anchor("chat-1", event=_event(), subject_preview="项目计划", now=1000)
    assert store.topic_anchor_prompt("chat-1", now=1000 + store.TURN_TTL_SECONDS + 1) == ""


def test_anchor_load_failure_degrades_without_blocking_pre_budget_inputs():
    class _BrokenStore:
        def summary(self, chat_id):
            raise RuntimeError("snapshot unavailable")

    class _Event:
        def __init__(self):
            self._extra = {}

        def get_extra(self, key, default=None):
            return self._extra.get(key, default)

        def set_extra(self, key, value):
            self._extra[key] = value

    loader = PlanningInputLoader(SimpleNamespace(conversation_continuity=_BrokenStore()))
    inputs = asyncio.run(loader.load_pre_budget(_Event(), "chat-1"))
    assert inputs.conversation_summary == ""


def test_anchor_is_injected_into_runtime_prompt_block_only():
    store = ConversationContinuityStore()
    store.update_topic_anchor("chat-1", event=_event(), subject_preview="项目计划", now=__import__("time").time())

    class _Event:
        def __init__(self):
            self._extra = {}
            self.message_str = "继续这个话题"
            self.unified_msg_origin = "default:GroupMessage:group-1"

        def get_extra(self, key, default=None):
            return self._extra.get(key, default)

        def set_extra(self, key, value):
            self._extra[key] = value

        def get_sender_name(self):
            return "Alice"

    event = _Event()
    loader = PlanningInputLoader(SimpleNamespace(conversation_continuity=store))
    continuity = loader._continuity_snapshot("chat-1")
    loader._apply_continuity(event, continuity)
    anchor_view = ensure_turn_context(event).continuity.topic_attention_anchor
    assert isinstance(anchor_view, TopicAttentionAnchor)
    assert anchor_view.subject_preview == "项目计划"
    envelope = PromptEnvelope()
    asyncio.run(loader.load_prompt_inputs(event, "chat-1", envelope, [], 0))
    refiner = PromptRefiner(
        memory_engine=None,
        config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
    )
    stable_system, dynamic_prompt = asyncio.run(
        refiner.refine_prompt(
            event=event,
            system_prompt="stable system prompt",
            prompt="",
            context={"disable_rag_injection": False},
            prompt_envelope=envelope,
        )
    )

    assert stable_system == "stable system prompt"
    assert "话题注意力锚点" in dynamic_prompt
    assert "项目计划" in dynamic_prompt
    assert '<untrusted_context type="derived" source="topic_attention_anchor"' in dynamic_prompt
