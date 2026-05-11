import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.original_ported.helpers import _install_astrbot_stubs
from tests.helpers.reply_engine_stubs import install_reply_engine_stubs


class _FakeStateEngine:
    def __init__(self):
        self.gateway = SimpleNamespace(context=None)
        self.config = SimpleNamespace(
            reply=SimpleNamespace(segment_min_len=4, no_segment_max_len=200, meme_probability=0, emotion_mapping={}, fallback_text="fallback", typing_speed_factor=0.0),
            infra=SimpleNamespace(api_timeout=15),
            attention=SimpleNamespace(bg_pool_size=20),
            global_settings=SimpleNamespace(debug_mode=False),
        )


class VisibleReplyArtifactPortedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        install_reply_engine_stubs()
        sys.modules.pop("astrmai.conversation.execution.reply_service", None)
        self.reply_mod = importlib.import_module("astrmai.conversation.execution.reply_service")
        self.reply_mod = importlib.reload(self.reply_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_artifact_blocks_dirty_provider_text(self):
        service = self.reply_mod.ReplyService(_FakeStateEngine(), mood_manager=SimpleNamespace())
        artifact = service._build_visible_reply_artifact('JSON response: {"candidates": []}')
        self.assertFalse(artifact.blocked)
        self.assertEqual(artifact.visible_text, "fallback")

    def test_artifact_keeps_sendable_segments_and_persistable_text(self):
        service = self.reply_mod.ReplyService(_FakeStateEngine(), mood_manager=SimpleNamespace())
        artifact = service._build_visible_reply_artifact("assistant: hmm...\nDo not be sad, I am here.")
        self.assertFalse(artifact.blocked)
        self.assertIn("hmm", artifact.visible_text)
        self.assertIn("Do not be sad", artifact.persistable_text)


if __name__ == "__main__":
    unittest.main()
