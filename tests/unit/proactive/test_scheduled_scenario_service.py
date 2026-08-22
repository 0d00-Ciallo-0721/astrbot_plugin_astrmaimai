import asyncio
from datetime import datetime
import importlib
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest

from tests.helpers.astrbot_stubs import install_astrbot_stubs


def _config(**overrides):
    values = {
        "enable_proactive": True,
        "enable_group_proactive": True,
        "enable_private_proactive": True,
        "scheduled_scenarios_enabled": True,
        "scheduled_scenarios_allow_inactive_chat": False,
        "daily_schedule_enabled": True,
        "daily_schedule_ai_enabled": False,
        "morning_greeting_enabled": True,
        "morning_greeting_time": "08:00",
        "morning_greeting_window_min": 90,
        "night_greeting_enabled": True,
        "night_greeting_time": "22:30",
        "night_greeting_window_min": 90,
        "festival_greeting_enabled": True,
        "weather_context_enabled": False,
        "wakeup_cooldown": 600,
        "wakeup_min_energy": 0.0,
        "proactive_max_unanswered": 2,
        "proactive_failure_retry_sec": 300,
        "proactive_quiet_hours": [],
    }
    values.update(overrides)
    return SimpleNamespace(
        life=SimpleNamespace(**values),
        reply=SimpleNamespace(base_frequency=0.7),
        persona=SimpleNamespace(persona_id="global", name="Mai"),
    )


class _Dispatcher:
    def __init__(self, *, complete_immediately=False):
        self.complete_immediately = complete_immediately
        self.intents = []
        self.on_complete = None

    async def dispatch(self, intent, *, on_complete=None):
        self.intents.append(intent)
        self.on_complete = on_complete
        if self.complete_immediately and on_complete:
            await on_complete(True, "早安")
        return SimpleNamespace(
            synthetic_event_queued=True,
            reply_sent=self.complete_immediately,
            blocked_reason="",
            status="sent" if self.complete_immediately else "queued",
        )


class ScheduledScenarioServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        for module_name in (
            "astrmai.proactive.dispatcher",
            "astrmai.proactive.scheduled_scenario_service",
        ):
            sys.modules.pop(module_name, None)
        self.module = importlib.import_module("astrmai.proactive.scheduled_scenario_service")
        self.db_path = Path(self.temp_dir.name) / "scheduled.db"
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                CREATE TABLE proactive_daily_plan (
                    plan_date TEXT PRIMARY KEY,
                    plan_json TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT 'fallback',
                    created_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            db.execute(
                """
                CREATE TABLE proactive_scenario_delivery (
                    delivery_key TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    scenario TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'claimed',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                )
                """
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _service(self, dispatcher, config=None):
        state = SimpleNamespace(chat_id="ff:GroupMessage:10001", chat_kind="group")
        return self.module.ScheduledScenarioService(
            state_engine=SimpleNamespace(get_active_states=lambda: [state]),
            dispatcher=dispatcher,
            config=config or _config(),
            db_path=self.db_path,
            call_background_lane=lambda *args, **kwargs: asyncio.sleep(0, result="{}"),
            task_launcher=lambda factory: factory().close(),
        )

    def test_morning_candidate_is_persisted_and_not_repeated_after_restart(self):
        timestamp = datetime(2026, 5, 11, 8, 15).timestamp()
        first_dispatcher = _Dispatcher()
        first = self._service(first_dispatcher)

        first_report = asyncio.run(first.tick(now=timestamp))

        second_dispatcher = _Dispatcher()
        second = self._service(second_dispatcher)
        second_report = asyncio.run(second.tick(now=timestamp + 120))

        self.assertEqual(first_report["scenario"], "morning_greeting")
        self.assertEqual(first_report["dispatched"], 1)
        self.assertEqual(second_report["dispatched"], 0)
        self.assertEqual(len(first_dispatcher.intents), 1)
        self.assertEqual(len(second_dispatcher.intents), 0)
        intent = first_dispatcher.intents[0]
        self.assertEqual(intent.source, "scheduled_scenario")
        self.assertEqual(intent.metadata["schedule_slot"], "forenoon")
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT status, attempts FROM proactive_scenario_delivery"
            ).fetchone()
        self.assertEqual(row, ("queued", 1))

    def test_immediate_completion_is_not_overwritten_by_queued_state(self):
        timestamp = datetime(2026, 5, 11, 8, 20).timestamp()
        service = self._service(_Dispatcher(complete_immediately=True))

        report = asyncio.run(service.tick(now=timestamp))

        self.assertEqual(report["dispatched"], 1)
        with sqlite3.connect(self.db_path) as db:
            status = db.execute(
                "SELECT status FROM proactive_scenario_delivery"
            ).fetchone()[0]
        self.assertEqual(status, "sent")

    def test_skipped_completion_uses_configured_retry_backoff(self):
        timestamp = datetime(2026, 5, 11, 8, 20).timestamp()
        dispatcher = _Dispatcher()
        service = self._service(
            dispatcher,
            _config(proactive_failure_retry_sec=420),
        )

        asyncio.run(service.tick(now=timestamp))
        asyncio.run(dispatcher.on_complete(False, ""))

        with sqlite3.connect(self.db_path) as db:
            status, next_retry_at, updated_at = db.execute(
                "SELECT status, next_retry_at, updated_at FROM proactive_scenario_delivery"
            ).fetchone()
        self.assertEqual(status, "skipped")
        self.assertGreaterEqual(next_retry_at - updated_at, 419)

    def test_model_schedule_replaces_fallback_atomically(self):
        payload = {slot: f"{slot} activity" for slot in self.module.SCHEDULE_SLOTS}

        async def _call(*args, **kwargs):
            import json

            return json.dumps(payload)

        service = self._service(_Dispatcher())
        service.call_background_lane = _call

        asyncio.run(service._generate_schedule("2026-05-11"))
        loaded = asyncio.run(service.schedule_store.load("2026-05-11"))

        self.assertEqual(loaded, (payload, "model"))

    def test_failed_model_schedule_is_retryable_with_backoff(self):
        service = self._service(
            _Dispatcher(),
            _config(
                daily_schedule_ai_enabled=True,
                daily_schedule_max_retries=2,
                daily_schedule_retry_base_sec=30,
            ),
        )
        service.call_background_lane = lambda *args, **kwargs: asyncio.sleep(0, result="invalid")

        asyncio.run(service._generate_schedule("2026-05-11"))

        self.assertEqual(service._generation_attempts["2026-05-11"], 1)
        self.assertNotIn("2026-05-11", service._generation_started)
        self.assertGreater(service._generation_retry_at["2026-05-11"], 0.0)
        self.assertEqual(service.describe_status()["generation_attempts"]["2026-05-11"], 1)

    def test_launch_rejection_releases_date_and_schedules_same_day_retry(self):
        service = self._service(
            _Dispatcher(),
            _config(daily_schedule_ai_enabled=True, daily_schedule_retry_base_sec=30),
        )

        def reject(_factory):
            raise RuntimeError("budget queue full")

        service.task_launcher = reject
        service._start_schedule_generation("2026-05-11")

        self.assertNotIn("2026-05-11", service._generation_started)
        self.assertEqual(service.describe_status()["generation_state"]["2026-05-11"], "launch_rejected")
        self.assertGreater(service._generation_retry_at["2026-05-11"], 0.0)
        self.assertIn("RuntimeError", service.describe_status()["generation_last_error"]["2026-05-11"])

    def test_invalid_schedule_shapes_are_retryable(self):
        invalid_payloads = ("{}", '{"morning": ""}', '{"unexpected": "value"}', "[]")
        for raw in invalid_payloads:
            service = self._service(
                _Dispatcher(),
                _config(daily_schedule_ai_enabled=True, daily_schedule_retry_base_sec=30),
            )
            service.call_background_lane = lambda *args, raw=raw, **kwargs: asyncio.sleep(0, result=raw)

            asyncio.run(service._generate_schedule("2026-05-11"))

            self.assertEqual(service._generation_attempts["2026-05-11"], 1)
            self.assertEqual(
                service.describe_status()["generation_state"]["2026-05-11"],
                "retry_scheduled",
            )
            self.assertNotEqual(service._schedule_cache.get("2026-05-11", (None, ""))[1], "model")

    def test_generation_retries_exhaust_to_explicit_terminal_state(self):
        service = self._service(
            _Dispatcher(),
            _config(daily_schedule_ai_enabled=True, daily_schedule_max_retries=1),
        )
        service._schedule_generation_failed("2026-05-11", ValueError("invalid shape"))
        service._schedule_generation_failed("2026-05-11", ValueError("invalid shape"))

        status = service.describe_status()
        self.assertEqual(status["generation_state"]["2026-05-11"], "exhausted")
        self.assertNotIn("2026-05-11", status["generation_retry_at"])
        self.assertNotIn("2026-05-11", status["generation_dates"])

    def test_failed_generation_can_retry_and_replace_fallback(self):
        payload = {slot: f"{slot} activity" for slot in self.module.SCHEDULE_SLOTS}
        service = self._service(
            _Dispatcher(),
            _config(daily_schedule_ai_enabled=True, daily_schedule_retry_base_sec=30),
        )
        service.call_background_lane = lambda *args, **kwargs: asyncio.sleep(0, result="invalid")
        asyncio.run(service._generate_schedule("2026-05-11"))
        service._generation_retry_at["2026-05-11"] = 0.0

        async def valid_call(*args, **kwargs):
            import json

            return json.dumps(payload)

        service.call_background_lane = valid_call
        asyncio.run(service._generate_schedule("2026-05-11"))

        self.assertEqual(
            asyncio.run(service.schedule_store.load("2026-05-11")),
            (payload, "model"),
        )
        self.assertEqual(service.describe_status()["generation_state"]["2026-05-11"], "succeeded")

    def test_festival_provider_covers_fixed_and_floating_dates(self):
        provider = self.module.FestivalProvider

        self.assertEqual(provider.get_name(datetime(2026, 1, 1).date()), "元旦")
        self.assertEqual(provider.get_name(datetime(2026, 5, 10).date()), "母亲节")
        self.assertEqual(provider.get_name(datetime(2026, 6, 21).date()), "父亲节")

    def test_festival_provider_covers_lunar_festival_and_new_year_eve(self):
        class _Lunar:
            @staticmethod
            def fromSolarDate(year, month, day):
                values = {
                    (2026, 2, 17): (1, 1),
                    (2026, 2, 16): (12, 29),
                }
                lunar_month, lunar_day = values[(year, month, day)]
                return SimpleNamespace(month=lunar_month, day=lunar_day)

        provider = self.module.FestivalProvider

        self.assertEqual(
            provider.get_name(datetime(2026, 2, 17).date(), lunar_converter=_Lunar),
            "春节",
        )
        self.assertEqual(
            provider.get_name(datetime(2026, 2, 16).date(), lunar_converter=_Lunar),
            "除夕",
        )

    def test_disabled_daily_schedule_is_not_generated_persisted_or_injected(self):
        timestamp = datetime(2026, 5, 11, 8, 20).timestamp()
        dispatcher = _Dispatcher()
        service = self._service(
            dispatcher,
            _config(daily_schedule_enabled=False, daily_schedule_ai_enabled=True),
        )

        report = asyncio.run(service.tick(now=timestamp))

        self.assertEqual(report["schedule_source"], "disabled")
        self.assertNotIn("角色当前日程背景", dispatcher.intents[0].guidance)
        self.assertEqual(service._generation_started, set())
        with sqlite3.connect(self.db_path) as db:
            count = db.execute("SELECT COUNT(*) FROM proactive_daily_plan").fetchone()[0]
        self.assertEqual(count, 0)


class ScheduledScenarioDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.proactive.dispatcher", None)
        self.module = importlib.import_module("astrmai.proactive.dispatcher")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _dispatcher(self, config, *, unanswered=0):
        class _AttentionGate:
            async def inject_external_event(self, chat_id, event_data):
                return True

        state = SimpleNamespace(
            energy=1.0,
            unanswered_proactive_count=unanswered,
            last_real_user_activity_at=0.0,
        )
        return self.module.ProactiveDispatcher(
            attention_gate=_AttentionGate(),
            runtime_coordinator=SimpleNamespace(
                get_activity_snapshot=lambda chat_id: asyncio.sleep(
                    0,
                    result={"wait_targets": [], "executor_pending": 0},
                )
            ),
            state_engine=SimpleNamespace(
                bot_id="bot",
                get_state=lambda chat_id: asyncio.sleep(0, result=state),
            ),
            config=config,
        )

    def test_inactive_chat_requires_config_and_intent_authorization(self):
        async def _run(config_allow, metadata_allow):
            dispatcher = self._dispatcher(
                _config(scheduled_scenarios_allow_inactive_chat=config_allow)
            )
            return await dispatcher.dispatch(
                self.module.ProactiveMessageIntent(
                    chat_id="ff:GroupMessage:10001",
                    source="scheduled_scenario",
                    reason="morning_greeting",
                    guidance="自然地问候",
                    metadata={
                        "chat_kind": "group",
                        "group_id": "10001",
                        "allow_inactive_chat": metadata_allow,
                    },
                )
            )

        blocked_config = asyncio.run(_run(False, True))
        blocked_intent = asyncio.run(_run(True, False))
        allowed = asyncio.run(_run(True, True))

        self.assertEqual(blocked_config.blocked_reason, "chat_inactive")
        self.assertEqual(blocked_intent.blocked_reason, "chat_inactive")
        self.assertTrue(allowed.allowed)
        self.assertTrue(allowed.safety_checks["scheduled_inactive_allowed"])

    def test_unanswered_limit_blocks_scheduled_candidate(self):
        dispatcher = self._dispatcher(
            _config(scheduled_scenarios_allow_inactive_chat=True),
            unanswered=2,
        )

        decision = asyncio.run(
            dispatcher.dispatch(
                self.module.ProactiveMessageIntent(
                    chat_id="ff:GroupMessage:10001",
                    source="scheduled_scenario",
                    reason="night_greeting",
                    guidance="自然地道晚安",
                    metadata={"allow_inactive_chat": True},
                )
            )
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.blocked_reason, "max_unanswered")


if __name__ == "__main__":
    unittest.main()
