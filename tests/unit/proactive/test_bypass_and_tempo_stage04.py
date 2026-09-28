from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from astrmai.conversation.attention.gate import AttentionGate
from astrmai.conversation.contracts.reread import RereadActionRequest
from astrmai.conversation.contracts.turn_outcome import get_turn_outcome
from astrmai.conversation.execution.reread_action_dispatcher import RereadActionDispatcher
from astrmai.proactive.dispatcher import ProactiveDispatcher, ProactiveMessageIntent
from astrmai.proactive.dream_scheduler import DreamScheduler
from astrmai.proactive.group_signin_service import GroupSigninService


class _Event:
    def __init__(self, text: str = "早", *, component_type: str = "Plain") -> None:
        self.message_str = text
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self.message_obj = SimpleNamespace(
            message_id="message-1",
            message=[SimpleNamespace(type=component_type)],
        )
        self._extra: dict[str, object] = {}

    def get_sender_id(self) -> str:
        return "user-1"

    def get_sender_name(self) -> str:
        return "Alice"

    def get_self_id(self) -> str:
        return "bot-1"

    def get_group_id(self) -> str:
        return "group-1"

    def get_extra(self, key: str, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key: str, value) -> None:
        self._extra[key] = value


class _Context:
    def __init__(self) -> None:
        self.sent = []

    async def send_message(self, origin, chain):
        self.sent.append((origin, chain))
        return "outbound-1"


class _Coordinator:
    def __init__(self, snapshot=None, *, error: Exception | None = None) -> None:
        self.snapshot = dict(snapshot or {})
        self.error = error
        self.claims: set[str] = set()

    async def get_activity_snapshot(self, _chat_id: str) -> dict:
        if self.error is not None:
            raise self.error
        return dict(self.snapshot)

    async def claim_send(self, _chat_id: str, send_key: str) -> bool:
        if send_key in self.claims:
            return False
        self.claims.add(send_key)
        return True

    async def commit_send(self, _chat_id: str, send_key: str, _message_ids) -> bool:
        self.claims.discard(send_key)
        return True

    async def mark_send_failed(self, _chat_id: str, send_key: str, _reason: str) -> bool:
        self.claims.discard(send_key)
        return True


class _Cooldowns:
    def __init__(self, active: bool = False) -> None:
        self.active = active

    def is_cooldown_active(self, _chat_id: str, _intent_class: str) -> bool:
        return self.active


def _reread_request(text: str = "早") -> RereadActionRequest:
    return RereadActionRequest(
        chat_id="default:GroupMessage:group-1",
        text=text,
        fingerprint=f"fingerprint:{text}",
        trigger_kind="group_reread_passive",
        source_event_ids=("message-1",),
    )


def _reread_config(*, quiet: bool = False):
    return SimpleNamespace(
        life=SimpleNamespace(
            proactive_quiet_hours=["00:00-23:59"] if quiet else [],
        ),
        conversation=SimpleNamespace(group_reread_recent_bot_guard_sec=8.0),
    )


def test_reread_fast_path_sends_in_normal_group_culture() -> None:
    context = _Context()
    dispatcher = RereadActionDispatcher(
        context=context,
        config=_reread_config(),
        runtime_coordinator=_Coordinator(
            {"latest_activity_ts": time.time(), "latest_activity_sender_id": "user-1", "wait_targets": []}
        ),
        state_engine=SimpleNamespace(bot_id="bot-1"),
        proactive_dispatcher=_Cooldowns(False),
    )

    result = asyncio.run(dispatcher.dispatch(_Event(), _reread_request()))

    assert result.sent is True
    assert len(context.sent) == 1


