import importlib
import sys
import tempfile
import unittest

from tests.original_ported.helpers import _install_astrbot_stubs


class MojibakeOutputGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.gateway.output_guard", None)
        self.guard_mod = importlib.import_module("astrmai.infrastructure.gateway.output_guard")
        self.guard_mod = importlib.reload(self.guard_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_prompt_and_state_lines_are_stripped_from_mixed_reply(self):
        dirty_reply = (
            "user: [月色序曲] 说: [发了一张图片][鍥剧墖]\n"
            "[当前心情: 兴奋 (1.0) | 精力: 0.55]\n"
            "【你的想法】嘿嘿，看到图片啦！\n"
            "【对话目标】回应图片并保持活泼。\n"
            "嘿嘿，让高性能的亚托莉看看！\n"
            "哇！这是什么呀？"
        )

        cleaned = self.guard_mod.sanitize_visible_reply_text(dirty_reply, fallback_text="")

        self.assertNotIn("user:", cleaned)
        self.assertNotIn("鍥剧墖", cleaned)
        self.assertNotIn("当前心情", cleaned)
        self.assertNotIn("你的想法", cleaned)
        self.assertIn("嘿嘿，让高性能的亚托莉看看！", cleaned)
        self.assertIn("哇！这是什么呀？", cleaned)

    def test_bot_name_prefix_is_stripped_without_touching_inline_mentions(self):
        cleaned = self.guard_mod.sanitize_visible_reply_text(
            "ATRI: 快来让亚托莉也蹭欧气～",
            fallback_text="",
            speaker_names=["ATRI", "亚托莉"],
        )
        self.assertEqual(cleaned, "快来让亚托莉也蹭欧气～")

        inline = self.guard_mod.sanitize_visible_reply_text(
            "我看到你写了 ATRI:xxx，先别急。",
            fallback_text="",
            speaker_names=["ATRI", "亚托莉"],
        )
        self.assertEqual(inline, "我看到你写了 ATRI:xxx，先别急。")

    def test_chinese_bot_name_prefix_is_stripped_only_at_line_start(self):
        cleaned = self.guard_mod.sanitize_visible_reply_text(
            "\u4e9a\u6258\u8389\uff1a\u4f60\u597d\u5440",
            fallback_text="",
            speaker_names=["ATRI", "\u4e9a\u6258\u8389"],
        )
        self.assertEqual(cleaned, "\u4f60\u597d\u5440")

        inline = self.guard_mod.sanitize_visible_reply_text(
            "\u6211\u770b\u5230\u4f60\u5199\u4e86\u4e9a\u6258\u8389\uff1a\u4f60\u597d\u5440\uff0c\u6240\u4ee5\u6211\u6765\u56de\u5e94\u3002",
            fallback_text="",
            speaker_names=["ATRI", "\u4e9a\u6258\u8389"],
        )
        self.assertEqual(
            inline,
            "\u6211\u770b\u5230\u4f60\u5199\u4e86\u4e9a\u6258\u8389\uff1a\u4f60\u597d\u5440\uff0c\u6240\u4ee5\u6211\u6765\u56de\u5e94\u3002",
        )

    def test_memory_prompt_scaffold_is_stripped_without_blocking_natural_impression(self):
        dirty_reply = (
            "---记忆闪回---\n"
            "内心浮现的印象，仅供我自己判断当下。\n"
            "---任意分区标题---\n"
            "我印象里你挺在意天气的，先看看今天会不会下雨。"
        )

        cleaned = self.guard_mod.sanitize_visible_reply_text(dirty_reply, fallback_text="")

        self.assertNotIn("---记忆闪回---", cleaned)
        self.assertNotIn("内心浮现的印象", cleaned)
        self.assertNotIn("---任意分区标题---", cleaned)
        self.assertEqual(cleaned, "我印象里你挺在意天气的，先看看今天会不会下雨。")

        leaked = self.guard_mod.sanitize_visible_reply_text(
            "仅供内心参考，不要出现在回复正文中。",
            fallback_text="",
        )
        self.assertEqual(leaked, "")


if __name__ == "__main__":
    unittest.main()
