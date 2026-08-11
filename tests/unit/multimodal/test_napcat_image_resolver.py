import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


class _Api:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        return self.response


class _Event:
    def __init__(self, segment, api):
        self.message_obj = SimpleNamespace(
            raw_message={"message": [segment]},
            message=[],
            message_id="message-1",
        )
        self.bot = SimpleNamespace(api=api)
        self._extras = {}

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)


class NapCatImageResolverTests(unittest.TestCase):
    def test_download_timeout_uses_central_timing_and_refreshes(self):
        from astrmai.multimodal.napcat_image_resolver import NapCatImageResolver

        with tempfile.TemporaryDirectory() as tmp:
            resolver = NapCatImageResolver(
                Path(tmp) / "cache",
                config=SimpleNamespace(timing=SimpleNamespace(image_resolve_timeout_sec=150.0)),
            )
            self.assertEqual(resolver._download_timeout_seconds(), 150.0)

            resolver.refresh_config(
                SimpleNamespace(timing=SimpleNamespace(image_resolve_timeout_sec=240.0))
            )
            self.assertEqual(resolver._download_timeout_seconds(), 240.0)

    def test_opaque_onebot_reference_is_resolved_through_get_image(self):
        from astrmai.multimodal.napcat_image_resolver import NapCatImageResolver

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "napcat-source.jpg"
            source.write_bytes(b"image-bytes")
            api = _Api({"file": str(source)})
            event = _Event({"type": "image", "data": {"file": "opaque-file-id"}}, api)
            resolver = NapCatImageResolver(Path(tmp) / "cache")

            result = asyncio.run(resolver.resolve_event_images(event))

            self.assertTrue(result.had_images)
            self.assertEqual(len(result.images), 1)
            self.assertTrue(Path(result.images[0].local_path).exists())
            self.assertEqual(api.calls[0], ("get_image", {"file": "opaque-file-id"}))

    def test_existing_local_image_does_not_call_napcat_api(self):
        from astrmai.multimodal.napcat_image_resolver import NapCatImageResolver

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "already-local.png"
            source.write_bytes(b"image-bytes")
            api = _Api({})
            event = _Event({"type": "image", "data": {"path": str(source)}}, api)
            resolver = NapCatImageResolver(Path(tmp) / "cache")

            result = asyncio.run(resolver.resolve_event_images(event))

            self.assertEqual(len(result.images), 1)
            self.assertEqual(api.calls, [])

    def test_historical_message_payload_resolves_nested_local_image(self):
        from astrmai.multimodal.napcat_image_resolver import NapCatImageResolver

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "historical.png"
            source.write_bytes(b"image-bytes")
            api = _Api({})
            event = _Event({"type": "text", "data": {"text": "current"}}, api)
            resolver = NapCatImageResolver(Path(tmp) / "cache")

            result = asyncio.run(
                resolver.resolve_message_payload(
                    event,
                    {
                        "data": {
                            "message": [
                                {"type": "image", "data": {"path": str(source)}}
                            ]
                        }
                    },
                )
            )

            self.assertTrue(result.had_images)
            self.assertEqual(len(result.images), 1)
            self.assertEqual(result.images[0].index, 0)
            self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()
