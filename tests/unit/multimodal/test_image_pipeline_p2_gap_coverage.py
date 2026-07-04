import base64
import io
import os
import tempfile
import unittest

from PIL import Image

from tests.helpers.astrbot_stubs import install_astrbot_stubs


def _image_b64(format_name="PNG") -> str:
    image = Image.new("RGB", (3, 2), color=(10, 20, 30))
    buffer = io.BytesIO()
    image.save(buffer, format=format_name)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _gif_b64() -> str:
    first = Image.new("RGB", (3, 2), color=(255, 0, 0))
    second = Image.new("RGB", (3, 2), color=(0, 255, 0))
    buffer = io.BytesIO()
    first.save(buffer, format="GIF", save_all=True, append_images=[second], duration=20, loop=0)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class ImagePipelineP2GapCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_prepare_image_writes_and_cleanup_removes_temp_file(self):
        from astrmai.multimodal.image_pipeline import ImagePipeline

        prepared = ImagePipeline.prepare_image(_image_b64())
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.image_format, "png")
        self.assertTrue(os.path.exists(prepared.file_path))

        ImagePipeline.cleanup(prepared)

        self.assertFalse(os.path.exists(prepared.file_path))

    def test_transform_gif_returns_jpeg_base64_and_prepare_converts_format(self):
        from astrmai.multimodal.image_pipeline import ImagePipeline

        transformed = ImagePipeline.transform_gif(_gif_b64(), similarity_threshold=0.0)
        prepared = ImagePipeline.prepare_image(_gif_b64())

        self.assertIsInstance(transformed, str)
        self.assertGreater(len(base64.b64decode(transformed)), 0)
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.image_format, "jpeg")
        ImagePipeline.cleanup(prepared)

    def test_serialize_tags_accepts_only_lists(self):
        from astrmai.multimodal.image_pipeline import ImagePipeline

        self.assertEqual(ImagePipeline.serialize_tags(["happy"]), '["happy"]')
        self.assertEqual(ImagePipeline.serialize_tags({"tag": "happy"}), "[]")


if __name__ == "__main__":
    unittest.main()
