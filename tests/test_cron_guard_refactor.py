import asyncio
import importlib
import json
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

    class CronJob:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    po_mod.CronJob = CronJob
    sys.modules["astrbot.core.db.po"] = po_mod


class CronGuardRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        _install_cron_guard_stubs()
        sys.modules.pop("astrmai.workmode.cron_guard.heartbeat", None)
        self.guard_mod = importlib.import_module("astrmai.workmode.cron_guard.heartbeat")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_guard_revives_missing_jobs_from_snapshots(self):
        snapshots = [
            SimpleNamespace(job_id="job-1", name="n", cron_expression="* * * * *", run_at=None, run_once=False, payload={}),
        ]
        revived = []

        class _DB:
            async def get_all_active_cron_snapshots(self):
                return snapshots

            async def deactivate_cron_snapshot(self, job_id):
                revived.append(("deactivate", job_id))

        class _CronMgr:
            async def list_jobs(self):
                return []

            async def add_active_job(self, name, cron_expression, payload, run_once, run_at):
                revived.append(("add", name))

        guard = self.guard_mod.CronHeartbeatGuard(_DB(), SimpleNamespace(cron_manager=_CronMgr()))
        result = asyncio.run(guard.reload_all_lost_jobs())
        self.assertEqual(result, 1)
        self.assertIn(("add", "n"), revived)

    def test_guard_replaces_snapshot_when_framework_returns_new_job_id(self):
        saved = []
        deactivated = []

        class _DB:
            async def save_cron_snapshot(self, snapshot):
                saved.append(snapshot)

            async def deactivate_cron_snapshot(self, job_id):
                deactivated.append(job_id)

        class _CronMgr:
            async def add_active_job(self, name, cron_expression, payload, run_once, run_at):
                self.added = {
                    "name": name,
                    "cron_expression": cron_expression,
                    "payload": payload,
                    "run_once": run_once,
                    "run_at": run_at,
                }
                return SimpleNamespace(id="new-job")

        cron_mgr = _CronMgr()
        guard = self.guard_mod.CronHeartbeatGuard(_DB(), SimpleNamespace(cron_manager=cron_mgr))
        snap = SimpleNamespace(
            job_id="old-job",
            name="n",
            cron_expression=None,
            run_at=None,
            run_once=True,
            target_origin="umo",
            payload=json.dumps({"session": "umo", "run_at": "2026-07-03T08:30:00+08:00"}, ensure_ascii=False),
        )

        result = asyncio.run(guard._revive_job(cron_mgr, snap))

        self.assertTrue(result)
        self.assertEqual(cron_mgr.added["payload"]["session"], "umo")
        self.assertEqual(cron_mgr.added["run_at"].isoformat(), "2026-07-03T08:30:00+08:00")
        self.assertEqual(deactivated, ["old-job"])
        self.assertEqual(saved[0].job_id, "new-job")
        self.assertEqual(json.loads(saved[0].payload)["session"], "umo")


if __name__ == "__main__":
    unittest.main()
