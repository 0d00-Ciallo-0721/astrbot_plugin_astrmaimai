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
    def __init__(self, *, chat_responses=None, tool_responses=None, models=None):
        self.calls = []
        self.chat_responses = dict(chat_responses or {})
        self.tool_responses = dict(tool_responses or {})
        self.models = list(models or ["model-a"])
        self.config = SimpleNamespace(
            agent=SimpleNamespace(max_steps=5, timeout=10),
            infra=SimpleNamespace(api_timeout=15),
            global_settings=SimpleNamespace(debug_mode=False, enable_error_interception=False, admin_ids=[]),
            reply=SimpleNamespace(fallback_text="fallback"),
            vision=SimpleNamespace(
                enable_vision=True,
                image_recognition_probability=1.0,
                use_native_main_reply_vision=False,
                native_main_reply_failure_cooldown_sec=180,
            ),
        )

    def get_agent_models(self):
        return list(self.models)

    async def chat_in_lane_result(self, **kwargs):
        self.calls.append(("chat", kwargs))
        model_id = kwargs["models"][0]
        response = self.chat_responses.get(model_id, "lane-text-reply")
        if callable(response):
            response = response(kwargs)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(text=response)

    async def tool_chat_in_lane_result(self, **kwargs):
        self.calls.append(("tool", kwargs))
        model_id = kwargs["models"][0]
        response = self.tool_responses.get(model_id, "[TERMINAL_YIELD]: tool-finished")
        if callable(response):
            response = response(kwargs)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(text=response)

    async def call_vision_task(self, **kwargs):
        self.calls.append(("vision", kwargs))
        return {
            "description": "一只在窗边打盹的猫",
            "emotion_tags": ["安静", "柔软"],
        }


class _CooldownAwareFakeGateway(_FakeGateway):
    def __init__(self, *, skipped_models, **kwargs):
        super().__init__(**kwargs)
        self.skipped_models = list(skipped_models)

    def get_agent_models(self):
        self._last_agent_model_selection = {
            "skipped_cooldown_models": list(self.skipped_models),
            "cooldown_overridden": False,
        }
        skipped_ids = {item["model_id"] for item in self.skipped_models}
        return [model for model in self.models if model not in skipped_ids]