def test_reread_quiet_serious_wait_recent_bot_and_cooldown_are_blocked() -> None:
    cases = [
        (
            "quiet_hours",
            _reread_config(quiet=True),
            {"latest_activity_ts": time.time(), "latest_activity_sender_id": "user-1", "wait_targets": []},
            _Cooldowns(False),
            "早",
        ),
        (
            "serious_topic",
            _reread_config(),
            {"latest_activity_ts": time.time(), "latest_activity_sender_id": "user-1", "wait_targets": []},
            _Cooldowns(False),
            "我想自杀",
        ),
        (
            "user_waiting",
            _reread_config(),
            {"latest_activity_ts": time.time(), "latest_activity_sender_id": "user-1", "wait_targets": ["user-2"]},
            _Cooldowns(False),
            "早",
        ),
        (
            "recent_bot_message",
            _reread_config(),
            {"latest_activity_ts": time.time(), "latest_activity_sender_id": "bot-1", "wait_targets": []},
            _Cooldowns(False),
            "早",
        ),
        (
            "cooldown",
            _reread_config(),
            {"latest_activity_ts": time.time(), "latest_activity_sender_id": "user-1", "wait_targets": []},
            _Cooldowns(True),
            "早",
        ),
    ]

    for reason, config, snapshot, cooldowns, text in cases:
        context = _Context()
        dispatcher = RereadActionDispatcher(
            context=context,
            config=config,
            runtime_coordinator=_Coordinator(snapshot),
            state_engine=SimpleNamespace(bot_id="bot-1"),
            proactive_dispatcher=cooldowns,
        )
        result = asyncio.run(dispatcher.dispatch(_Event(text), _reread_request(text)))
        assert result.sent is False, reason
        assert result.detail == reason
        assert context.sent == []


def test_reread_state_failure_does_not_send_or_claim_the_event() -> None:
    context = _Context()
    dispatcher = RereadActionDispatcher(
        context=context,
        config=_reread_config(),
        runtime_coordinator=_Coordinator(error=RuntimeError("snapshot unavailable")),
        state_engine=SimpleNamespace(bot_id="bot-1"),
        proactive_dispatcher=_Cooldowns(False),
    )
    event = _Event()

    result = asyncio.run(dispatcher.dispatch(event, _reread_request()))

    assert result.sent is False
    assert result.detail == "guard_unavailable"
    assert context.sent == []
    outcome = get_turn_outcome(event)
    assert outcome is not None
    assert outcome.output_claim == ""
    assert outcome.reply_sent is False


class _IntentSink:
    def __init__(self, *, allowed: bool = True, queued: bool = True) -> None:
        self.allowed = allowed
        self.queued = queued
        self.intents: list[ProactiveMessageIntent] = []

    async def dispatch(self, intent, on_complete=None):
        self.intents.append(intent)
        return SimpleNamespace(
            allowed=self.allowed,
            synthetic_event_queued=self.queued,
            blocked_reason="" if self.allowed else "quiet_hours",
            reply_sent=False,
            safety_checks={},
        )


def test_dream_visible_uses_dispatcher_intent_generation_and_cooldown() -> None:
    sink = _IntentSink()
    scheduler = DreamScheduler(
        context=SimpleNamespace(),
        memory_engine=SimpleNamespace(),
        config=SimpleNamespace(life=SimpleNamespace(dream_interval_min=30, dream_visible=True)),
        semaphore=asyncio.Semaphore(1),
        dream_visible=True,
        dispatcher=sink,
        state_engine=SimpleNamespace(
            get_state=lambda _chat_id: asyncio.sleep(0, result=SimpleNamespace(proactive_generation=7))
        ),
    )

    settled, queued, reason = asyncio.run(
        scheduler._dispatch_visible_dream("default:GroupMessage:group-1", "一段梦境", {"run_id": "dream-1"})
    )

    assert (settled, queued, reason) == (True, True, "")
    assert len(sink.intents) == 1
    intent = sink.intents[0]
    assert intent.source == "dream_visible"
    assert intent.intent == "share"
    assert intent.metadata["captured_generation"] == 7
    assert intent.metadata["intent_class"] == "scheduled_ritual"
    assert intent.cooldown > 0


def test_group_signin_has_switch_and_dispatch_contract() -> None:
    sink = _IntentSink()
    state = SimpleNamespace(
        chat_id="default:GroupMessage:group-1",
        proactive_generation=4,
        group_config={},
        is_dirty=False,
    )

    class _Persistence:
        async def save_chat_state(self, _chat_id, _state):
            return None

    disabled = GroupSigninService(
        state_engine=SimpleNamespace(get_active_states=lambda: [state]),
        persistence=_Persistence(),
        dispatcher=sink,
        config=SimpleNamespace(life=SimpleNamespace(enable_proactive=False)),
    )
    sign_calls = []
    disabled._sign_group = lambda group_id: asyncio.sleep(0, result=sign_calls.append(group_id) or True)
    now = time.mktime((2026, 9, 28, 8, 0, 0, 0, 0, -1))
    asyncio.run(disabled.run_once(now))
    assert sign_calls == []
    assert disabled.describe_status()["enabled"] is False

    enabled = GroupSigninService(
        state_engine=SimpleNamespace(),
        persistence=_Persistence(),
        dispatcher=sink,
        config=SimpleNamespace(life=SimpleNamespace(enable_proactive=True, wakeup_cooldown=600)),
    )
    asyncio.run(enabled._dispatch_after_sign(state.chat_id, "group-1", 4))
    intent = sink.intents[-1]
    assert intent.intent == "ritual"
    assert intent.metadata["captured_generation"] == 4
    assert intent.metadata["intent_class"] == "scheduled_ritual"
    assert intent.cooldown == 600


