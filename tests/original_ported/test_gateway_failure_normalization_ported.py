import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.original_ported.helpers import _FakeConversationManager
from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeResponse:
    def __init__(self, text):
        self.completion_text = text
        self.usage = SimpleNamespace(input=10, input_cached=0, output=0)


class _FailureContext:
    async def llm_generate(self, **kwargs):
        return _FakeResponse(
            "JSON 响应: {\"candidates\": [{\"finishReason\": \"SAFETY\"}]}\n"
            "HTTP 状态码: 200\n"
            "(request_id: 20260410000000)"
        )


class _ScaffoldContext:
    def __init__(self):
        self.calls = []

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse("assistant: 呜……\n不要难过，亚托莉抱抱你！")


class GatewayFailureNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        datamodels_mod = sys.modules.get("astrmai.infra.datamodels")
        if datamodels_mod is not None and not hasattr(datamodels_mod, "ExpressionPattern"):
            for name in ("astrmai.infra.datamodels", "astrmai.infra.database", "astrmai.infra"):
                sys.modules.pop(name, None)
        sys.modules.pop("astrmai.infrastructure.runtime.lane_manager", None)
        sys.modules.pop("astrmai.infrastructure.gateway.model_gateway", None)
        self.lane_mod = importlib.import_module("astrmai.infrastructure.runtime.lane_manager")
        self.gateway_mod = importlib.import_module("astrmai.infrastructure.gateway.model_gateway")
        self.lane_mod = importlib.reload(self.lane_mod)
        self.gateway_mod = importlib.reload(self.gateway_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_provider_failure_payload_raises_cascade_failure(self):
        gateway = self.gateway_mod.GlobalModelGateway(
            _FailureContext(),
            SimpleNamespace(
                infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
                provider=SimpleNamespace(fallback_models=[]),
            ),
        )

        async def _run():
            await gateway._elastic_call(
                pool_name="task",
                prompt="hello",
                system_prompt="system",
                models=["model-a"],
                use_fallback=False,
            )

        try:
            asyncio.run(_run())
        except Exception as exc:
            self.assertEqual(exc.__class__.__name__, "LLMCascadeFailureException")
            self.assertEqual(exc.last_failure_kind, "provider_failure_text")
            self.assertEqual(exc.attempted_models, ["model-a"])
            self.assertIn("request_id", exc.raw_completion.lower().replace(" ", "_"))
        else:
            self.fail("expected LLMCascadeFailureException")

    def test_lane_history_persists_sanitized_assistant_text(self):
        fake_context = _ScaffoldContext()
        gateway = self.gateway_mod.GlobalModelGateway(
            fake_context,
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
            return await gateway.chat_in_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prompt="hello",
                system_prompt="stable prompt",
                models=["model-a"],
                prefix_hash="hash-1",
                use_fallback=False,
                raw_user_text="[Alice] 说: hello",
            )

        reply_text = asyncio.run(_run())
        lane_umo = lane_manager.resolve_lane_umo("default:GroupMessage:group-1", lane_key)
        conversation_id = asyncio.run(lane_manager.conversation_manager.get_curr_conversation_id(lane_umo))
        conversation = asyncio.run(lane_manager.conversation_manager.get_conversation(lane_umo, conversation_id))

        self.assertEqual(reply_text, "呜……\n不要难过，亚托莉抱抱你！")
        self.assertEqual(conversation.history[-1]["content"], "呜……\n不要难过，亚托莉抱抱你！")


if __name__ == "__main__":
    unittest.main()
