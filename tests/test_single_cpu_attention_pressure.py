import asyncio
import tempfile
import unittest
from pathlib import Path

from tests.manual.single_cpu_attention_pressure import (
    PressureOptions,
    _heartbeat_summary,
    _percentile,
    run_pressure,
)


class SingleCpuAttentionPressureHarnessTests(unittest.TestCase):
    def test_percentile_and_heartbeat_summary_are_deterministic(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]

        self.assertEqual(_percentile(values, 50), 3.0)
        self.assertEqual(_percentile(values, 95), 4.8)
        self.assertEqual(
            _heartbeat_summary(values),
            {
                "samples": 5,
                "mean_ms": 3.0,
                "p50_ms": 3.0,
                "p95_ms": 4.8,
                "p99_ms": 4.96,
                "max_ms": 5.0,
            },
        )

    def test_short_fixture_uses_real_chain_and_recovers_after_reload(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            options = PressureOptions(
                duration_sec=0.8,
                groups=2,
                messages_per_group_per_sec=10.0,
                provider_delay_min_sec=0.03,
                provider_delay_max_sec=0.05,
                reply_every=1,
                pool_limit=10,
                background_concurrency=2,
                background_queue_limit=16,
                background_wait_timeout_sec=2.0,
                background_execution_timeout_sec=2.0,
                gateway_concurrency=2,
                gateway_wait_timeout_sec=1.0,
                attention_drain_timeout_sec=2.0,
                physical_drain_timeout_sec=2.0,
                sample_interval_sec=0.05,
                heartbeat_interval_sec=0.01,
                heartbeat_p95_limit_ms=100.0,
                heartbeat_p99_limit_ms=150.0,
                apply_cpu_affinity=False,
                report_path=str(Path(temp_dir) / "unused.json"),
            )

            report = asyncio.run(run_pressure(options))

        self.assertTrue(report["passed"], report["acceptance"])
        self.assertIn("AttentionGate.process_event", report["real_components"])
        self.assertIn("GlobalModelGateway", report["real_components"])
        self.assertGreater(report["provider"]["started"], 0)
        self.assertTrue(report["reload"]["provider_completed"])
        self.assertEqual(report["snapshots"]["final"]["background_budget"]["active"], 0)
        self.assertEqual(report["snapshots"]["final"]["background_budget"]["queued"], 0)


if __name__ == "__main__":
    unittest.main()
