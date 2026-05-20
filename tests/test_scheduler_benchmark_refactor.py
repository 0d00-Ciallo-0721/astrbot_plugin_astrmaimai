import importlib
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class SchedulerBenchmarkRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.conversation.loop.scheduler_benchmark", None)
        self.mod = importlib.import_module("astrmai.conversation.loop.scheduler_benchmark")
        self.mod = importlib.reload(self.mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_profile_matrix_covers_all_profiles_and_scenarios(self):
        matrix = self.mod.run_scheduler_profile_matrix_sync()

        self.assertEqual(
            matrix["profiles"],
            ["dialogue_first", "balanced", "maintenance_friendly"],
        )
        self.assertIn("hot_dialogue_pressure", matrix["scenarios"])
        self.assertIn("maintenance_backlog", matrix["scenarios"])
        self.assertIn("busy_executor_pressure", matrix["scenarios"])
        self.assertIn("retry_pressure_mix", matrix["scenarios"])
        self.assertIn("forced_promotion_pressure", matrix["scenarios"])

    def test_profiles_produce_distinct_selection_metrics(self):
        matrix = self.mod.run_scheduler_profile_matrix_sync(
            scenario_names=("maintenance_backlog", "busy_executor_pressure", "forced_promotion_pressure"),
        )
        maintenance_profiles = matrix["scenarios"]["maintenance_backlog"]["profiles"]
        busy_profiles = matrix["scenarios"]["busy_executor_pressure"]["profiles"]
        forced_profiles = matrix["scenarios"]["forced_promotion_pressure"]["profiles"]

        self.assertGreater(
            maintenance_profiles["maintenance_friendly"]["scheduler_batch_plan"]["maintenance_slots"],
            maintenance_profiles["balanced"]["scheduler_batch_plan"]["maintenance_slots"],
        )
        self.assertGreaterEqual(
            maintenance_profiles["balanced"]["scheduler_batch_plan"]["maintenance_slots"],
            maintenance_profiles["dialogue_first"]["scheduler_batch_plan"]["maintenance_slots"],
        )
        self.assertGreater(
            busy_profiles["maintenance_friendly"]["maintenance_selected_count"],
            busy_profiles["balanced"]["maintenance_selected_count"],
        )
        self.assertGreater(
            busy_profiles["balanced"]["quota_skip_counts"]["skipped_by_maintenance_quota"],
            busy_profiles["maintenance_friendly"]["quota_skip_counts"]["skipped_by_maintenance_quota"],
        )
        self.assertGreater(
            forced_profiles["balanced"]["forced_promotion_count"],
            forced_profiles["dialogue_first"]["forced_promotion_count"],
        )

    def test_artifact_writer_emits_matrix_assets(self):
        matrix = self.mod.run_scheduler_profile_matrix_sync(
            scenario_names=("hot_dialogue_pressure",),
        )
        meta = self.mod.build_scheduler_benchmark_meta(
            scenario_names=["hot_dialogue_pressure"],
            profile_names=matrix["profiles"],
            label="smoke",
        )
        output_root = Path(self.temp_dir.name) / "artifacts"
        run_dir = self.mod.write_scheduler_benchmark_artifacts(
            output_root=output_root,
            matrix=matrix,
            meta=meta,
            label="smoke",
            repo_root=Path(__file__).resolve().parents[1],
        )

        self.assertTrue((run_dir / "samples_meta.json").exists())
        self.assertTrue((run_dir / "matrix_results.json").exists())
        self.assertTrue((run_dir / "benchmark_summary.json").exists())
        markdown = (run_dir / "summary.md").read_text(encoding="utf-8")
        self.assertIn("Scheduler Profile Matrix Benchmark", markdown)
        self.assertIn("hot_dialogue_pressure", markdown)
