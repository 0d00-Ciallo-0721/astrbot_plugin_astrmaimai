import asyncio
import importlib
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs
from tests.test_workmode_router_refactor import _install_workmode_stubs


class _CronDb:
    def __init__(self):
        self.saved = []
        self.deactivated = []

    async def save_cron_snapshot(self, snapshot):
        self.saved.append(snapshot)

    async def get_all_active_cron_snapshots(self):
        return []

    async def deactivate_cron_snapshot(self, job_id):
        self.deactivated.append(job_id)


class WorkmodeGapCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        _install_workmode_stubs()
        for module_name in (
            "astrmai.workmode.subagents.base_agent",
            "astrmai.workmode.subagents.cron_agent",
            "astrmai.workmode.tools.handoff_registry",
        ):
            sys.modules.pop(module_name, None)
        self.cron_mod = importlib.import_module("astrmai.workmode.subagents.cron_agent")
        self.handoff_mod = importlib.import_module("astrmai.workmode.tools.handoff_registry")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_cron_agent_syncs_string_run_at_without_masking_result(self):
        jobs = [
            SimpleNamespace(
                id="job-valid",
                name="valid",
                payload={"session": "chat-1"},
                run_at="2030-01-02T03:04:05+00:00",
                cron_expression=None,
                run_once=True,
            ),
            SimpleNamespace(
                id="job-invalid",
                name="invalid",
                payload={"session": "chat-1"},
                run_at="not-a-date",
                cron_expression=None,
                run_once=True,
            ),
        ]

        class _CronManager:
            async def list_jobs(self):
                return jobs

        async def _provider_id(_origin):
            return "provider-1"

        async def _tool_loop_agent(**_kwargs):
            return SimpleNamespace(completion_text="cron task handled")

        db = _CronDb()
        agent = self.cron_mod.CronAgent(db_service=db)
        host_context = SimpleNamespace(
            cron_manager=_CronManager(),
            get_current_chat_provider_id=_provider_id,
            tool_loop_agent=_tool_loop_agent,
        )
        wrapper = SimpleNamespace(
            context=SimpleNamespace(
                context=host_context,
                event=SimpleNamespace(unified_msg_origin="chat-1"),
            )
        )

        result = asyncio.run(agent.call(wrapper, query="create reminder"))

        self.assertEqual(result, "cron task handled")
        self.assertEqual([item.job_id for item in db.saved], ["job-valid", "job-invalid"])
        self.assertEqual(
            db.saved[0].run_at,
            datetime.fromisoformat("2030-01-02T03:04:05+00:00").timestamp(),
        )
        self.assertIsNone(db.saved[1].run_at)

    def test_cron_agent_sync_accepts_json_payload_and_payload_run_at(self):
        job = SimpleNamespace(
            job_id="job-payload",
            name="payload",
            payload='{"session":"chat-2","run_at":"2031-02-03T04:05:06+00:00"}',
            run_at=None,
            cron_expression=None,
            run_once=True,
        )

        class _CronManager:
            async def list_jobs(self):
                return [job]

        async def _provider_id(_origin):
            return "provider-1"

        async def _tool_loop_agent(**_kwargs):
            return SimpleNamespace(completion_text="ok")

        db = _CronDb()
        agent = self.cron_mod.CronAgent(db_service=db)
        wrapper = SimpleNamespace(
            context=SimpleNamespace(
                context=SimpleNamespace(
                    cron_manager=_CronManager(),
                    get_current_chat_provider_id=_provider_id,
                    tool_loop_agent=_tool_loop_agent,
                ),
                event=SimpleNamespace(unified_msg_origin="chat-2"),
            )
        )

        asyncio.run(agent.call(wrapper, query="create reminder"))

        self.assertEqual(len(db.saved), 1)
        self.assertEqual(
            db.saved[0].run_at,
            datetime.fromisoformat("2031-02-03T04:05:06+00:00").timestamp(),
        )

    def test_static_agent_uses_injected_gateway_pool_without_host_provider(self):
        observed = {}

        class _Gateway:
            def get_agent_models(self):
                return ["agent-primary", "agent-fallback"]

            async def tool_chat_in_lane_result(self, **kwargs):
                observed.update(kwargs)
                return SimpleNamespace(text="handled by configured pool")

        agent = self.cron_mod.CronAgent(db_service=None)
        agent._gateway = _Gateway()
        wrapper = SimpleNamespace(
            context=SimpleNamespace(
                context=SimpleNamespace(),
                event=SimpleNamespace(unified_msg_origin="chat-1"),
            )
        )

        result = asyncio.run(agent.call(wrapper, query="create reminder"))

        self.assertEqual(result, "handled by configured pool")
        self.assertEqual(observed["models"], ["agent-primary", "agent-fallback"])
        self.assertEqual(observed["prompt"], "create reminder")

    def test_handoff_registry_removes_agents_no_longer_active(self):
        alpha = SimpleNamespace(name="dynamic_alpha", active=True)
        beta = SimpleNamespace(name="dynamic_beta", active=True)
        orchestrator = SimpleNamespace(handoffs=[alpha])
        registry = self.handoff_mod.HandoffRegistry(
            SimpleNamespace(subagent_orchestrator=orchestrator)
        )

        first = asyncio.run(registry.discover(set()))
        alpha.active = False
        orchestrator.handoffs = [alpha, beta]
        second = asyncio.run(registry.discover(set()))

        self.assertEqual([item.name for item in first], ["dynamic_alpha"])
        self.assertEqual([item.name for item in second], ["dynamic_beta"])
        self.assertEqual(registry.list_loaded_names(), ["dynamic_beta"])

    def test_handoff_registry_replaces_agent_instance_with_same_name(self):
        first_agent = SimpleNamespace(name="dynamic_alpha", active=True, version=1)
        second_agent = SimpleNamespace(name="dynamic_alpha", active=True, version=2)
        orchestrator = SimpleNamespace(handoffs=[first_agent])
        registry = self.handoff_mod.HandoffRegistry(
            SimpleNamespace(subagent_orchestrator=orchestrator)
        )

        asyncio.run(registry.discover(set()))
        orchestrator.handoffs = [second_agent]
        discovered = asyncio.run(registry.discover(set()))

        self.assertEqual(len(discovered), 1)
        self.assertIs(discovered[0], second_agent)
        self.assertEqual(discovered[0].version, 2)


if __name__ == "__main__":
    unittest.main()
