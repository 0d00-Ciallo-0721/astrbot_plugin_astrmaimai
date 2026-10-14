import asyncio
import sqlite3
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrmai.app.plugin_facade import PluginFacade
from astrmai.infrastructure.runtime.group_lifecycle import GroupLifecycleRegistry
from astrmai.infrastructure.runtime.background_task_budget import BackgroundTaskBudget
from astrmai.infrastructure.runtime.outbound_send_guard import outbound_send_allowed
from astrmai.presentation.events.message_entry import handle_global_message
from astrmai.proactive.dispatcher import ProactiveDispatcher, ProactiveMessageIntent
from astrmai.proactive.review_dispatcher import ReviewDispatcher


def _schema(path):
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE group_lifecycle(
                chat_id TEXT PRIMARY KEY, group_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
                retired_at REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
                source_event_id TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL DEFAULT 0
            )"""
        )
        db.execute("CREATE TABLE attention_deferred_outbox(chat_id TEXT)")
        db.execute("CREATE TABLE attention_deferred_outbox_terminal_history(chat_id TEXT)")
        db.execute("CREATE TABLE qq_action_ledger(chat_id TEXT)")
        db.execute("CREATE TABLE proactive_scenario_delivery(chat_id TEXT)")
        db.execute("CREATE TABLE reflection_outbox(chat_id TEXT)")
        db.execute("CREATE TABLE background_task_ledger(scope_id TEXT)")
        db.execute("CREATE TABLE memory_turn_ledger(chat_id TEXT)")
        db.executemany("INSERT INTO attention_deferred_outbox VALUES (?)", [("default:GroupMessage:g",), ("other",)])
        db.executemany("INSERT INTO qq_action_ledger VALUES (?)", [("default:GroupMessage:g",), ("other",)])
        db.executemany("INSERT INTO proactive_scenario_delivery VALUES (?)", [("default:GroupMessage:g",), ("other",)])
        db.commit()


def test_group_lifecycle_retire_is_persistent_and_generation_fenced(tmp_path):
    path = tmp_path / "state.db"
    _schema(path)
    registry = GroupLifecycleRegistry(path)
    chat_id = "default:GroupMessage:g"

    first = asyncio.run(registry.retire(chat_id, group_id="g", reason="bot_left_group"))
    assert first.status == "retired"
    assert first.generation == 1
    assert asyncio.run(registry.allowed(chat_id, first.generation)) is False

    restarted = GroupLifecycleRegistry(path)
    state = asyncio.run(restarted.state(chat_id))
    assert state is not None and state.status == "retired"
    assert asyncio.run(restarted.allowed(chat_id, state.generation)) is False
    active = asyncio.run(restarted.activate(chat_id, group_id="g"))
    assert active.status == "active"
    assert active.generation == 2
    assert asyncio.run(restarted.allowed(chat_id, active.generation)) is True


def test_same_database_runtime_reloads_inherit_retired_cache(tmp_path):
    path = tmp_path / "state.db"
    _schema(path)
    chat_id = "default:GroupMessage:g"
    first = GroupLifecycleRegistry(path)
    asyncio.run(first.retire(chat_id, group_id="g", reason="bot_left_group"))
    second = GroupLifecycleRegistry(path)
    assert second.is_retired_cached(chat_id) is True


def test_old_runtime_retire_updates_current_same_database_send_fence(tmp_path):
    path = tmp_path / "state.db"
    _schema(path)
    chat_id = "default:GroupMessage:g"
    old = GroupLifecycleRegistry(path)
    current = GroupLifecycleRegistry(path)
    asyncio.run(old.retire(chat_id, group_id="g", reason="bot_left_group"))
    assert current.is_retired_cached(chat_id) is True


def test_retire_closes_cached_send_fence_when_durable_table_is_unavailable(tmp_path):
    registry = GroupLifecycleRegistry(tmp_path / "unmigrated.db")
    chat_id = "default:GroupMessage:g"
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(registry.retire(chat_id, group_id="g", reason="bot_left_group"))
    assert registry.is_retired_cached(chat_id) is True


def test_allowed_group_without_lifecycle_row_is_negative_cached(tmp_path):
    registry = GroupLifecycleRegistry(tmp_path / "state.db")
    registry._read = AsyncMock(return_value=None)
    chat_id = "default:GroupMessage:g"
    assert asyncio.run(registry.state(chat_id)) is None
    assert asyncio.run(registry.state(chat_id)) is None
    assert registry._read.await_count == 1


def test_bound_event_is_fenced_if_group_retires_during_turn(tmp_path):
    path = tmp_path / "state.db"
    _schema(path)
    registry = GroupLifecycleRegistry(path)

    class Event:
        unified_msg_origin = "default:GroupMessage:g"

        def __init__(self):
            self.extras = {}

        def set_extra(self, key, value):
            self.extras[key] = value

        def get_extra(self, key, default=None):
            return self.extras.get(key, default)

    event = Event()
    asyncio.run(registry.bind_event(event))
    assert outbound_send_allowed(event) is True
    asyncio.run(registry.retire(event.unified_msg_origin, group_id="g"))
    assert outbound_send_allowed(event) is False


@pytest.mark.asyncio
async def test_rejoining_group_does_not_reauthorize_a_pre_exit_event(tmp_path):
    path = tmp_path / "state.db"
    _schema(path)
    registry = GroupLifecycleRegistry(path)
    chat_id = "default:GroupMessage:g"

    def event():
        extras = {}
        return SimpleNamespace(
            unified_msg_origin=chat_id,
            set_extra=lambda key, value: extras.__setitem__(key, value),
            get_extra=lambda key, default=None: extras.get(key, default),
        )

    old = event()
    assert await registry.bind_event(old) == 0
    await registry.retire(chat_id, group_id="g")
    await registry.activate(chat_id, group_id="g")
    assert outbound_send_allowed(old) is False
    assert await registry.allowed(chat_id, 0) is False
    new = event()
    await registry.bind_event(new)
    assert outbound_send_allowed(new) is True


@pytest.mark.asyncio
async def test_group_fence_state_is_isolated_between_database_paths(tmp_path):
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    _schema(first_path)
    _schema(second_path)
    first = GroupLifecycleRegistry(first_path)
    second = GroupLifecycleRegistry(second_path)
    await first.retire("default:GroupMessage:g", group_id="g")
    assert await second.state("default:GroupMessage:g") is None


@pytest.mark.asyncio
async def test_restart_loads_retired_state_from_database_without_inherited_cache(tmp_path):
    path = tmp_path / "state.db"
    _schema(path)
    registry = GroupLifecycleRegistry(path)
    await registry.retire("default:GroupMessage:g", group_id="g")
    restarted = GroupLifecycleRegistry(path)
    restarted._cache.clear()
    state = await restarted.state("default:GroupMessage:g")
    assert state is not None and state.status == "retired"


@pytest.mark.asyncio
async def test_retiring_group_removes_review_requests_without_touching_other_groups(tmp_path):
    from astrmai.learning.review.reflect_tracker import ReflectTracker

    path = tmp_path / "state.db"
    _schema(path)
    registry = GroupLifecycleRegistry(path)
    tracker = ReflectTracker(SimpleNamespace(), SimpleNamespace(config=SimpleNamespace()))
    tracker._pending = {
        "gone": {"pattern_id": "gone", "umo": "default:GroupMessage:g", "sent": False},
        "other": {"pattern_id": "other", "umo": "default:GroupMessage:other", "sent": False},
    }
    facade = object.__new__(PluginFacade)
    facade.runtime = SimpleNamespace(
        reflect_tracker=tracker, group_lifecycle=registry,
        core=SimpleNamespace(db_service=SimpleNamespace(db_path=path)),
    )
    event = SimpleNamespace(
        unified_msg_origin="default:GroupMessage:g",
        raw_event={"notice_type": "group_decrease", "group_id": "g", "user_id": "bot", "self_id": "bot"},
    )
    assert await facade.handle_group_membership_notice(event) is True
    assert "gone" not in tracker._pending
    assert "other" in tracker._pending


@pytest.mark.asyncio
async def test_group_pending_purge_covers_every_scoped_queue_and_preserves_history(tmp_path):
    path = tmp_path / "state.db"
    _schema(path)
    tables = (
        ("attention_deferred_outbox", "chat_id"),
        ("attention_deferred_outbox_terminal_history", "chat_id"),
        ("qq_action_ledger", "chat_id"),
        ("proactive_scenario_delivery", "chat_id"),
        ("reflection_outbox", "chat_id"),
        ("background_task_ledger", "scope_id"),
        ("memory_turn_ledger", "chat_id"),
    )
    with sqlite3.connect(path) as db:
        for table, column in tables:
            db.execute(f"DELETE FROM {table}")
            db.executemany(f"INSERT INTO {table} ({column}) VALUES (?)", [("default:GroupMessage:g",), ("other",)])
        db.execute("CREATE TABLE messagelog (group_id TEXT, content TEXT)")
        db.execute("INSERT INTO messagelog VALUES ('default:GroupMessage:g', 'historical evidence')")
    facade = object.__new__(PluginFacade)
    facade.runtime = SimpleNamespace(core=SimpleNamespace(db_service=SimpleNamespace(db_path=path)))
    assert await facade._purge_group_pending_records("default:GroupMessage:g") == {"pending_records": True}
    with sqlite3.connect(path) as db:
        for table, column in tables:
            assert db.execute(f"SELECT {column} FROM {table}").fetchall() == [("other",)]
        assert db.execute("SELECT content FROM messagelog").fetchall() == [("historical evidence",)]


@pytest.mark.asyncio
async def test_pending_purge_failure_rolls_back_prior_deletes_and_keeps_retirement_fence(tmp_path):
    path = tmp_path / "state.db"
    _schema(path)
    registry = GroupLifecycleRegistry(path)
    chat_id = "default:GroupMessage:g"
    await registry.retire(chat_id, group_id="g")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER fail_purge BEFORE DELETE ON qq_action_ledger BEGIN SELECT RAISE(ABORT, 'purge failed'); END")
    facade = object.__new__(PluginFacade)
    facade.runtime = SimpleNamespace(core=SimpleNamespace(db_service=SimpleNamespace(db_path=path)))
    assert await facade._purge_group_pending_records(chat_id) == {"pending_records": False}
    assert registry.is_retired_cached(chat_id) is True
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM attention_deferred_outbox WHERE chat_id=?", (chat_id,)).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_architecture_migration_creates_real_lifecycle_schema(tmp_path):
    from astrmai.infrastructure.persistence.persistence_schema import _run_migrations

    path = tmp_path / "migrated.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 175")
        _run_migrations(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 177
        assert len(db.execute("PRAGMA table_info(group_lifecycle)").fetchall()) == 8
        assert "ix_group_lifecycle_status" in {row[1] for row in db.execute("PRAGMA index_list(group_lifecycle)")}
    registry = GroupLifecycleRegistry(path)
    retired = await registry.retire("default:GroupMessage:g", group_id="g", source_event_id="exit")
    assert retired.generation == 1
    active = await registry.activate("default:GroupMessage:g", group_id="g", source_event_id="join")
    assert active.generation == 2
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT status, generation, source_event_id FROM group_lifecycle").fetchone() == ("active", 2, "join")


def test_group_membership_notice_retires_only_self_and_purges_pending(tmp_path):
    path = tmp_path / "state.db"
    _schema(path)
    registry = GroupLifecycleRegistry(path)
    facade = object.__new__(PluginFacade)
    facade.runtime = SimpleNamespace(
        group_lifecycle=registry,
        core=SimpleNamespace(db_service=SimpleNamespace(db_path=path)),
        attention_gate=None,
        state_engine=None,
        runtime_coordinator=None,
        dialogue_store=None,
        group_social_feedback_observer=None,
        group_reread_observer=None,
        chat_loop_kernel=None,
        proactive_task=None,
        group_reply_wait_manager=None,
    )
    event = SimpleNamespace(
        unified_msg_origin="default:GroupMessage:g",
        raw_event={
            "notice_type": "group_decrease",
            "group_id": "g",
            "user_id": "bot",
            "self_id": "bot",
        },
        get_self_id=lambda: "bot",
        message_id="notice-1",
    )
    assert asyncio.run(facade.handle_group_membership_notice(event)) is True
    state = asyncio.run(registry.state("default:GroupMessage:g"))
    assert state is not None and state.status == "retired"
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM attention_deferred_outbox WHERE chat_id=?",
            ("default:GroupMessage:g",),
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM qq_action_ledger WHERE chat_id=?",
            ("default:GroupMessage:g",),
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM proactive_scenario_delivery WHERE chat_id=?",
            ("default:GroupMessage:g",),
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM proactive_scenario_delivery WHERE chat_id='other'"
        ).fetchone()[0] == 1


def test_group_membership_notice_for_self_rejoins_and_reactivates(tmp_path):
    path = tmp_path / "state.db"
    _schema(path)
    registry = GroupLifecycleRegistry(path)
    facade = object.__new__(PluginFacade)
    facade.runtime = SimpleNamespace(
        group_lifecycle=registry,
        core=SimpleNamespace(db_service=SimpleNamespace(db_path=path)),
        attention_gate=None,
        state_engine=None,
        runtime_coordinator=None,
        dialogue_store=None,
        group_social_feedback_observer=None,
        group_reread_observer=None,
        chat_loop_kernel=None,
        proactive_task=None,
        group_reply_wait_manager=None,
    )
    chat_id = "default:GroupMessage:g"
    asyncio.run(registry.retire(chat_id, group_id="g", reason="bot_left_group"))
    event = SimpleNamespace(
        unified_msg_origin=chat_id,
        raw_event={
            "notice_type": "group_increase",
            "group_id": "g",
            "user_id": "bot",
            "self_id": "bot",
        },
        get_self_id=lambda: "bot",
        message_id="notice-2",
    )
    assert asyncio.run(facade.handle_group_membership_notice(event)) is True
    state = asyncio.run(registry.state(chat_id))
    assert state is not None and state.status == "active" and state.generation == 2


def test_other_member_join_does_not_reactivate_retired_group(tmp_path):
    path = tmp_path / "state.db"
    _schema(path)
    registry = GroupLifecycleRegistry(path)
    chat_id = "default:GroupMessage:g"
    retired = asyncio.run(
        registry.retire(chat_id, group_id="g", reason="bot_left_group")
    )
    facade = object.__new__(PluginFacade)
    facade.runtime = SimpleNamespace(group_lifecycle=registry)
    event = SimpleNamespace(
        unified_msg_origin=chat_id,
        raw_event={
            "notice_type": "group_increase",
            "group_id": "g",
            "user_id": "other-user",
            "self_id": "bot",
        },
        get_self_id=lambda: "bot",
        message_id="notice-other",
    )
    assert asyncio.run(facade.handle_group_membership_notice(event)) is False
    state = asyncio.run(registry.state(chat_id))
    assert state == retired


@pytest.mark.asyncio
async def test_proactive_dispatch_blocks_retired_group_before_other_safety_checks(tmp_path):
    path = tmp_path / "state.db"
    _schema(path)
    registry = GroupLifecycleRegistry(path)
    chat_id = "default:GroupMessage:g"
    retired = await registry.retire(chat_id, group_id="g", reason="bot_left_group")
    dispatcher = ProactiveDispatcher(group_lifecycle=registry)
    decision = await dispatcher.dispatch(
        ProactiveMessageIntent(
            chat_id=chat_id,
            source="wakeup",
            reason="direct answer",
            guidance="reply",
            metadata={"candidate_version": 1},
        )
    )
    assert decision.allowed is False
    assert decision.blocked_reason == "group_retired"
    assert decision.safety_checks["group_lifecycle_generation"] == retired.generation


@pytest.mark.asyncio
async def test_review_dispatch_discards_retired_group_without_requeue_or_degraded_log(tmp_path, monkeypatch):
    from astrmai.learning.review.reflect_tracker import ReflectTracker
    from astrmai.proactive import review_dispatcher as module

    path = tmp_path / "state.db"
    _schema(path)
    registry = GroupLifecycleRegistry(path)
    chat_id = "default:GroupMessage:g"
    await registry.retire(chat_id, group_id="g", reason="bot_left_group")
    tracker = ReflectTracker(SimpleNamespace(), SimpleNamespace(config=SimpleNamespace()))
    tracker._pending = {
        "p1": {"pattern_id": "p1", "umo": chat_id, "question": "approve?", "sent": False},
        "p2": {"pattern_id": "p2", "umo": "default:GroupMessage:other", "question": "other?", "sent": False},
    }
    tracker.requeue_request = AsyncMock(wraps=tracker.requeue_request)
    warning = Mock()
    monkeypatch.setattr(module, "logger", SimpleNamespace(warning=warning))
    monkeypatch.setattr(module, "outbound_send_allowed", lambda: True)
    context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    dispatcher = ReviewDispatcher(
        context,
        tracker,
        group_lifecycle=registry,
    )
    await dispatcher.dispatch_pending()
    await dispatcher.dispatch_pending()
    assert "p1" not in tracker._pending
    assert tracker._pending["p2"]["sent"] is True
    assert context.send_message.await_count == 1
    assert context.send_message.call_args.args[0] == "default:GroupMessage:other"
    tracker.requeue_request.assert_not_awaited()
    warning.assert_not_called()


@pytest.mark.asyncio
async def test_proactive_lifecycle_read_failure_blocks_before_activity_or_enqueue():
    dispatcher = ProactiveDispatcher(
        group_lifecycle=SimpleNamespace(state=AsyncMock(side_effect=RuntimeError("db unavailable")))
    )
    dispatcher._activity_snapshot = AsyncMock(return_value={})
    decision = await dispatcher.dispatch(ProactiveMessageIntent(
        chat_id="default:GroupMessage:g", source="wakeup",
        reason="direct answer", guidance="reply", metadata={"candidate_version": 1},
    ))
    assert decision.allowed is False
    assert decision.blocked_reason == "group_lifecycle_unavailable"
    assert decision.synthetic_event_queued is False
    dispatcher._activity_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_lifecycle_read_failure_requeues_without_sending():
    tracker = SimpleNamespace(
        claim_unsent_requests=AsyncMock(return_value=[
            {"pattern_id": "p1", "umo": "default:GroupMessage:g", "question": "approve?"}
        ]),
        requeue_request=AsyncMock(return_value=True),
    )
    context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    await ReviewDispatcher(context, tracker, group_lifecycle=SimpleNamespace(
        state=AsyncMock(side_effect=RuntimeError("db unavailable"))
    )).dispatch_pending()
    context.send_message.assert_not_awaited()
    tracker.requeue_request.assert_awaited_once_with("p1")


@pytest.mark.asyncio
async def test_proactive_lifecycle_read_cancellation_propagates():
    dispatcher = ProactiveDispatcher(group_lifecycle=SimpleNamespace(
        state=AsyncMock(side_effect=asyncio.CancelledError)
    ))
    with pytest.raises(asyncio.CancelledError):
        await dispatcher.dispatch(ProactiveMessageIntent(
            chat_id="default:GroupMessage:g", source="wakeup", reason="reply", guidance="reply",
        ))


@pytest.mark.asyncio
async def test_retired_group_message_stops_at_ingress_before_attention():
    class Event:
        unified_msg_origin = "default:GroupMessage:g"
        message_str = "still there?"
        message_obj = SimpleNamespace(message_id="msg-after-left", message=[])

        def __init__(self):
            self.extras = {}
            self.stopped = False

        def get_sender_id(self):
            return "user"

        def get_group_id(self):
            return "g"

        def get_self_id(self):
            return "bot"

        def set_extra(self, key, value):
            self.extras[key] = value

        def get_extra(self, key, default=None):
            return self.extras.get(key, default)

        def stop_event(self):
            self.stopped = True

    event = Event()
    facade = SimpleNamespace(
        is_accepting_events=lambda: True,
        bind_group_lifecycle=AsyncMock(return_value=False),
    )
    results = [item async for item in handle_global_message(facade, event)]
    assert results == []
    assert event.stopped is True
    facade.bind_group_lifecycle.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_lifecycle_binding_failure_fails_closed_for_group_ingress():
    class Event:
        unified_msg_origin = "default:GroupMessage:g"
        message_str = "hello"

        def __init__(self):
            self.extras = {}
            self.stopped = False

        def get_sender_id(self): return "user"
        def get_group_id(self): return "g"
        def get_self_id(self): return "bot"
        def set_extra(self, key, value): self.extras[key] = value
        def get_extra(self, key, default=None): return self.extras.get(key, default)
        def stop_event(self): self.stopped = True

    event = Event()
    facade = SimpleNamespace(
        is_accepting_events=lambda: True,
        bind_group_lifecycle=AsyncMock(side_effect=RuntimeError("db unavailable")),
    )
    results = [item async for item in handle_global_message(facade, event)]
    assert results == []
    assert event.stopped is True


@pytest.mark.asyncio
async def test_background_budget_reports_owner_scope_and_queue_evidence():
    budget = BackgroundTaskBudget(limit=1, max_queue=2, wait_timeout_sec=0.2)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        entered.set()
        await release.wait()

    task = asyncio.create_task(
        budget.run(holder, task_name="visual-cortex:analysis", scope_id="default:GroupMessage:g")
    )
    await entered.wait()
    waiter = asyncio.create_task(
        budget.run(lambda: asyncio.sleep(0), task_name="proactive.scan", scope_id="default:GroupMessage:g")
    )
    await asyncio.sleep(0)
    status = budget.status()
    assert status["active"] == 1
    assert status["queued"] == 1
    assert status["active_owners"][0]["task_name"] == "visual-cortex:analysis"
    assert status["active_owners"][0]["scope"].endswith("default:GroupMessage:g")
    release.set()
    await task
    await waiter

