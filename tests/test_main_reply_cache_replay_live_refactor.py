import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path


class MainReplyCacheReplayLiveTests(unittest.TestCase):
    def setUp(self):
        self.mod = importlib.import_module("tests.manual.main_reply_cache_replay_live")
        self.mod = importlib.reload(self.mod)

    def test_run_live_requires_api_key(self):
        original = os.environ.pop("MAIN_REPLY_LIVE_API_KEY", None)
        original_family = os.environ.get("MAIN_REPLY_LIVE_PROVIDER_FAMILY")
        original_argv = list(sys.argv)
        try:
            os.environ["MAIN_REPLY_LIVE_PROVIDER_FAMILY"] = "anthropic"
            sys.argv = ["main_reply_cache_replay_live.py"]
            result = self.mod.main()
        finally:
            sys.argv = original_argv
            if original is not None:
                os.environ["MAIN_REPLY_LIVE_API_KEY"] = original
            if original_family is None:
                os.environ.pop("MAIN_REPLY_LIVE_PROVIDER_FAMILY", None)
            else:
                os.environ["MAIN_REPLY_LIVE_PROVIDER_FAMILY"] = original_family
        self.assertEqual(result, 0)

    def test_run_live_native_chat_requires_base_url(self):
        original_key = os.environ.get("MAIN_REPLY_LIVE_API_KEY")
        original_family = os.environ.get("MAIN_REPLY_LIVE_PROVIDER_FAMILY")
        original_base = os.environ.pop("MAIN_REPLY_LIVE_BASE_URL", None)
        original_argv = list(sys.argv)
        try:
            os.environ["MAIN_REPLY_LIVE_PROVIDER_FAMILY"] = "native_chat"
            sys.argv = ["main_reply_cache_replay_live.py"]
            result = self.mod.main()
        finally:
            sys.argv = original_argv
            if original_key is None:
                os.environ.pop("MAIN_REPLY_LIVE_API_KEY", None)
            else:
                os.environ["MAIN_REPLY_LIVE_API_KEY"] = original_key
            if original_family is None:
                os.environ.pop("MAIN_REPLY_LIVE_PROVIDER_FAMILY", None)
            else:
                os.environ["MAIN_REPLY_LIVE_PROVIDER_FAMILY"] = original_family
            if original_base is not None:
                os.environ["MAIN_REPLY_LIVE_BASE_URL"] = original_base
        self.assertEqual(result, 0)

    def test_run_live_without_api_key_writes_dry_run_provider_artifacts(self):
        original_key = os.environ.pop("MAIN_REPLY_LIVE_API_KEY", None)
        original_family = os.environ.get("MAIN_REPLY_LIVE_PROVIDER_FAMILY")
        original_argv = list(sys.argv)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            try:
                os.environ["MAIN_REPLY_LIVE_PROVIDER_FAMILY"] = "native_chat"
                sys.argv = ["main_reply_cache_replay_live.py", "--output-dir", temp_dir]
                result = self.mod.main()
            finally:
                sys.argv = original_argv
                if original_key is not None:
                    os.environ["MAIN_REPLY_LIVE_API_KEY"] = original_key
                if original_family is None:
                    os.environ.pop("MAIN_REPLY_LIVE_PROVIDER_FAMILY", None)
                else:
                    os.environ["MAIN_REPLY_LIVE_PROVIDER_FAMILY"] = original_family

            self.assertEqual(result, 0)
            provider_dir = Path(temp_dir) / "native_chat"
            self.assertTrue((provider_dir / "summary.json").exists())
            self.assertTrue((provider_dir / "summary.md").exists())
            self.assertTrue((provider_dir / "samples.jsonl").exists())
            summary = (provider_dir / "summary.json").read_text(encoding="utf-8")
            self.assertIn('"dry_run": true', summary)
            self.assertIn('"validation_verdict": "dry_run_capability_only"', summary)
            self.assertIn('"base_url_required": true', summary)
            self.assertIn('"request_execution_possible": false', summary)
            self.assertIn('"blocking_reason": "missing_api_key"', summary)

    def test_write_summary_includes_cache_and_hook_diagnostics(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            output_dir = Path(temp_dir)
            rows = [
                {
                    "case_id": "same_chat_turn_1",
                    "chat_id": "chat-1",
                    "cache_ready": True,
                    "cache_ready_reasons": ["explicit_cache_hint", "session_reuse"],
                    "cache_hit": False,
                    "cache_hit_evidence_supported": True,
                    "cache_hit_evidence_unavailable": False,
                    "cache_hint_observed_enabled": True,
                    "semantic_hash_stable_vs_previous": False,
                    "hook_changed_system": True,
                    "hash_stable_vs_previous": False,
                    "continuity": {
                        "semantic_system_hash": "sem1",
                        "semantic_system_length": 180,
                        "usage_input_cached": 0,
                        "gateway_system_hash": "g1",
                        "gateway_prompt_hash": "gp1",
                        "request_cache_control": '{"type":"ephemeral"}',
                        "request_session_id": "session-1",
                        "provider_visible_system_hash": "pv1",
                        "post_hook_system_hash": "ph1",
                        "provider_visible_prompt_hash": "pp1",
                        "prefix_hash": "pref1",
                        "frozen_prefix_length": 100,
                        "semi_stable_length": 10,
                        "dynamic_prompt_length": 20,
                    },
                },
                {
                    "case_id": "same_chat_turn_2",
                    "chat_id": "chat-1",
                    "cache_ready": True,
                    "cache_ready_reasons": ["explicit_cache_hint", "session_reuse", "semantic_system_hash_stable"],
                    "cache_hit": False,
                    "cache_hit_evidence_supported": True,
                    "cache_hit_evidence_unavailable": False,
                    "cache_hint_observed_enabled": True,
                    "semantic_hash_stable_vs_previous": True,
                    "hook_changed_system": False,
                    "hash_stable_vs_previous": True,
                    "continuity": {
                        "semantic_system_hash": "sem1",
                        "semantic_system_length": 180,
                        "usage_input_cached": 0,
                        "gateway_system_hash": "g1",
                        "gateway_prompt_hash": "gp2",
                        "request_cache_control": '{"type":"ephemeral"}',
                        "request_session_id": "session-1",
                        "provider_visible_system_hash": "pv1",
                        "post_hook_system_hash": "pv1",
                        "provider_visible_prompt_hash": "pp2",
                        "prefix_hash": "pref1",
                        "frozen_prefix_length": 100,
                        "semi_stable_length": 10,
                        "dynamic_prompt_length": 25,
                    },
                },
            ]
            self.mod._write_summary(
                output_dir,
                rows,
                "anthropic",
                "claude-3-5-sonnet",
                provider_supports_cache_hint=True,
                provider_supports_usage_reporting=True,
                provider_supports_session_id=False,
            )
            summary = (output_dir / "summary.json").read_text(encoding="utf-8")
            self.assertIn('"cache_ready_count": 2', summary)
            self.assertIn('"cache_ready_rate": 1.0', summary)
            self.assertIn('"cache_ready_but_hit_miss_count": 2', summary)
            self.assertIn('"hash_stable_but_cache_miss_count": 1', summary)
            self.assertIn('"semantic_hash_stable_count": 1', summary)
            self.assertIn('"semantic_stable_but_provider_visible_changed_count": 0', summary)
            self.assertIn('"cache_ready_reason_frequency"', summary)
            self.assertIn('"hook_changed_system_case_ids"', summary)
            self.assertIn('"cache_hint_observed_enabled": true', summary)
            self.assertIn('"provider_supports_cache_hint": true', summary)
            self.assertIn('"provider_supports_usage_reporting": true', summary)
            self.assertIn('"provider_supports_session_id": false', summary)
            self.assertIn('"validation_verdict": "supported_but_no_observed_hit"', summary)
            markdown = (output_dir / "summary.md").read_text(encoding="utf-8")
            self.assertIn("Cache Ready", markdown)
            self.assertIn("semantic_system_hash", markdown)
            self.assertIn("gateway_system_hash", markdown)
            self.assertIn("hash_stable_vs_previous", markdown)
            self.assertIn("validation_verdict", markdown)

    def test_provider_output_dir_uses_provider_family_subdirectory(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            provider_dir = self.mod._provider_output_dir(root, "Anthropic")
            self.assertEqual(provider_dir, root / "anthropic")


if __name__ == "__main__":
    unittest.main()