class _MediaStateEngine:
    def __init__(self) -> None:
        self.bot_id = "bot-1"
        self.config = SimpleNamespace()
        self.transitions = []

    async def record_real_user_activity_transition(self, chat_id: str, **kwargs):
        self.transitions.append((chat_id, kwargs))
        before = SimpleNamespace(proactive_generation=2)
        after = SimpleNamespace(proactive_generation=3)
        return before, after

    async def record_real_user_activity(self, chat_id: str, **kwargs):
        return (await self.record_real_user_activity_transition(chat_id, **kwargs))[1]


def test_semantic_media_invalidates_proactive_generation() -> None:
    for component_type in ("Image", "Json", "Record", "Video"):
        state_engine = _MediaStateEngine()
        gate = object.__new__(AttentionGate)
        gate.state_engine = state_engine
        gate.runtime_coordinator = None
        gate.conversation_continuity = None
        gate._get_or_create_session = lambda _chat_id: asyncio.sleep(
            0, result=SimpleNamespace(last_active_user_time=0.0)
        )
        gate._is_at_bot_event = lambda _event, _self_id: False
        gate._is_reply_to_bot_event = lambda _event, _self_id: False
        event = _Event("", component_type=component_type)

        asyncio.run(gate._record_event_activity(event.unified_msg_origin, event, event.get_sender_id()))

        assert len(state_engine.transitions) == 1, component_type
        assert event.get_extra("astrmai_proactive_generation_invalidated") is True


def _dispatcher_for_cooldown_tests() -> ProactiveDispatcher:
    dispatcher = ProactiveDispatcher(
        attention_gate=SimpleNamespace(inject_external_event=lambda *_args, **_kwargs: True),
        state_engine=SimpleNamespace(bot_id="bot-1"),
        config=SimpleNamespace(life=SimpleNamespace(proactive_quiet_hours=[], wakeup_min_energy=0.0)),
    )
    dispatcher._activity_snapshot = lambda _chat_id: asyncio.sleep(
        0,
        result={
            "latest_activity_ts": time.time(),
            "latest_activity_sender_id": "user-1",
            "wait_targets": [],
            "executor_pending": 0,
        },
    )
    dispatcher._proactive_generation_current = lambda _intent: asyncio.sleep(0, result=(True, 1))
    dispatcher._state_energy = lambda _chat_id: asyncio.sleep(0, result=1.0)
    dispatcher._scheduling_snapshot = lambda _chat_id: asyncio.sleep(0, result={})
    dispatcher._feedback_snapshot = lambda _chat_id, actor_id="": asyncio.sleep(0, result={})
    return dispatcher


def test_intent_class_cooldowns_are_isolated_and_direct_wakeup_bypasses() -> None:
    dispatcher = _dispatcher_for_cooldown_tests()
    now = time.time()
    dispatcher.set_cooldown("chat-1", "greeting", now + 60)

    greeting = ProactiveMessageIntent(
        "chat-1",
        "wakeup",
        "silence",
        "say hi",
        intent="break_silence",
        metadata={"captured_generation": 1},
    )
    ritual = ProactiveMessageIntent(
        "chat-1",
        "group_signin",
        "daily",
        "ritual",
        intent="ritual",
        metadata={"captured_generation": 1},
    )
    direct = ProactiveMessageIntent(
        "chat-1",
        "wakeup",
        "direct question",
        "answer",
        intent="answer",
        metadata={"captured_generation": 1, "direct_wakeup": True},
    )

    greeting_result = asyncio.run(dispatcher._safety_check(greeting, now=now))
    ritual_result = asyncio.run(dispatcher._safety_check(ritual, now=now))
    direct_result = asyncio.run(dispatcher._safety_check(direct, now=now))

    assert greeting_result[:2] == (False, "cooldown")
    assert ritual_result[0] is True
    assert direct_result[0] is True

