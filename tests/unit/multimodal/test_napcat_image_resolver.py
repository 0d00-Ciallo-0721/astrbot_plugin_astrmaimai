import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image


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
    @staticmethod
    def _gif_bytes() -> bytes:
        output = io.BytesIO()
        frames = [
            Image.new("RGB", (12, 12), color="white"),
            Image.new("RGB", (12, 12), color="red"),
        ]
        frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=100,
            loop=0,
        )
        return output.getvalue()

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

    def test_final_candidate_falls_back_from_get_image_to_get_file(self):
        from astrmai.multimodal.napcat_image_resolver import NapCatImageResolver

        class _FallbackApi:
            def __init__(self, source):
                self.source = source
                self.calls = []

            async def call_action(self, action, **params):
                self.calls.append((action, params))
                if action == "get_file":
                    return {"status": "ok", "retcode": 0, "data": {"file": self.source}}
                return {"status": "ok", "retcode": 0, "data": {}}

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "refreshed.png"
            source.write_bytes(b"image-bytes")
            api = _FallbackApi(str(source))
            event = _Event({"type": "text", "data": {"text": "current"}}, api)
            resolver = NapCatImageResolver(Path(tmp) / "cache")

            result = asyncio.run(
                resolver.resolve_candidate(
                    event,
                    {
                        "message_id": "message-1",
                        "candidate_refs": ["expired-image-reference"],
                    },
                )
            )

            self.assertEqual(len(result.images), 1)
            self.assertEqual(result.images[0].strategy, "get_file")
            self.assertIn(("get_file", {"file": "expired-image-reference"}), api.calls)

    def test_reply_candidate_uses_get_msg_when_component_chain_is_missing(self):
        from astrmai.multimodal.napcat_image_resolver import NapCatImageResolver

        class _MessageApi:
            def __init__(self, source):
                self.source = source
                self.calls = []

            async def call_action(self, action, **params):
                self.calls.append((action, params))
                if action == "get_msg":
                    return {
                        "status": "ok",
                        "retcode": 0,
                        "data": {
                            "message": [
                                {"type": "image", "data": {"path": self.source}}
                            ]
                        },
                    }
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "quoted.png"
            source.write_bytes(b"image-bytes")
            api = _MessageApi(str(source))
            event = _Event({"type": "reply", "data": {"id": "42"}}, api)
            resolver = NapCatImageResolver(Path(tmp) / "cache")

            result = asyncio.run(
                resolver.resolve_candidate(
                    event,
                    {
                        "message_id": "current",
                        "reply_to_message_id": "42",
                        "candidate_refs": ["onebot-message://42"],
                    },
                )
            )

            self.assertEqual(len(result.images), 1)
            self.assertEqual(result.images[0].strategy, "get_msg")
            self.assertEqual(api.calls[-1], ("get_msg", {"message_id": 42}))

    def test_candidate_without_reference_records_no_reference(self):
        from astrmai.multimodal.napcat_image_resolver import NapCatImageResolver

        with tempfile.TemporaryDirectory() as tmp:
            event = _Event({"type": "text", "data": {"text": "current"}}, _Api({}))
            resolver = NapCatImageResolver(Path(tmp) / "cache")

            result = asyncio.run(resolver.resolve_candidate(event, {}))

            self.assertEqual(result.images, [])
            self.assertEqual(result.failure_details, [{"index": 0, "reason": "no_reference"}])

    def test_candidate_with_reference_and_missing_api_is_not_reported_as_no_reference(self):
        from astrmai.multimodal.napcat_image_resolver import NapCatImageResolver

        with tempfile.TemporaryDirectory() as tmp:
            event = _Event({"type": "text", "data": {"text": "current"}}, _Api({}))
            event.bot.api = object()
            resolver = NapCatImageResolver(Path(tmp) / "cache")

            result = asyncio.run(
                resolver.resolve_candidate(
                    event,
                    {
                        "message_id": "fake-message-api-missing",
                        "candidate_refs": ["fake-image-reference"],
                    },
                )
            )

            reasons = [item["reason"] for item in result.failure_details]
            self.assertNotIn("no_reference", reasons)
            self.assertEqual(
                reasons,
                ["get_image_failed", "get_file_failed", "get_msg_failed"],
            )

    def test_failed_fallback_chain_records_each_structured_reason_in_order(self):
        from astrmai.multimodal.napcat_image_resolver import NapCatImageResolver

        class _FailingApi:
            def __init__(self):
                self.calls = []

            async def call_action(self, action, **params):
                self.calls.append((action, params))
                raise RuntimeError(f"{action} unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            api = _FailingApi()
            event = _Event({"type": "text", "data": {"text": "current"}}, api)
            resolver = NapCatImageResolver(Path(tmp) / "cache")

            result = asyncio.run(
                resolver.resolve_candidate(
                    event,
                    {
                        "message_id": "fake-message-1",
                        "candidate_refs": ["fake-image-reference"],
                    },
                )
            )

            reasons = [item["reason"] for item in result.failure_details]
            self.assertEqual(
                reasons,
                ["get_image_failed", "get_file_failed", "get_msg_failed"],
            )
            self.assertEqual(
                [call[0] for call in api.calls],
                ["get_image", "get_image", "get_file", "get_file", "get_msg"],
            )

    def test_failed_http_materialization_records_download_failed(self):
        from astrmai.multimodal.napcat_image_resolver import NapCatImageResolver

        with tempfile.TemporaryDirectory() as tmp:
            event = _Event({"type": "text", "data": {"text": "current"}}, _Api({}))
            resolver = NapCatImageResolver(Path(tmp) / "cache")
            resolver._download_to_cache = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("synthetic download failure")
            )

            result = asyncio.run(
                resolver.resolve_candidate(
                    event,
                    {
                        "message_id": "fake-message-2",
                        "candidate_refs": ["https://invalid.example/fake-image.png"],
                    },
                )
            )

            self.assertIn("download_failed", [item["reason"] for item in result.failure_details])

    def test_get_msg_cq_string_degrades_without_regex_parsing(self):
        from astrmai.multimodal.napcat_image_resolver import NapCatImageResolver

        class _CQStringApi:
            async def call_action(self, action, **params):
                if action == "get_msg":
                    return {
                        "data": {
                            "message": "[CQ:image,file=fake-image.jpg,url=https://invalid.example/fake.jpg]"
                        }
                    }
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            event = _Event(
                {"type": "text", "data": {"text": "current"}},
                _CQStringApi(),
            )
            resolver = NapCatImageResolver(Path(tmp) / "cache")

            result = asyncio.run(
                resolver.resolve_candidate(
                    event,
                    {
                        "reply_to_message_id": "fake-message-3",
                        "candidate_refs": ["onebot-message://fake-message-3"],
                    },
                )
            )

            self.assertIn(
                {"index": 0, "reason": "get_msg_failed", "detail": "unsupported_cq_string"},
                result.failure_details,
            )

    def test_local_gif_with_jpg_suffix_is_cached_with_detected_suffix(self):
        from astrmai.multimodal.napcat_image_resolver import NapCatImageResolver

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "qq-animation.jpg"
            source.write_bytes(self._gif_bytes())
            api = _Api({})
            event = _Event({"type": "image", "data": {"path": str(source)}}, api)
            resolver = NapCatImageResolver(Path(tmp) / "cache")

            result = asyncio.run(resolver.resolve_event_images(event))

            self.assertEqual(len(result.images), 1)
            resolved = Path(result.images[0].local_path)
            self.assertEqual(resolved.suffix, ".gif")
            with Image.open(resolved) as image:
                self.assertEqual(image.format, "GIF")
                self.assertGreater(image.n_frames, 1)


if __name__ == "__main__":
    unittest.main()
