from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

from astrmai.conversation.contracts.focus_context import FreshnessState
from astrmai.conversation.execution.reply_freshness import ReplyFreshnessMixin
from astrmai.infrastructure.persistence.orm_models import ChatState
from astrmai.infrastructure.persistence.persistence_schema import PersistenceSchemaMixin
from astrmai.infrastructure.persistence.state_profile_persistence import (
    StateProfilePersistenceMixin,
)
from astrmai.proactive.dispatcher import ProactiveDispatcher, ProactiveMessageIntent
from astrmai.proactive.wakeup_service import WakeupService
from astrmai.state.chat_state_service import ChatStateService


class _SqliteStatePersistence(PersistenceSchemaMixin, StateProfilePersistenceMixin):
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db_sync()


class _Event:
    def __init__(self, **extras):
        self.extras = dict(extras)

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


def _config(**life_overrides):
    values = {
        "silence_threshold": 10,
        "wakeup_min_energy": 0.2,
        "wakeup_cost": 0.1,
        "wakeup_cooldown": 300,
        "enable_private_proactive": True,
        "enable_group_proactive": True,
        "proactive_max_unanswered": 2,
        "proactive_failure_retry_sec": 60,
        "proactive_claim_lease_sec": 300,
        "proactive_quiet_hours": [],
    }
    values.update(life_overrides)
    return SimpleNamespace(
        life=SimpleNamespace(**values),
        persona=SimpleNamespace(persona_id="global", name="Mai"),
        reply=SimpleNamespace(base_frequency=0.7),
    )


def test_real_user_activity_and_committed_bot_watermarks_are_independent():
    async def _run(db_path: Path):
        persistence = _SqliteStatePersistence(db_path)
        service = ChatStateService(persistence, _config())

        first = await service.record_real_user_activity(
            "ff:FriendMessage:42",
            chat_kind="private",
            occurred_at=100.0,
        )
        assert first.proactive_generation == 1
        assert first.last_real_user_activity_at == 100.0
        assert first.next_proactive_due_at == 700.0

        normal = await service.record_committed_bot_reply(
            "ff:FriendMessage:42",
            committed_at=150.0,
            is_proactive=False,
            commit_id="normal-1",
        )
        assert normal.last_real_user_activity_at == 100.0
        assert normal.last_committed_bot_reply_at == 150.0
        assert normal.unanswered_proactive_count == 0

        proactive = await service.record_committed_bot_reply(
            "ff:FriendMessage:42",
            committed_at=180.0,
            is_proactive=True,
            commit_id="proactive-1",
        )
        assert proactive.unanswered_proactive_count == 1
        assert proactive.last_proactive_commit_id == "proactive-1"

        resumed = await service.record_real_user_activity(
            "ff:FriendMessage:42",
            chat_kind="private",
            occurred_at=200.0,
        )
        assert resumed.proactive_generation == 2
        assert resumed.unanswered_proactive_count == 0
        assert resumed.last_proactive_cancel_reason == "user_activity"
        assert resumed.last_committed_bot_reply_at == 180.0

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
        asyncio.run(_run(Path(temp_dir) / "state.db"))


def test_persistent_due_scan_and_atomic_claim_are_restart_safe():
    async def _run(db_path: Path):
        persistence = _SqliteStatePersistence(db_path)
        await persistence.save_chat_state(
            "ff:GroupMessage:7",
            ChatState(
                chat_id="ff:GroupMessage:7",
                chat_kind="group",
                next_proactive_due_at=100.0,
                proactive_generation=4,
            ),
        )

        assert await persistence.list_due_chat_state_ids(now=101.0) == [
            "ff:GroupMessage:7"
        ]
        first = await persistence.atomic_claim_proactive_due(
            "ff:GroupMessage:7",
            expected_generation=4,
            claim_token="claim-a",
            now=101.0,
            lease_seconds=300.0,
        )
        second = await persistence.atomic_claim_proactive_due(
            "ff:GroupMessage:7",
            expected_generation=4,
            claim_token="claim-b",
            now=102.0,
            lease_seconds=300.0,
        )
        assert first is True
        assert second is False
        assert await persistence.atomic_settle_proactive_claim(
            "ff:GroupMessage:7",
            claim_token="wrong",
            next_due_at=500.0,
            cancel_reason="wrong",
        ) is False
        assert await persistence.atomic_settle_proactive_claim(
            "ff:GroupMessage:7",
            claim_token="claim-a",
            next_due_at=500.0,
            cancel_reason="retry",
        ) is True
        loaded = await persistence.load_chat_state("ff:GroupMessage:7")
        assert loaded is not None
        assert loaded.proactive_claim_token == ""
        assert loaded.next_proactive_due_at == 500.0
        assert loaded.last_proactive_cancel_reason == "retry"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
        asyncio.run(_run(Path(temp_dir) / "state.db"))


