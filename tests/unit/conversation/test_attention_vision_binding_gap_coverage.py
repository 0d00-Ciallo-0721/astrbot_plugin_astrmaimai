import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class VisionBindingGapCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_extract_prefers_component_file_to_base64(self):
        from astrmai.conversation.attention.vision_binding import extract_image_base64

        class _Image:
            url = "http://example.invalid/image.png"

            async def file_to_base64(self):
                return "already-encoded"

        result = asyncio.run(extract_image_base64(SimpleNamespace(), _Image()))

        self.assertEqual(result, "already-encoded")

    def test_extract_falls_back_to_local_file_after_component_failure(self):
        from astrmai.conversation.attention.vision_binding import extract_image_base64

        image_path = Path(self.temp_dir.name) / "image.bin"
        image_path.write_bytes(b"local-bytes")

        class _Image:
            file = str(image_path)

            async def file_to_base64(self):
                raise RuntimeError("broken component")

        result = asyncio.run(extract_image_base64(SimpleNamespace(), _Image()))

        self.assertEqual(result, base64.b64encode(b"local-bytes").decode("utf-8"))

    def test_extract_url_rejects_unsafe_scheme_without_http_client(self):
        from astrmai.conversation.attention.vision_binding import extract_image_base64_from_url

        result = asyncio.run(extract_image_base64_from_url(SimpleNamespace(), "file:///tmp/image.png"))

        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
