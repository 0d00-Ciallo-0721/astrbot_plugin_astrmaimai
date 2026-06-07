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
    def __init__(self, vision_result):
        self.calls = []
        self.vision_result = vision_result
        self.config = SimpleNamespace(
            agent=SimpleNamespace(max_steps=5, timeout=10),
            infra=SimpleNamespace(api_timeout=15),
            global_settings=SimpleNamespace(debug_mode=False, enable_error_interception=False, admin_ids=[]),
            reply=SimpleNamespace(fallback_text="fallback"),
            vision=SimpleNamespace(),
        )

    async def call_vision_task(self, **kwargs):
        self.calls.append(("vision", kwargs))
        if callable(self.vision_result):
            return self.vision_result(kwargs)
        return self.vision_result


class _FakeReplyService:
    async def handle_reply(self, event, text, chat_id):
        return None


class _FakeEvolution:
    async def process_bot_reply(self, chat_id, bot_id, reply_text):
        return None


class _FakeEvent:
    def __init__(self):
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self.message_str = "hello"
        self.message_obj = None
        self._extra = {}

    def get_self_id(self):
        return "bot-1"

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class RefactoredExecutorVisionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        install_executor_stubs()
        api_all_mod = types.ModuleType("astrbot.api.all")
        api_all_mod.Context = type("Context", (), {})
        sys.modules["astrbot.api.all"] = api_all_mod
        sys.modules.pop("astrmai.conversation.execution.executor", None)
        sys.modules.pop("astrmai.infrastructure.gateway.gateway_exceptions", None)
        executor_mod = importlib.import_module("astrmai.conversation.execution.executor")
        gateway_exc_mod = importlib.import_module("astrmai.infrastructure.gateway.gateway_exceptions")
        self.executor_mod = importlib.reload(executor_mod)
        self.gateway_exc_mod = importlib.reload(gateway_exc_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _executor(self, vision_result):
        gateway = _FakeGateway(vision_result=vision_result)
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=_FakeReplyService(),
            evolution_manager=_FakeEvolution(),
            config=gateway.config,
        )
        return executor, gateway

    def _vision_bundle(self, image_path):
        return self.executor_mod.VisionBundle(
            image_urls=[image_path],
            direct_image_urls=[image_path],
            is_direct_request=True,
            is_image_only=True,
            source="event_extra",
        )

    def test_invalid_provider_like_vision_output_is_rejected(self):
        executor, _gateway = self._executor(
            {"description": "request id: 1\nstatus code: 500", "emotion_tags": ["oops"]}
        )
        event = _FakeEvent()
        temp_image = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        temp_image.close()

        async def _run():
            return await executor._inject_direct_vision_context(
                event,
                "default:GroupMessage:group-1",
                "prompt",
                "system",
                self._vision_bundle(temp_image.name),
            )

        try:
            model_prompt, system_prompt = asyncio.run(_run())
        finally:
            try:
                os.remove(temp_image.name)
            except OSError:
                pass

        self.assertEqual(model_prompt, "prompt")
        self.assertEqual(system_prompt, "system")
        self.assertTrue(event.get_extra("vision_direct_invoked"))
        self.assertEqual(event.get_extra("vision_direct_outcome"), "invalid_output")

    def test_invalid_tags_are_dropped_but_description_is_kept(self):
        executor, _gateway = self._executor(
            {"description": "涓€鍙畨闈欑殑鐚?", "emotion_tags": 42}
        )
        event = _FakeEvent()
        temp_image = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        temp_image.close()

        async def _run():
            return await executor._inject_direct_vision_context(
                event,
                "default:GroupMessage:group-1",
                "prompt",
                "system",
                self._vision_bundle(temp_image.name),
            )

        try:
            model_prompt, _system_prompt = asyncio.run(_run())
        finally:
            try:
                os.remove(temp_image.name)
            except OSError:
                pass

        self.assertIn("涓€鍙畨闈欑殑鐚?", model_prompt)
        self.assertNotIn("42", model_prompt)
        self.assertEqual(event.get_extra("vision_direct_outcome"), "success")

    def test_no_direct_vision_urls_marks_skip_reason(self):
        executor, _gateway = self._executor({"description": "unused", "emotion_tags": []})
        event = _FakeEvent()
        event.set_extra("vision_direct_skip_reason", "probability_gate")

        async def _run():
            return await executor._inject_direct_vision_context(
                event,
                "default:GroupMessage:group-1",
                "prompt",
                "system",
                self.executor_mod.VisionBundle(
                    image_urls=[],
                    direct_image_urls=[],
                    is_direct_request=False,
                    is_image_only=False,
                    source="event_extra",
                ),
            )

        model_prompt, system_prompt = asyncio.run(_run())

        self.assertEqual(model_prompt, "prompt")
        self.assertEqual(system_prompt, "system")
        self.assertFalse(event.get_extra("vision_direct_invoked"))
        self.assertEqual(event.get_extra("vision_direct_outcome"), "skipped")
        self.assertEqual(event.get_extra("vision_direct_skip_reason"), "probability_gate")

    def test_vision_failure_keeps_attempted_models_metadata(self):
        failure = self.gateway_exc_mod.LLMCascadeFailureException(
            "vision model pool exhausted: empty_description",
            pool_name="vision",
            last_failure_kind="unknown",
            attempted_models=["vision-a", "vision-b"],
            model_id="vision-b",
            failure_reason="empty_description",
        )
        executor, gateway = self._executor(lambda _kwargs: (_ for _ in ()).throw(failure))
        event = _FakeEvent()
        temp_image = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        temp_image.close()

        async def _run():
            return await executor._inject_direct_vision_context(
                event,
                "default:GroupMessage:group-1",
                "prompt",
                "system",
                self._vision_bundle(temp_image.name),
            )

        try:
            model_prompt, system_prompt = asyncio.run(_run())
        finally:
            try:
                os.remove(temp_image.name)
            except OSError:
                pass

        self.assertEqual(model_prompt, "prompt")
        self.assertEqual(system_prompt, "system")
        self.assertEqual(event.get_extra("vision_direct_outcome"), "exception")
        self.assertEqual(event.get_extra("vision_direct_attempted_models"), ["vision-a", "vision-b"])
        self.assertEqual(event.get_extra("vision_direct_failure_reason"), "empty_description")
        self.assertEqual(len(gateway.calls), 1)

    def test_remote_image_ref_is_ignored_when_remote_fetching_is_disabled(self):
        executor, _gateway = self._executor({"description": "cat", "emotion_tags": []})
        event = _FakeEvent()

        async def _run():
            return await executor._inject_direct_vision_context(
                event,
                "default:GroupMessage:group-1",
                "prompt",
                "system",
                self.executor_mod.VisionBundle(
                    image_urls=["https://assets.example.com/cat.jpg"],
                    direct_image_urls=["https://assets.example.com/cat.jpg"],
                    is_direct_request=True,
                    is_image_only=True,
                    source="event_extra",
                ),
            )

        model_prompt, system_prompt = asyncio.run(_run())

        self.assertEqual(model_prompt, "prompt")
        self.assertEqual(system_prompt, "system")
        self.assertEqual(event.get_extra("vision_direct_outcome"), "exception")


if __name__ == "__main__":
    unittest.main()
