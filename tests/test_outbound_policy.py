import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.test_persistence_regressions import _install_astrbot_stubs
from tests.test_reply_engine_focus_anchor import _FakeStateEngine, _install_reply_engine_stubs


class OutboundPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        _install_reply_engine_stubs()
        sys.modules.pop("astrmai.Brain.reply_engine", None)
        sys.modules.pop("astrmai.infra.runtime_contracts", None)
        self.reply_engine_mod = importlib.import_module("astrmai.Brain.reply_engine")
        self.reply_engine_mod = importlib.reload(self.reply_engine_mod)
        self.contracts_mod = importlib.import_module("astrmai.infra.runtime_contracts")
        self.contracts_mod = importlib.reload(self.contracts_mod)

    def tearDown(self):
        for name in (
            "astrmai.Brain.reply_engine",
            "astrmai.infra.runtime_contracts",
            "astrmai.Heart.affection_router",
            "astrmai.Heart.state_engine",
            "astrmai.Heart.mood_manager",
            "astrmai.infra.datamodels",
            "astrmai.meme_engine.meme_config",
            "astrmai.meme_engine.meme_sender",
            "astrbot.api.message_components",
        ):
            sys.modules.pop(name, None)
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_policy_changes_with_reply_mode_and_freshness(self):
        engine = self.reply_engine_mod.ReplyEngine(_FakeStateEngine(), mood_manager=SimpleNamespace())

        playful = engine._build_outbound_policy(
            self.contracts_mod.ReplyMode.PLAYFUL_INTERACTION,
            self.contracts_mod.FreshnessState.FRESH,
            "",
        )
        support = engine._build_outbound_policy(
            self.contracts_mod.ReplyMode.EMOTIONAL_SUPPORT,
            self.contracts_mod.FreshnessState.FRESH,
            "",
        )
        stale = engine._build_outbound_policy(
            self.contracts_mod.ReplyMode.CASUAL_FOLLOWUP,
            self.contracts_mod.FreshnessState.STALE_BUT_SALVAGEABLE,
            "superseded",
        )

        self.assertEqual(playful.segment_strategy, "single")
        self.assertEqual(support.segment_strategy, "gentle_two_step")
        self.assertTrue(stale.late_rewrite_allowed)
        self.assertEqual(stale.length_class, "short")


if __name__ == "__main__":
    unittest.main()
