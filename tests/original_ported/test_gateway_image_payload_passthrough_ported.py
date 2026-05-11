import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeResponse:
    def __init__(self, text="图片看到了"):
        self.completion_text = text
        self.usage = SimpleNamespace(input=10, input_cached=0, output=4)


class _FakeContext:
    def __init__(self):
        self.calls = []

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse()


class GatewayImagePayloadPassthroughTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.gateway.model_gateway", None)
        self.gateway_mod = importlib.import_module("astrmai.infrastructure.gateway.model_gateway")
        self.gateway_mod = importlib.reload(self.gateway_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_image_urls_are_sent_via_kwarg_without_content_part_wrapping(self):
        fake_context = _FakeContext()
        gateway = self.gateway_mod.GlobalModelGateway(
            fake_context,
            SimpleNamespace(
                infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
                provider=SimpleNamespace(fallback_models=[]),
            ),
        )

        async def _run():
            return await gateway._elastic_call(
                pool_name="vision",
                prompt="帮我看看这张图",
                system_prompt="你是看图助手",
                models=["vision-model"],
                image_urls=["https://example.com/a.jpg"],
                use_fallback=False,
            )

        reply_text = asyncio.run(_run())

        self.assertEqual(reply_text, "图片看到了")
        self.assertEqual(fake_context.calls[0]["prompt"], "帮我看看这张图")
        self.assertEqual(fake_context.calls[0]["image_urls"], ["https://example.com/a.jpg"])


if __name__ == "__main__":
    unittest.main()
