import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class PromptMetricsCompareRefactorTests(unittest.TestCase):
    def setUp(self):
        self.mod = importlib.import_module("tests.manual.prompt_metrics_compare")
        self.mod = importlib.reload(self.mod)

    def test_summarize_trace_rows_tracks_length_and_hash_stability(self):
        rows = [
            {
                "case_id": "a",
                "status": "ok",
                "continuity": {
                    "system_prompt_length": 100,
                    "prompt_length": 140,
                    "dynamic_prompt_length": 20,
                    "frozen_prefix_length": 90,
                    "semi_stable_length": 30,
                    "semantic_system_hash": "sem-1",
                    "stable_prefix_hash": "hash-1",
                    "prefix_hash": "native-1",
                    "prefix_stable": True,
                    "prefix_changed_reason": "unknown",
                    "frozen_prefix_blocks": {"stable_state": 50, "persona_core": 30, "style_block": 5, "system_rules": 5},
                    "semi_stable_blocks": {"stable_expression": 30},
                    "dynamic_prompt_blocks": {"cognitive_drive": 20, "soft_background": 10},
                    "system_rules_items": [{"key": "current_message_first", "length": 20, "default_target": "candidate_for_runtime_instruction"}],
                    "system_rules_candidate_items": ["current_message_first"],
                },
            },
            {
                "case_id": "b",
                "status": "ok",
                "continuity": {
                    "system_prompt_length": 110,
                    "prompt_length": 170,
                    "dynamic_prompt_length": 40,
                    "frozen_prefix_length": 90,
                    "semi_stable_length": 30,
                    "semantic_system_hash": "sem-1",
                    "stable_prefix_hash": "hash-1",
                    "prefix_hash": "native-1",
                    "prefix_stable": True,
                    "prefix_changed_reason": "",
                    "frozen_prefix_blocks": {"stable_state": 50, "persona_core": 30, "style_block": 5, "system_rules": 5},
                    "semi_stable_blocks": {"stable_expression": 30},
                    "dynamic_prompt_blocks": {"cognitive_drive": 40, "soft_background": 20},
                    "system_rules_items": [{"key": "current_message_first", "length": 20, "default_target": "candidate_for_runtime_instruction"}],
                    "system_rules_candidate_items": ["current_message_first"],
                },
            },
            {
                "case_id": "c",
                "status": "failed",
                "continuity": {
                    "system_prompt_length": 140,
                    "prompt_length": 210,
                    "dynamic_prompt_length": 80,
                    "frozen_prefix_length": 95,
                    "semi_stable_length": 35,
                    "semantic_system_hash": "sem-2",
                    "stable_prefix_hash": "hash-2",
                    "prefix_hash": "native-2",
                    "prefix_stable": False,
                    "prefix_changed_reason": "frozen_rules_or_persona_changed",
                    "frozen_prefix_blocks": {"stable_behavior_rules": 55, "cold_summary": 40, "persona_core": 10, "style_block": 5, "system_rules": 5},
                    "semi_stable_blocks": {"stable_jargon": 35},
                    "dynamic_prompt_blocks": {"cognitive_drive": 40, "soft_background": 35, "planner_runtime_instruction": 40},
                    "system_rules_items": [{"key": "visible_reply_only", "length": 22, "default_target": "keep_in_system"}],
                    "system_rules_candidate_items": [],
                },
            },
        ]

        summary = self.mod.summarize_trace_rows(rows)

        self.assertEqual(summary["sample_count"], 3)
        self.assertEqual(summary["validation_failure_count"], 1)
        self.assertEqual(summary["system_prompt_length"]["median"], 110.0)
        self.assertEqual(summary["dynamic_prompt_length"]["p95"], 40)
        self.assertEqual(summary["semi_stable_length"]["median"], 30.0)
        self.assertEqual(summary["stable_prefix_hash"]["unique_count"], 2)
        self.assertEqual(summary["stable_prefix_hash"]["pairwise_stability_rate"], 0.5)
        self.assertEqual(summary["semantic_system_hash"]["unique_count"], 2)
        self.assertEqual(summary["semantic_system_hash"]["pairwise_stability_rate"], 0.5)
        self.assertEqual(summary["native_prefix_stable_rate"], 0.6667)
        self.assertEqual(summary["prefix_changed_reasons"], {"frozen_rules_or_persona_changed": 1, "unavailable_in_trace": 2})
        self.assertEqual(summary["remaining_system_composition"]["frozen_prefix_blocks"]["persona_or_identity"], 100)
        self.assertEqual(summary["remaining_prompt_background_composition"]["soft_background_blocks"]["stable_expression"], 60)
        self.assertEqual(summary["block_analysis_modes"]["unknown"], 3)
        self.assertEqual(summary["system_rules_candidate_frequency"]["current_message_first"], 2)
        self.assertEqual(summary["system_rules_keep_frequency"]["visible_reply_only"], 1)

    def test_build_report_compares_aligned_cases_and_deltas(self):
        baseline_rows = [
            {
                "case_id": "case-1",
                "status": "ok",
                "continuity": {
                    "system_prompt_length": 180,
                    "prompt_length": 220,
                    "dynamic_prompt_length": 10,
                    "frozen_prefix_length": 180,
                    "semi_stable_length": 40,
                    "semantic_system_hash": "sem-a",
                    "stable_prefix_hash": "stable-a",
                    "prefix_hash": "native-a",
                    "prefix_stable": True,
                    "prefix_changed_reason": "unsupported_in_baseline",
                    "frozen_prefix_blocks": {"stable_behavior_rules": 120, "cold_summary": 60, "persona_core": 10, "style_block": 5, "system_rules": 5},
                    "semi_stable_blocks": {"stable_expression": 40},
                    "system_rules_items": [{"key": "visible_reply_only", "length": 22, "default_target": "keep_in_system"}],
                    "system_rules_candidate_items": [],
                },
            }
        ]
        current_rows = [
            {
                "case_id": "case-1",
                "status": "ok",
                "continuity": {
                    "system_prompt_length": 120,
                    "prompt_length": 230,
                    "dynamic_prompt_length": 60,
                    "frozen_prefix_length": 120,
                    "semi_stable_length": 30,
                    "semantic_system_hash": "sem-a",
                    "stable_prefix_hash": "stable-a",
                    "prefix_hash": "native-a",
                    "prefix_stable": True,
                    "prefix_changed_reason": "unavailable_in_trace",
                    "frozen_prefix_blocks": {"stable_behavior_rules": 80, "cold_summary": 40, "persona_core": 30, "style_block": 20, "system_rules": 20},
                    "semi_stable_blocks": {"stable_expression": 30},
                    "dynamic_prompt_blocks": {"soft_background": 40, "planner_runtime_instruction": 60},
                    "system_rules_items": [{"key": "current_message_first", "length": 20, "default_target": "candidate_for_runtime_instruction"}],
                    "system_rules_candidate_items": ["current_message_first"],
                },
            },
            {
                "case_id": "tool_intent",
                "status": "failed",
                "continuity": {
                    "system_prompt_length": 100,
                    "prompt_length": 180,
                    "dynamic_prompt_length": 70,
                    "frozen_prefix_length": 100,
                    "semi_stable_length": 20,
                    "semantic_system_hash": "sem-b",
                    "stable_prefix_hash": "stable-b",
                    "prefix_hash": "native-b",
                    "prefix_stable": False,
                    "prefix_changed_reason": "frozen_rules_or_persona_changed",
                    "frozen_prefix_blocks": {"stable_behavior_rules": 70, "cold_summary": 30, "persona_core": 50, "style_block": 20, "system_rules": 10},
                    "semi_stable_blocks": {"stable_jargon": 20},
                    "dynamic_prompt_blocks": {"soft_background": 30, "planner_runtime_instruction": 70},
                    "system_rules_items": [{"key": "current_message_first", "length": 20, "default_target": "candidate_for_runtime_instruction"}],
                    "system_rules_candidate_items": ["current_message_first"],
                },
            },
        ]

        report = self.mod.build_report(
            baseline_label="before",
            current_label="after",
            baseline_rows=baseline_rows,
            current_rows=current_rows,
            baseline_benchmark={"overview": {"avg_stable_prefix_length": 180, "avg_dynamic_payload_length": 10}},
            current_benchmark={"overview": {"avg_stable_prefix_length": 120, "avg_dynamic_payload_length": 60}},
            baseline_root=self.mod.REPO_ROOT,
            current_root=self.mod.REPO_ROOT,
        )

        self.assertEqual(report["comparison"]["aligned_case_count"], 2)
        self.assertEqual(len(report["comparison"]["status_mismatches"]), 1)
        self.assertEqual(report["delta"]["system_prompt_length_mean"], -70.0)
        self.assertEqual(report["delta"]["dynamic_prompt_length_mean"], 55.0)
        self.assertIn("semantic_system_hash_pairwise_rate", report["delta"])
        self.assertEqual(report["comparison"]["migration_priority_rows"][0]["case_id"], "tool_intent")
        self.assertEqual(report["comparison"]["migration_priority_rows"][0]["recommended_migration_target"], "planner_runtime_instruction")
        self.assertEqual(report["comparison"]["migration_priority_rows"][0]["largest_migratable_block"], "stable_behavior_rules")
        self.assertEqual(report["comparison"]["migration_candidate_frequency"]["stable_behavior_rules"], 1)
        self.assertIn("baseline_vs_current_block_delta", report)
        self.assertIn("system_rules", report["baseline_vs_current_block_delta"])
        self.assertEqual(report["baseline_vs_current_block_delta"]["system_rules"]["delta_mode"], "comparable")
        self.assertIn("legacy_cold_summary_changed_case_ids", report["diagnostics"])
        self.assertTrue(report["diagnostics"]["system_rules_comparable"])
        self.assertEqual(report["next_real_migration_candidate"], "current_message_first")
        self.assertIn("current_message_first", report["system_rules_migration_candidates"])
        self.assertEqual(report["system_rules_keep_items"], {})
        markdown = self.mod.render_report_markdown(report)
        self.assertIn("Prompt Metrics Before/After Report", markdown)
        self.assertIn("Remaining System Composition", markdown)
        self.assertIn("Soft Background Blocks", markdown)
        self.assertIn("Semantic System Diagnostics", markdown)
        self.assertIn("provider_visible_system_hash", markdown)
        self.assertIn("prefix_hash", markdown)
        self.assertIn("System Rules Breakdown", markdown)
        self.assertIn("Next Real Migration Candidate", markdown)
        self.assertIn("High-Dynamic Case Priority", markdown)
        self.assertIn("Stable Global Candidate", markdown)
        self.assertIn("Status Mismatches", markdown)
        self.assertIn("Validation Verdict", markdown)
        self.assertIn("Session Reuse Validation Deferred", markdown)
        self.assertIn("continuity/native prefix compatibility signal", markdown)

    def test_baseline_fallback_parser_marks_simple_system_prompt_as_parsed_system_rules(self):
        blocks, mode = self.mod._HARNESS_CODE, None
        parsed_blocks, parsed_mode = self.mod._run_harness, None  # keep lints quiet
        parsed_blocks, parsed_mode = self.mod._fallback_parse_baseline_blocks(
            "You are AstrMai. Only output the visible chat reply.",
            baseline_mode=True,
        )
        self.assertEqual(parsed_mode, "fallback_parsed")
        self.assertIn("system_rules", parsed_blocks)

    def test_baseline_fallback_parser_marks_unknown_shape_as_not_comparable(self):
        parsed_blocks, parsed_mode = self.mod._fallback_parse_baseline_blocks(
            "opaque system prompt without known anchors",
            baseline_mode=True,
        )
        self.assertEqual(parsed_mode, "not_comparable")
        self.assertEqual(parsed_blocks["system_rules"], self.mod.BASELINE_BLOCK_SENTINEL)

    def test_harness_collects_non_empty_prefix_meta_for_current_repo(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            output_path = Path(temp_dir) / "trace.json"
            result = subprocess.run(
                [sys.executable, "-c", self.mod._HARNESS_CODE, str(output_path)],
                cwd=str(self.mod.REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        rows = list(payload.get("rows", []) or [])
        self.assertTrue(rows)
        self.assertTrue(
            any(
                dict((row.get("continuity") or {}).get("frozen_prefix_blocks", {}) or {})
                or dict((row.get("continuity") or {}).get("semi_stable_blocks", {}) or {})
                for row in rows
            )
        )
        reasons = {
            str((row.get("continuity") or {}).get("prefix_changed_reason", "") or "")
            for row in rows
        }
        self.assertNotEqual(reasons, {"unavailable_in_trace"})


if __name__ == "__main__":
    unittest.main()
