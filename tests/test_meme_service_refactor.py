import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers.astrbot_stubs import install_astrbot_stubs


def _install_meme_stubs():
    comp_mod = types.ModuleType("astrbot.api.message_components")

    class _Image:
        @staticmethod
        def fromFileSystem(path):
            return {"path": path}

    comp_mod.Image = _Image
    sys.modules["astrbot.api.message_components"] = comp_mod

    event_mod = sys.modules["astrbot.api.event"]

    class MessageChain:
        def __init__(self):
            self.chain = []

    event_mod.MessageChain = MessageChain


class MemeServiceRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        _install_meme_stubs()
        sys.modules.pop("astrmai.multimodal", None)
        sys.modules.pop("astrmai.multimodal.meme", None)
        for mod in [
            "astrmai.multimodal.meme.meme_sender",
            "astrmai.multimodal.meme.meme_init",
            "astrmai.multimodal.meme.meme_config",
        ]:
            sys.modules.pop(mod, None)
        self.sender_mod = importlib.import_module("astrmai.multimodal.meme.meme_sender")
        self.init_mod = importlib.import_module("astrmai.multimodal.meme.meme_init")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_send_meme_uses_local_multimodal_directory(self):
        memes_dir = Path(self.temp_dir.name) / "happy"
        memes_dir.mkdir(parents=True, exist_ok=True)
        (memes_dir / "a.png").write_bytes(b"fake")
        sent = []

        extras = {}

        class _Event:
            unified_msg_origin = "group-1"

            def set_extra(self, key, value):
                extras[key] = value

        class _Context:
            async def send_message(self, origin, chain):
                sent.append((origin, chain.chain))

        asyncio.run(
            self.sender_mod.send_meme(
                event=_Event(),
                emotion_tag="happy",
                probability=100,
                memes_dir=Path(self.temp_dir.name),
                context=_Context(),
            )
        )
        self.assertEqual(sent[0][0], "group-1")
        self.assertTrue(sent[0][1])
        self.assertEqual(extras["astrmai_meme_send_result"]["reason"], "sent")

    def test_send_meme_adapter_failure_is_best_effort(self):
        memes_dir = Path(self.temp_dir.name) / "happy"
        memes_dir.mkdir(parents=True, exist_ok=True)
        (memes_dir / "a.png").write_bytes(b"fake")
        extras = {}

        class _Event:
            unified_msg_origin = "group-1"

            def set_extra(self, key, value):
                extras[key] = value

        class _Context:
            async def send_message(self, _origin, _chain):
                raise RuntimeError("adapter unavailable")

        result = asyncio.run(
            self.sender_mod.send_meme(
                event=_Event(),
                emotion_tag="happy",
                probability=100,
                memes_dir=Path(self.temp_dir.name),
                context=_Context(),
            )
        )

        self.assertFalse(result)
        self.assertTrue(extras["astrmai_meme_send_degraded"])
        self.assertEqual(extras["astrmai_meme_send_result"]["reason"], "send_failed")

    def test_send_meme_records_directory_missing_reason(self):
        extras = {}

        class _Event:
            def set_extra(self, key, value):
                extras[key] = value

        result = asyncio.run(
            self.sender_mod.send_meme(
                event=_Event(),
                emotion_tag="happy",
                probability=100,
                memes_dir=Path(self.temp_dir.name),
            )
        )

        self.assertFalse(result)
        self.assertEqual(extras["astrmai_meme_send_result"]["reason"], "directory_missing")

    def test_send_meme_records_neutral_reason(self):
        extras = {}

        class _Event:
            def set_extra(self, key, value):
                extras[key] = value

        result = asyncio.run(
            self.sender_mod.send_meme(
                event=_Event(),
                emotion_tag="neutral",
                probability=100,
                memes_dir=Path(self.temp_dir.name),
            )
        )

        self.assertFalse(result)
        self.assertEqual(extras["astrmai_meme_send_result"]["reason"], "neutral")

    def test_send_meme_records_probability_miss_reason(self):
        extras = {}

        class _Event:
            def set_extra(self, key, value):
                extras[key] = value

        with patch.object(self.sender_mod.random, "randint", return_value=100):
            result = asyncio.run(
                self.sender_mod.send_meme(
                    event=_Event(),
                    emotion_tag="happy",
                    probability=80,
                    memes_dir=Path(self.temp_dir.name),
                )
            )

        self.assertFalse(result)
        self.assertEqual(extras["astrmai_meme_send_result"]["reason"], "probability_miss")

    def test_send_meme_records_directory_empty_reason(self):
        extras = {}
        (Path(self.temp_dir.name) / "happy").mkdir()

        class _Event:
            def set_extra(self, key, value):
                extras[key] = value

        result = asyncio.run(
            self.sender_mod.send_meme(
                event=_Event(),
                emotion_tag="happy",
                probability=100,
                memes_dir=Path(self.temp_dir.name),
            )
        )

        self.assertFalse(result)
        self.assertEqual(extras["astrmai_meme_send_result"]["reason"], "directory_empty")

    def test_send_meme_records_file_unreadable_reason(self):
        extras = {}
        memes_dir = Path(self.temp_dir.name) / "happy"
        memes_dir.mkdir()
        (memes_dir / "a.png").write_bytes(b"fake")

        class _Event:
            def set_extra(self, key, value):
                extras[key] = value

        with patch.object(Path, "open", side_effect=OSError("denied")):
            result = asyncio.run(
                self.sender_mod.send_meme(
                    event=_Event(),
                    emotion_tag="happy",
                    probability=100,
                    memes_dir=Path(self.temp_dir.name),
                )
            )

        self.assertFalse(result)
        self.assertEqual(extras["astrmai_meme_send_result"]["reason"], "file_unreadable")


if __name__ == "__main__":
    unittest.main()
