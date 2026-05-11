import asyncio
import base64
import importlib
import io
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace

from PIL import Image

from tests.helpers.astrbot_stubs import install_astrbot_stubs


def _install_visual_stubs():
    comp_mod = types.ModuleType("astrbot.api.message_components")
    comp_mod.Image = type("Image", (), {"fromFileSystem": staticmethod(lambda path: {"path": path})})
    sys.modules["astrbot.api.message_components"] = comp_mod

    event_mod = sys.modules["astrbot.api.event"]
    event_mod.MessageChain = type("MessageChain", (), {"__init__": lambda self: setattr(self, "chain", [])})

    datamodels_mod = types.ModuleType("astrmai.infra.datamodels")

    class VisualMemory:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    datamodels_mod.VisualMemory = VisualMemory
    sys.modules["astrmai.infra.datamodels"] = datamodels_mod

    lane_mod = types.ModuleType("astrmai.infra.lane_manager")

    class LaneKey:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    lane_mod.LaneKey = LaneKey
    sys.modules["astrmai.infra.lane_manager"] = lane_mod


class VisualCortexRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        _install_visual_stubs()
        for mod in [
            "astrmai.multimodal.image_pipeline",
            "astrmai.multimodal.visual_cortex",
        ]:
            sys.modules.pop(mod, None)
        self.visual_mod = importlib.import_module("astrmai.multimodal.visual_cortex")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _build_png_base64(self):
        img = Image.new("RGB", (2, 2), color="red")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("utf-8")

    def test_visual_cortex_processes_and_persists_result(self):
        stored = {}

        class _Session:
            def get(self, cls, key):
                return stored.get(key)

            def add(self, item):
                stored[item.picid] = item

            def commit(self):
                return None

        class _Ctx:
            def __enter__(self):
                return _Session()

            def __exit__(self, exc_type, exc, tb):
                return False

        class _DB:
            def get_session(self):
                return _Ctx()

        class _Gateway:
            async def call_vision_task(self, **kwargs):
                return {"type": "emoji", "description": "test", "emotion_tags": ["happy"]}

        cortex = self.visual_mod.VisualCortex(_Gateway(), _DB())
        asyncio.run(cortex.process_image_async("pic-1", self._build_png_base64(), scope_id="chat-1"))
        self.assertIn("pic-1", stored)
        self.assertEqual(stored["pic-1"].description, "test")


if __name__ == "__main__":
    unittest.main()
