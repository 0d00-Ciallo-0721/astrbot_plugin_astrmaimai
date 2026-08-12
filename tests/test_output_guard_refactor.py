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

    def test_normal_technical_answer_with_provider_terms_is_preserved(self):
        answer = (
            "HTTP status code 429 usually indicates rate limiting, while quota is the broader usage budget.\n"
            "The response should include a request id so support can trace the call."
        )

        safe_text, failure_kind = self.guard_mod.validate_visible_output_text(answer)

        self.assertEqual(safe_text, answer)
        self.assertEqual(failure_kind, "")

    def test_status_code_explanation_line_is_not_discarded_as_noise(self):
        answer = "status code: 429 means the provider is asking the client to retry later."

        self.assertEqual(
            self.guard_mod.sanitize_visible_reply_text(answer, fallback_text="fallback"),
            answer,
        )

    def test_validate_visible_output_text_classifies_prompt_scaffold(self):
        safe_text, failure_kind = self.guard_mod.validate_visible_output_text("[RollingSummary]")
        self.assertEqual(safe_text, "")
        self.assertEqual(failure_kind, "prompt_scaffold_text")

    def test_validate_visible_output_text_classifies_tool_protocol(self):
        safe_text, failure_kind = self.guard_mod.validate_visible_output_text("[SYSTEM_WAIT_SIGNAL]")
        self.assertEqual(safe_text, "")
        self.assertEqual(failure_kind, "tool_protocol_text")

    def test_validate_visible_output_text_blocks_internal_event_envelope(self):
        leaked = (
            "我们之前在聊“[事件=1727617753 | 发言人=恸（ID:516779421） | "
            "角色=成员 | 类型=image | 来源=original | 媒体=图片:1] "
            "内容：[表情包转述：一个金发双马尾女孩]”，还要继续吗？"
        )

        safe_text, failure_kind = self.guard_mod.validate_visible_output_text(leaked)

        self.assertEqual(safe_text, "")
        self.assertEqual(failure_kind, "internal_event_envelope")
        self.assertEqual(
            self.guard_mod.sanitize_visible_reply_text(leaked, fallback_text="要继续刚才的话题吗？"),
            "要继续刚才的话题吗？",
        )

    def test_validate_visible_output_text_blocks_standalone_internal_vision_marker(self):
        leaked = "[表情包转述：一个金发女孩双手捧着碗，传达情绪：委屈]"

        safe_text, failure_kind = self.guard_mod.validate_visible_output_text(leaked)

        self.assertEqual(safe_text, "")
        self.assertEqual(failure_kind, "internal_media_context")


if __name__ == "__main__":
    unittest.main()
