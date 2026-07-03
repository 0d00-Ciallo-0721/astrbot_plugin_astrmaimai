import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from datetime import datetime
from types import SimpleNamespace

from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeGateway:
    def __init__(self):
        self.prompts = []
        self.config = SimpleNamespace(
            system1=SimpleNamespace(wakeup_words=["atri"], keyword_reactions=[]),
            global_settings=SimpleNamespace(debug_mode=False),
            provider=SimpleNamespace(task_models=[]),
        )

    async def chat_in_lane_result(self, **kwargs):
        self.prompts.append(kwargs["prompt"])
        return SimpleNamespace(parsed_json={"action": "IGNORE", "reason": "noop", "relevance": 0, "necessity": 0.0})


class _ResultGateway(_FakeGateway):
    def __init__(self, parsed_json):
        super().__init__()
        self._parsed_json = dict(parsed_json)

    async def chat_in_lane_result(self, **kwargs):
        self.prompts.append(kwargs["prompt"])
        return SimpleNamespace(parsed_json=dict(self._parsed_json))


class _FakeStateEngine:
    def __init__(self, persistence):
        self.persistence = persistence

    async def get_state(self, chat_id):
        return SimpleNamespace(last_reply_time=0.0, mood=0.0)

    async def should_drop_by_energy(self, chat_id, msg_count):
        return False

    async def atomic_update_mood(self, chat_id, delta=0.0):
        return 0.0


class _CaptureStateEngine(_FakeStateEngine):
    def __init__(self, persistence):
        super().__init__(persistence)
        self.mood_updates = []

    async def atomic_update_mood(self, chat_id, delta=0.0):
        self.mood_updates.append((chat_id, delta))
        return delta


class _LegacyHistoryPersistence:
    def __init__(self, *, recent_messages=None, chat_history=None, database_service=None):
        self._recent_messages = list(recent_messages or [])
        self._chat_history = list(chat_history or [])
        self.database_service = database_service

    async def get_recent_messages(self, chat_id, limit=8):
        return list(self._recent_messages)[:limit]

    async def get_chat_history(self, chat_id, limit=8):
        return list(self._chat_history)[:limit]


class _ShortCircuitPersistence:
    def __init__(self, recent_messages=None):
        self._recent_messages = list(recent_messages or [])
        self.calls = []

    async def get_recent_messages(self, chat_id, limit=8):
        self.calls.append("recent")
        return list(self._recent_messages)[:limit]

    async def get_chat_history(self, chat_id, limit=8):
        self.calls.append("history")
        raise AssertionError("secondary loader should not run after recent_messages succeeds")


class _DatabaseServiceStub:
    def __init__(self, records):
        self.records = list(records)
        self.calls = []
        self.chat_repository = None

    def get_recent_message_logs(self, group_id, limit=8, max_age_seconds=None, include_processed=True):
        self.calls.append(
            {
                "group_id": group_id,
                "limit": limit,
                "max_age_seconds": max_age_seconds,
                "include_processed": include_processed,
            }
        )
        return list(self.records)[:limit]


