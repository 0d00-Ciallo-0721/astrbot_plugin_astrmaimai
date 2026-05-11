from pathlib import Path
import unittest


class BootstrapP102RefactorTests(unittest.TestCase):
    def test_bootstrap_uses_local_workmode_multimodal_and_proactive(self):
        path = Path(__file__).resolve().parents[1] / "astrmai" / "app" / "bootstrap.py"
        content = path.read_text(encoding="utf-8")
        self.assertIn("from ..proactive import ProactiveTask", content)
        self.assertIn("from ..workmode import CronHeartbeatGuard, Sys3Router", content)
        self.assertIn("runtime.feature_flags.work_mode_enabled", content)
        self.assertIn("runtime.feature_flags.proactive_enabled", content)
        self.assertNotIn("from astrmai.work.router import Sys3Router", content)
        self.assertNotIn("from astrmai.work.cron_guard.heartbeat import CronHeartbeatGuard", content)
        self.assertNotIn("from astrmai.evolution.proactive_task import ProactiveTask", content)


if __name__ == "__main__":
    unittest.main()
