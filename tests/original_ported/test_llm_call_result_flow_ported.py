import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.original_ported.helpers import _FakeConversationManager
from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeResponse:
    def __init__(self, text="结构化结果"):
        self.completion_text = text
        self.usage = SimpleNamespace(input=10, input_cached=0, output=4)


class _FakeContext:
    async def llm_generate(self, **kwargs):
        return _FakeResponse()


class LLMCallResultFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.runtime.lane_manager", None)
        sys.modules.pop("astrmai.infrastructure.gateway.model_gateway", None)
        self.lane_mod = importlib.import_module("astrmai.infrastructure.runtime.lane_manager")
        self.lane_mod = importlib.reload(self.lane_mod)
        self.gateway_mod = importlib.import_module("astrmai.infrastructure.gateway.model_gateway")
        self.gateway_mod = importlib.reload(self.gateway_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_chat_in_lane_result_returns_structured_result(self):
        gateway = self.gateway_mod.GlobalModelGateway(
            _FakeContext(),
            SimpleNamespace(
                infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
                provider=SimpleNamespace(fallback_models=[]),
                global_settings=SimpleNamespace(debug_mode=False),
            ),
        )
        lane_manager = self.lane_mod.LaneManager(_FakeConversationManager())
        gateway.set_lane_manager(lane_manager)
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")

        async def _run():
            return await gateway.chat_in_lane_result(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prompt="hello",
                system_prompt="system",
                models=["model-a"],
                prefix_hash="hash-1",
                use_fallback=False,
                raw_user_text="[Alice] 说: hello",
            )

        result = asyncio.run(_run())
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "结构化结果")


if __name__ == "__main__":
    unittest.main()