class _FakeReplyService:
    def __init__(self):
        self.calls = []

    async def handle_reply(self, event, text, chat_id):
        self.calls.append((chat_id, text))
        event.set_extra("astrmai_reply_sent", True)
        return SimpleNamespace(sent=True, blocked_reason="", persistable_text=text)


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

    def test_required_tool_outcome_distinguishes_satisfied_and_missing(self):
        event = _FakeEvent()
        event.set_extra("astrmai_required_tools", ["proactive_poke", "omni_perception_query"])
        event.set_extra("astrmai_prepared_required_tools", ["proactive_poke"])

        missing = self.executor_mod.ConcurrentExecutor._record_required_tool_outcomes(event)

        outcomes = [
            item
            for item in event.get_extra("astrmai_tool_lifecycle_trace")
            if item["phase"] == "required_tool_outcome"
        ]
        self.assertEqual(
            {(item["tool"], item["status"]) for item in outcomes},
            {("proactive_poke", "satisfied"), ("omni_perception_query", "missing")},
        )
        self.assertEqual(missing, ["omni_perception_query"])

    def test_tool_mode_retries_once_with_only_missing_required_tools(self):
        calls = 0

        def _tool_response(kwargs):
            nonlocal calls
            calls += 1
            event = kwargs["event"]
            if calls == 2:
                event.set_extra(
                    "astrmai_tool_execution_trace",
                    [{"tool_name": "omni_perception_query", "status": "success"}],
                )
                return "已经根据查询结果回答"
            return "未调用工具的普通回答"

        gateway = _FakeGateway(tool_responses={"model-a": _tool_response})
        reply_service = _FakeReplyService()
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=reply_service,
            evolution_manager=_FakeEvolution(),
            config=gateway.config,
        )
        event = _FakeEvent()
        event.set_extra("astrmai_required_tools", ["omni_perception_query"])
        tool = SimpleNamespace(name="omni_perception_query")

        result = asyncio.run(executor.execute(event, "prompt", "system", tools=[tool]))

        self.assertEqual(result, "已经根据查询结果回答")
        self.assertEqual(calls, 2)
        retry_call = gateway.calls[1][1]
        self.assertIn("SYSTEM TOOL ENFORCEMENT", retry_call["prompt"])
        lifecycle = event.get_extra("astrmai_tool_lifecycle_trace", [])
        self.assertTrue(any(item["phase"] == "required_tool_retry" for item in lifecycle))

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

    def test_tool_mode_wait_signal_sets_execution_signal_without_visible_reply(self):
        gateway = _FakeGateway(tool_responses={"model-a": "[SYSTEM_WAIT_SIGNAL]"})
        reply_service = _FakeReplyService()
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=reply_service,
            evolution_manager=_FakeEvolution(),
            config=gateway.config,
        )
        event = _FakeEvent()

        async def _run():
            return await executor.execute(event, "prompt", "system", tools=[object()])

        result = asyncio.run(_run())

        self.assertIsNone(result)
        self.assertEqual(event.get_extra("astrmai_execution_signal"), "wait")
        self.assertEqual(event.get_extra("astrmai_execution_status"), "skipped_wait")
        self.assertEqual(reply_service.calls, [])

    def test_tool_mode_can_expand_readonly_disclosure_package_once(self):
        call_count = {"value": 0}

        def _tool_response(kwargs):
            call_count["value"] += 1
            if call_count["value"] == 1:
                kwargs["event"].set_extra("astrmai_requested_tool_packages", ["identity"])
                return "[TERMINAL_YIELD]: need identity tools"
            tool_names = [getattr(tool, "name", "") for tool in kwargs["tools"].tools]
            self.assertIn("qq_friend_lookup", tool_names)
            self.assertIn("qq_user_identity_lookup", tool_names)
            self.assertEqual(kwargs["max_steps"], 7)
            return "[TERMINAL_YIELD]: expanded identity result"

        gateway = _FakeGateway(tool_responses={"model-a": _tool_response})
        gateway.config.agent.max_steps = 7
        gateway.config.conversation = SimpleNamespace(
            tool_disclosure_allow_second_pass=True,
            tool_disclosure_max_tools_task=16,
        )
        reply_service = _FakeReplyService()
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=reply_service,
            evolution_manager=_FakeEvolution(),
            config=gateway.config,
        )
        event = _FakeEvent()
        event.set_extra("astrmai_disclosure_second_pass_packages", ["identity"])
        setattr(
            event,
            "_astrmai_disclosure_hidden_tools",
            [
                SimpleNamespace(name="qq_friend_lookup"),
                SimpleNamespace(name="qq_user_identity_lookup"),
            ],
        )

        result = asyncio.run(executor.execute(event, "prompt", "system", tools=[SimpleNamespace(name="bot_capability_lookup")]))

        self.assertEqual(result, "expanded identity result")
        self.assertEqual(call_count["value"], 2)
        self.assertEqual(event.get_extra("astrmai_disclosure_expanded_packages"), ["identity"])

    def test_chat_tool_tier_uses_configured_multi_tool_max_steps(self):
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
        self.assertEqual(runtime["max_steps"], 8)

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
        self.assertEqual(system_prompt, "system")
        self.assertNotIn("System note", model_prompt)
        self.assertNotIn("[Vision]", model_prompt)
        self.assertTrue(any(mode == "vision" for mode, _kwargs in gateway.calls))

    def test_direct_vision_context_exception_does_not_delete_original_file(self):
        gateway = _FakeGateway()

        async def _raise_vision(**kwargs):
            raise RuntimeError("vision failed")

        gateway.call_vision_task = _raise_vision
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
            asyncio.run(_run())
            self.assertTrue(os.path.exists(temp_image.name))
        finally:
            try:
                os.remove(temp_image.name)
            except OSError:
                pass

    def test_text_mode_switches_model_on_prompt_scaffold_output_and_traces_failure(self):
        gateway = _FakeGateway(
            models=["model-a", "model-b"],
            chat_responses={
                "model-a": "[RollingSummary]",
                "model-b": "second-ok",
            },
        )
        reply_service = _FakeReplyService()
        evolution = _FakeEvolution()
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=reply_service,
            evolution_manager=evolution,
            config=gateway.config,
        )
        event = _FakeEvent()

        async def _run():
            return await executor.execute(event, "prompt", "system")

        result = asyncio.run(_run())

        self.assertEqual(result, "second-ok")
        self.assertEqual([kwargs["models"][0] for mode, kwargs in gateway.calls if mode == "chat"], ["model-a", "model-b"])
        trace_log = event.get_extra("astrmai_trace_log", [])
        failure_records = [record for record in trace_log if record.get("stage") == "execution.executor.model_failure"]
        self.assertTrue(failure_records)
        self.assertEqual(failure_records[0]["failure_kind"], "prompt_scaffold_text")

    def test_text_mode_uses_gateway_cooldown_filtered_agent_models(self):
        gateway = _CooldownAwareFakeGateway(
            models=["model-a", "model-b"],
            skipped_models=[
                {
                    "pool_name": "agent",
                    "model_id": "model-a",
                    "cooldown_reason": "quota_exhausted",
                    "cooldown_until": 123.0,
                }
            ],
            chat_responses={"model-b": "second-ok"},
        )
        reply_service = _FakeReplyService()
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=reply_service,
            evolution_manager=_FakeEvolution(),
            config=gateway.config,
        )
        event = _FakeEvent()

        async def _run():
            return await executor.execute(event, "prompt", "system")

        result = asyncio.run(_run())

        self.assertEqual(result, "second-ok")
        self.assertEqual([kwargs["models"][0] for mode, kwargs in gateway.calls if mode == "chat"], ["model-b"])

    def test_text_mode_records_pool_exhausted_summary_for_invalid_outputs(self):
        gateway = _FakeGateway(
            models=["model-a", "model-b"],
            chat_responses={
                "model-a": "request id: 1\nstatus code: 500",
                "model-b": "request id: 2\nstatus code: 502",
            },
        )
        reply_service = _FakeReplyService()
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=reply_service,
            evolution_manager=_FakeEvolution(),
            config=gateway.config,
        )
        event = _FakeEvent()

        async def _run():
            return await executor.execute(event, "prompt", "system")

        result = asyncio.run(_run())

        self.assertEqual(result, "fallback")
        self.assertEqual(event.get_extra("astrmai_execution_status"), "fallback_sent")
        self.assertEqual(reply_service.calls, [("default:GroupMessage:group-1", "fallback")])
        trace_log = event.get_extra("astrmai_trace_log", [])
        exhausted = [record for record in trace_log if record.get("stage") == "execution.executor.model_pool_exhausted"]
        self.assertTrue(exhausted)
        self.assertEqual(exhausted[0]["attempted_models"], ["model-a", "model-b"])
        self.assertEqual(exhausted[0]["last_failure_kind"], "provider_failure_text")
        self.assertTrue(exhausted[0]["fallback_triggered"])

    def test_fatal_fallback_skips_visible_reply_for_stale_drop(self):
        gateway = _FakeGateway()
        reply_service = _FakeReplyService()
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=reply_service,
            evolution_manager=_FakeEvolution(),
            config=gateway.config,
        )
        event = _FakeEvent()
        event.set_extra("astrmai_execution_status", "stale_drop")

        async def _run():
            await executor._handle_fatal_fallback(event, event.unified_msg_origin, "boom")

        asyncio.run(_run())

        self.assertEqual(reply_service.calls, [])

    def test_stale_reply_artifact_does_not_update_evolution(self):
        gateway = _FakeGateway()
        evolution = _FakeEvolution()

        class _StaleReplyService:
            async def handle_reply(self, event, text, chat_id):
                return SimpleNamespace(
                    sent=False,
                    blocked_reason="stale_generation",
                    metadata={"send_status": "failed"},
                )

        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=_StaleReplyService(),
            evolution_manager=evolution,
            config=gateway.config,
        )
        event = _FakeEvent()

        result = asyncio.run(executor.execute(event, "prompt", "system"))

        self.assertIsNone(result)
        self.assertEqual(event.get_extra("astrmai_execution_status"), "stale_drop")
        self.assertEqual(evolution.calls, [])

    def test_send_failure_retries_next_model_and_commits_only_successful_reply(self):
        gateway = _FakeGateway(
            models=["model-a", "model-b"],
            chat_responses={"model-a": "first", "model-b": "second"},
        )
        evolution = _FakeEvolution()

        class _RetryReplyService:
            def __init__(self):
                self.calls = []

            async def handle_reply(self, event, text, chat_id):
                self.calls.append(text)
                if text == "first":
                    raise RuntimeError("transport failed")
                event.set_extra("astrmai_reply_sent", True)
                return SimpleNamespace(sent=True, blocked_reason="", persistable_text=text, metadata={})

        reply_service = _RetryReplyService()
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=reply_service,
            evolution_manager=evolution,
            config=gateway.config,
        )
        event = _FakeEvent()

        result = asyncio.run(executor.execute(event, "prompt", "system"))

        self.assertEqual(result, "second")
        self.assertEqual(reply_service.calls, ["first", "second"])
        self.assertEqual(
            evolution.calls,
            [(event.unified_msg_origin, "bot-1", "second")],
        )

    def test_text_mode_pool_exhausted_trace_includes_gateway_cooldown_skips(self):
        gateway = _CooldownAwareFakeGateway(
            models=["model-a", "model-b"],
            skipped_models=[
                {
                    "pool_name": "agent",
                    "model_id": "model-a",
                    "cooldown_reason": "quota_exhausted",
                    "cooldown_until": 123.0,
                }
            ],
            chat_responses={"model-b": "request id: 2\nstatus code: 502"},
        )
        reply_service = _FakeReplyService()
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=reply_service,
            evolution_manager=_FakeEvolution(),
            config=gateway.config,
        )
        event = _FakeEvent()

        async def _run():
            return await executor.execute(event, "prompt", "system")

        result = asyncio.run(_run())

        self.assertEqual(result, "fallback")
        self.assertEqual(event.get_extra("astrmai_execution_status"), "fallback_sent")
        trace_log = event.get_extra("astrmai_trace_log", [])
        exhausted = [record for record in trace_log if record.get("stage") == "execution.executor.model_pool_exhausted"]
        self.assertEqual(exhausted[0]["attempted_models"], ["model-b"])
        self.assertEqual(exhausted[0]["skipped_cooldown_models"][0]["model_id"], "model-a")
        self.assertFalse(exhausted[0]["cooldown_overridden"])

    def test_text_mode_failure_trace_marks_last_attempt_as_no_switch(self):
        gateway = _FakeGateway(
            models=["model-a", "model-b"],
            chat_responses={
                "model-a": "[RollingSummary]",
                "model-b": "request id: 2\nstatus code: 502",
            },
        )
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=_FakeReplyService(),
            evolution_manager=_FakeEvolution(),
            config=gateway.config,
        )
        event = _FakeEvent()

        async def _run():
            return await executor.execute(event, "prompt", "system")

        result = asyncio.run(_run())

        self.assertEqual(result, "fallback")
        self.assertEqual(event.get_extra("astrmai_execution_status"), "fallback_sent")
        trace_log = event.get_extra("astrmai_trace_log", [])
        failure_records = [record for record in trace_log if record.get("stage") == "execution.executor.model_failure"]
        self.assertEqual(len(failure_records), 2)
        self.assertTrue(failure_records[0]["will_retry_or_switch"])
        self.assertFalse(failure_records[1]["will_retry_or_switch"])

    def test_native_main_reply_vision_text_mode_passes_direct_images_without_relay_injection(self):
        gateway = _FakeGateway()
        gateway.config.vision.use_native_main_reply_vision = True
        reply_service = _FakeReplyService()
        evolution = _FakeEvolution()
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=reply_service,
            evolution_manager=evolution,
            config=gateway.config,
        )
        event = _FakeEvent()
        temp_image = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        temp_image.close()

        async def _run():
            return await executor.execute(
                event,
                "prompt",
                "system",
                direct_vision_urls=[temp_image.name],
            )

        try:
            result = asyncio.run(_run())
        finally:
            try:
                os.remove(temp_image.name)
            except OSError:
                pass

        self.assertEqual(result, "lane-text-reply")
        mode, kwargs = gateway.calls[0]
        self.assertEqual(mode, "chat")
        self.assertEqual(kwargs["image_urls"], [temp_image.name])
        self.assertEqual(kwargs["prompt"], "prompt")
        self.assertEqual(event.get_extra("vision_main_reply_strategy"), "native_direct")
        self.assertEqual(event.get_extra("vision_native_direct_outcome"), "success")
        self.assertFalse(any(mode == "vision" for mode, _kwargs in gateway.calls))

    def test_native_main_reply_vision_failure_falls_back_to_relay_and_opens_breaker(self):
        gateway = _FakeGateway(
            models=["model-a"],
            chat_responses={
                "model-a": lambda kwargs: (
                    "request id: 1\nstatus code: 500" if kwargs.get("image_urls") else "lane-text-reply"
                )
            },
        )
        gateway.config.vision.use_native_main_reply_vision = True
        gateway.config.vision.native_main_reply_failure_cooldown_sec = 90
        reply_service = _FakeReplyService()
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=reply_service,
            evolution_manager=_FakeEvolution(),
            config=gateway.config,
        )
        event = _FakeEvent()
        temp_image = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        temp_image.close()

        async def _run():
            return await executor.execute(
                event,
                "prompt",
                "system",
                direct_vision_urls=[temp_image.name],
            )

        try:
            result = asyncio.run(_run())
        finally:
            try:
                os.remove(temp_image.name)
            except OSError:
                pass

        self.assertEqual(result, "lane-text-reply")
        chat_calls = [kwargs for mode, kwargs in gateway.calls if mode == "chat"]
        self.assertEqual(len(chat_calls), 2)
        self.assertEqual(chat_calls[0]["image_urls"], [temp_image.name])
        self.assertIsNone(chat_calls[1]["image_urls"])
        self.assertEqual(event.get_extra("vision_main_reply_strategy"), "native_direct")
        self.assertEqual(event.get_extra("vision_native_direct_outcome"), "fallback_to_relay")
        self.assertEqual(event.get_extra("vision_native_direct_fallback_reason"), "provider_failure_text")
        self.assertGreater(float(event.get_extra("vision_native_direct_breaker_until", 0.0) or 0.0), 0.0)
        self.assertTrue(any(mode == "vision" for mode, _kwargs in gateway.calls))

    def test_native_main_reply_vision_tool_mode_passes_direct_images(self):
        gateway = _FakeGateway()
        gateway.config.vision.use_native_main_reply_vision = True
        reply_service = _FakeReplyService()
        evolution = _FakeEvolution()
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=reply_service,
            evolution_manager=evolution,
            config=gateway.config,
        )
        event = _FakeEvent()
        temp_image = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        temp_image.close()

        async def _run():
            return await executor.execute(
                event,
                "prompt",
                "system",
                tools=[object()],
                direct_vision_urls=[temp_image.name],
            )

        try:
            result = asyncio.run(_run())
        finally:
            try:
                os.remove(temp_image.name)
            except OSError:
                pass

        self.assertEqual(result, "tool-finished")
        mode, kwargs = gateway.calls[0]
        self.assertEqual(mode, "tool")
        self.assertEqual(kwargs["image_urls"], [temp_image.name])
        self.assertEqual(event.get_extra("vision_native_direct_outcome"), "success")

    def test_native_main_reply_breaker_skips_native_retry_within_same_session(self):
        gateway = _FakeGateway(
            models=["model-a"],
            chat_responses={
                "model-a": lambda kwargs: (
                    "request id: 1\nstatus code: 500" if kwargs.get("image_urls") else "lane-text-reply"
                )
            },
        )
        gateway.config.vision.use_native_main_reply_vision = True
        gateway.config.vision.native_main_reply_failure_cooldown_sec = 180
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=_FakeReplyService(),
            evolution_manager=_FakeEvolution(),
            config=gateway.config,
        )
        temp_image = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        temp_image.close()
        first_event = _FakeEvent()
        second_event = _FakeEvent()

        async def _run_once(ev):
            return await executor.execute(
                ev,
                "prompt",
                "system",
                direct_vision_urls=[temp_image.name],
            )

        try:
            first_result = asyncio.run(_run_once(first_event))
            first_call_count = len(gateway.calls)
            second_result = asyncio.run(_run_once(second_event))
        finally:
            try:
                os.remove(temp_image.name)
            except OSError:
                pass

        self.assertEqual(first_result, "lane-text-reply")
        self.assertEqual(second_result, "lane-text-reply")
        additional_calls = gateway.calls[first_call_count:]
        self.assertEqual([mode for mode, _kwargs in additional_calls], ["vision", "chat"])
        self.assertEqual(second_event.get_extra("vision_main_reply_strategy"), "relay")
        self.assertEqual(second_event.get_extra("vision_native_direct_outcome"), "breaker_open")


if __name__ == "__main__":
    unittest.main()
