import asyncio
import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class P2PrelaunchRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_unknown_provider_disables_native_prompt_cache(self):
        from astrmai.infrastructure.gateway.provider_capabilities import infer_provider_capabilities

        caps = infer_provider_capabilities(None)

        self.assertEqual(caps.provider_family, "unknown")
        self.assertFalse(caps.supports_native_prompt_cache)

    def test_raw_trace_store_falls_back_when_replace_is_locked(self):
        from astrmai.infrastructure.runtime.raw_trace_store import RawTraceEventStore

        store = RawTraceEventStore(Path(self.temp_dir.name), filename="raw.json")

        with patch("astrmai.infrastructure.runtime.raw_trace_store.os.replace", side_effect=PermissionError("locked")):
            asyncio.run(store.append({"chat_id": "chat-1", "created_at": 1.0, "kind": "test"}))

        self.assertTrue(store.path.exists())
        self.assertEqual(asyncio.run(store.recent(chat_id="chat-1"))[0]["kind"], "test")

    def test_message_scope_handles_nonstandard_event_accessors(self):
        from astrmai.presentation.dto.message_scope import MessageScope

        class _Event:
            unified_msg_origin = "default:FriendMessage:user-1"

            def get_sender_id(self):
                raise RuntimeError("sender unavailable")

            def get_group_id(self):
                raise RuntimeError("group unavailable")

        scope = MessageScope.from_event(_Event())

        self.assertEqual(scope.sender_id, "")
        self.assertEqual(scope.group_id, "")

    def test_meme_probability_accepts_decimal_string(self):
        from astrmai.shared.constants.defaults import build_infrastructure_settings

        config = SimpleNamespace(
            provider=SimpleNamespace(),
            infra=SimpleNamespace(),
            global_settings=SimpleNamespace(),
            system1=SimpleNamespace(),
            sys3=SimpleNamespace(),
            vision=SimpleNamespace(),
            life=SimpleNamespace(),
            reply=SimpleNamespace(meme_probability="10.5"),
            conversation=SimpleNamespace(),
        )

        settings = build_infrastructure_settings(config)

        self.assertTrue(settings.features.meme_enabled)

    def test_admin_safe_count_respects_memory_event_where_clause(self):
        from astrmai.webui.backend.services.admin_ui_service import AdminUiService

        class _Cursor:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def fetchone(self):
                return (2,)

        class _Db:
            def __init__(self):
                self.calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=()):
                self.calls.append((sql, params))
                return _Cursor()

        db = _Db()
        service = AdminUiService(SimpleNamespace(), db_factory=lambda: db)

        count = asyncio.run(service._safe_count("MemoryEvent", where="session_id = ?", params=("chat-1",)))

        self.assertEqual(count, 2)
        self.assertIn("WHERE session_id = ?", db.calls[0][0])
        self.assertEqual(db.calls[0][1], ("chat-1",))

    def test_capability_overview_degrades_when_describe_status_fails(self):
        from astrmai.app.runtime_context import PluginRuntimeContext

        class _BadCronGuard:
            def describe_status(self):
                raise RuntimeError("cron unavailable")

        runtime = PluginRuntimeContext(
            host_context=SimpleNamespace(),
            raw_config={},
            config=SimpleNamespace(),
            runtime_coordinator=SimpleNamespace(),
            host_bridge=SimpleNamespace(),
        )
        runtime.workmode.cron_guard = _BadCronGuard()

        overview = runtime.build_capability_overview_sync()

        self.assertEqual(overview["workmode"]["cron_guard"]["running"], False)
        self.assertIn("cron unavailable", overview["workmode"]["cron_guard"]["error"])

    def test_review_dispatcher_backs_off_after_send_failure(self):
        from astrmai.proactive.review_dispatcher import ReviewDispatcher

        class _Context:
            async def send_message(self, umo, chain):
                raise RuntimeError("send failed")

        class _Tracker:
            async def get_unsent_requests(self):
                return [{"pattern_id": "p1", "group_id": "chat-1", "question": "review?"}]

        sleeps = []

        async def _sleep(delay):
            sleeps.append(delay)

        with patch("astrmai.proactive.review_dispatcher.asyncio.sleep", new=_sleep):
            asyncio.run(ReviewDispatcher(_Context(), _Tracker()).dispatch_pending())

        self.assertEqual(sleeps, [0.2])

    def test_diary_service_loads_persona_cache_off_event_loop(self):
        diary_mod = importlib.import_module("astrmai.proactive.diary_service")

        class _Semaphore:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class _Persistence:
            def load_persona_cache(self):
                return {"persona-1": {"summary": "quiet"}}

        service = diary_mod.DiaryService(
            persistence=_Persistence(),
            memory_engine=SimpleNamespace(get_recent_memories=lambda *args, **kwargs: asyncio.sleep(0, result=[])),
            config=SimpleNamespace(persona=SimpleNamespace(persona_id="persona-1")),
            call_background_lane=lambda *args, **kwargs: asyncio.sleep(0, result=None),
            semaphore=_Semaphore(),
        )
        to_thread_calls = []

        async def _to_thread(func, *args, **kwargs):
            to_thread_calls.append(func)
            return func(*args, **kwargs)

        with patch("astrmai.proactive.diary_service.asyncio.to_thread", new=_to_thread):
            asyncio.run(service.run_once([SimpleNamespace(chat_id="chat-1")]))

        self.assertEqual(len(to_thread_calls), 1)


if __name__ == "__main__":
    unittest.main()
