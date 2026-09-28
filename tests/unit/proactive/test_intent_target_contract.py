from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrmai.conversation.contracts.turn_context import ensure_turn_context
from astrmai.conversation.contracts.turn_target import TargetKind, TurnTarget
from astrmai.conversation.planning.planner import Planner
from astrmai.conversation.execution.reply_post_send import ReplyPostSendMixin
from astrmai.proactive.dispatcher import (
    ALLOWED_PROACTIVE_INTENTS,
    ProactiveDispatcher,
    ProactiveMessageIntent,
    normalize_proactive_intent,
    normalize_proactive_target,
)


class _Event:
    def __init__(self, extras=None):
        self._extras = dict(extras or {})

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


def _target(**overrides):
    values = {
        "target_kind": TargetKind.ACTOR,
        "target_actor_id": "actor-1",
        "target_event_id": "message-1",
        "target_thread_id": "thread-1",
        "target_source": "focus_resolver",
        "evidence": "explicit_reply",
        "confidence": 0.92,
    }
    values.update(overrides)
    return TurnTarget(**values)


def test_allowed_intents_round_trip_and_legacy_positional_order():
    for value in ALLOWED_PROACTIVE_INTENTS:
        intent = ProactiveMessageIntent("chat", "wakeup", "reason", "guidance", intent=value)
        restored = ProactiveMessageIntent.from_value(intent.as_dict())
        assert restored.intent == value
        assert restored.source == "wakeup"
        assert restored.metadata == {}

    # Historical positional argument five is suggested_social_intent.
    legacy = ProactiveMessageIntent("chat", "wakeup", "reason", "guidance", "join")
    assert legacy.suggested_social_intent == "join"
    assert legacy.intent == "break_silence"


def test_unknown_intent_has_source_reason_fallback():
    assert normalize_proactive_intent("new_priority", source="group_signin") == "ritual"
    assert normalize_proactive_intent("new_priority", source="wakeup", reason="direct question") == "answer"
    assert normalize_proactive_intent("new_priority", source="unknown") == "break_silence"


def test_all_active_sources_have_stable_intent_mapping_and_keep_reason_metadata():
    expected = {
        "wakeup": "break_silence",
        "heartflow": "followup",
        "scheduled_scenario": "ritual",
        "group_signin": "ritual",
    }
    for source, expected_intent in expected.items():
        candidate = ProactiveMessageIntent("chat", source, "legacy_reason", "line")
        assert candidate.intent == expected_intent
        assert candidate.source == source
        assert candidate.reason == "legacy_reason"
        candidate.metadata["captured_generation"] = 7
        assert candidate.metadata["captured_generation"] == 7


def test_target_round_trip_and_invalid_target_degrades_to_public_empty_target():
    target = _target()
    intent = ProactiveMessageIntent("chat", "heartflow", "reason", "line", target=target)
    restored = ProactiveMessageIntent.from_value(intent.as_dict())
    assert restored.target == target

    for metadata in (
        {"target_actor_exists": False},
        {"target_message_exists": False},
        {"target_thread_exists": False},
        {"target_expired": True},
        {"target_source_trusted": False},
    ):
        assert normalize_proactive_target(target, metadata=metadata) == TurnTarget()
    assert normalize_proactive_target(_target(confidence=0.49)) == TurnTarget()
    assert normalize_proactive_target(_target(target_source="", evidence="")) == TurnTarget()
    assert normalize_proactive_target(None) == TurnTarget()


def test_dispatcher_transmits_intent_and_target_without_guessing():
    captured = {}

    class Gate:
        async def inject_external_event(self, chat_id, event_data):
            captured.update(event_data)
            return True

    dispatcher = ProactiveDispatcher(attention_gate=Gate())
    dispatcher._safety_check = lambda intent, now: asyncio.sleep(0, result=(True, "", {}))
    dispatcher._proactive_generation_current = lambda intent: asyncio.sleep(0, result=(True, None))
    intent = ProactiveMessageIntent(
        "chat",
        "heartflow",
        "comfort",
        "line",
        target=_target(),
        intent="comfort",
    )
    decision = asyncio.run(dispatcher.dispatch(intent))
    assert decision.synthetic_event_queued is True
    extra = captured["extra"]
    assert extra["astrmai_proactive_intent"] == "comfort"
    assert extra["astrmai_proactive_target"] == _target().as_dict()


def test_planner_consumes_target_and_empty_target_stays_public():
    planner = object.__new__(Planner)
    event = _Event(
        {
            "astrmai_is_proactive_event": True,
            "astrmai_proactive_intent": "followup",
            "astrmai_proactive_target": _target().as_dict(),
        }
    )
    planner._apply_proactive_context(event)
    context = ensure_turn_context(event)
    assert context.proactive.intent == "followup"
    assert context.attention.turn_target.target_actor_id == "actor-1"

    empty_event = _Event({"astrmai_is_proactive_event": True})
    planner._apply_proactive_context(empty_event)
    assert ensure_turn_context(empty_event).attention.turn_target == TurnTarget()

    assert ReplyPostSendMixin()._resolve_reply_target(empty_event) == TurnTarget()


def test_dynamic_contract_is_user_prompt_only_by_source_boundary():
    source = open("astrmai/conversation/planning/prompt_refiner.py", encoding="utf-8").read()
    marker = '"---主动动机与目标（仅本轮动态参考）---'
    assert marker in source
    assert source.index(marker) > source.index("sections = []")
    assert "final_system_prompt =" in source
    assert source.index(marker) > source.index("final_system_prompt =")
