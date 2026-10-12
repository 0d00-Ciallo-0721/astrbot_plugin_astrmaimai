"""A quote is context; only new content can make it a reply wakeup."""
from types import SimpleNamespace

import pytest

from config import AstrMaiConfig
from astrmai.conversation.attention.event_normalizer import build_normalized_events
from astrmai.conversation.attention.gate import AttentionGate
from tests.unit.test_prompt_context_closure import ComponentType, Event


def plain(text):
    return SimpleNamespace(type=ComponentType.Plain, text=text)


@pytest.mark.parametrize("body", ["", " \t\n", "\u200b\u200c\u200d\ufeff", " \u2060\u200b "])
@pytest.mark.parametrize("sender_id,sender_name", [("bot", "Bot"), ("", "Mai")])
def test_empty_reply_is_context_without_strong_wakeup(body, sender_id, sender_name):
    gate = AttentionGate(state_engine=SimpleNamespace(bot_id="bot"), judge=None,
                         config=AstrMaiConfig(), sensors=None, system2_callback=None)
    gate.config.system1.nicknames = ["Mai"]
    reply = SimpleNamespace(type=ComponentType.Reply, id="quoted", sender_id=sender_id,
                            sender_nickname=sender_name, message_str="old bot answer",
                            chain=[plain("old bot answer")])
    event = Event(body, [reply, plain(body)])
    candidate = build_normalized_events(gate, [event], "bot")[0]
    assert not candidate.is_reply_to_bot
    assert not gate._is_strong_wakeup(event)
    assert candidate.canonical_event.reply_target_event_id == "quoted"
    assert candidate.canonical_event.reply_target_text == "old bot answer"


@pytest.mark.parametrize("body", ["？", "嗯", "为什么？", "\u200b继续\u200b"])
def test_new_reply_text_keeps_short_followups(body):
    gate = AttentionGate(state_engine=SimpleNamespace(bot_id="bot"), judge=None,
                         config=AstrMaiConfig(), sensors=None, system2_callback=None)
    event = Event(body, [SimpleNamespace(type=ComponentType.Reply, sender_id="bot"), plain(body)])
    assert gate._is_reply_to_bot_event(event, "bot")
    assert gate._is_strong_wakeup(event)


def test_rendered_at_other_is_not_reply_body():
    gate = AttentionGate(state_engine=SimpleNamespace(bot_id="bot"), judge=None,
                         config=AstrMaiConfig(), sensors=None, system2_callback=None)
    event = Event("@Carol(123)", [SimpleNamespace(type=ComponentType.Reply, sender_id="bot"),
                                 SimpleNamespace(type=ComponentType.At, qq="123")])
    assert not gate._is_reply_to_bot_event(event, "bot")
    assert not gate._is_strong_wakeup(event)


def test_explicit_at_bot_keeps_wakeup_even_without_reply_body():
    gate = AttentionGate(state_engine=SimpleNamespace(bot_id="bot"), judge=None,
                         config=AstrMaiConfig(), sensors=None, system2_callback=None)
    event = Event("", [SimpleNamespace(type=ComponentType.Reply, sender_id="bot"), plain(""),
                      SimpleNamespace(type=ComponentType.At, qq="bot")])
    assert not gate._is_reply_to_bot_event(event, "bot")
    assert gate._is_strong_wakeup(event)


def test_empty_plain_cannot_use_rendered_message_or_quoted_text_as_body():
    gate = AttentionGate(state_engine=SimpleNamespace(bot_id="bot"), judge=None,
                         config=AstrMaiConfig(), sensors=None, system2_callback=None)
    event = Event("@Carol old bot answer", [SimpleNamespace(type=ComponentType.Reply, sender_id="bot",
                                                          message_str="old bot answer"), plain("")])
    assert not gate._is_reply_to_bot_event(event, "bot")


def test_repeated_quoted_text_is_valid_new_plain_body():
    gate = AttentionGate(state_engine=SimpleNamespace(bot_id="bot"), judge=None,
                         config=AstrMaiConfig(), sensors=None, system2_callback=None)
    event = Event("old bot answer", [SimpleNamespace(type=ComponentType.Reply, sender_id="bot",
                                                     message_str="old bot answer"), plain("old bot answer")])
    assert gate._is_reply_to_bot_event(event, "bot")


