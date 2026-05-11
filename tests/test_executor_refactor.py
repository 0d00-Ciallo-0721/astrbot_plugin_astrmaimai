import asyncio
import importlib
import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs
from tests.helpers.executor_stubs import install_executor_stubs


class _FakeGateway:
    def __init__(self):
        self.calls = []
        self.config = SimpleNamespace(
            agent=SimpleNamespace(max_steps=5, timeout=10),
            infra=SimpleNamespace(api_timeout=15),
            global_settings=SimpleNamespace(debug_mode=False, enable_error_interception=False, admin_ids=[]),
            reply=SimpleNamespace(fallback_text="fallback"),
        )

    def get_agent_models(self):
        return ["model-a"]

    async def chat_in_lane_result(self, **kwargs):
        self.calls.append(("chat", kwargs))
        return SimpleNamespace(text="lane-text-reply")

    async def tool_chat_in_lane_result(self, **kwargs):
        self.calls.append(("tool", kwargs))
        return SimpleNamespace(text="[TERMINAL_YIELD]: tool-finished")

    async def call_vision_task(self, **kwargs):
        self.calls.append(("vision", kwargs))
        return {
            "description": "一只在窗边打盹的猫",
            "emotion_tags": ["安静", "柔软"],
        }


class _FakeReplyService:
    def __init__(self):
        self.calls = []

    async def handle_reply(self, event, text, chat_id):
        self.calls.append((chat_id, text))


class _FakeEvolution:
    def __init__(self):
        self.calls = []

    async def process_bot_reply(self, chat_id, bot_id, reply_text):
        self.calls.append((chat_id, bot_id, reply_text))


class _FakeEvent:
    def __init__(self):
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self.message_str = "hello"
        self.message_obj = None
        self._extra = {"astrmai_prefix_hash": "hash-1"}

    def get_self_id(self):
        return "bot-1"

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class RefactoredExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        install_executor_stubs()
        api_all_mod = types.ModuleType("astrbot.api.all")
        api_all_mod.Context = type("Context", (), {})
        sys.modules["astrbot.api.all"] = api_all_mod
        sys.modules.pop("astrmai.conversation.execution.executor", None)
        executor_mod = importlib.import_module("astrmai.conversation.execution.executor")
        self.executor_mod = importlib.reload(executor_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_text_mode_runs_on_dialog_lane_and_records_reply(self):
        gateway = _FakeGateway()
        reply_service = _FakeReplyService()
        evolution = _FakeEvolution()
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=reply_service,
            evolution_manager=evolution,
            config=gateway.config,
        )

        async def _run():
            return await executor.execute(_FakeEvent(), "prompt", "system")

        result = asyncio.run(_run())

        self.assertEqual(result, "lane-text-reply")
        self.assertEqual(len(gateway.calls), 1)
        mode, kwargs = gateway.calls[0]
        self.assertEqual(mode, "chat")
        self.assertEqual(kwargs["lane_key"].task_family, "dialog")
        self.assertEqual(kwargs["base_origin"], "default:GroupMessage:group-1")
        self.assertEqual(reply_service.calls, [("default:GroupMessage:group-1", "lane-text-reply")])
        self.assertEqual(
            evolution.calls,
            [("default:GroupMessage:group-1", "bot-1", "lane-text-reply")],
        )

    def test_tool_mode_yield_is_forwarded_as_terminal_content(self):
        gateway = _FakeGateway()
        reply_service = _FakeReplyService()
        evolution = _FakeEvolution()
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=reply_service,
            evolution_manager=evolution,
            config=gateway.config,
        )

        async def _run():
            return await executor.execute(_FakeEvent(), "prompt", "system", tools=[object()])

        result = asyncio.run(_run())

        self.assertEqual(result, "tool-finished")
        self.assertEqual(len(gateway.calls), 1)
        mode, _kwargs = gateway.calls[0]
        self.assertEqual(mode, "tool")
        self.assertEqual(reply_service.calls, [("default:GroupMessage:group-1", "tool-finished")])
        self.assertEqual(
            evolution.calls,
            [("default:GroupMessage:group-1", "bot-1", "tool-finished")],
        )

    def test_chat_tool_tier_limits_runtime_max_steps(self):
        gateway = _FakeGateway()
        gateway.config.agent.max_steps = 8
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=_FakeReplyService(),
            evolution_manager=_FakeEvolution(),
            config=gateway.config,
        )
        event = _FakeEvent()
        event.set_extra("astrmai_tool_tier", "chat")

        runtime = executor._execution_runtime_values(event, event.unified_msg_origin)

        self.assertEqual(runtime["tool_tier"], "chat")
        self.assertEqual(runtime["max_steps"], 2)

    def test_full_and_sys3_tool_tiers_keep_existing_max_steps_rule(self):
        gateway = _FakeGateway()
        gateway.config.agent.max_steps = 3
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=_FakeReplyService(),
            evolution_manager=_FakeEvolution(),
            config=gateway.config,
        )

        full_event = _FakeEvent()
        full_runtime = executor._execution_runtime_values(full_event, full_event.unified_msg_origin)
        self.assertEqual(full_runtime["tool_tier"], "full")
        self.assertEqual(full_runtime["max_steps"], 5)

        sys3_event = _FakeEvent()
        sys3_event.set_extra("astrmai_tool_tier", "sys3")
        sys3_runtime = executor._execution_runtime_values(sys3_event, sys3_event.unified_msg_origin)
        self.assertEqual(sys3_runtime["tool_tier"], "sys3")
        self.assertEqual(sys3_runtime["max_steps"], 5)

    def test_direct_vision_context_is_injected_in_first_person(self):
        gateway = _FakeGateway()
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=_FakeReplyService(),
            evolution_manager=_FakeEvolution(),
            config=gateway.config,
        )
        temp_image = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        temp_image.close()
        vision_bundle = self.executor_mod.VisionBundle(
            image_urls=[temp_image.name],
            direct_image_urls=[temp_image.name],
            is_direct_request=True,
            is_image_only=True,
            source="event_extra",
        )

        async def _run():
            return await executor._inject_direct_vision_context(
                _FakeEvent(),
                "default:GroupMessage:group-1",
                "prompt",
                "system",
                vision_bundle,
            )

        try:
            model_prompt, system_prompt = asyncio.run(_run())
        finally:
            try:
                os.remove(temp_image.name)
            except OSError:
                pass

        self.assertIn("我刚看到一张图片，画面是：一只在窗边打盹的猫。", model_prompt)
        self.assertIn("它给我的感觉是：安静, 柔软。", model_prompt)
        self.assertIn("我刚看到一张图片，画面是：一只在窗边打盹的猫。", system_prompt)
        self.assertNotIn("System note", model_prompt)
        self.assertNotIn("[Vision]", model_prompt)
        self.assertTrue(any(mode == "vision" for mode, _kwargs in gateway.calls))


if __name__ == "__main__":
    unittest.main()
