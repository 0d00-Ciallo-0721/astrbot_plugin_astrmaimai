from __future__ import annotations

import asyncio
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class RuntimeMetricsSourceAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _runtime(self):
        from astrmai.app.runtime_context import PluginRuntimeContext

        config = SimpleNamespace(
            provider=SimpleNamespace(
                task_models=["task-model"],
                agent_models=["agent-model"],
                fallback_models=["fallback-model"],
                vision_models=[],
                embedding_models=[],
            ),
            infra=SimpleNamespace(
                api_timeout=5.0,
                llm_retries=1,
                backoff_factor=1.0,
                max_concurrent_llm_calls=2,
                background_task_concurrency=2,
                background_task_queue_limit=8,
                background_task_wait_timeout_sec=3.0,
                background_task_execution_timeout_sec=10.0,
            ),
        )
        runtime = PluginRuntimeContext(
            host_context=SimpleNamespace(),
            raw_config={},
            config=config,
            runtime_coordinator=SimpleNamespace(),
            host_bridge=SimpleNamespace(),
        )
        runtime.diagnostics_sample_interval_sec = 0.0
        return runtime

    @staticmethod
    def _trace(turn_id: str, chat_id: str, generation: int, elapsed: float, call_id: str):
        return {
            "turn_id": turn_id,
            "trace_id": f"trace-{turn_id}",
            "chat_id": chat_id,
            "thread_id": f"thread-{chat_id}",
            "generation": generation,
            "started_at": 100.0 + elapsed,
            "trace_finalized_at": 100.0 + elapsed + elapsed / 1000.0,
            "turn_total_elapsed_ms": elapsed,
            "status": "completed",
            "reply_sent": True,
            "llm_call_ledger": [
                {
                    "call_id": call_id,
                    "status": "success",
                    "elapsed_ms": elapsed / 2.0,
                    "model_attempts": [
                        {"retry_index": 0, "status": "error", "fallback": False},
                        {"retry_index": 1, "status": "success", "fallback": True},
                    ],
                }
            ],
            "stage_ledger": [
                {"stage": "gateway.semaphore_wait", "status": "success", "elapsed_ms": 4.0},
                {"stage": "system2.chat_lock_wait", "status": "success", "elapsed_ms": 6.0},
                {"stage": "executor.chat_lock_wait", "status": "success", "elapsed_ms": 8.0},
                {"stage": "gateway.retry_backoff", "status": "success", "elapsed_ms": 2.0},
            ],
        }

    def test_build_diagnostics_uses_real_trace_producer_and_preserves_identity(self):
        runtime = self._runtime()
        traces = [
            self._trace("turn-a", "chat-a", 1, 100.0, "call-a"),
            self._trace("turn-b", "chat-b", 2, 300.0, "call-b"),
        ]
        runtime.cognition.system2_planner = SimpleNamespace(turn_trace_history=traces)

        schema = runtime.build_diagnostics()["runtime_status_schema"]

        self.assertIsNone(schema["turns"]["total_elapsed_ms"])
        self.assertEqual(schema["turns"]["total_elapsed_p95_ms"], 300.0)
        self.assertEqual({item["turn_id"] for item in schema["turns"]["records"]}, {"turn-a", "turn-b"})
        self.assertEqual({item["generation"] for item in schema["turns"]["records"]}, {1, 2})
        self.assertEqual(schema["provider"]["provider_request_count"], 2)
        self.assertEqual(schema["provider"]["retry_count"], 2)
        self.assertEqual(schema["provider"]["fallback_count"], 2)
        self.assertEqual(schema["provider"]["provider_latency_max_ms"], 150.0)
        self.assertEqual(schema["provider"]["retry_backoff_ms"], 2.0)
        self.assertEqual(schema["queues"]["gateway_semaphore_wait_ms"], 4.0)
        self.assertEqual(schema["queues"]["sys2_lock_wait_ms"], 6.0)
        self.assertEqual(schema["queues"]["executor_lock_wait_ms"], 8.0)
        self.assertIsNone(schema["queues"]["attention_queue_wait_ms"])

    def test_repeated_trace_snapshot_does_not_double_count_logical_calls(self):
        runtime = self._runtime()
        trace = self._trace("turn-a", "chat-a", 1, 100.0, "call-a")
        runtime.cognition.system2_planner = SimpleNamespace(
            turn_trace_history=[trace, dict(trace, llm_call_ledger=list(trace["llm_call_ledger"]))]
        )

        schema = runtime.build_diagnostics()["runtime_status_schema"]

        self.assertEqual(schema["provider"]["provider_request_count"], 1)
        self.assertEqual(schema["provider"]["provider_latency_max_ms"], 50.0)
        self.assertEqual(schema["turns"]["sample_size"], 1)

    def test_background_status_is_consumed_from_budget_producer_with_kind_scope(self):
        runtime = self._runtime()
        runtime.background_task_budget = SimpleNamespace(
            status=lambda: {
                "active": 2,
                "queued": 3,
                "queue_wait_ms_by_kind": {"attention.judge": {"p95_ms": 12.0}},
                "cancelled_by_kind": {"attention.judge": 2, "dream": 1},
                "late_completed_by_kind": {"dream": 4},
                "timed_out_by_kind": {"dream": 5},
                "rejected_by_kind": {"dream": 6},
            }
        )

        schema = runtime.build_diagnostics()["runtime_status_schema"]

        self.assertEqual(schema["background"]["active"], 2)
        self.assertEqual(schema["background"]["queued"], 3)
        self.assertEqual(schema["background"]["cancelled_count"], 3)
        self.assertEqual(schema["background"]["late_completed_count"], 4)
        self.assertEqual(schema["background"]["queue_timeout_count"], 5)
        self.assertEqual(schema["queues"]["background_budget_queue_wait_ms"], None)
        self.assertEqual(
            schema["queues"]["background_budget_queue_wait_ms_by_kind"]["attention.judge"]["p95_ms"],
            12.0,
        )

    def test_history_and_webui_keep_complete_schema_without_mutating_sources(self):
        runtime = self._runtime()
        router_state = {"before": True}

        class Router:
            def describe_status(self, *, read_only=False):
                self.read_only = read_only
                return {"queue_wait_ms": None, "router_state": router_state}

        router = Router()
        runtime.interaction.attention_gate = SimpleNamespace(decision_router=router, describe_status=lambda: {})
        runtime.cognition.system2_planner = SimpleNamespace(turn_trace_history=[])
        diagnostics = runtime.build_diagnostics()

        self.assertTrue(router.read_only)
        self.assertEqual(router_state, {"before": True})
        self.assertIn("runtime_status_schema", diagnostics["history"][0])
        self.assertEqual(
            diagnostics["history"][0]["runtime_status_schema"]["runtime_status_schema_version"],
            diagnostics["runtime_status_schema"]["runtime_status_schema_version"],
        )

        from astrmai.webui.backend.services.runtimeuiservice import RuntimeUiService

        class Facade:
            def get_runtime_diagnostics(self):
                return diagnostics

        class Api:
            def __init__(self):
                self.facade = Facade()

            async def get_runtime_diagnostics(self):
                return self.facade.get_runtime_diagnostics()

            def has_bound_facade(self):
                return True

        result = asyncio.run(RuntimeUiService(Api()).runtime_status_history())
        self.assertEqual(
            result["data"]["runtime_status_schema"]["runtime_status_schema_version"],
            1,
        )
        self.assertIn("runtime_status_schema", result["data"]["history"][0])


if __name__ == "__main__":
    unittest.main()
