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


class OutboundPolicyPortedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        install_reply_engine_stubs()
        sys.modules.pop("astrmai.conversation.execution.reply_service", None)
        sys.modules.pop("astrmai.infrastructure.runtime.runtime_contracts", None)
        self.reply_mod = importlib.import_module("astrmai.conversation.execution.reply_service")
        self.reply_mod = importlib.reload(self.reply_mod)
        self.contracts_mod = importlib.import_module("astrmai.infrastructure.runtime.runtime_contracts")
        self.contracts_mod = importlib.reload(self.contracts_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_policy_changes_with_reply_mode_and_freshness(self):
        service = self.reply_mod.ReplyService(_FakeStateEngine(), mood_manager=SimpleNamespace())
        playful = service._build_outbound_policy(self.contracts_mod.ReplyMode.PLAYFUL_INTERACTION, self.contracts_mod.FreshnessState.FRESH, "")
        support = service._build_outbound_policy(self.contracts_mod.ReplyMode.EMOTIONAL_SUPPORT, self.contracts_mod.FreshnessState.FRESH, "")
        stale = service._build_outbound_policy(self.contracts_mod.ReplyMode.CASUAL_FOLLOWUP, self.contracts_mod.FreshnessState.STALE_BUT_SALVAGEABLE, "superseded")
        self.assertEqual(playful.segment_strategy, "single")
        self.assertEqual(support.segment_strategy, "gentle_two_step")
        self.assertTrue(stale.late_rewrite_allowed)
        self.assertEqual(stale.length_class, "short")


if __name__ == "__main__":
    unittest.main()
