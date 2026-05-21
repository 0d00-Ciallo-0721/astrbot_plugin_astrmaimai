import importlib
import sys
import tempfile
import unittest

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class OutputGuardRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.gateway.output_guard", None)
        guard_mod = importlib.import_module("astrmai.infrastructure.gateway.output_guard")
        self.guard_mod = importlib.reload(guard_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_validate_visible_output_text_classifies_provider_error(self):
        safe_text, failure_kind = self.guard_mod.validate_visible_output_text(
            "request id: 1\nstatus code: 500"
        )
        self.assertEqual(safe_text, "")
        self.assertEqual(failure_kind, "provider_failure_text")

    def test_validate_visible_output_text_classifies_wrapped_tool_loop_provider_error(self):
        safe_text, failure_kind = self.guard_mod.validate_visible_output_text(
            "All chat models failed: PermissionDeniedError: Error code: 403 - "
            "{'error': {'message': \"You've reached your usage limit for this billing cycle.\"}}"
        )
        self.assertEqual(safe_text, "")
        self.assertEqual(failure_kind, "provider_failure_text")

    def test_validate_visible_output_text_classifies_prompt_scaffold(self):
        safe_text, failure_kind = self.guard_mod.validate_visible_output_text("[RollingSummary]")
        self.assertEqual(safe_text, "")
        self.assertEqual(failure_kind, "prompt_scaffold_text")

    def test_validate_visible_output_text_classifies_tool_protocol(self):
        safe_text, failure_kind = self.guard_mod.validate_visible_output_text("[SYSTEM_WAIT_SIGNAL]")
        self.assertEqual(safe_text, "")
        self.assertEqual(failure_kind, "tool_protocol_text")


if __name__ == "__main__":
    unittest.main()
