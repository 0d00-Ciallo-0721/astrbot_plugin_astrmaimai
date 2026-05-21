import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers.pre_release_validation import (
    _categorize_text_probe,
    write_pre_release_validation_artifacts,
)


class PreReleaseValidationRefactorTests(unittest.TestCase):
    def test_probe_categorization_prefers_clean_alive_and_rejects_reasoning_only(self):
        self.assertEqual(
            _categorize_text_probe({"status": "passed", "content": "alive", "reasoning_content": ""}),
            "recommended",
        )
        self.assertEqual(
            _categorize_text_probe({"status": "passed", "content": "alive, here you go", "reasoning_content": ""}),
            "backup",
        )
        self.assertEqual(
            _categorize_text_probe({"status": "passed", "content": "", "reasoning_content": "thinking..."}),
            "not_recommended",
        )
        self.assertEqual(
            _categorize_text_probe({"status": "failed", "content": "", "reasoning_content": ""}),
            "not_recommended",
        )

    def test_write_pre_release_validation_artifacts_emits_json_and_markdown(self):
        payload = {
            "generated_at": "2026-05-21T10:00:00",
            "overall_status": "passed",
            "release_ready": True,
            "static_gate": {"status": "passed", "compiled_count": 3},
            "local_regression": {"groups": [{"name": "g1", "status": "passed", "module_count": 2, "duration_seconds": 0.2}]},
            "real_provider_core": {"mood_live_status": "passed", "host_ingress_matched": True, "host_post_send_matched": True},
            "provider_matrix": {"recommended_models": ["openai/kimi-k2.5"], "backup_models": ["openai/kimi-k2.6"], "not_recommended_models": ["openai/mimo-v2.5"]},
            "plugin_model_pool": {"status": "passed", "distinct_fallback_available": False},
            "fallback_validation": {"status": "passed", "mode": "synthetic_distinct_fallback_probe"},
            "scheduler_admin_smoke": {"status": "passed", "pending_review_count": 1, "page_acceptance_artifacts": ["20260519T230500Z-direct-open-admin-full"]},
            "business_smoke": {"cases": [{"name": "group_at_bot_normal_qa", "status": "passed", "evidence": "host_mood_chain_audit"}]},
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            paths = write_pre_release_validation_artifacts(temp_dir, payload)
            json_path = Path(paths["json_path"])
            md_path = Path(paths["markdown_path"])
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = md_path.read_text(encoding="utf-8")
            self.assertEqual(loaded["overall_status"], "passed")
            self.assertIn("# Pre-release Full Test Report", markdown)
            self.assertIn("Real Provider Core Chain", markdown)
            self.assertIn("openai/kimi-k2.5", markdown)
            self.assertIn("真实 provider", markdown)
            self.assertIn("浏览器点击流本轮复用了已存在的宿主页/直开页验收产物", markdown)


if __name__ == "__main__":
    unittest.main()
