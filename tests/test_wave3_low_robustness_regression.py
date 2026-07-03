import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class Wave3LowRobustnessRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_dream_detail_surfaces_db_errors(self):
        from astrmai.memory.dream.dream_agent import DreamAgent

        class _DB:
            def get_session(self):
                raise RuntimeError("db locked")

        agent = DreamAgent(SimpleNamespace(config=SimpleNamespace()), _DB())

        result = asyncio.run(agent._tool_get_detail({"event_id": "event-1"}))

        self.assertIn("db locked", result)
        self.assertNotIn("未找到", result)

    def test_group_wait_info_uses_monotonic_clock(self):
        mod = importlib.import_module("astrmai.state.group_wait.group_reply_wait_manager")
        manager = mod.GroupReplyWaitManager(timeout_sec=30.0)
        manager._states["chat-1"] = mod.GroupReplyWaitState(
            chat_id="chat-1",
            target_user_id="user-1",
            expires_at=mod.monotonic() + 30.0,
        )

        with patch("astrmai.state.group_wait.group_reply_wait_manager.time.time", return_value=10**12):
            info = manager.get_wait_info("chat-1")

        self.assertGreater(info["remaining_seconds"], 0.0)

    def test_save_pattern_tracks_canonical_background_task_when_lifecycle_exists(self):
        from astrmai.infrastructure.persistence.database_review import ReviewPersistenceMixin
        from astrmai.infrastructure.persistence.orm_models import ExpressionPattern

        tracked = []

        class _LifecycleManager:
            def track_task(self, coro):
                tracked.append(coro)
                coro.close()
                return SimpleNamespace(add_done_callback=lambda _callback: None)

        class _Service(ReviewPersistenceMixin):
            memory_engine = SimpleNamespace(expression_pattern_service=SimpleNamespace(write_pattern=True))
            lifecycle = SimpleNamespace(manager=_LifecycleManager())

            async def _save_pattern_to_canonical_async(self, pattern):
                return None

            def get_session(self):
                raise RuntimeError("skip orm")

        pattern = ExpressionPattern(group_id="g", situation="s", expression="e")

        async def _run():
            _Service().save_pattern(pattern)

        asyncio.run(_run())

        self.assertEqual(len(tracked), 1)

    def test_goal_parser_rejects_non_string_goal_values(self):
        from astrmai.conversation.planning.goal_service import GoalManager

        manager = object.__new__(GoalManager)

        goals = manager._parse_goals([
            {"goal": ["bad"], "reasoning": "list"},
            {"goal": {"bad": True}, "reasoning": "dict"},
            {"goal": "valid", "reasoning": "ok"},
        ])

        self.assertEqual([item.goal for item in goals], ["valid"])

    def test_memory_injection_trace_persist_failure_is_logged(self):
        from astrmai.memory.contracts.memory_query import MemoryInjectionTrace, MemoryQuery
        from astrmai.memory.services.memory_injection_service import MemoryInjectionService

        class _DB:
            async def save_retrieval_trace_async(self, record):
                raise RuntimeError("trace db down")

        service = MemoryInjectionService(SimpleNamespace(engine=SimpleNamespace(db_service=_DB())))
        event = SimpleNamespace(
            unified_msg_origin="chat-1",
            get_sender_name=lambda: "Alice",
        )
        query = MemoryQuery(query="q", session_id="chat-1")
        trace = MemoryInjectionTrace(trace_id="trace-1")

        warnings = []

        def _warning(message, *args, **kwargs):
            warnings.append((message, kwargs))

        with patch("astrmai.memory.services.memory_injection_service.logger.warning", new=_warning):
            asyncio.run(service._persist_trace(event, query, trace, []))

        self.assertTrue(warnings)
        self.assertTrue(warnings[0][1].get("exc_info"))

    def test_memory_context_builder_does_not_emit_ellipsis_only_line(self):
        from astrmai.memory.contracts.memory_query import MemoryCandidate
        from astrmai.memory.services.memory_context_builder import MemoryContextBuilder

        candidate = MemoryCandidate(
            id="m1",
            kind="memory",
            source="unit",
            content="abcdef",
            summary="abcdef",
            session_id="chat-1",
            relevance_score=1.0,
        )
        builder = MemoryContextBuilder(max_chars=240)
        builder.max_chars = 2

        rendered, _guidance = builder.render_prompt_block([candidate])

        self.assertNotIn("\n...", rendered)
        self.assertEqual(rendered, "")

    def test_unknown_profile_memory_categories_do_not_become_speech_style(self):
        from astrmai.state.user_profile_service import UserProfileService

        service = object.__new__(UserProfileService)

        categorized = service.categorize_memory_points(["习惯:每天早睡:0.9", "性格:外向:0.8"])

        self.assertEqual(categorized["speech_style_points"], [])

    def test_decay_service_degrades_when_state_or_profile_listing_fails(self):
        from astrmai.proactive.decay_service import DecayService

        class _StateEngine:
            def __init__(self):
                self.profile_decay_called = False

            def get_active_states(self):
                raise RuntimeError("states offline")

            def get_active_profiles(self):
                self.profile_decay_called = True
                raise RuntimeError("profiles offline")

        engine = _StateEngine()
        config = SimpleNamespace(evolution=SimpleNamespace(enable_relationship_engine=False))
        service = DecayService(engine, None, config)

        asyncio.run(service.run_once())

        self.assertTrue(engine.profile_decay_called)

    def test_dashboard_snapshot_degrades_system_metrics_failures(self):
        sys.modules.pop("astrmai.webui.backend.services.dashboard_service", None)
        mod = importlib.import_module("astrmai.webui.backend.services.dashboard_service")

        class _Repo:
            async def snapshot_counts(self):
                return {"total_users": 1}

        class _PluginApi:
            async def get_runtime_diagnostics(self):
                return {"status": "ok"}

            async def get_capability_overview(self):
                return {"capabilities": []}

        service = mod.DashboardService(_PluginApi(), lambda: None)
        service._repo = _Repo()

        with patch.object(mod.os.path, "exists", side_effect=OSError("fs unavailable")), \
             patch.object(mod.psutil, "Process", side_effect=RuntimeError("ps unavailable")):
            snapshot = asyncio.run(service.get_snapshot())

        self.assertEqual(snapshot["db_size_kb"], 0)
        self.assertEqual(snapshot["webui_mem_mb"], 0)
        self.assertEqual(snapshot["total_users"], 1)


if __name__ == "__main__":
    unittest.main()
