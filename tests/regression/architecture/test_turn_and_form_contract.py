from types import SimpleNamespace

from astrmai.conversation.attention.participation_policy import ParticipationPolicy
from astrmai.conversation.contracts.reply_form import FORM_SPECS, ReplyForm, ReplyFormDecision, normalize_reply_form
from astrmai.conversation.planning.cognitive_loop import CognitiveLoop


class _Event:
    def __init__(self, actor_id, text, timestamp, *, extras=None, self_id="bot-1"):
        self.message_str = text
        self.timestamp = timestamp
        self._actor_id = actor_id
        self._extras = dict(extras or {})
        self._self_id = self_id

    def get_sender_id(self):
        return self._actor_id

    def get_self_id(self):
        return self._self_id

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)


def test_social_signals_record_dyad_question_and_recent_bot_turn():
    policy = ParticipationPolicy()
    first = _Event("user-a", "你觉得呢？", 100.0)
    second = _Event("user-b", "我也不知道", 101.0)
    focus = _Event("user-a", "为什么？", 102.0)
    result, _ = policy.evaluate(focus_event=focus, batch_events=[first, second, focus], now=106.0)
    assert "human_dyad_active" in result.social_signals
    assert "open_question_waiting" in result.social_signals

    bot = _Event("bot-1", "我刚说过", 101.5)
    result, _ = policy.evaluate(focus_event=focus, batch_events=[bot, focus], now=106.0)
    assert "bot_recently_spoke" in result.social_signals
    assert "open_question_waiting" not in result.social_signals

    answered = _Event("user-b", "我来回答了", 103.0)
    result, _ = policy.evaluate(focus_event=answered, batch_events=[focus, answered], now=106.0)
    assert "open_question_waiting" not in result.social_signals
    assert dict(result.social_evidence).get("open_question_answered") == "later_human_event_in_window"


def test_human_dyad_gets_wait_admission_without_direct_wakeup():
    policy = ParticipationPolicy()
    events = [
        _Event("user-a", "第一句", 100.0),
        _Event("user-b", "第二句", 101.0),
        _Event("user-a", "第三句", 102.0),
    ]
    result, _ = policy.evaluate(focus_event=events[-1], batch_events=events, now=102.0)
    assert "human_dyad_active" in result.social_signals
    assert result.social_admission == "wait"


def test_direct_wakeup_is_a_structured_signal():
    result, _ = ParticipationPolicy().evaluate(
        focus_event=_Event("user-a", "@bot 你好", 100.0, extras={"is_at_bot": True}),
        batch_events=[],
        now=100.0,
    )
    assert "direct_wakeup" in result.social_signals


def test_reply_form_is_closed_and_unknown_forms_degrade_to_silence():
    assert {item.value for item in ReplyForm} == {
        "silence", "short_ack", "quote_reply", "answer", "comfort", "reaction", "topic_start"
    }
    decision = normalize_reply_form("invented_action")
    assert decision.form is ReplyForm.SILENCE
    assert decision.reason == "unsupported_form"
    assert decision.degraded


def test_each_form_has_serializable_sender_spec_and_round_trip():
    for form in ReplyForm:
        decision = normalize_reply_form(form, intent="answer" if form in {ReplyForm.ANSWER, ReplyForm.SHORT_ACK, ReplyForm.QUOTE_REPLY} else "")
        payload = decision.as_dict()
        restored = ReplyFormDecision.from_dict(payload)
        assert restored.form is decision.form
        assert form in FORM_SPECS
        assert FORM_SPECS[form].sender_route

    quote = normalize_reply_form(
        ReplyForm.QUOTE_REPLY,
        intent="answer",
        target={"target_event_id": "event-1", "confidence": 0.9},
    )
    assert quote.form is ReplyForm.QUOTE_REPLY
    assert quote.spec.allow_quote

    unsupported_reaction = normalize_reply_form(
        ReplyForm.REACTION,
        intent="answer",
        target={"target_event_id": "event-1", "confidence": 0.9},
        reaction_supported=False,
    )
    assert unsupported_reaction.form is ReplyForm.SHORT_ACK
    assert unsupported_reaction.reason == "reaction_unsupported"


def test_cognitive_form_follows_social_intent_without_open_action_enum():
    assert CognitiveLoop._normalize_form(None, social_intent="comfort", reply_need="reply") == "comfort"
    assert CognitiveLoop._normalize_form("reaction", social_intent="answer", reply_need="reply") == "short_ack"
    assert CognitiveLoop._normalize_form("answer", social_intent="answer", reply_need="wait") == "silence"
