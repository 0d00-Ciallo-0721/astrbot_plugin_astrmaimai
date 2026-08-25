from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.manual.astrmai_host_probe import (
    AstrMaiHostProbe,
    HostApiClient,
    HostProbeConfig,
    MEASUREMENT_SCOPE,
    run_host_probe,
)


class AstrMaiHostProbeTests(unittest.TestCase):
    def test_runtime_probe_without_host_is_explicitly_not_configured(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                payload = await run_host_probe(
                    scenarios=["main_reply_private"],
                    output_root=Path(temp_dir),
                    config=HostProbeConfig(),
                )
                self.assertEqual(payload["measurement_scope"], MEASUREMENT_SCOPE)
                self.assertEqual(payload["status"], "not_configured")
                scenario = payload["scenarios"][0]
                self.assertEqual(scenario["scenario_status"], "not_configured")
                self.assertIsNone(scenario["metrics"]["gateway_queue_wait_ms"])
                self.assertIn("event_adapter", scenario["measurement_missing"])
                run_dir = Path(temp_dir) / payload["run_id"]
                self.assertTrue((run_dir / "runtime_samples.jsonl").exists())
                self.assertTrue((run_dir / "turns.jsonl").exists())
                self.assertTrue((run_dir / "summary.json").exists())
                self.assertNotIn("Authorization", (run_dir / "run.json").read_text(encoding="utf-8"))

        asyncio.run(run())

    def test_host_runtime_status_preserves_diagnostics_and_classifies_http_error(self):
        async def run():
            config = HostProbeConfig(base_url="http://host.test", api_key="secret")
            client = HostApiClient(config)
            with patch.object(client, "request", new=AsyncMock(return_value={
                "status": "passed",
                "measurement_scope": MEASUREMENT_SCOPE,
                "body": {
                    "status": "degraded",
                    "data": {
                        "snapshot_at": 123,
                        "diagnostics_status": "degraded",
                        "component_errors": [{"component": "attention"}],
                        "infrastructure": {"gateway": {"api_timeout": 15}},
                        "long_turn": {"active": 2},
                    },
                },
            })):
                probe = AstrMaiHostProbe(config, output_root=Path(tempfile.mkdtemp()))
                probe.client = client
                sample = await probe.collect_runtime(reason="test")
                self.assertEqual(sample["snapshot"]["diagnostics_status"], "degraded")
                self.assertEqual(sample["snapshot"]["long_turn"]["active"], 2)

        asyncio.run(run())

    def test_configured_adapter_records_measured_values_only(self):
        async def adapter(payload):
            return {
                "status": "completed",
                "host_turn_id": f"turn-{payload['event_id']}",
                "trace_id": f"trace-{payload['event_id']}",
                "host_event_id": payload["event_id"],
                "host_chat_id": "private-probe",
                "host_event_type": "private_message",
                "injected_at": payload["sent_at"],
                "final_status": "completed",
                "turn_total_elapsed_ms": 12.5,
                "gateway_queue_wait_ms": 1.2,
                "turn_final_status": "completed",
                "retry_count": 0,
                "fallback": False,
            }

        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                config = HostProbeConfig(event_adapter="tests.test_astrmai_host_probe:adapter")
                with patch("tests.manual.astrmai_host_probe._load_adapter", return_value=adapter):
                    payload = await run_host_probe(
                        scenarios=["main_reply_private"],
                        repeat=2,
                        output_root=Path(temp_dir),
                        config=config,
                    )
                scenario = payload["scenarios"][0]
                self.assertEqual(scenario["scenario_status"], "passed")
                self.assertEqual(scenario["requests_finished"], 2)
                self.assertEqual(scenario["metrics"]["gateway_queue_wait_ms"], 1.2)
                self.assertEqual(scenario["metrics"]["turn_final_status"], "completed")
                run_dir = Path(temp_dir) / payload["run_id"]
                turns = [line for line in (run_dir / "turns.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
                self.assertEqual(len(turns), 2)

    def test_pressure_plan_expands_b01_into_concurrent_events(self):
        async def adapter(payload):
            return {
                "status": "completed",
                "host_turn_id": f"turn-{payload['event_id']}",
                "trace_id": f"trace-{payload['event_id']}",
                "host_event_id": payload["event_id"],
                "host_chat_id": payload["intent"].get("group_id", "group-probe"),
                "host_event_type": "group_message",
                "injected_at": payload["sent_at"],
                "final_status": "completed",
                "metrics": {
                    "gateway_queue_wait_ms": 1,
                    "semaphore_wait_ms": 1,
                    "lane_wait_ms": 1,
                    "sys2_lock_wait_ms": 1,
                    "executor_lock_wait_ms": 1,
                },
            }

        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                config = HostProbeConfig(event_adapter="configured")
                with patch("tests.manual.astrmai_host_probe._load_adapter", return_value=adapter):
                    payload = await run_host_probe(
                        scenarios=["multi_group_queue_b01"],
                        output_root=Path(temp_dir),
                        config=config,
                    )
                scenario = payload["scenarios"][0]
                self.assertEqual(scenario["scenario_status"], "passed")
                self.assertEqual(scenario["requests_started"], 24)
                self.assertEqual(scenario["requests_finished"], 24)
                self.assertEqual(scenario["turns_finalized"], 24)
                request_lines = (Path(temp_dir) / payload["run_id"] / "host_requests.jsonl").read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(request_lines), 24)

        asyncio.run(run())

    def test_mock_measurement_scope_is_explicitly_separate_from_host(self):
        async def adapter(payload):
            return {
                "status": "completed",
                "final_status": "completed",
                "host_turn_id": f"mock-turn-{payload['event_id']}",
                "trace_id": f"mock-trace-{payload['event_id']}",
                "host_event_id": payload["event_id"],
                "host_chat_id": "mock-chat",
                "host_event_type": "private_message",
                "injected_at": payload["sent_at"],
            }

        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                with patch("tests.manual.astrmai_host_probe._load_adapter", return_value=adapter):
                    payload = await run_host_probe(
                        scenarios=["main_reply_private"],
                        output_root=Path(temp_dir),
                        config=HostProbeConfig(event_adapter="configured"),
                        measurement_scope="offline_mock",
                    )
                self.assertEqual(payload["measurement_scope"], "offline_mock")
                run_dir = Path(temp_dir) / payload["run_id"]
                self.assertIn(
                    '"measurement_scope": "offline_mock"',
                    (run_dir / "run.json").read_text(encoding="utf-8"),
                )

        asyncio.run(run())

    def test_accepted_without_terminal_contract_is_measurement_incomplete(self):
        async def adapter(_payload):
            return {
                "status": "accepted",
                "host_turn_id": "turn-pending",
                "trace_id": "trace-pending",
                "host_event_id": "event-pending",
                "host_chat_id": "chat-pending",
                "host_event_type": "private_message",
                "injected_at": "2026-01-01T00:00:00+00:00",
            }

        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                config = HostProbeConfig(event_adapter="configured")
                with patch("tests.manual.astrmai_host_probe._load_adapter", return_value=adapter):
                    payload = await run_host_probe(
                        scenarios=["main_reply_private"], output_root=Path(temp_dir), config=config
                    )
                self.assertEqual(payload["status"], "degraded")
                self.assertEqual(payload["scenarios"][0]["scenario_status"], "measurement_incomplete")

        asyncio.run(run())

    def test_completed_turn_without_trace_is_measurement_incomplete(self):
        async def adapter(payload):
            return {
                "status": "completed",
                "final_status": "completed",
                "host_turn_id": "turn-1",
                "host_event_id": payload["event_id"],
                "host_chat_id": "chat-1",
                "host_event_type": "private_message",
                "injected_at": payload["sent_at"],
            }

        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                with patch("tests.manual.astrmai_host_probe._load_adapter", return_value=adapter):
                    payload = await run_host_probe(
                        scenarios=["main_reply_private"],
                        output_root=Path(temp_dir),
                        config=HostProbeConfig(event_adapter="configured"),
                    )
                scenario = payload["scenarios"][0]
                self.assertEqual(scenario["scenario_status"], "measurement_incomplete")
                self.assertEqual(scenario["failure_class"], "trace_id_missing")

        asyncio.run(run())

    def test_judge_scenario_requires_judge_evidence(self):
        async def adapter(payload):
            return {
                "status": "completed",
                "final_status": "completed",
                "host_turn_id": f"turn-{payload['event_id']}",
                "trace_id": f"trace-{payload['event_id']}",
                "host_event_id": payload["event_id"],
                "host_chat_id": "judge-chat",
                "host_event_type": "group_message",
                "injected_at": payload["sent_at"],
            }

        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                with patch("tests.manual.astrmai_host_probe._load_adapter", return_value=adapter):
                    payload = await run_host_probe(
                        scenarios=["judge_b05"],
                        output_root=Path(temp_dir),
                        config=HostProbeConfig(event_adapter="configured"),
                    )
                scenario = payload["scenarios"][0]
                self.assertEqual(scenario["scenario_status"], "measurement_incomplete")
                self.assertIn("scenario_evidence_missing", scenario["failure_class"])

        asyncio.run(run())

    def test_b05_b07_b08_positive_evidence_contracts_pass(self):
        evidence = {
            "judge_b05": {
                "judge_called": True,
                "judge_skipped": False,
                "filter_reason": "engaged",
                "expected_action": "reply",
                "actual_action": "reply",
            },
            "memory_b07": {
                "vector_status": "ready",
                "index_generation": 3,
                "faiss_latency_ms": 4.2,
                "fallback_source": "faiss",
                "outbox_pending_count": 0,
            },
            "background_b08": {
                "background_active": 2,
                "queue_wait_ms": 1.5,
                "execution_timeout": False,
                "late_completed": 0,
            },
        }

        async def adapter(payload):
            result = {
                "status": "completed",
                "final_status": "completed",
                "host_turn_id": f"turn-{payload['event_id']}",
                "trace_id": f"trace-{payload['event_id']}",
                "host_event_id": payload["event_id"],
                "host_chat_id": "evidence-chat",
                "host_event_type": "group_message",
                "injected_at": payload["sent_at"],
            }
            result.update(evidence[payload["scenario"]])
            return result

        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                with patch("tests.manual.astrmai_host_probe._load_adapter", return_value=adapter):
                    payload = await run_host_probe(
                        scenarios=list(evidence), output_root=Path(temp_dir), config=HostProbeConfig(event_adapter="configured")
                    )
                self.assertEqual(payload["status"], "passed")
                self.assertTrue(all(item["scenario_status"] == "passed" for item in payload["scenarios"]))

        asyncio.run(run())

    def test_b07_and_b08_missing_evidence_are_incomplete(self):
        async def adapter(payload):
            return {
                "status": "completed",
                "final_status": "completed",
                "host_turn_id": f"turn-{payload['event_id']}",
                "trace_id": f"trace-{payload['event_id']}",
                "host_event_id": payload["event_id"],
                "host_chat_id": "missing-evidence-chat",
                "host_event_type": "group_message",
                "injected_at": payload["sent_at"],
            }

        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                with patch("tests.manual.astrmai_host_probe._load_adapter", return_value=adapter):
                    payload = await run_host_probe(
                        scenarios=["memory_b07", "background_b08"],
                        output_root=Path(temp_dir), config=HostProbeConfig(event_adapter="configured")
                    )
                self.assertEqual(payload["status"], "degraded")
                self.assertTrue(all(item["scenario_status"] == "measurement_incomplete" for item in payload["scenarios"]))

        asyncio.run(run())

    def test_non_passed_terminal_scenario_blocks_overall_pass(self):
        async def adapter(payload):
            terminal = payload["intent"].get("terminal", "skipped")
            return {
                "status": terminal,
                "final_status": terminal,
                "host_turn_id": f"turn-{payload['event_id']}",
                "trace_id": f"trace-{payload['event_id']}",
                "host_event_id": payload["event_id"],
                "host_chat_id": "terminal-chat",
                "host_event_type": "private_message",
                "injected_at": payload["sent_at"],
            }

        async def run():
            for terminal in ("skipped", "timeout", "budget_exhausted", "degraded"):
                with tempfile.TemporaryDirectory() as temp_dir:
                    original = {"kind": "turn", "channel": "private", "text": "terminal", "terminal": terminal}
                    with patch.dict("tests.manual.astrmai_host_probe.SCENARIOS", {"main_reply_private": original}):
                        with patch("tests.manual.astrmai_host_probe._load_adapter", return_value=adapter):
                            payload = await run_host_probe(
                                scenarios=["main_reply_private"], output_root=Path(temp_dir), config=HostProbeConfig(event_adapter="configured")
                            )
                    self.assertNotEqual(payload["status"], "passed")

        asyncio.run(run())

    def test_python_adapter_timeout_is_enforced(self):
        def blocking_adapter(_payload):
            import time
            time.sleep(0.2)
            return {}

        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                with patch("tests.manual.astrmai_host_probe._load_adapter", return_value=blocking_adapter):
                    payload = await run_host_probe(
                        scenarios=["main_reply_private"], output_root=Path(temp_dir),
                        config=HostProbeConfig(event_adapter="configured", adapter_timeout_sec=0.02),
                    )
                self.assertNotEqual(payload["status"], "passed")
                self.assertEqual(payload["scenarios"][0]["scenario_status"], "measurement_incomplete")

        asyncio.run(run())

    def test_artifact_does_not_write_host_key(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                config = HostProbeConfig(api_key="super-secret")
                payload = await run_host_probe(
                    scenarios=[], output_root=Path(temp_dir), config=config
                )
                run_json = Path(temp_dir, payload["run_id"], "run.json").read_text(encoding="utf-8")
                self.assertNotIn("super-secret", run_json)
                self.assertIn("api_key_fingerprint", run_json)

        asyncio.run(run())

    def test_artifact_redacts_event_url_query_token(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                config = HostProbeConfig(event_url="http://host.test/events?token=super-secret")
                payload = await run_host_probe(scenarios=[], output_root=Path(temp_dir), config=config)
                run_json = Path(temp_dir, payload["run_id"], "run.json").read_text(encoding="utf-8")
                self.assertNotIn("super-secret", run_json)
                self.assertIn("[redacted]", run_json)

        asyncio.run(run())

    def test_artifacts_redact_path_and_nested_sensitive_values(self):
        async def adapter(payload):
            return {
                "status": "completed",
                "final_status": "completed",
                "host_turn_id": "turn-safe",
                "trace_id": "trace-safe",
                "host_event_id": payload["event_id"],
                "host_chat_id": "chat-safe",
                "host_event_type": "private_message",
                "injected_at": payload["sent_at"],
                "headers": {"Authorization": "Bearer auth-secret"},
                "cookies": {"session": "cookie-secret"},
                "nested": [{"secret": "nested-secret"}, {"text": "raw-user-text"}],
                "error": "Authorization: Bearer exception-secret",
            }

        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                config = HostProbeConfig(
                    event_adapter="configured",
                    event_url="http://host.test/events/token/path-secret?token=query-secret",
                )
                with patch("tests.manual.astrmai_host_probe._load_adapter", return_value=adapter):
                    payload = await run_host_probe(
                        scenarios=["main_reply_private"], output_root=Path(temp_dir), config=config
                    )
                run_dir = Path(temp_dir) / payload["run_id"]
                artifacts = "\n".join(
                    path.read_text(encoding="utf-8")
                    for path in run_dir.iterdir()
                    if path.suffix in {".json", ".jsonl", ".md"}
                )
                for secret in ("path-secret", "query-secret", "auth-secret", "cookie-secret", "nested-secret", "raw-user-text", "exception-secret"):
                    self.assertNotIn(secret, artifacts)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
