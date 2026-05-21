import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _VisionResponse:
    def __init__(self, payload):
        self.completion_text = payload
        self.usage = SimpleNamespace(input=10, input_cached=0, output=4)


class _VisionContext:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        return _VisionResponse(self.responses.pop(0))


class GatewayVisionRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.gateway.model_gateway", None)
        gateway_mod = importlib.import_module("astrmai.infrastructure.gateway.model_gateway")
        self.gateway_mod = importlib.reload(gateway_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_call_vision_task_retries_within_vision_pool_only(self):
        context = _VisionContext(
            [
                '{"description": "", "emotion_tags": []}',
                '{"description": "a cat on the desk", "emotion_tags": ["calm"]}',
            ]
        )
        gateway = self.gateway_mod.GlobalModelGateway(
            context,
            SimpleNamespace(
                infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
                provider=SimpleNamespace(fallback_models=["fallback-a"]),
            ),
            settings=SimpleNamespace(
                fallback_models=["fallback-a"],
                task_models=[],
                agent_models=[],
                vision_models=["vision-a", "vision-b"],
                llm_retries=0,
                backoff_factor=1.5,
                api_timeout=10,
                max_concurrent_llm_calls=2,
                debug_mode=False,
            ),
        )

        async def _run():
            return await gateway.call_vision_task(
                image_data="image.png",
                prompt="Analyze",
                system_prompt="Return JSON",
            )

        result = asyncio.run(_run())

        self.assertEqual(result["description"], "a cat on the desk")
        self.assertEqual([call["chat_provider_id"] for call in context.calls], ["vision-a", "vision-b"])


if __name__ == "__main__":
    unittest.main()