class _FakeWindowEvent:
    def __init__(self, sender_name, text, timestamp):
        self.message_str = text
        self.timestamp = timestamp
        self._sender_name = sender_name
        self._extra = {"astrmai_timestamp": timestamp}

    def get_sender_name(self):
        return self._sender_name

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class JudgeHistoryWindowRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.conversation.decision.judge", None)
        self.mod = importlib.import_module("astrmai.conversation.decision.judge")
        self.mod = importlib.reload(self.mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_normal_judge_history_keeps_only_recent_timestamped_records(self):
        now = time.time()
        recent_ts = now - 120
        persistence = _LegacyHistoryPersistence(
            chat_history=[
                {"sender_name": "RecentUser", "content": "recent clue", "timestamp": recent_ts},
                {"sender_name": "OldUser", "content": "stale clue", "timestamp": now - 3600},
                {"sender_name": "NoTsUser", "content": "missing timestamp"},
            ]
        )
        gateway = _FakeGateway()
        judge = self.mod.Judge(gateway, _FakeStateEngine(persistence), config=gateway.config)

        asyncio.run(
            judge.evaluate(
                chat_id="default:GroupMessage:group-1",
                message="hello there",
                is_force_wakeup=False,
                persona_summary="persona summary",
                window_events_count=2,
            )
        )

        prompt = gateway.prompts[-1]
        expected_time = datetime.fromtimestamp(recent_ts).strftime("%Y-%m-%d %H:%M")
        self.assertIn(f"[{expected_time}] RecentUser: recent clue", prompt)
        self.assertNotIn("OldUser: stale clue", prompt)
        self.assertNotIn("NoTsUser: missing timestamp", prompt)

    def test_keyword_wakeup_extends_history_window_to_thirty_minutes(self):
        now = time.time()
        recent_ts = now - 20 * 60
        persistence = _LegacyHistoryPersistence(
            recent_messages=[
                {"sender_name": "WakeRecent", "content": "still relevant", "timestamp": recent_ts},
                {"sender_name": "TooOld", "content": "should be dropped", "timestamp": now - 45 * 60},
            ]
        )
        gateway = _FakeGateway()
        judge = self.mod.Judge(gateway, _FakeStateEngine(persistence), config=gateway.config)

        asyncio.run(
            judge.evaluate(
                chat_id="default:GroupMessage:group-1",
                message="atri can you explain this",
                is_force_wakeup=False,
                persona_summary="persona summary",
                window_events_count=2,
            )
        )

        prompt = gateway.prompts[-1]
        expected_time = datetime.fromtimestamp(recent_ts).strftime("%Y-%m-%d %H:%M")
        self.assertIn(f"[{expected_time}] WakeRecent: still relevant", prompt)
        self.assertNotIn("TooOld: should be dropped", prompt)

    def test_judge_falls_back_to_database_service_recent_logs(self):
        now = time.time()
        db_service = _DatabaseServiceStub(
            [SimpleNamespace(sender_name="DbUser", content="db-backed clue", timestamp=now - 30)]
        )
        persistence = SimpleNamespace(database_service=db_service)
        gateway = _FakeGateway()
        judge = self.mod.Judge(gateway, _FakeStateEngine(persistence), config=gateway.config)

        asyncio.run(
            judge.evaluate(
                chat_id="default:GroupMessage:group-1",
                message="hello from db fallback",
                is_force_wakeup=False,
                persona_summary="persona summary",
                window_events_count=2,
            )
        )

        self.assertEqual(db_service.calls[-1]["max_age_seconds"], 900.0)
        self.assertIn("DbUser: db-backed clue", gateway.prompts[-1])

    def test_judge_prefers_attention_window_events_over_database_history(self):
        now = time.time()
        db_service = _DatabaseServiceStub(
            [SimpleNamespace(sender_name="DbUser", content="db-backed clue", timestamp=now - 30)]
        )
        persistence = SimpleNamespace(database_service=db_service)
        gateway = _FakeGateway()
        judge = self.mod.Judge(gateway, _FakeStateEngine(persistence), config=gateway.config)
        previous = _FakeWindowEvent("WindowUser", "window-backed clue", now - 5)
        focus = _FakeWindowEvent("FocusUser", "current message", now)

        asyncio.run(
            judge.evaluate(
                chat_id="default:GroupMessage:group-1",
                message="FocusUser: current message",
                is_force_wakeup=False,
                persona_summary="persona summary",
                window_events_count=2,
                window_events=[previous, focus],
                focus_event=focus,
            )
        )

        self.assertIn("WindowUser: window-backed clue", gateway.prompts[-1])
        self.assertNotIn("DbUser: db-backed clue", gateway.prompts[-1])
        self.assertEqual(db_service.calls, [])

    def test_load_recent_history_records_short_circuits_after_first_valid_loader(self):
        now = time.time()
        persistence = _ShortCircuitPersistence(
            recent_messages=[{"sender_name": "RecentUser", "content": "recent clue", "timestamp": now - 30}]
        )
        judge = self.mod.Judge(_FakeGateway(), _FakeStateEngine(persistence))

        records = asyncio.run(
            judge._load_recent_history_records(
                "default:GroupMessage:group-1",
                max_age_seconds=900.0,
                limit=8,
            )
        )

        self.assertEqual([record["sender_name"] for record in records], ["RecentUser"])
        self.assertEqual(persistence.calls, ["recent"])

    def test_flatten_history_content_uses_readable_placeholders(self):
        flattened = self.mod.Judge._flatten_history_content(
            [
                {"type": "text", "text": "look "},
                {"type": "image"},
                {"type": "at"},
            ]
        )

        self.assertEqual(flattened, "look [image][@mention]")
        self.assertNotIn("[鍥剧墖]", flattened)
        self.assertNotIn("[@鏌愪汉]", flattened)


    def test_primary_mood_prepass_turns_small_judge_delta_into_microadjust(self):
        gateway = _ResultGateway(
            {
                "action": "REPLY",
                "reason": "reply",
                "thought": "go on",
                "relevance": 8,
                "necessity": 8.0,
                "retrieve_keys": [],
                "mood_tag": "happy",
                "mood_delta": 0.1,
            }
        )
        state_engine = _CaptureStateEngine(_LegacyHistoryPersistence())
        judge = self.mod.Judge(gateway, state_engine, config=gateway.config)
        focus_event = _FakeWindowEvent("FocusUser", "current message", time.time())
        focus_event._extra["astrmai_primary_mood_applied"] = True

        asyncio.run(
            judge.evaluate(
                chat_id="default:GroupMessage:group-1",
                message="FocusUser: current message",
                is_force_wakeup=False,
                persona_summary="persona summary",
                window_events_count=1,
                window_events=[focus_event],
                focus_event=focus_event,
            )
        )

        self.assertEqual(state_engine.mood_updates, [])

    def test_primary_mood_prepass_scales_large_judge_delta(self):
        gateway = _ResultGateway(
            {
                "action": "REPLY",
                "reason": "reply",
                "thought": "go on",
                "relevance": 8,
                "necessity": 8.0,
                "retrieve_keys": [],
                "mood_tag": "angry",
                "mood_delta": -0.4,
            }
        )
        state_engine = _CaptureStateEngine(_LegacyHistoryPersistence())
        judge = self.mod.Judge(gateway, state_engine, config=gateway.config)
        focus_event = _FakeWindowEvent("FocusUser", "current message", time.time())
        focus_event._extra["astrmai_primary_mood_applied"] = True

        asyncio.run(
            judge.evaluate(
                chat_id="default:GroupMessage:group-1",
                message="FocusUser: current message",
                is_force_wakeup=False,
                persona_summary="persona summary",
                window_events_count=1,
                window_events=[focus_event],
                focus_event=focus_event,
            )
        )

        self.assertEqual(state_engine.mood_updates, [("default:GroupMessage:group-1", -0.1)])
        self.assertAlmostEqual(focus_event.get_extra("astrmai_judge_mood_delta"), -0.1)

    def test_judge_releases_active_group_lock_after_gateway_failure(self):
        class _FailingGateway(_FakeGateway):
            async def chat_in_lane_result(self, **kwargs):
                self.prompts.append(kwargs["prompt"])
                raise RuntimeError("gateway down")

        gateway = _FailingGateway()
        judge = self.mod.Judge(gateway, _FakeStateEngine(_LegacyHistoryPersistence()), config=gateway.config)
        self.mod.logger.exception = lambda *args, **kwargs: None

        first = asyncio.run(
            judge.evaluate(
                chat_id="default:GroupMessage:group-1",
                message="first turn",
                is_force_wakeup=False,
                persona_summary="persona summary",
                window_events_count=1,
            )
        )
        second = asyncio.run(
            judge.evaluate(
                chat_id="default:GroupMessage:group-1",
                message="second turn",
                is_force_wakeup=False,
                persona_summary="persona summary",
                window_events_count=1,
            )
        )

        self.assertEqual(first.action, "REPLY")
        self.assertEqual(second.action, "REPLY")
        self.assertNotIn("default:GroupMessage:group-1", judge.active_sys1_groups)
        self.assertEqual(len(gateway.prompts), 2)


if __name__ == "__main__":
    unittest.main()
