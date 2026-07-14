import asyncio
import base64
import contextlib
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
        self.assertIn("chat-1:pic-1", stored)
        self.assertEqual(stored["chat-1:pic-1"].description, "test")

    def test_visual_prompt_requests_detailed_image_and_emoji_transcription(self):
        captured = {}

        class _Gateway:
            async def call_vision_task(self, **kwargs):
                captured.update(kwargs)
                return {
                    "type": "image",
                    "description": "一张用于测试的普通图片",
                    "emotion_tags": ["中性"],
                }

        cortex = self.visual_mod.VisualCortex(_Gateway(), db_service=None)
        result = asyncio.run(
            cortex.analyze_image_path("pic-prompt", "image.png", scope_id="chat-1")
        )

        self.assertEqual(result["description"], "一张用于测试的普通图片")
        combined_prompt = f'{captured["prompt"]}\n{captured["system_prompt"]}'
        for requirement in (
            "普通图片",
            "主体",
            "可见文字",
            "表情包",
            "也必须先完整描述画面内容",
            "情绪强度",
            "表达意图",
            "不得猜测",
            "只输出一个 JSON 对象",
        ):
            self.assertIn(requirement, combined_prompt)
        self.assertIn('"type"', captured["system_prompt"])
        self.assertIn('"description"', captured["system_prompt"])
        self.assertIn('"emotion_tags"', captured["system_prompt"])

    def test_worker_marks_queue_item_done_when_processing_raises(self):
        async def _run():
            cortex = self.visual_mod.VisualCortex(gateway=None, db_service=None)

            async def _raise(_picid, _base64_data):
                raise RuntimeError("vision unavailable")

            cortex.process_image_async = _raise
            worker = asyncio.create_task(cortex._worker())
            await cortex.queue.put(("pic-error", "payload"))
            await asyncio.wait_for(cortex.queue.join(), timeout=1.0)
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

        asyncio.run(_run())

    def test_worker_marks_queue_item_done_when_cancelled_during_processing(self):
        async def _run():
            cortex = self.visual_mod.VisualCortex(gateway=None, db_service=None)
            started = asyncio.Event()

            async def _block(_picid, _base64_data):
                started.set()
                await asyncio.sleep(60)

            cortex.process_image_async = _block
            worker = asyncio.create_task(cortex._worker())
            await cortex.queue.put(("pic-cancel", "payload"))
            await asyncio.wait_for(started.wait(), timeout=1.0)
            worker.cancel()
            await worker
            await asyncio.wait_for(cortex.queue.join(), timeout=1.0)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
