from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrmai.proactive.dispatcher import ProactiveDispatcher, ProactiveMessageIntent
from astrmai.proactive.group_signin_service import GroupSigninService
from astrmai.proactive.heartflow.manager import HeartflowManager
from astrmai.proactive.heartflow.models import HeartflowImpulseDecision, HeartflowPulse
from astrmai.proactive.wakeup_service import WakeupService


class _IntentSink:
    def __init__(self):
        self.intents = []

    async def dispatch(self, intent, on_complete=None):
        self.intents.append(intent)
        return SimpleNamespace(allowed=True, synthetic_event_queued=True, reply_sent=False, safety_checks={})


def test_all_proactive_sources_capture_the_chat_generation():
    async def _opening(_chat_id):
        return "one short line"

    wakeup = WakeupService(
        context=SimpleNamespace(),
        state_engine=SimpleNamespace(),
        persistence=SimpleNamespace(load_persona_cache=lambda: {}),
        call_background_lane=None,
        config=SimpleNamespace(),
    )
    wakeup.generate_opening_line = _opening
    wakeup_intent = asyncio.run(
        wakeup.build_wakeup_intent(SimpleNamespace(chat_id="ff:FriendMessage:1", proactive_generation=7), 1, 2)
    )

    sink = _IntentSink()
    heartflow = HeartflowManager(
        dispatcher=sink,
        state_engine=SimpleNamespace(
            get_state=lambda _chat_id: asyncio.sleep(
                0,
                result=SimpleNamespace(energy=0.8, mood=0.1, proactive_generation=7),
            )
        ),
    )
    heartflow_state = asyncio.run(
        heartflow._build_chat_state(
            "ff:GroupMessage:1",
            {"latest_activity_ts": 1, "latest_activity_preview": "hello"},
            now=1000,
            session=None,
        )
    )
    assert heartflow_state.captured_generation == 7
    pulse = HeartflowPulse("ff:GroupMessage:1", 1, "proactive_hint", "test", "say", "join", "chat", 0.9)
    decision = HeartflowImpulseDecision("ff:GroupMessage:1", 1, "proactive_hint", visible_candidate_allowed=True)
    asyncio.run(heartflow._maybe_dispatch_visible_candidate(heartflow_state, pulse, decision))

    signin = GroupSigninService(state_engine=SimpleNamespace(), persistence=SimpleNamespace(), dispatcher=sink)
    asyncio.run(signin._dispatch_after_sign("ff:GroupMessage:1", "1", 7))

    assert wakeup_intent.metadata["captured_generation"] == 7
    assert sink.intents[0].metadata["captured_generation"] == 7
    assert sink.intents[1].metadata["captured_generation"] == 7


def test_generation_check_allows_current_and_blocks_superseded_candidate():
    class _State:
        async def is_proactive_generation_current(self, _chat_id, captured):
            return captured == 2

    dispatcher = ProactiveDispatcher(state_engine=_State())
    current = ProactiveMessageIntent("chat", "wakeup", "test", "line", metadata={"captured_generation": 2})
    stale = ProactiveMessageIntent("chat", "wakeup", "test", "line", metadata={"captured_generation": 1})

    assert asyncio.run(dispatcher._proactive_generation_current(current)) == (True, 2)
    assert asyncio.run(dispatcher._proactive_generation_current(stale)) == (False, 1)
