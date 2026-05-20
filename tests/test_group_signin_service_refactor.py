import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _FakeApi:
    def __init__(self, *, should_fail=False):
        self.should_fail = should_fail
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if self.should_fail:
            raise RuntimeError("sign failed")
        return {"ok": True}


class _FakeDispatcher:
    def __init__(self):
        self.intents = []

    async def dispatch(self, intent, *, on_complete=None):
        self.intents.append(intent)
        return SimpleNamespace(allowed=True, blocked_reason="")


class _FakePersistence:
    def __init__(self):
        self.saved = []

    async def save_chat_state(self, chat_id, state):
        self.saved.append((chat_id, dict(getattr(state, "group_config", {}) or {})))


class GroupSigninServiceRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.proactive.group_signin_service", None)
        self.mod = importlib.import_module("astrmai.proactive.group_signin_service")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _build_service(self, *, states, api, dispatcher=None, persistence=None):
        gateway = SimpleNamespace(context=SimpleNamespace(client=SimpleNamespace(api=api)))
        state_engine = SimpleNamespace(gateway=gateway, get_active_states=lambda: states)
        return self.mod.GroupSigninService(
            state_engine=state_engine,
            persistence=persistence or _FakePersistence(),
            dispatcher=dispatcher or _FakeDispatcher(),
            config=SimpleNamespace(),
        )

    def test_run_once_signs_active_group_and_dispatches_followup(self):
        state = SimpleNamespace(chat_id="default:GroupMessage:12345", group_config={}, is_dirty=False)
        api = _FakeApi()
        dispatcher = _FakeDispatcher()
        persistence = _FakePersistence()
        service = self._build_service(states=[state], api=api, dispatcher=dispatcher, persistence=persistence)

        asyncio.run(service.run_once(now_ts=1768695000.0))

        self.assertEqual(api.calls, [("set_group_sign", {"group_id": "12345"})])
        self.assertEqual(len(dispatcher.intents), 1)
        self.assertEqual(dispatcher.intents[0].source, "group_signin")
        self.assertEqual(dispatcher.intents[0].chat_id, "default:GroupMessage:12345")
        self.assertEqual(state.group_config["group_signin"]["last_date"], "2026-01-18")
        self.assertTrue(state.is_dirty)
        self.assertEqual(len(persistence.saved), 1)

    def test_run_once_skips_duplicate_same_day(self):
        state = SimpleNamespace(
            chat_id="default:GroupMessage:12345",
            group_config={"group_signin": {"last_date": "2026-01-18"}},
            is_dirty=False,
        )
        api = _FakeApi()
        dispatcher = _FakeDispatcher()
        service = self._build_service(states=[state], api=api, dispatcher=dispatcher)

        asyncio.run(service.run_once(now_ts=1768695000.0))

        self.assertEqual(api.calls, [])
        self.assertEqual(dispatcher.intents, [])

    def test_run_once_sign_failure_does_not_dispatch(self):
        state = SimpleNamespace(chat_id="default:GroupMessage:12345", group_config={}, is_dirty=False)
        api = _FakeApi(should_fail=True)
        dispatcher = _FakeDispatcher()
        persistence = _FakePersistence()
        service = self._build_service(states=[state], api=api, dispatcher=dispatcher, persistence=persistence)

        asyncio.run(service.run_once(now_ts=1768695000.0))

        self.assertEqual(len(api.calls), 1)
        self.assertEqual(dispatcher.intents, [])
        self.assertEqual(persistence.saved, [])

    def test_run_once_before_window_skips_all_groups(self):
        state = SimpleNamespace(chat_id="default:GroupMessage:12345", group_config={}, is_dirty=False)
        api = _FakeApi()
        dispatcher = _FakeDispatcher()
        service = self._build_service(states=[state], api=api, dispatcher=dispatcher)

        before_window = time.mktime((2026, 1, 18, 7, 0, 0, 0, 0, -1))
        asyncio.run(service.run_once(now_ts=before_window))

        self.assertEqual(api.calls, [])
        self.assertEqual(dispatcher.intents, [])

    def test_run_once_after_sign_hour_does_not_late_patch_sign(self):
        state = SimpleNamespace(chat_id="default:GroupMessage:12345", group_config={}, is_dirty=False)
        api = _FakeApi()
        dispatcher = _FakeDispatcher()
        service = self._build_service(states=[state], api=api, dispatcher=dispatcher)

        after_window = time.mktime((2026, 1, 18, 15, 0, 0, 0, 0, -1))
        asyncio.run(service.run_once(now_ts=after_window))

        self.assertEqual(api.calls, [])
        self.assertEqual(dispatcher.intents, [])
