import base64
import io
import os
import tempfile
import unittest
from unittest.mock import patch

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


def _animated_frames(count: int = 20) -> list[Image.Image]:
    frames = []
    for index in range(count):
        frame = Image.new("RGBA", (48, 36), color=(255, 255, 255, 0))
        for x in range(index % 24, min(index % 24 + 16, 48)):
            for y in range(8, 28):
                frame.putpixel((x, y), (20 + index * 5, 80, 220, 255))
        frames.append(frame)
    return frames


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

    def test_materialize_image_preserves_original_animation_for_production_analysis(self):
        from astrmai.multimodal.image_pipeline import ImagePipeline

        prepared = ImagePipeline.materialize_image(_gif_b64())

        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.image_format, "gif")
        self.assertEqual(prepared.source_format, "gif")
        self.assertTrue(prepared.is_animated)
        self.assertEqual(prepared.source_frame_count, 2)
        with Image.open(prepared.file_path) as materialized:
            self.assertEqual(materialized.format, "GIF")
            self.assertEqual(materialized.n_frames, 2)
        ImagePipeline.cleanup(prepared)

    def test_prepare_image_path_detects_gif_bytes_with_jpg_suffix_and_covers_timeline(self):
        from astrmai.multimodal.image_pipeline import ImagePipeline

        source_path = os.path.join(self.temp_dir.name, "qq-animation.jpg")
        frames = _animated_frames(20)
        frames[0].save(
            source_path,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=80,
            disposal=2,
            loop=0,
        )

        prepared = ImagePipeline.prepare_image_path(
            source_path,
            max_frames=6,
            max_edge_px=600,
        )

        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.source_format, "gif")
        self.assertEqual(prepared.declared_suffix, ".jpg")
        self.assertTrue(prepared.is_animated)
        self.assertEqual(prepared.source_frame_count, 20)
        self.assertEqual(prepared.sampled_indices[0], 0)
        self.assertEqual(prepared.sampled_indices[-1], 19)
        self.assertGreater(len(prepared.sampled_indices), 1)
        self.assertEqual(prepared.image_format, "jpeg")
        with Image.open(prepared.file_path) as contact_sheet:
            self.assertEqual(contact_sheet.format, "JPEG")
            self.assertLessEqual(max(contact_sheet.size), 600)
        ImagePipeline.cleanup(prepared)

    def test_frame_difference_uses_non_wrapping_numeric_type(self):
        from astrmai.multimodal.image_pipeline import ImagePipeline

        black = Image.new("RGB", (8, 8), color=(0, 0, 0))
        white = Image.new("RGB", (8, 8), color=(255, 255, 255))

        self.assertGreater(ImagePipeline._frame_difference(black, white), 1000.0)

    def test_variable_frame_sizes_are_normalized_before_selection(self):
        from astrmai.multimodal.image_pipeline import ImagePipeline

        frames = [
            Image.new("RGBA", (40, 32), color=(255, 0, 0, 255)),
            Image.new("RGBA", (44, 36), color=(0, 255, 0, 255)),
            Image.new("RGBA", (48, 34), color=(0, 0, 255, 255)),
        ]

        normalized = ImagePipeline._normalize_animation_frames(frames)

        self.assertEqual([frame.size for frame in normalized], [(48, 36)] * 3)
        self.assertGreater(ImagePipeline._frame_difference(normalized[0], normalized[1]), 1000.0)

    def test_animation_preprocess_failure_falls_back_to_first_frame_jpeg(self):
        from astrmai.multimodal.image_pipeline import ImagePipeline

        source_path = os.path.join(self.temp_dir.name, "fallback.jpg")
        frames = _animated_frames(3)
        frames[0].save(
            source_path,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=60,
            loop=0,
        )

        with patch.object(ImagePipeline, "_render_contact_sheet", side_effect=RuntimeError("boom")):
            prepared = ImagePipeline.prepare_image_path(source_path)

        self.assertIsNotNone(prepared)
        self.assertTrue(prepared.is_animated)
        self.assertEqual(prepared.preprocess_status, "fallback_first_frame")
        self.assertEqual(prepared.fallback_reason, "RuntimeError")
        self.assertEqual(prepared.sampled_indices, (0,))
        with Image.open(prepared.file_path) as fallback:
            self.assertEqual(fallback.format, "JPEG")
        ImagePipeline.cleanup(prepared)

    def test_serialize_tags_accepts_only_lists(self):
        from astrmai.multimodal.image_pipeline import ImagePipeline

        self.assertEqual(ImagePipeline.serialize_tags(["happy"]), '["happy"]')
        self.assertEqual(ImagePipeline.serialize_tags({"tag": "happy"}), "[]")


if __name__ == "__main__":
    unittest.main()
