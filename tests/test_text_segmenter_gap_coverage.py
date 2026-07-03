from __future__ import annotations

import importlib
import sys
import tempfile
import unittest

from tests.original_ported.helpers import _install_astrbot_stubs


class TextSegmenterGapCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.conversation.execution.text_segmenter", None)
        self.mod = importlib.import_module("astrmai.conversation.execution.text_segmenter")
        self.mod = importlib.reload(self.mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_segmenter_keeps_url_intact_across_sentence_split(self):
        segmenter = self.mod.TextSegmenter(min_length=8, max_length=32)
        text = "open https://example.com/a/b?x=1&y=2 please. then reply briefly."

        segments = segmenter.segment(text)

        self.assertTrue(any("https://example.com/a/b?x=1&y=2" in segment for segment in segments))
        self.assertFalse(any("https://example.com/a/b" in segment and "y=2" not in segment for segment in segments))

    def test_segmenter_keeps_fenced_code_block_as_single_unit(self):
        segmenter = self.mod.TextSegmenter(min_length=8, max_length=36)
        text = "before\n```python\nprint('a.b?c!')\n```\nafter this sentence continues."

        segments = segmenter.segment(text)

        code_segments = [segment for segment in segments if "```python" in segment]
        self.assertEqual(len(code_segments), 1)
        self.assertIn("print('a.b?c!')", code_segments[0])
        self.assertIn("```", code_segments[0])


if __name__ == "__main__":
    unittest.main()