def test_legacy_reply_only_event_with_distinct_text_still_wakes():
    gate = AttentionGate(state_engine=SimpleNamespace(bot_id="bot"), judge=None,
                         config=AstrMaiConfig(), sensors=None, system2_callback=None)
    event = Event("继续解释", [SimpleNamespace(type=ComponentType.Reply, sender_id="bot",
                                              message_str="old bot answer")])
    assert gate._is_reply_to_bot_event(event, "bot")


def test_reply_author_name_fallback_with_valid_body_still_wakes():
    config = AstrMaiConfig()
    config.system1.nicknames = ["Mai"]
    gate = AttentionGate(state_engine=SimpleNamespace(bot_id="bot"), judge=None,
                         config=config, sensors=None, system2_callback=None)
    event = Event("继续", [SimpleNamespace(type=ComponentType.Reply, sender_id="",
                                        sender_nickname="Mai"), plain("继续")])
    assert gate._is_reply_to_bot_event(event, "bot")


@pytest.mark.parametrize("kind", ["Image", "Record", "Video", "File"])
def test_new_media_reply_is_not_an_empty_quote(kind):
    gate = AttentionGate(state_engine=SimpleNamespace(bot_id="bot"), judge=None,
                         config=AstrMaiConfig(), sensors=None, system2_callback=None)
    event = Event("", [SimpleNamespace(type=ComponentType.Reply, sender_id="bot"),
                      SimpleNamespace(type=kind)])
    assert gate._is_reply_to_bot_event(event, "bot")


def test_media_only_inside_quote_is_not_new_followup():
    gate = AttentionGate(state_engine=SimpleNamespace(bot_id="bot"), judge=None,
                         config=AstrMaiConfig(), sensors=None, system2_callback=None)
    event = Event("", [SimpleNamespace(type=ComponentType.Reply, sender_id="bot",
                      chain=[SimpleNamespace(type="Image")])])
    assert not gate._is_reply_to_bot_event(event, "bot")


def test_split_plain_reply_keeps_content_without_mutating_event():
    gate = AttentionGate(state_engine=SimpleNamespace(bot_id="bot"), judge=None,
                         config=AstrMaiConfig(), sensors=None, system2_callback=None)
    chain = [SimpleNamespace(type=ComponentType.Reply, sender_id="bot"),
             plain("\u200b"), plain("？"), plain(" ")]
    event = Event("\u200b？ ", chain)
    assert gate._is_reply_to_bot_event(event, "bot")
    assert event.message_str == "\u200b？ "
    assert event.message_obj.message == chain


def test_nested_quote_and_unknown_author_keep_current_speaker():
    from astrmai.conversation.attention.turn_target_resolver import resolve_turn_target
    from astrmai.conversation.contracts.focus_context import FocusThreadContext
    from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin
    from astrmai.conversation.planning.prompt_refiner import PromptRefiner

    gate = AttentionGate(state_engine=SimpleNamespace(bot_id="bot"), judge=None,
                         config=AstrMaiConfig(), sensors=None, system2_callback=None)
    inner = SimpleNamespace(type=ComponentType.Reply, id="inner", sender_id="carol")
    outer = SimpleNamespace(type=ComponentType.Reply, id="outer", sender_id="outside-group",
                            sender_nickname="UnknownAuthor", chain=[inner], message_str="quoted text")
    event = Event("为什么？", [outer, plain("为什么？")])
    candidate = build_normalized_events(gate, [event], "bot")[0]
    canonical = candidate.canonical_event
    assert canonical.reply_target_actor_id == "outside-group"
    assert canonical.reply_target_event_id == "outer"
    target, actors = resolve_turn_target(candidate, None, [candidate], bot_id="bot")
    context = FocusThreadContext(focus_event=event, turn_target=target, actor_set=actors)
    block = PlannerPromptContextMixin._build_current_speaker_block(event, context, is_lightweight_event=False)
    assert "实际发言人：Alice(QQ:alice)" in block
    assert "outside-group" in block
    assert "carol" not in block
    assert "Alice" in PromptRefiner._render_final_speaker_lock(block)
    assert not candidate.is_reply_to_bot


def test_repeated_nickname_keeps_one_prefix_score():
    from astrmai.conversation.ingress.sensors import PreFilters

    config = AstrMaiConfig()
    event = Event("MaiMaiMai", [plain("MaiMaiMai")])
    score = PreFilters(config)._calculate_nickname_wakeup_score(event, event.message_str, ["Mai"], "bot")
    assert score == config.system1.nickname_prefix_score
