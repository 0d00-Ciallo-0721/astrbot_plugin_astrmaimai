from __future__ import annotations

import argparse
import json
import unittest

from tests.manual.run_provider_matrix import build_cases, run_matrix


class ProviderMatrixRunnerTests(unittest.TestCase):
    def test_build_cases_tracks_cumulative_budget(self):
        cases = build_cases(profiles="medium:4,long:8", levels=[1, 2, 4], calls_per_level=5, max_total_calls=30)
        self.assertEqual([item["requested_calls"] for item in cases], [15, 15])
        self.assertEqual(cases[-1]["cumulative_requested_calls"], 30)
        self.assertTrue(all(item["within_global_budget"] for item in cases))

    def test_dry_run_never_starts_calls(self):
        args = argparse.Namespace(
            profiles="medium:4,long:8",
            levels="1,2",
            calls_per_level=5,
            max_total_calls=20,
            timeout_sec=45.0,
            max_tokens=512,
            model="",
            output_dir="artifacts/live_validation",
            stream=False,
            json=False,
            tool_call=False,
            vision=False,
            execute=False,
        )
        payload = run_matrix(args)
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(payload["started_calls"], 0)

    def test_plan_exceeding_global_budget_is_blocked(self):
        args = argparse.Namespace(
            profiles="medium:4,long:8",
            levels="1,2,4",
            calls_per_level=5,
            max_total_calls=20,
            timeout_sec=45.0,
            max_tokens=512,
            model="",
            output_dir="artifacts/live_validation",
            stream=False,
            json=False,
            tool_call=False,
            vision=False,
            execute=False,
        )
        payload = run_matrix(args)
        self.assertEqual(payload["status"], "budget_plan_exceeded")
        self.assertEqual(payload["started_calls"], 0)

    def test_matrix_manifest_is_public_json(self):
        args = argparse.Namespace(
            profiles="short:1",
            levels="1",
            calls_per_level=1,
            max_total_calls=1,
            timeout_sec=1.0,
            max_tokens=32,
            model="model-id",
            output_dir="artifacts/live_validation",
            stream=False,
            json=False,
            tool_call=False,
            vision=False,
            execute=False,
        )
        payload = run_matrix(args)
        self.assertEqual(payload["cases"][0]["context_profile"], "short")
        self.assertNotIn("api_key", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
