import asyncio
import copy
import importlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs

# G1 (OPT-13): 签到窗口判定用 time.localtime（机器本地时区），因此测试时间戳必须
# 按“本地时间”构造。旧代码硬编码 epoch 1768695000.0——它只在 UTC+8 下等于 08:10
# （写测试时的机器时区），在别的时区（如本机 UTC-8 为 16:10）直接落到窗口外，
# 导致 3 个用例随机器时区红/绿。下面的 helper 由本地时间反推 epoch，时区无关。
_SIGN_DAY = (2026, 1, 18)


def _local_ts(hour: int, minute: int = 0) -> float:
    year, month, day = _SIGN_DAY
    return time.mktime((year, month, day, int(hour), int(minute), 0, 0, 0, -1))


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
        self.saved.append((chat_id, copy.deepcopy(getattr(state, "group_config", {}) or {})))


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

        asyncio.run(service.run_once(now_ts=_local_ts(self.mod.GroupSigninService.SIGN_HOUR, 10)))

        self.assertEqual(api.calls, [("set_group_sign", {"group_id": "12345"})])
        self.assertEqual(len(dispatcher.intents), 1)
        self.assertEqual(dispatcher.intents[0].source, "group_signin")
        self.assertEqual(dispatcher.intents[0].chat_id, "default:GroupMessage:12345")
        self.assertEqual(state.group_config["group_signin"]["last_date"], "2026-01-18")
        self.assertTrue(state.is_dirty)
        self.assertEqual(len(persistence.saved), 2)
        self.assertEqual(persistence.saved[0][1]["group_signin"]["status"], "intent")
        self.assertEqual(persistence.saved[1][1]["group_signin"]["status"], "complete")

    def test_run_once_skips_duplicate_same_day(self):
        state = SimpleNamespace(
            chat_id="default:GroupMessage:12345",
            group_config={"group_signin": {"last_date": "2026-01-18"}},
            is_dirty=False,
        )
        api = _FakeApi()
        dispatcher = _FakeDispatcher()
        service = self._build_service(states=[state], api=api, dispatcher=dispatcher)

        asyncio.run(service.run_once(now_ts=_local_ts(self.mod.GroupSigninService.SIGN_HOUR, 10)))

        self.assertEqual(api.calls, [])
        self.assertEqual(dispatcher.intents, [])

    def test_run_once_sign_failure_does_not_dispatch(self):
        state = SimpleNamespace(chat_id="default:GroupMessage:12345", group_config={}, is_dirty=False)
        api = _FakeApi(should_fail=True)
        dispatcher = _FakeDispatcher()
        persistence = _FakePersistence()
        service = self._build_service(states=[state], api=api, dispatcher=dispatcher, persistence=persistence)

        asyncio.run(service.run_once(now_ts=_local_ts(self.mod.GroupSigninService.SIGN_HOUR, 10)))

        self.assertEqual(len(api.calls), 1)
        self.assertEqual(dispatcher.intents, [])
        self.assertEqual(len(persistence.saved), 2)
        self.assertEqual(persistence.saved[-1][1]["group_signin"]["status"], "failed")
        self.assertEqual(state.group_config["group_signin"]["last_date"], "")

    def test_persist_failure_after_external_success_is_partial_and_not_repeated_after_restart(self):
        class _FailFinalPersistence:
            def __init__(self):
                self.calls = 0
                self.persisted = None

            async def save_chat_state(self, _chat_id, state):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("db locked")
                self.persisted = copy.deepcopy(state.group_config)

        state = SimpleNamespace(chat_id="default:GroupMessage:12345", group_config={}, is_dirty=False)
        api = _FakeApi()
        dispatcher = _FakeDispatcher()
        persistence = _FailFinalPersistence()
        service = self._build_service(states=[state], api=api, dispatcher=dispatcher, persistence=persistence)

        asyncio.run(service.run_once(now_ts=_local_ts(self.mod.GroupSigninService.SIGN_HOUR, 10)))

        self.assertEqual(len(api.calls), 1)
        self.assertEqual(dispatcher.intents, [])
        self.assertEqual(service.describe_status()["last_run"]["status"], "partial")

        restarted_state = SimpleNamespace(
            chat_id=state.chat_id,
            group_config=copy.deepcopy(persistence.persisted),
            is_dirty=False,
        )
        restarted_api = _FakeApi()
        restarted_dispatcher = _FakeDispatcher()
        restarted = self._build_service(
            states=[restarted_state],
            api=restarted_api,
            dispatcher=restarted_dispatcher,
        )
        asyncio.run(restarted.run_once(now_ts=_local_ts(self.mod.GroupSigninService.SIGN_HOUR, 10)))

        self.assertEqual(restarted_api.calls, [])
        self.assertEqual(restarted_dispatcher.intents, [])

    def test_sign_window_predicate_is_timezone_independent(self):
        # G1 (OPT-13) 锚定：窗口判定必须只依赖“本地小时”，测试时间戳一律由
        # _local_ts 从 SIGN_HOUR 派生——任何机器时区下结论一致。
        # 若有人再把时间戳写成裸 epoch，本用例会在非 UTC+8 机器上立刻变红。
        service_cls = self.mod.GroupSigninService
        sign_hour = service_cls.SIGN_HOUR

        self.assertTrue(service_cls._within_sign_window(_local_ts(sign_hour, 0)))
        self.assertTrue(service_cls._within_sign_window(_local_ts(sign_hour, 59)))
        self.assertFalse(service_cls._within_sign_window(_local_ts(sign_hour - 1, 59)))
        self.assertFalse(service_cls._within_sign_window(_local_ts(sign_hour + 1, 0)))

        # 派生时间戳的本地小时就是 SIGN_HOUR（构造即保证，与机器时区无关）
        self.assertEqual(time.localtime(_local_ts(sign_hour, 10)).tm_hour, sign_hour)

    def test_run_once_before_window_skips_all_groups(self):
        state = SimpleNamespace(chat_id="default:GroupMessage:12345", group_config={}, is_dirty=False)
        api = _FakeApi()
        dispatcher = _FakeDispatcher()
        service = self._build_service(states=[state], api=api, dispatcher=dispatcher)

        before_window = _local_ts(self.mod.GroupSigninService.SIGN_HOUR - 1)
        asyncio.run(service.run_once(now_ts=before_window))

        self.assertEqual(api.calls, [])
        self.assertEqual(dispatcher.intents, [])

    def test_run_once_after_sign_hour_does_not_late_patch_sign(self):
        state = SimpleNamespace(chat_id="default:GroupMessage:12345", group_config={}, is_dirty=False)
        api = _FakeApi()
        dispatcher = _FakeDispatcher()
        service = self._build_service(states=[state], api=api, dispatcher=dispatcher)

        after_window = _local_ts(self.mod.GroupSigninService.SIGN_HOUR + 7)
        asyncio.run(service.run_once(now_ts=after_window))

        self.assertEqual(api.calls, [])
        self.assertEqual(dispatcher.intents, [])
