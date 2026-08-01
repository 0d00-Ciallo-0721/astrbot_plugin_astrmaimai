import asyncio
import base64
import contextlib
import importlib
import io
import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace

from PIL import Image
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

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
        image_path = os.path.join(self.temp_dir.name, "prompt.png")
        Image.new("RGB", (4, 4), color="blue").save(image_path, format="PNG")
        result = asyncio.run(
            cortex.analyze_image_path("pic-prompt", image_path, scope_id="chat-1")
        )

        self.assertEqual(result["description"], "一张用于测试的普通图片")
        self.assertEqual(result["emotion_tags"], [])
        combined_prompt = f'{captured["prompt"]}\n{captured["system_prompt"]}'
        for requirement in (
            "普通图片",
            "主体",
            "可见文字",
            "表情包",
            "聊天反应",
            "必须返回空数组",
            "表达意图",
            "不得猜测",
            "只输出一个 JSON 对象",
        ):
            self.assertIn(requirement, combined_prompt)
        self.assertIn('"type"', captured["system_prompt"])
        self.assertIn('"description"', captured["system_prompt"])
        self.assertIn('"emotion_tags"', captured["system_prompt"])

    def test_content_cache_reuses_same_pixels_across_scopes_and_binds_messages(self):
        from astrmai.infrastructure.persistence.orm_models import (
            VisualAsset,
            VisualMemory,
            VisualMessageBinding,
        )

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)

        class _DB:
            def get_session(self):
                return Session(engine)

        class _Gateway:
            def __init__(self):
                self.calls = 0

            async def call_vision_task(self, **kwargs):
                self.calls += 1
                return {
                    "type": "emoji",
                    "description": "同一张测试表情包",
                    "emotion_tags": ["开心"],
                    "_vision_model_id": "vision-test",
                }

        png_path = os.path.join(self.temp_dir.name, "same.png")
        bmp_path = os.path.join(self.temp_dir.name, "same.bmp")
        image = Image.new("RGB", (8, 8), color=(12, 34, 56))
        image.save(png_path, format="PNG")
        image.save(bmp_path, format="BMP")
        gateway = _Gateway()
        cortex = self.visual_mod.VisualCortex(
            gateway,
            _DB(),
            config=SimpleNamespace(
                vision=SimpleNamespace(
                    enable_visual_result_cache=True,
                    store_visual_asset_files=False,
                    visual_prompt_version="v1",
                )
            ),
        )

        async def _run():
            first = await cortex.analyze_image_path(
                "legacy-a",
                png_path,
                scope_id="chat-a",
                binding_context={
                    "chat_id": "chat-a",
                    "message_id": "message-a",
                    "sender_id": "user-a",
                    "image_index": 0,
                    "source_ref": "private-source-a",
                },
            )
            second = await cortex.analyze_image_path(
                "legacy-b",
                bmp_path,
                scope_id="chat-b",
                binding_context={
                    "chat_id": "chat-b",
                    "message_id": "message-b",
                    "sender_id": "user-b",
                    "image_index": 0,
                    "source_ref": "private-source-b",
                },
            )
            return first, second

        first, second = asyncio.run(_run())
        self.assertEqual(gateway.calls, 1)
        self.assertEqual(first["_asset_id"], second["_asset_id"])
        self.assertFalse(first["_cache_hit"])
        self.assertTrue(second["_cache_hit"])
        self.assertEqual(second["_cache_kind"], "content")
        with Session(engine) as session:
            assets = list(session.exec(select(VisualAsset)).all())
            bindings = list(session.exec(select(VisualMessageBinding)).all())
            legacy = list(session.exec(select(VisualMemory)).all())
        self.assertEqual(len(assets), 1)
        self.assertEqual(len(bindings), 2)
        self.assertEqual(len(legacy), 2)
        self.assertEqual(assets[0].model_id, "vision-test")
        self.assertNotIn("private-source", bindings[0].source_ref_hash)

    def test_concurrent_same_image_uses_singleflight(self):
        class _Gateway:
            def __init__(self):
                self.calls = 0

            async def call_vision_task(self, **kwargs):
                self.calls += 1
                await asyncio.sleep(0.05)
                return {
                    "type": "image",
                    "description": "并发测试图片",
                    "emotion_tags": [],
                }

        path = os.path.join(self.temp_dir.name, "singleflight.png")
        Image.new("RGB", (6, 6), color="green").save(path, format="PNG")
        gateway = _Gateway()
        cortex = self.visual_mod.VisualCortex(gateway, db_service=None)

        async def _run():
            return await asyncio.gather(
                cortex.analyze_image_path("one", path, scope_id="chat-a"),
                cortex.analyze_image_path("two", path, scope_id="chat-b"),
            )

        first, second = asyncio.run(_run())
        self.assertEqual(gateway.calls, 1)
        self.assertEqual(first["_asset_id"], second["_asset_id"])
        self.assertTrue(
            first["_singleflight_join"] or second["_singleflight_join"]
        )

    def test_failed_image_enters_cooldown_and_does_not_repeat_model_call(self):
        class _Gateway:
            def __init__(self):
                self.calls = 0

            async def call_vision_task(self, **kwargs):
                self.calls += 1
                raise RuntimeError("vision unavailable")

        path = os.path.join(self.temp_dir.name, "failed.png")
        Image.new("RGB", (6, 6), color="black").save(path, format="PNG")
        gateway = _Gateway()
        cortex = self.visual_mod.VisualCortex(
            gateway,
            db_service=None,
            config=SimpleNamespace(
                vision=SimpleNamespace(
                    enable_visual_result_cache=True,
                    store_visual_asset_files=False,
                    visual_prompt_version="v1",
                    visual_failure_cooldown_sec=120,
                )
            ),
        )

        async def _run():
            with self.assertRaises(RuntimeError):
                await cortex.analyze_image_path("first", path, scope_id="chat")
            await asyncio.sleep(0)
            with self.assertRaises(self.visual_mod.VisionAnalysisCoolingDown):
                await cortex.analyze_image_path("second", path, scope_id="chat")

        asyncio.run(_run())
        self.assertEqual(gateway.calls, 1)
        self.assertEqual(cortex.describe_status()["failure_cooldown_count"], 1)

    def test_empty_image_result_also_enters_failure_cooldown(self):
        class _Gateway:
            def __init__(self):
                self.calls = 0

            async def call_vision_task(self, **kwargs):
                self.calls += 1
                return None

        path = os.path.join(self.temp_dir.name, "empty.png")
        Image.new("RGB", (6, 6), color="white").save(path, format="PNG")
        gateway = _Gateway()
        cortex = self.visual_mod.VisualCortex(
            gateway,
            db_service=None,
            config=SimpleNamespace(
                vision=SimpleNamespace(
                    enable_visual_result_cache=True,
                    store_visual_asset_files=False,
                    visual_prompt_version="v1",
                    visual_failure_cooldown_sec=120,
                )
            ),
        )

        async def _run():
            self.assertIsNone(
                await cortex.analyze_image_path("first", path, scope_id="chat")
            )
            await asyncio.sleep(0)
            with self.assertRaises(self.visual_mod.VisionAnalysisCoolingDown):
                await cortex.analyze_image_path("second", path, scope_id="chat")

        asyncio.run(_run())
        self.assertEqual(gateway.calls, 1)

    def test_prompt_version_change_does_not_reuse_legacy_transcription(self):
        from astrmai.infrastructure.persistence.orm_models import VisualAsset

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)

        class _DB:
            def get_session(self):
                return Session(engine)

        class _Gateway:
            def __init__(self):
                self.calls = 0

            async def call_vision_task(self, **kwargs):
                self.calls += 1
                return {
                    "type": "image",
                    "description": f"提示词版本 {self.calls}",
                    "emotion_tags": [],
                }

        path = os.path.join(self.temp_dir.name, "versioned.png")
        Image.new("RGB", (6, 6), color="orange").save(path, format="PNG")
        config = SimpleNamespace(
            vision=SimpleNamespace(
                enable_visual_result_cache=True,
                store_visual_asset_files=False,
                visual_prompt_version="v1",
            )
        )
        gateway = _Gateway()
        cortex = self.visual_mod.VisualCortex(gateway, _DB(), config=config)

        first = asyncio.run(
            cortex.analyze_image_path("same-message", path, scope_id="chat")
        )
        config.vision.visual_prompt_version = "v2"
        second = asyncio.run(
            cortex.analyze_image_path("same-message", path, scope_id="chat")
        )

        self.assertEqual(gateway.calls, 2)
        self.assertNotEqual(first["_asset_id"], second["_asset_id"])
        self.assertEqual(second["_cache_kind"], "miss")
        self.assertEqual(second["_prompt_version"], "v2")
        with Session(engine) as session:
            assets = list(session.exec(select(VisualAsset)).all())
        self.assertEqual(len(assets), 2)

    def test_animated_identity_includes_frames_after_the_first(self):
        first_frame = Image.new("RGBA", (8, 8), color="white")
        second_red = Image.new("RGBA", (8, 8), color="red")
        second_blue = Image.new("RGBA", (8, 8), color="blue")
        red_path = os.path.join(self.temp_dir.name, "animated-red.gif")
        blue_path = os.path.join(self.temp_dir.name, "animated-blue.gif")
        first_frame.save(
            red_path,
            format="GIF",
            save_all=True,
            append_images=[second_red],
            duration=[100, 100],
            loop=0,
        )
        first_frame.save(
            blue_path,
            format="GIF",
            save_all=True,
            append_images=[second_blue],
            duration=[100, 100],
            loop=0,
        )

        red_identity = self.visual_mod.build_visual_asset_identity(red_path)
        blue_identity = self.visual_mod.build_visual_asset_identity(blue_path)

        self.assertEqual(red_identity.frame_count, 2)
        self.assertEqual(blue_identity.frame_count, 2)
        self.assertNotEqual(red_identity.pixel_hash, blue_identity.pixel_hash)
        self.assertNotEqual(red_identity.asset_id, blue_identity.asset_id)

    def test_optional_standardized_file_storage_does_not_store_source_path(self):
        from astrmai.infrastructure.persistence.orm_models import VisualAsset

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)

        class _DB:
            def get_session(self):
                return Session(engine)

        class _Gateway:
            async def call_vision_task(self, **kwargs):
                return {
                    "type": "image",
                    "description": "文件存储测试",
                    "emotion_tags": [],
                }

        source_path = os.path.join(self.temp_dir.name, "source.png")
        asset_dir = os.path.join(self.temp_dir.name, "assets")
        Image.new("RGB", (32, 16), color="purple").save(source_path, format="PNG")
        cortex = self.visual_mod.VisualCortex(
            _Gateway(),
            _DB(),
            asset_dir=asset_dir,
            config=SimpleNamespace(
                vision=SimpleNamespace(
                    enable_visual_result_cache=True,
                    store_visual_asset_files=True,
                    visual_asset_max_edge_px=16,
                    visual_asset_retention_days=30,
                    visual_asset_max_disk_mb=16,
                    visual_prompt_version="v1",
                )
            ),
        )

        result = asyncio.run(
            cortex.analyze_image_path("stored", source_path, scope_id="chat")
        )
        with Session(engine) as session:
            asset = session.get(VisualAsset, result["_asset_id"])
        self.assertEqual(asset.storage_path, f"{result['_asset_id']}.jpg")
        self.assertNotIn(self.temp_dir.name, asset.storage_path)
        stored_path = os.path.join(asset_dir, asset.storage_path)
        self.assertTrue(os.path.isfile(stored_path))
        with Image.open(stored_path) as stored:
            self.assertLessEqual(max(stored.size), 16)

    def test_visual_normalizer_cleans_prefixes_and_keeps_emoji_tags(self):
        payload, reason = self.visual_mod.normalize_vision_result(
            {
                "type": "emoji",
                "description": "这是一个表情包，熊猫头低着头，文字为“我太难了”。通常用于自我调侃。",
                "emotion_tags": ["无奈", "无奈", "疲惫", "自嘲", "抱怨", "多余"],
            }
        )

        self.assertEqual(reason, "")
        self.assertEqual(payload["type"], "emoji")
        self.assertTrue(payload["description"].startswith("熊猫头"))
        self.assertEqual(payload["emotion_tags"], ["无奈", "疲惫", "自嘲", "抱怨", "多余"])

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
