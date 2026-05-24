import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _FakeConversation:
    def __init__(self, history=None):
        self.history = history or []


class _FakeConversationManager:
    def __init__(self):
        self.curr = {}
        self.conversations = {}
        self.counter = 0

    async def get_curr_conversation_id(self, unified_msg_origin):
        return self.curr.get(unified_msg_origin)

    async def new_conversation(self, unified_msg_origin, platform_id=None, content=None, title=None, persona_id=None):
        self.counter += 1
        cid = f"conv-{self.counter}"
        self.curr[unified_msg_origin] = cid
        self.conversations[cid] = _FakeConversation(history=content or [])
        return cid

    async def get_conversation(self, unified_msg_origin, conversation_id, create_if_not_exists=False):
        return self.conversations.get(conversation_id)

    async def update_conversation(self, unified_msg_origin, conversation_id=None, history=None, title=None, persona_id=None, token_usage=None):
        conversation_id = conversation_id or self.curr.get(unified_msg_origin)
        self.conversations[conversation_id] = _FakeConversation(history=history or [])


class _FakeResponse:
    def __init__(self, text):
        self.completion_text = text
        self.usage = SimpleNamespace(input=10, input_cached=6, output=4)


class _FakeContext:
    def __init__(self):
        self.calls = []

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse("ok")

    async def tool_loop_agent(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse("tool-ok")


class GatewayContextPassthroughRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
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

    def test_chat_in_lane_reuses_history_as_contexts(self):
        fake_context = _FakeContext()
        config = SimpleNamespace(
            infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
            provider=SimpleNamespace(fallback_models=[]),
        )
        gateway = self.gateway_mod.GlobalModelGateway(fake_context, config)
        lane_manager = self.lane_mod.LaneManager(_FakeConversationManager())
        gateway.set_lane_manager(lane_manager)
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")

        async def _run():
            await gateway.chat_in_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prompt="hello",
                system_prompt="stable prompt",
                models=["model-a"],
                prefix_hash="hash-1",
                use_fallback=False,
            )
            await gateway.chat_in_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prompt="again",
                system_prompt="stable prompt",
                models=["model-a"],
                prefix_hash="hash-1",
                use_fallback=False,
            )

        asyncio.run(_run())

        self.assertEqual(len(fake_context.calls), 2)
        self.assertEqual(fake_context.calls[0]["contexts"], [])
        self.assertEqual(len(fake_context.calls[1]["contexts"]), 2)
        self.assertEqual(fake_context.calls[1]["system_prompt"], "stable prompt")

    def test_chat_in_lane_result_records_request_trace_on_event(self):
        class _TraceEvent:
            def __init__(self):
                self.extras = {}

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

            def set_extra(self, key, value):
                self.extras[key] = value

        fake_context = _FakeContext()
        config = SimpleNamespace(
            infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
            provider=SimpleNamespace(fallback_models=[]),
        )
        gateway = self.gateway_mod.GlobalModelGateway(fake_context, config)
        lane_manager = self.lane_mod.LaneManager(_FakeConversationManager())
        gateway.set_lane_manager(lane_manager)
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")
        event = _TraceEvent()

        async def _run():
            return await gateway.chat_in_lane_result(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prompt="hello",
                system_prompt="stable prompt",
                models=["claude-3-5-sonnet"],
                prefix_hash="hash-1",
                use_fallback=False,
                event=event,
            )

        result = asyncio.run(_run())

        request_trace = event.get_extra("astrmai_request_trace", {})
        self.assertEqual(result.text, "ok")
        self.assertTrue(request_trace["gateway_system_hash"])
        self.assertTrue(request_trace["gateway_prompt_hash"])
        self.assertEqual(request_trace["request_provider_family"], "anthropic")
        self.assertEqual(request_trace["request_model_id"], "claude-3-5-sonnet")
        self.assertEqual(request_trace["request_cache_control"], '{"type": "ephemeral"}')
        self.assertEqual(request_trace["usage_input_tokens"], 10)
        self.assertEqual(request_trace["usage_input_cached"], 6)
        self.assertTrue(request_trace["provider_visible_system_hash"])
        self.assertTrue(request_trace["provider_visible_prompt_hash"])

    def test_tool_chat_in_lane_passes_image_urls_to_tool_loop_agent(self):
        fake_context = _FakeContext()
        config = SimpleNamespace(
            infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
            provider=SimpleNamespace(fallback_models=[]),
        )
        gateway = self.gateway_mod.GlobalModelGateway(fake_context, config)
        lane_manager = self.lane_mod.LaneManager(_FakeConversationManager())
        gateway.set_lane_manager(lane_manager)
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")
        event = SimpleNamespace()

        async def _run():
            await gateway.tool_chat_in_lane_result(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                event=event,
                prompt="look",
                system_prompt="stable prompt",
                tools=object(),
                models=["model-a"],
                max_steps=5,
                timeout=10,
                image_urls=["https://example.com/vision.jpg"],
                prefix_hash="hash-1",
            )

        asyncio.run(_run())

        self.assertEqual(len(fake_context.calls), 1)
        self.assertEqual(fake_context.calls[0]["image_urls"], ["https://example.com/vision.jpg"])

    def test_tool_chat_in_lane_passthroughs_terminal_yield_protocol(self):
        class _ProtocolContext(_FakeContext):
            async def tool_loop_agent(self, **kwargs):
                self.calls.append(kwargs)
                return _FakeResponse("[TERMINAL_YIELD]: tool-finished")

        fake_context = _ProtocolContext()
        config = SimpleNamespace(
            infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
            provider=SimpleNamespace(fallback_models=[]),
        )
        gateway = self.gateway_mod.GlobalModelGateway(fake_context, config)
        lane_manager = self.lane_mod.LaneManager(_FakeConversationManager())
        gateway.set_lane_manager(lane_manager)
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")
        event = SimpleNamespace()

        async def _run():
            return await gateway.tool_chat_in_lane_result(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                event=event,
                prompt="look",
                system_prompt="stable prompt",
                tools=object(),
                models=["model-a"],
                max_steps=5,
                timeout=10,
                prefix_hash="hash-1",
            )

        result = asyncio.run(_run())

        self.assertEqual(result.text, "[TERMINAL_YIELD]: tool-finished")
        lane_umo = lane_manager.resolve_lane_umo("default:GroupMessage:group-1", lane_key)
        conversation_id = asyncio.run(lane_manager.conversation_manager.get_curr_conversation_id(lane_umo))
        conversation = asyncio.run(lane_manager.conversation_manager.get_conversation(lane_umo, conversation_id))
        self.assertEqual(conversation.history[-1]["content"], "tool-finished")

    def test_tool_chat_in_lane_retries_wrapped_provider_failure_text(self):
        class _WrappedFailureContext(_FakeContext):
            async def tool_loop_agent(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs["chat_provider_id"] == "model-a":
                    return _FakeResponse(
                        "All chat models failed: PermissionDeniedError: Error code: 403 - "
                        "{'error': {'message': \"You've reached your usage limit for this billing cycle.\"}}"
                    )
                return _FakeResponse("tool-ok")

        class _TraceEvent:
            def __init__(self):
                self.extras = {}

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

            def set_extra(self, key, value):
                self.extras[key] = value

        fake_context = _WrappedFailureContext()
        config = SimpleNamespace(
            infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
            provider=SimpleNamespace(fallback_models=[]),
        )
        gateway = self.gateway_mod.GlobalModelGateway(fake_context, config)
        lane_manager = self.lane_mod.LaneManager(_FakeConversationManager())
        gateway.set_lane_manager(lane_manager)
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")
        event = _TraceEvent()

        async def _run():
            return await gateway.tool_chat_in_lane_result(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                event=event,
                prompt="look",
                system_prompt="stable prompt",
                tools=object(),
                models=["model-a", "model-b"],
                max_steps=5,
                timeout=10,
                prefix_hash="hash-1",
            )

        result = asyncio.run(_run())

        self.assertEqual(result.text, "tool-ok")
        self.assertEqual([call["chat_provider_id"] for call in fake_context.calls], ["model-a", "model-b"])
        failures = [
            record for record in event.get_extra("astrmai_trace_log", [])
            if record.get("stage") == "gateway_tool_call_failure"
        ]
        self.assertEqual(failures[0]["failure_kind"], "provider_failure_text")
        self.assertEqual(failures[0]["attempted_models"], ["model-a"])
        self.assertIn("All chat models failed", failures[0]["raw_completion"])
        self.assertTrue(failures[0]["will_retry_or_switch"])
        self.assertGreater(failures[0]["model_cooldown_until"], 0)
        self.assertEqual(failures[0]["cooldown_reason"], "quota_exhausted")

    def test_tool_chat_in_lane_skips_cooldown_model_on_next_call(self):
        class _WrappedFailureContext(_FakeContext):
            async def tool_loop_agent(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs["chat_provider_id"] == "model-a":
                    return _FakeResponse(
                        "All chat models failed: PermissionDeniedError: Error code: 403 - "
                        "{'error': {'message': \"You've reached your usage limit for this billing cycle.\"}}"
                    )
                return _FakeResponse("tool-ok")

        class _TraceEvent:
            def __init__(self):
                self.extras = {}

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

            def set_extra(self, key, value):
                self.extras[key] = value

        fake_context = _WrappedFailureContext()
        config = SimpleNamespace(
            infra=SimpleNamespace(
                max_concurrent_llm_calls=2,
                llm_retries=0,
                backoff_factor=1.5,
                api_timeout=10,
                rate_limit_model_cooldown_sec=120,
                quota_model_cooldown_sec=1800,
            ),
            provider=SimpleNamespace(fallback_models=[]),
        )
        gateway = self.gateway_mod.GlobalModelGateway(fake_context, config)
        lane_manager = self.lane_mod.LaneManager(_FakeConversationManager())
        gateway.set_lane_manager(lane_manager)
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")

        async def _run_once(event):
            return await gateway.tool_chat_in_lane_result(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                event=event,
                prompt="look",
                system_prompt="stable prompt",
                tools=object(),
                models=["model-a", "model-b"],
                max_steps=5,
                timeout=10,
                prefix_hash="hash-1",
            )

        first_event = _TraceEvent()
        second_event = _TraceEvent()
        first_result = asyncio.run(_run_once(first_event))
        second_result = asyncio.run(_run_once(second_event))

        self.assertEqual(first_result.text, "tool-ok")
        self.assertEqual(second_result.text, "tool-ok")
        self.assertEqual([call["chat_provider_id"] for call in fake_context.calls], ["model-a", "model-b", "model-b"])
        successes = [
            record for record in second_event.get_extra("astrmai_trace_log", [])
            if record.get("stage") == "gateway_tool_call"
        ]
        self.assertEqual(successes[0]["skipped_cooldown_models"][0]["model_id"], "model-a")
        self.assertFalse(successes[0]["cooldown_overridden"])

    def test_tool_chat_in_lane_overrides_when_all_models_are_cooled(self):
        class _RateLimitedContext(_FakeContext):
            async def tool_loop_agent(self, **kwargs):
                self.calls.append(kwargs)
                return _FakeResponse("tool-ok")

        class _TraceEvent:
            def __init__(self):
                self.extras = {}

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

            def set_extra(self, key, value):
                self.extras[key] = value

        fake_context = _RateLimitedContext()
        config = SimpleNamespace(
            infra=SimpleNamespace(
                max_concurrent_llm_calls=2,
                llm_retries=0,
                backoff_factor=1.5,
                api_timeout=10,
                rate_limit_model_cooldown_sec=120,
                quota_model_cooldown_sec=1800,
            ),
            provider=SimpleNamespace(fallback_models=[]),
        )
        gateway = self.gateway_mod.GlobalModelGateway(fake_context, config)
        gateway._open_model_cooldown("dialog", "model-a", "Error code: 429 rate limit")
        gateway._open_model_cooldown("dialog", "model-b", "Error code: 429 rate limit")
        lane_manager = self.lane_mod.LaneManager(_FakeConversationManager())
        gateway.set_lane_manager(lane_manager)
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")
        event = _TraceEvent()

        async def _run():
            return await gateway.tool_chat_in_lane_result(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                event=event,
                prompt="look",
                system_prompt="stable prompt",
                tools=object(),
                models=["model-a", "model-b"],
                max_steps=5,
                timeout=10,
                prefix_hash="hash-1",
            )

        result = asyncio.run(_run())

        self.assertEqual(result.text, "tool-ok")
        self.assertEqual(len(fake_context.calls), 1)
        successes = [
            record for record in event.get_extra("astrmai_trace_log", [])
            if record.get("stage") == "gateway_tool_call"
        ]
        self.assertTrue(successes[0]["cooldown_overridden"])
        self.assertEqual(
            [item["model_id"] for item in successes[0]["skipped_cooldown_models"]],
            ["model-a", "model-b"],
        )

    def test_get_agent_models_filters_runtime_cooldown_for_executor_entrypoint(self):
        fake_context = _FakeContext()
        config = SimpleNamespace(
            infra=SimpleNamespace(
                max_concurrent_llm_calls=2,
                llm_retries=0,
                backoff_factor=1.5,
                api_timeout=10,
                rate_limit_model_cooldown_sec=120,
                quota_model_cooldown_sec=1800,
            ),
            provider=SimpleNamespace(
                agent_models=["model-a", "model-b"],
                fallback_models=[],
                task_models=[],
                vision_models=[],
            ),
        )
        gateway = self.gateway_mod.GlobalModelGateway(fake_context, config)
        gateway._open_model_cooldown("agent", "model-a", "Error code: 429 rate limit")

        models = gateway.get_agent_models()

        self.assertEqual(models, ["model-b"])
        self.assertEqual(
            gateway._last_agent_model_selection["skipped_cooldown_models"][0]["model_id"],
            "model-a",
        )
        self.assertFalse(gateway._last_agent_model_selection["cooldown_overridden"])


if __name__ == "__main__":
    unittest.main()
