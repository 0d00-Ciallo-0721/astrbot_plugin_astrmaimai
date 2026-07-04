import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


def _install_cron_guard_stubs():
    tool_mod = types.ModuleType("astrbot.core.agent.tool")

    class _FunctionTool:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    class _ToolSet:
        def __init__(self, tools):
            self.tools = list(tools)

        def get_light_tool_set(self):
            return self

    tool_mod.FunctionTool = _FunctionTool
    tool_mod.ToolExecResult = str
    tool_mod.ToolSet = _ToolSet
    sys.modules["astrbot.core.agent.tool"] = tool_mod

    run_context_mod = types.ModuleType("astrbot.core.agent.run_context")

    class _ContextWrapper:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    run_context_mod.ContextWrapper = _ContextWrapper
    sys.modules["astrbot.core.agent.run_context"] = run_context_mod

    agent_ctx_mod = types.ModuleType("astrbot.core.astr_agent_context")
    agent_ctx_mod.AstrAgentContext = object
    sys.modules["astrbot.core.astr_agent_context"] = agent_ctx_mod

    cron_tools_mod = types.ModuleType("astrbot.core.tools.cron_tools")
    cron_tools_mod.CREATE_CRON_JOB_TOOL = SimpleNamespace(name="create")
    cron_tools_mod.DELETE_CRON_JOB_TOOL = SimpleNamespace(name="delete")
    cron_tools_mod.LIST_CRON_JOBS_TOOL = SimpleNamespace(name="list")
    sys.modules["astrbot.core.tools.cron_tools"] = cron_tools_mod

    po_mod = types.ModuleType("astrbot.core.db.po")
    po_mod.CronJob = type("CronJob", (), {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)})
    sys.modules["astrbot.core.db.po"] = po_mod


class P1PrelaunchRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_search_nodes_treats_percent_and_underscore_as_literals(self):
        from astrmai.infrastructure.persistence.database_service import DatabaseService
        from astrmai.infrastructure.persistence.orm_models import MemoryNode
        from astrmai.infrastructure.persistence.persistence_manager import PersistenceManager

        manager = PersistenceManager()
        db = DatabaseService(manager)
        db.update_nodes(
            [
                MemoryNode(name="plain node", type="topic", description="ordinary"),
                MemoryNode(name="literal%node", type="topic", description="has percent"),
                MemoryNode(name="literal_node", type="topic", description="has underscore"),
            ]
        )

        percent_results = db.search_nodes("%", limit=10, include_description=False)
        underscore_results = db.search_nodes("_", limit=10, include_description=False)

        self.assertEqual([item.name for item in percent_results], ["literal%node"])
        self.assertEqual([item.name for item in underscore_results], ["literal_node"])
        manager.dispose()

    def test_temporal_boost_for_uninitialized_created_at_returns_alpha(self):
        from astrmai.memory.contracts.memory_query import MemoryCandidate
        from astrmai.memory.services.memory_scoring import MemoryScoringConfig, compute_temporal_boost

        candidate = MemoryCandidate(id="mem-1", kind="memory", source="test", summary="", content="", created_at=0)
        config = MemoryScoringConfig(deep_temporal_alpha=0.6)

        self.assertEqual(compute_temporal_boost(candidate, now=1000.0, config=config), 0.6)

    def test_reflector_empty_scores_keeps_batch_for_retry(self):
        sys.modules.pop("astrmai.learning.review.reflector", None)
        from astrmai.learning.review.reflector import ExpressionReflector

        class _Gateway:
            async def call_data_process_task(self, *args, **kwargs):
                return []

        reflector = ExpressionReflector(SimpleNamespace(memory_engine=None), _Gateway())
        reflector._pending_reflections = [
            {
                "pattern_id": str(index),
                "chat_id": "chat-1",
                "situation": "situation",
                "expression": f"expression-{index}",
                "reply": "reply",
                "reaction": "",
                "time": float(index),
            }
            for index in range(3)
        ]

        asyncio.run(reflector.reflect_batch("chat-1"))

        self.assertEqual([item["pattern_id"] for item in reflector._pending_reflections], ["0", "1", "2"])

    def test_save_jargons_prevalidates_before_first_write(self):
        from astrmai.learning.evolution_manager import EvolutionManager

        class _Writer:
            def __init__(self):
                self.writes = []

            async def write(self, request):
                self.writes.append(request)
                return "mem-1"

        writer = _Writer()
        manager = object.__new__(EvolutionManager)
        manager.db = SimpleNamespace(memory_engine=SimpleNamespace(write_service=writer))

        with self.assertRaises(ValueError):
            asyncio.run(
                manager._save_jargons(
                    "chat-1",
                    [
                        {"content": "ok", "activation_score": 0.6},
                        {"content": "bad", "activation_score": "not-a-float"},
                    ],
                )
            )

        self.assertEqual(writer.writes, [])

    def test_private_chat_eviction_skips_waiting_sessions(self):
        from astrmai.state.private_chat.private_chat_manager import PrivateChatManager

        manager = PrivateChatManager()
        manager.MAX_SESSIONS = 2
        waiting = manager._get_or_create_session("waiting")
        waiting.is_bot_waiting = True
        waiting.last_message_time = 1.0
        idle = manager._get_or_create_session("idle")
        idle.last_message_time = 2.0

        manager._get_or_create_session("new")

        self.assertIn("waiting", manager._sessions)
        self.assertIn("new", manager._sessions)
        self.assertNotIn("idle", manager._sessions)

    def test_dashboard_snapshot_degrades_diagnostics_and_capability_failures(self):
        sys.modules.pop("astrmai.webui.backend.services.dashboard_service", None)
        mod = importlib.import_module("astrmai.webui.backend.services.dashboard_service")

        class _Repo:
            async def snapshot_counts(self):
                return {"total_users": 1}

        class _PluginApi:
            async def get_runtime_diagnostics(self):
                raise RuntimeError("diagnostics offline")

            async def get_capability_overview(self):
                raise RuntimeError("capabilities offline")

        service = mod.DashboardService(_PluginApi(), lambda: None)
        service._repo = _Repo()

        snapshot = asyncio.run(service.get_snapshot())

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["diagnostics"]["status"], "degraded")
        self.assertEqual(snapshot["capabilities"]["status"], "degraded")
        self.assertEqual(snapshot["total_users"], 1)

    def test_cron_reload_continues_after_one_snapshot_fails(self):
        sys.modules.pop("astrmai.workmode.cron_guard.heartbeat", None)
        _install_cron_guard_stubs()
        guard_mod = importlib.import_module("astrmai.workmode.cron_guard.heartbeat")
        snapshots = [
            SimpleNamespace(job_id="job-1", name="bad", cron_expression="* * * * *", run_at=None, run_once=False, payload={}),
            SimpleNamespace(job_id="job-2", name="good", cron_expression="* * * * *", run_at=None, run_once=False, payload={}),
        ]

        class _DB:
            async def get_all_active_cron_snapshots(self):
                return snapshots

            async def save_cron_snapshot(self, snapshot):
                return None

            async def deactivate_cron_snapshot(self, job_id):
                return None

        class _CronMgr:
            async def list_jobs(self):
                return []

            async def add_active_job(self, **kwargs):
                if kwargs["name"] == "bad":
                    raise RuntimeError("cannot restore")
                return SimpleNamespace(id="new-good")

        guard = guard_mod.CronHeartbeatGuard(_DB(), SimpleNamespace(cron_manager=_CronMgr()))

        self.assertEqual(asyncio.run(guard.reload_all_lost_jobs()), 1)


if __name__ == "__main__":
    unittest.main()
