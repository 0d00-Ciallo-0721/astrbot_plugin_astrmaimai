from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers.live_test_budget import LiveBudgetExceeded, LiveCallBudget
from tests.helpers.live_test_config import load_live_llm_config
from tests.manual.aggregate_live_rounds import aggregate
from tests.manual.live_llm_probe import _error_class, _write_artifacts, run_probe


class LiveTestHarnessTests(unittest.TestCase):
    def test_live_config_uses_environment_paths_and_does_not_expose_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            host_path = root / "cmd_config.json"
            plugin_path = root / "plugin_config.json"
            host_path.write_text(
                json.dumps(
                    {
                        "provider_sources": [
                            {
                                "id": "opencode",
                                "provider": "openai",
                                "enable": True,
                                "api_base": "https://example.test/v1",
                                "key": ["file-secret"],
                            }
                        ],
                        "provider": [],
                    }
                ),
                encoding="utf-8",
            )
            plugin_path.write_text(
                json.dumps(
                    {
                        "provider": {
                            "task_models": ["openai/task"],
                            "agent_models": ["openai/agent"],
                            "fallback_models": ["openai/task", "openai/fallback"],
                        },
                        "infra": {
                            "api_timeout": 15,
                            "llm_retries": 2,
                            "max_concurrent_llm_calls": 3,
                        },
                        "timing": {"model_request_timeout_sec": 45},
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {
                    "ASTRMAI_HOST_CMD_CONFIG": str(host_path),
                    "ASTRMAI_PLUGIN_CONFIG": str(plugin_path),
                    "ASTRMAI_LIVE_API_KEY": "env-secret",
                },
                clear=False,
            ):
                config = load_live_llm_config()

            self.assertEqual(config.api_key, "env-secret")
            self.assertEqual(config.default_model, "openai/task")
            self.assertEqual(config.all_models, ["openai/task", "openai/agent", "openai/fallback"])
            summary = config.public_summary()
            self.assertEqual(summary["provider_id"], "opencode")
            self.assertEqual(summary["effective_gateway_timeout"], 15)
            self.assertEqual(summary["effective_model_request_timeout"], 45)
            self.assertEqual(summary["llm_retries"], 2)
            self.assertEqual(summary["max_concurrent_llm_calls"], 3)
            self.assertNotIn("env-secret", json.dumps(config.public_summary()))

    def test_live_config_reads_provider_key_from_separate_secrets_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            host_path = root / "cmd_config.json"
            plugin_path = root / "plugin_config.json"
            secrets_path = root / "secrets.json"
            host_path.write_text(json.dumps({
                "provider_sources": [{
                    "id": "opencode",
                    "provider": "openai",
                    "enable": True,
                    "api_base": "https://example.test/v1",
                    "key": [],
                }],
                "provider": [],
            }), encoding="utf-8")
            plugin_path.write_text(json.dumps({
                "provider": {"task_models": ["opencode/task"]},
                "infra": {},
                "timing": {},
            }), encoding="utf-8")
            secrets_path.write_text(json.dumps({
                "providers": {"opencode": {"api_key": "file-secret"}},
            }), encoding="utf-8")
            with patch.dict("os.environ", {
                "ASTRMAI_HOST_CMD_CONFIG": str(host_path),
                "ASTRMAI_PLUGIN_CONFIG": str(plugin_path),
                "ASTRMAI_LIVE_SECRETS_FILE": str(secrets_path),
                "ASTRMAI_LIVE_API_KEY": "",
            }, clear=False):
                config = load_live_llm_config()
            self.assertEqual(config.api_key, "file-secret")
            self.assertEqual(config.secrets_file, secrets_path)
            self.assertNotIn("file-secret", json.dumps(config.public_summary()))

    def test_live_config_does_not_treat_secret_template_as_a_real_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "cmd.json").write_text(json.dumps({
                "provider_sources": [{
                    "id": "opencode",
                    "provider": "openai",
                    "enable": True,
                    "api_base": "https://example.test/v1",
                    "key": [],
                }],
                "provider": [],
            }), encoding="utf-8")
            (root / "plugin.json").write_text(json.dumps({
                "provider": {"task_models": ["opencode/task"]},
            }), encoding="utf-8")
            (root / "secrets.json").write_text(json.dumps({
                "providers": {"opencode": {"api_key": "<opencode-api-key>"}},
            }), encoding="utf-8")
            with patch.dict("os.environ", {
                "ASTRMAI_HOST_CMD_CONFIG": str(root / "cmd.json"),
                "ASTRMAI_PLUGIN_CONFIG": str(root / "plugin.json"),
                "ASTRMAI_LIVE_SECRETS_FILE": str(root / "secrets.json"),
                "ASTRMAI_LIVE_API_KEY": "",
            }, clear=False):
                with self.assertRaisesRegex(RuntimeError, "missing api key"):
                    load_live_llm_config()

    def test_live_budget_enforces_total_calls_and_records_stats(self):
        async def run():
            budget = LiveCallBudget(max_calls=1, max_concurrency=1, timeout_sec=1.0)
            first = await budget.run("first", lambda: asyncio.sleep(0, result="ok"))
            self.assertEqual(first, "ok")
            with self.assertRaises(LiveBudgetExceeded):
                await budget.run("second", lambda: asyncio.sleep(0, result="no"))
            summary = budget.summary()
            self.assertEqual(summary["calls_started"], 1)
            self.assertEqual(summary["calls_completed"], 1)
            self.assertEqual(summary["calls_failed"], 0)

        asyncio.run(run())

    def test_live_budget_timeout_counts_failed_call(self):
        async def run():
            budget = LiveCallBudget(max_calls=1, max_concurrency=1, timeout_sec=0.01)
            with self.assertRaises(asyncio.TimeoutError):
                await budget.run("slow", lambda: asyncio.sleep(0.1))
            self.assertEqual(budget.summary()["calls_failed"], 1)

        asyncio.run(run())

    def test_probe_artifacts_split_run_calls_and_summary_without_secrets(self):
        payload = {
            "schema_version": "llm-live-v2",
            "run_id": "run_test",
            "scenario": "provider_probe",
            "status": "passed",
            "config": {"api_key_present": True, "provider_id": "opencode"},
            "levels": [
                {
                    "rows": [
                        {
                            "status": "passed",
                            "request_id": "req_test",
                            "run_id": "run_test",
                            "total_elapsed_ms": 12.0,
                            "http_status": 200,
                            "error_class": None,
                        }
                    ]
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir, summary_path = _write_artifacts(payload, Path(temp_dir))
            self.assertTrue((run_dir / "run.json").exists())
            self.assertTrue((run_dir / "calls.jsonl").exists())
            self.assertTrue(summary_path.exists())
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["total_requests"], 1)
            self.assertEqual(summary["success_rate"], 1.0)
            self.assertNotIn("Authorization", (run_dir / "run.json").read_text(encoding="utf-8"))

    def test_error_classification_distinguishes_provider_failures(self):
        self.assertEqual(_error_class({"status": "failed", "http_status": 401}), "auth_error")
        self.assertEqual(_error_class({"status": "failed", "http_status": 403, "error": "region blocked"}), "region_error")
        self.assertEqual(_error_class({"status": "failed", "http_status": 403, "error": "permission denied"}), "permission_error")
        self.assertEqual(_error_class({"status": "failed", "http_status": 404, "error": "model not found"}), "provider_not_found")
        self.assertEqual(_error_class({"status": "failed", "http_status": 429}), "rate_limited")
        self.assertEqual(_error_class({"status": "failed", "http_status": 200, "response_nonempty": False}), "empty_response")
        self.assertEqual(_error_class({"status": "failed", "http_status": 200, "finish_reason": "length", "response_nonempty": False}), "truncated_response")
        self.assertEqual(_error_class({"status": "failed", "http_status": None, "error": "TimeoutError: timed out"}), "read_timeout")
        self.assertEqual(_error_class({"status": "cancelled", "cancelled": True}), "cancelled")

    def test_summary_excludes_budget_rejected_from_latency_and_marks_unmeasured(self):
        payload = {
            "schema_version": "llm-live-v2",
            "run_id": "run_stats",
            "scenario": "provider_probe",
            "status": "failed",
            "config": {},
            "levels": [{"rows": [
                {"status": "passed", "total_elapsed_ms": 100.0, "error_class": None},
                {"status": "budget_rejected", "total_elapsed_ms": 0.0, "error_class": "budget_exhausted"},
            ]}],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            _, summary_path = _write_artifacts(payload, Path(temp_dir))
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["started_provider_requests"], 1)
            self.assertEqual(summary["rejected_before_start"], 1)
            self.assertEqual(summary["p50_ms"], 100.0)
            self.assertIsNone(summary["retry_rate"])
            self.assertEqual(summary["retry_measurement"], "not_measured")

    def test_live_config_rejects_default_and_model_provider_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "cmd.json").write_text(json.dumps({
                "provider_sources": [
                    {"id": "source_a", "provider": "openai", "enable": True, "api_base": "https://a.test/v1", "key": ["a"]},
                    {"id": "source_b", "provider": "openai", "enable": True, "api_base": "https://b.test/v1", "key": ["b"]},
                ],
                "provider_settings": {"default_provider_id": "source_b/task"},
                "provider": [{"id": "source_a/task", "model": "task", "provider_source_id": "source_a", "enable": True}],
            }), encoding="utf-8")
            (root / "plugin.json").write_text(json.dumps({
                "provider": {"task_models": ["source_a/task"]},
                "infra": {"api_timeout": 15},
                "timing": {"model_request_timeout_sec": 45},
            }), encoding="utf-8")
            with patch.dict("os.environ", {
                "ASTRMAI_HOST_CMD_CONFIG": str(root / "cmd.json"),
                "ASTRMAI_PLUGIN_CONFIG": str(root / "plugin.json"),
                "ASTRMAI_LIVE_API_KEY": "test-key",
            }, clear=False):
                config = load_live_llm_config()
            self.assertEqual(config.configuration_status, "configuration_mismatch")
            self.assertTrue(any("default_provider_source_mismatch" in item for item in config.configuration_errors))

    def test_live_config_excludes_disabled_provider_models(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "cmd.json").write_text(json.dumps({
                "provider_sources": [{"id": "opencode", "provider": "openai", "enable": True, "api_base": "https://example.test/v1", "key": ["key"]}],
                "provider_settings": {"default_provider_id": "opencode/enabled"},
                "provider": [
                    {"id": "opencode/enabled", "model": "enabled", "provider_source_id": "opencode", "enable": True},
                    {"id": "opencode/disabled", "model": "disabled", "provider_source_id": "opencode", "enable": False},
                ],
            }), encoding="utf-8")
            (root / "plugin.json").write_text(json.dumps({
                "provider": {"task_models": ["opencode/disabled"]},
            }), encoding="utf-8")
            with patch.dict("os.environ", {
                "ASTRMAI_HOST_CMD_CONFIG": str(root / "cmd.json"),
                "ASTRMAI_PLUGIN_CONFIG": str(root / "plugin.json"),
                "ASTRMAI_LIVE_API_KEY": "test-key",
            }, clear=False):
                config = load_live_llm_config()
            self.assertEqual(config.configuration_status, "configuration_mismatch")
            self.assertTrue(any("model_not_configured" in item for item in config.configuration_errors))

    def test_run_probe_mock_populates_request_measurement_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "cmd.json").write_text(json.dumps({
                "provider_sources": [{"id": "opencode", "provider": "openai", "enable": True, "api_base": "https://example.test/v1", "key": ["file-key"]}],
                "provider": [],
            }), encoding="utf-8")
            (root / "plugin.json").write_text(json.dumps({
                "provider": {"task_models": ["opencode/task"]},
                "infra": {"api_timeout": 15, "llm_retries": 2, "max_concurrent_llm_calls": 3},
                "timing": {"model_request_timeout_sec": 45},
            }), encoding="utf-8")
            with patch.dict("os.environ", {
                "ASTRMAI_HOST_CMD_CONFIG": str(root / "cmd.json"),
                "ASTRMAI_PLUGIN_CONFIG": str(root / "plugin.json"),
                "ASTRMAI_LIVE_API_KEY": "test-key",
            }, clear=False):
                config = load_live_llm_config()
            with patch("tests.manual.live_llm_probe._post_chat", return_value={
                "status": "passed", "model_id": "opencode/task", "request_model": "task",
                "response_model": "task", "response_nonempty": True, "http_status": 200,
                "content_preview": "alive", "content_chars": 5, "reasoning_preview": "",
                "reasoning_chars": 0, "finish_reason": "stop", "duration_sec": 0.01,
            }):
                payload = asyncio.run(
                    run_probe(
                        config=config,
                        model="opencode/task",
                        levels=[1],
                        calls_per_level=1,
                        max_calls=1,
                        timeout_sec=1.0,
                        context_profile="medium",
                        conversation_rounds=3,
                    )
                )
            row = payload["levels"][0]["rows"][0]
            for field in ("request_id", "run_id", "sequence", "model_id", "request_model", "started_at", "completed_at", "status", "error_class", "measurement_scope"):
                self.assertIn(field, row)
            self.assertEqual(row["measurement_scope"], "provider_probe")
            self.assertIsNone(row["retry_count"])
            self.assertIsNone(row["fallback"])
            self.assertEqual(row["context_profile"], "medium")
            self.assertEqual(row["context_chars_requested"], 2048)
            self.assertEqual(row["conversation_rounds"], 3)
            self.assertGreater(row["prompt_chars"], 2048)

    def test_round_aggregate_keeps_only_public_summary_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider_dir = root / "provider_run"
            provider_dir.mkdir()
            (provider_dir / "summary.json").write_text(json.dumps({
                "run_id": "provider_run",
                "measurement_scope": "provider_probe",
                "status": "passed",
                "requests_started": 2,
                "success_count": 2,
                "failure_count": 0,
                "p50_ms": 10,
                "secret": "must-not-be-copied",
            }), encoding="utf-8")
            host_dir = root / "mock_run"
            host_dir.mkdir()
            (host_dir / "summary.json").write_text(json.dumps({
                "run_id": "mock_run",
                "measurement_scope": "offline_mock",
                "status": "passed",
                "scenario_count": 1,
                "passed_count": 1,
                "turn_count": 1,
            }), encoding="utf-8")
            payload = aggregate(root)
            self.assertEqual(payload["round_count"], 2)
            self.assertEqual(payload["scopes"]["provider_probe"]["requests_started"], 2)
            self.assertEqual(payload["scopes"]["offline_mock"]["turn_count"], 1)
            self.assertNotIn("must-not-be-copied", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