def test_wakeup_scan_includes_persisted_due_chat_not_in_active_cache():
    state = SimpleNamespace(
        chat_id="ff:FriendMessage:9",
        chat_kind="private",
        last_real_user_activity_at=100.0,
        last_reply_time=100.0,
        last_committed_bot_reply_at=0.0,
        next_proactive_due_at=200.0,
        next_wakeup_timestamp=200.0,
        proactive_generation=3,
        unanswered_proactive_count=0,
        energy=1.0,
    )

    class _StateEngine:
        def get_active_states(self):
            return []

        async def list_due_proactive_chat_ids(self, **_kwargs):
            return [state.chat_id]

        async def get_state(self, chat_id):
            assert chat_id == state.chat_id
            return state

        async def claim_proactive_due(self, chat_id, **_kwargs):
            assert chat_id == state.chat_id
            return "claim-3"

    class _Dispatcher:
        def __init__(self):
            self.intents = []

        async def dispatch(self, intent, *, on_complete=None):
            self.intents.append(intent)
            return SimpleNamespace(allowed=True, blocked_reason="", intent_id=intent.intent_id)

    dispatcher = _Dispatcher()
    service = WakeupService(
        context=SimpleNamespace(),
        state_engine=_StateEngine(),
        persistence=SimpleNamespace(load_persona_cache=lambda: {}),
        call_background_lane=None,
        config=_config(),
        dispatcher=dispatcher,
    )

    async def _run():
        service.generate_opening_line = lambda _chat_id: asyncio.sleep(
            0,
            result="say one short line",
        )
        import astrmai.proactive.wakeup_service as wakeup_module

        original = wakeup_module.time.time
        wakeup_module.time.time = lambda: 1000.0
        try:
            await service.run_once()
        finally:
            wakeup_module.time.time = original

    asyncio.run(_run())

    assert len(dispatcher.intents) == 1
    intent = dispatcher.intents[0]
    assert intent.chat_id == "ff:FriendMessage:9"
    assert intent.metadata["chat_kind"] == "private"
    assert "group_id" not in intent.metadata
    assert intent.metadata["captured_generation"] == 3


def test_dispatcher_fails_closed_when_generation_changes_before_injection():
    calls = []

    class _Gate:
        async def inject_external_event(self, chat_id, event_data):
            calls.append((chat_id, event_data))
            return True

    class _StateEngine:
        async def get_state(self, _chat_id):
            return SimpleNamespace(energy=1.0, proactive_generation=2)

        async def is_proactive_generation_current(self, _chat_id, captured):
            return captured == 2

    dispatcher = ProactiveDispatcher(
        attention_gate=_Gate(),
        state_engine=_StateEngine(),
        config=_config(),
    )
    decision = asyncio.run(
        dispatcher.dispatch(
            ProactiveMessageIntent(
                chat_id="ff:GroupMessage:1",
                source="wakeup",
                reason="test",
                guidance="hello",
                metadata={
                    "chat_kind": "group",
                    "group_id": "1",
                    "captured_generation": 1,
                },
            )
        )
    )

    assert decision.allowed is False
    assert decision.blocked_reason == "proactive_generation_superseded"
    assert calls == []


def test_reply_freshness_rechecks_proactive_generation_after_model_completion():
    class _StateEngine:
        async def is_proactive_generation_current(self, _chat_id, _captured):
            return False

    service = SimpleNamespace(
        state_engine=_StateEngine(),
        config=SimpleNamespace(),
    )
    service._record_freshness_observation = ReplyFreshnessMixin._record_freshness_observation
    event = _Event(
        astrmai_is_proactive_event=True,
        astrmai_proactive_generation=7,
    )

    state, reason = asyncio.run(
        ReplyFreshnessMixin._check_reply_freshness(
            service,
            event,
            "ff:FriendMessage:1",
        )
    )

    assert state == FreshnessState.EXPIRED
    assert reason == "proactive_generation_superseded"
    assert event.get_extra("astrmai_proactive_cancel_reason") == reason
