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
    def __init__(self, vision_result, *, vision_policy="超时后忽略图片并继续回复"):
        self.calls = []
        self.vision_result = vision_result
        self.config = SimpleNamespace(
            agent=SimpleNamespace(max_steps=5, timeout=10),
            infra=SimpleNamespace(api_timeout=15),
            global_settings=SimpleNamespace(debug_mode=False, enable_error_interception=False, admin_ids=[]),
            reply=SimpleNamespace(fallback_text="fallback"),
            vision=SimpleNamespace(vision_reply_policy=vision_policy),
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


class _FakeVisualCortex:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def analyze_image_path(self, picid, image_path, scope_id="global", timeout_override=None):
        self.calls.append(
            {
                "picid": picid,
                "image_path": image_path,
                "scope_id": scope_id,
                "timeout_override": timeout_override,
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


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

    def _executor(self, vision_result, *, visual_cortex=None, vision_policy="超时后忽略图片并继续回复"):
        gateway = _FakeGateway(vision_result=vision_result, vision_policy=vision_policy)
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=_FakeReplyService(),
            evolution_manager=_FakeEvolution(),
            config=gateway.config,
            visual_cortex=visual_cortex,
        )
        return executor, gateway

    def _vision_bundle(self, image_path, *, is_image_only=True):
        return self.executor_mod.VisionBundle(
            image_urls=[image_path],
            direct_image_urls=[image_path],
            is_direct_request=True,
            is_image_only=is_image_only,
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

        self.assertEqual(model_prompt, "prompt\n\n[图片]")
        self.assertTrue(system_prompt.startswith("system"))
        self.assertIn("不要反复说明图片加载", system_prompt)
        self.assertTrue(event.get_extra("vision_direct_invoked"))
        self.assertEqual(event.get_extra("vision_direct_outcome"), "invalid_output")
        self.assertTrue(event.get_extra("astrmai_vision_observability")["vision_fallback"])

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

    def test_direct_vision_uses_shared_prompt_and_emoji_rendering(self):
        executor, gateway = self._executor(
            {"type": "emoji", "description": "熊猫头低着头，文字为“我太难了”。通常用于自我调侃。", "emotion_tags": ["无奈", "自嘲"]}
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

        self.assertIn("[表情包转述：熊猫头低着头", model_prompt)
        self.assertIn("传达情绪：无奈、自嘲", model_prompt)
        self.assertIn("分析当前图片", gateway.calls[0][1]["prompt"])
        self.assertIn("聊天系统中的视觉转述模块", gateway.calls[0][1]["system_prompt"])
        self.assertEqual(event.get_extra("vision_direct_outcome"), "success")

    def test_direct_vision_reuses_visual_cortex_and_records_persistence_observation(self):
        visual_cortex = _FakeVisualCortex(
            {
                "type": "image",
                "description": "桌上放着一杯焦糖布丁。",
                "emotion_tags": ["开心"],
                "_cache_hit": False,
            }
        )
        executor, gateway = self._executor({}, visual_cortex=visual_cortex)
        event = _FakeEvent()
        temp_image = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        temp_image.close()

        try:
            model_prompt, _system_prompt = asyncio.run(
                executor._inject_direct_vision_context(
                    event,
                    "default:GroupMessage:group-1",
                    "prompt",
                    "system",
                    self._vision_bundle(temp_image.name),
                )
            )
        finally:
            try:
                os.remove(temp_image.name)
            except OSError:
                pass

        self.assertIn("焦糖布丁", model_prompt)
        self.assertEqual(gateway.calls, [])
        self.assertEqual(len(visual_cortex.calls), 1)
        observation = event.get_extra("astrmai_vision_observability")
        self.assertEqual(observation["vision_path"], "direct")
        self.assertEqual(observation["visual_memory_write_status"], "persisted_or_cache_hit")
        self.assertTrue(observation["visual_memory_ids"])
        self.assertTrue(observation["prompt_injected"])

    def test_direct_vision_timeout_fallback_injects_image_placeholder(self):
        visual_cortex = _FakeVisualCortex(asyncio.TimeoutError())
        executor, _gateway = self._executor({}, visual_cortex=visual_cortex)
        event = _FakeEvent()
        temp_image = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        temp_image.close()

        try:
            model_prompt, _system_prompt = asyncio.run(
                executor._inject_direct_vision_context(
                    event,
                    "default:GroupMessage:group-1",
                    "prompt",
                    "system",
                    self._vision_bundle(temp_image.name),
                )
            )
        finally:
            try:
                os.remove(temp_image.name)
            except OSError:
                pass

        self.assertEqual(model_prompt, "prompt\n\n[图片]")
        observation = event.get_extra("astrmai_vision_observability")
        self.assertEqual(observation["outcome"], "fallback")
        self.assertTrue(observation["vision_fallback"])
        self.assertEqual(observation["fallback_reason"], "image_only_placeholder")
        self.assertTrue(event.get_extra("astrmai_media_status_nonsemantic"))
        self.assertTrue(event.get_extra("astrmai_media_only_failure"))
        self.assertTrue(event.get_extra("astrmai_media_status")["image_only"])

    def test_direct_vision_text_message_continues_without_image_failure_topic(self):
        visual_cortex = _FakeVisualCortex(asyncio.TimeoutError())
        executor, _gateway = self._executor({}, visual_cortex=visual_cortex)
        event = _FakeEvent()
        temp_image = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        temp_image.close()

        try:
            model_prompt, system_prompt = asyncio.run(
                executor._inject_direct_vision_context(
                    event,
                    "default:GroupMessage:group-1",
                    "用户同时发送的文字",
                    "system",
                    self._vision_bundle(temp_image.name, is_image_only=False),
                )
            )
        finally:
            try:
                os.remove(temp_image.name)
            except OSError:
                pass

        self.assertEqual(model_prompt, "用户同时发送的文字")
        self.assertIn("媒体状态约束", system_prompt)
        self.assertIn("不要反复说明图片加载", system_prompt)
        self.assertTrue(event.get_extra("astrmai_media_status_nonsemantic"))
        self.assertFalse(event.get_extra("astrmai_media_only_failure"))
        self.assertFalse(event.get_extra("astrmai_media_status")["image_only"])
        observation = event.get_extra("astrmai_vision_observability")
        self.assertEqual(observation["fallback_reason"], "text_only_continue")
        self.assertFalse(observation["prompt_injected"])

    def test_direct_vision_strict_policy_stops_placeholder_fallback(self):
        visual_cortex = _FakeVisualCortex(RuntimeError("vision unavailable"))
        executor, _gateway = self._executor(
            {},
            visual_cortex=visual_cortex,
            vision_policy="必须识别成功后再回复",
        )
        event = _FakeEvent()
        temp_image = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        temp_image.close()

        try:
            model_prompt, _system_prompt = asyncio.run(
                executor._inject_direct_vision_context(
                    event,
                    "default:GroupMessage:group-1",
                    "prompt",
                    "system",
                    self._vision_bundle(temp_image.name),
                )
            )
        finally:
            try:
                os.remove(temp_image.name)
            except OSError:
                pass

        self.assertEqual(model_prompt, "prompt")
        self.assertTrue(event.get_extra("astrmai_vision_required_failed"))
        observation = event.get_extra("astrmai_vision_observability")
        self.assertEqual(observation["outcome"], "required_failed")
        self.assertFalse(observation["vision_fallback"])

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

        self.assertEqual(model_prompt, "prompt\n\n[图片]")
        self.assertTrue(system_prompt.startswith("system"))
        self.assertIn("不要反复说明图片加载", system_prompt)
        self.assertEqual(event.get_extra("vision_direct_outcome"), "exception")
        self.assertEqual(event.get_extra("vision_direct_attempted_models"), ["vision-a", "vision-b"])
        self.assertEqual(event.get_extra("vision_direct_failure_reason"), "empty_description")
        self.assertTrue(event.get_extra("astrmai_vision_observability")["vision_fallback"])
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

        self.assertEqual(model_prompt, "prompt\n\n[图片]")
        self.assertTrue(system_prompt.startswith("system"))
        self.assertIn("不要反复说明图片加载", system_prompt)
        self.assertEqual(event.get_extra("vision_direct_outcome"), "exception")
        observation = event.get_extra("astrmai_vision_observability")
        self.assertEqual(observation["resolved_count"], 0)
        self.assertTrue(observation["vision_fallback"])


if __name__ == "__main__":
    unittest.main()
