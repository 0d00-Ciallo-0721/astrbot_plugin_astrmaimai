import asyncio
import time
from types import SimpleNamespace

from astrmai.conversation.contracts.dialog_history_policy import DialogHistoryPolicy
from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
from astrmai.conversation.contracts.turn_context import ensure_turn_context
from astrmai.conversation.planning.conversation_continuity import ConversationContinuityStore
from astrmai.conversation.planning.planning_input_loader import PlanningInputLoader
from astrmai.conversation.planning.prompt_refiner import PromptRefiner


CHAT = "default:GroupMessage:group-1"


def _canonical(event_id="current", *, chat_id=CHAT, actor_id="alice", text="然后呢", reply_id=""):
    return SimpleNamespace(
        event_id=event_id,
        chat_id=chat_id,
        actor_id=actor_id,
        visible_text=text,
        reply_target_event_id=reply_id,
        quote_event_id=reply_id,
        causal_parent_event_id=reply_id,
        is_bot=False,
    )


def _previous(store, *, now=1000.0, reply="我们讨论项目计划。", open_loop=False):
    store.record(
        chat_id=CHAT,
        focus_preview="项目计划",
        reply_preview="周五有空吗？" if open_loop else reply,
        sender_id="alice",
        source_event_id="old-1",
        anchor_event=_canonical("old-1", text="项目计划"),
        topic_epoch=1,
        now=now,
    )


def _decide(store, *, event=None, epoch=2, now=1010.0, rotation_reason="new_topic"):
    return store.evaluate_topic_bridge(
        CHAT,
        event=event or _canonical(),
        target_topic_epoch=epoch,
        rotation_reason=rotation_reason,
        now=now,
    )


def test_explicit_reply_has_verified_source_and_read_only_bounded_decision():
    store = ConversationContinuityStore()
    _previous(store)
    decision = _decide(store, event=_canonical(reply_id="old-1"))
    assert decision.allowed is True
    assert decision.reason == "explicit_reply"
    assert decision.confidence >= 0.8
    assert decision.source_chat_key == decision.target_chat_key == CHAT
    assert (decision.source_topic_epoch, decision.target_topic_epoch) == (1, 2)
    assert decision.evidence_event_ids == ("old-1",)
    assert 0 < decision.expires_at - decision.created_at <= store.BRIDGE_TTL_SECONDS
    assert decision.is_active(1010.0)
    assert not decision.is_active(decision.expires_at + 0.01)
    assert len(decision.turns) <= 2
    assert len(decision.prompt_text()) <= store.BRIDGE_MAX_PROMPT_CHARS
    assert "old-1" not in decision.prompt_text()
    assert "alice" not in decision.prompt_text()


def test_open_loop_short_answer_and_same_actor_followup_are_distinct():
    store = ConversationContinuityStore()
    _previous(store, open_loop=True)
    answer = _decide(store, event=_canonical(text="周五有空"))
    assert answer.allowed and answer.reason == "open_loop_answer"
    assert answer.source_open_loop == "周五有空吗？"
    store.update_topic_anchor(CHAT, event=_canonical("answer", text="周五有空"), subject_preview="项目计划", now=1001)
    assert store.topic_anchor_view(CHAT, now=1002)["open_loop"] == ""
    followup = _decide(store, event=_canonical(text="然后呢"))
    assert followup.allowed and followup.reason == "same_actor_followup"


def test_structured_reply_wins_over_weak_text_and_unknown_target_is_denied():
    store = ConversationContinuityStore()
    _previous(store)
    explicit = _decide(store, event=_canonical(text="这个呢", reply_id="old-1"))
    assert explicit.reason == "explicit_reply"
    missing = _decide(store, event=_canonical(text="这个呢", reply_id="unknown"))
    assert not missing.allowed and missing.reason == "unverified_reply_target"


def test_bridge_rejects_cross_chat_stale_closed_or_unrelated_context():
    store = ConversationContinuityStore()
    _previous(store)
    assert _decide(store, event=_canonical(chat_id="default:GroupMessage:group-2", reply_id="old-1")).reason == "chat_mismatch"
    assert _decide(store, event=_canonical(text="换个话题，明天天气如何", reply_id="old-1")).reason == "explicit_topic_switch"
    assert _decide(store, event=_canonical(text="明天天气怎么样")).reason == "unrelated_topic"
    assert _decide(store, event=_canonical(text="好的"), now=1000 + store.BRIDGE_TTL_SECONDS + 1).reason == "expired"
    assert _decide(store, event=_canonical(text="然后呢"), epoch=3).reason == "non_adjacent_topic"
    assert _decide(store, event=_canonical(text="然后呢"), epoch=1).reason == "same_topic"
    assert _decide(store, event=_canonical(text="然后呢"), rotation_reason="explicit_topic_switch").reason == "explicit_topic_switch"


def test_weak_text_needs_actor_and_real_recent_evidence():
    store = ConversationContinuityStore()
    assert _decide(store).reason == "no_source_anchor"
    _previous(store)
    assert _decide(store, event=_canonical(actor_id="bob", text="然后呢")).reason == "actor_mismatch"
    assert _decide(store, event=_canonical(text="他怎么样了")).reason == "insufficient_evidence"
    assert _decide(store, event=_canonical(text="算了，不聊了", reply_id="old-1")).reason == "explicit_topic_switch"
    assert _decide(store, event=_canonical(text="然后呢", reply_id="old-1"), now=1000 + store.TURN_TTL_SECONDS + 1).reason == "expired"


def test_bridge_projection_is_dynamic_and_denied_bridge_hides_old_anchor():
    store = ConversationContinuityStore()
    now = time.time()
    _previous(store, now=now)

    class Event:
        message_str = "然后呢"
        unified_msg_origin = CHAT

        def __init__(self, canonical, reason="new_topic"):
            self.extra = {"astrmai_conversation_event": canonical}
            DialogHistoryPolicy(group_id=CHAT, topic_epoch=2, rotation_reason=reason).bind(self)

        def get_extra(self, name, default=None):
            return self.extra.get(name, default)

        def set_extra(self, name, value):
            self.extra[name] = value

        def get_sender_name(self):
            return "Alice"

    loader = PlanningInputLoader(SimpleNamespace(conversation_continuity=store))
    accepted = Event(_canonical(reply_id="old-1"))
    loader._apply_continuity(accepted, loader._continuity_snapshot(CHAT))
    decision = ensure_turn_context(accepted).continuity.topic_bridge
    assert decision.allowed and decision.reason == "explicit_reply"
    envelope = PromptEnvelope()
    asyncio.run(loader.load_prompt_inputs(accepted, CHAT, envelope, [], 0))
    refiner = PromptRefiner(memory_engine=None, config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)))
    system, prompt = asyncio.run(refiner.refine_prompt(event=accepted, system_prompt="stable system prompt", prompt="", context={"disable_rag_injection": False}, prompt_envelope=envelope))
    assert system == "stable system prompt"
    assert '<untrusted_context type="derived" source="cross_topic_bridge"' in prompt
    assert "项目计划" in prompt

    denied = Event(_canonical(text="换个话题，明天天气如何", reply_id="old-1"), reason="explicit_topic_switch")
    loader._apply_continuity(denied, loader._continuity_snapshot(CHAT))
    assert not ensure_turn_context(denied).continuity.topic_bridge.allowed
    assert denied.get_extra("astrmai_topic_attention_anchor_prompt", "") == ""
    assert denied.get_extra("astrmai_cross_topic_bridge_prompt", "") == ""
