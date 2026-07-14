import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.original_ported.helpers import _FakeConversationManager
from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeResponse:
    def __init__(self, text="ok"):
        self.completion_text = text
        self.usage = SimpleNamespace(input=8, input_cached=4, output=3)


class _FakeContext:
    def __init__(self):
        self.calls = []
        self.fail_models = set()

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("chat_provider_id") in self.fail_models:
            raise RuntimeError("simulated provider failure")
        return _FakeResponse()

    def get_provider_by_id(self, provider_id):
        provider_types = {
            "claude-3-5-sonnet": "anthropic",
            "dify-agent": "dify",
        }
        provider_type = provider_types.get(provider_id)
        if not provider_type:
            return None
        return SimpleNamespace(meta=lambda: SimpleNamespace(type=provider_type))


class GatewayLaneRequestKwargsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.runtime.lane_manager", None)
        sys.modules.pop("astrmai.infrastructure.gateway.model_gateway", None)
        sys.modules.pop("astrmai.infra.provider_capabilities", None)
        self.lane_mod = importlib.import_module("astrmai.infrastructure.runtime.lane_manager")
        self.gateway_mod = importlib.import_module("astrmai.infrastructure.gateway.model_gateway")
        self.lane_mod = importlib.reload(self.lane_mod)
        self.gateway_mod = importlib.reload(self.gateway_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_claude_lane_request_adds_cache_control(self):
        fake_context = _FakeContext()
        config = SimpleNamespace(
            infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
            provider=SimpleNamespace(fallback_models=[]),
        )
        gateway = self.gateway_mod.GlobalModelGateway(fake_context, config)
        gateway.set_lane_manager(self.lane_mod.LaneManager(_FakeConversationManager()))
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")

        async def _run():
            await gateway.chat_in_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prompt="hello",
                system_prompt="stable prompt",
                models=["claude-3-5-sonnet"],
                prefix_hash="hash-1",
                use_fallback=False,
            )

        asyncio.run(_run())

        self.assertEqual(fake_context.calls[0]["cache_control"], {"type": "ephemeral"})

    def test_runner_lane_request_adds_remote_session_id(self):
        fake_context = _FakeContext()
        config = SimpleNamespace(
            infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
            provider=SimpleNamespace(fallback_models=[]),
        )
        gateway = self.gateway_mod.GlobalModelGateway(fake_context, config)
        gateway.set_lane_manager(self.lane_mod.LaneManager(_FakeConversationManager()))
        lane_key = self.lane_mod.LaneKey(subsystem="bg", task_family="memory", scope_id="global", scope_kind="global")

        async def _run():
            await gateway.chat_in_lane(
                lane_key=lane_key,
                base_origin="",
                prompt="hello",
                system_prompt="stable prompt",
                models=["dify-agent"],
                prefix_hash="hash-1",
                use_fallback=False,
            )

        asyncio.run(_run())

        self.assertIn("session_id", fake_context.calls[0])
        session_id = str(fake_context.calls[0]["session_id"])
        self.assertIn("@@astrmai:bg:memory:", session_id)
        self.assertTrue(session_id.endswith("memory_global_summary:v1:text"))

    def test_fallback_model_recomputes_request_kwargs_from_actual_provider(self):
        fake_context = _FakeContext()
        fake_context.fail_models.add("claude-3-5-sonnet")
        config = SimpleNamespace(
            infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
            provider=SimpleNamespace(fallback_models=["dify-agent"]),
        )
        gateway = self.gateway_mod.GlobalModelGateway(fake_context, config)
        gateway.set_lane_manager(self.lane_mod.LaneManager(_FakeConversationManager()))
        lane_key = self.lane_mod.LaneKey(subsystem="bg", task_family="memory", scope_id="global", scope_kind="global")

        async def _run():
            await gateway.chat_in_lane(
                lane_key=lane_key,
                base_origin="",
                prompt="hello",
                system_prompt="stable prompt",
                models=["claude-3-5-sonnet"],
                prefix_hash="hash-1",
                use_fallback=True,
            )

        asyncio.run(_run())

        self.assertEqual(fake_context.calls[0]["chat_provider_id"], "claude-3-5-sonnet")
        self.assertEqual(fake_context.calls[0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(fake_context.calls[1]["chat_provider_id"], "dify-agent")
        self.assertIn("session_id", fake_context.calls[1])
        self.assertNotIn("cache_control", fake_context.calls[1])


if __name__ == "__main__":
    unittest.main()
