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


class _Conversation:
    def __init__(self, history=None):
        self.history = list(history or [])


class _ConversationManager:
    def __init__(self):
        self.current = {}
        self.conversations = {}

    async def get_curr_conversation_id(self, unified_msg_origin):
        return self.current.get(unified_msg_origin)

    async def new_conversation(self, unified_msg_origin, **_kwargs):
        conversation_id = f"conv-{len(self.conversations) + 1}"
        self.current[unified_msg_origin] = conversation_id
        self.conversations[conversation_id] = _Conversation()
        return conversation_id

    async def get_conversation(self, _unified_msg_origin, conversation_id, create_if_not_exists=False):
        if create_if_not_exists:
            self.conversations.setdefault(conversation_id, _Conversation())
        return self.conversations.get(conversation_id)

    async def update_conversation(self, _unified_msg_origin, conversation_id=None, history=None, **_kwargs):
        self.conversations[conversation_id] = _Conversation(history)


class _LedgerEvent:
    def __init__(self):
        self.extras = {}

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


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
        stats = gateway.router.get_stats()["vision"]["models"]
        self.assertEqual(stats["vision-a"]["failures"], 1)
        self.assertEqual(stats["vision-b"]["failures"], 0)

    def test_call_vision_task_uses_router_health_order(self):
        context = _VisionContext(
            ['{"description": "healthy result", "emotion_tags": ["calm"]}']
        )
        gateway = self.gateway_mod.GlobalModelGateway(
            context,
            SimpleNamespace(
                infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
                provider=SimpleNamespace(fallback_models=[], vision_models=["vision-a", "vision-b"]),
            ),
        )
        gateway.router.get_ranked_models("vision", ["vision-a", "vision-b"])
        gateway.router.report_failure("vision", "vision-a")

        result = asyncio.run(
            gateway.call_vision_task(
                image_data="image.png",
                prompt="Analyze",
                system_prompt="Return JSON",
            )
        )

        self.assertEqual(result["description"], "healthy result")
        self.assertEqual([call["chat_provider_id"] for call in context.calls], ["vision-b"])

    def test_call_vision_task_does_not_persist_invalid_lane_result(self):
        lane_mod = importlib.import_module("astrmai.infrastructure.runtime.lane_manager")
        context = _VisionContext(
            [
                '{"description": "", "emotion_tags": []}',
                '{"description": "valid image", "emotion_tags": ["calm"]}',
            ]
        )
        gateway = self.gateway_mod.GlobalModelGateway(
            context,
            SimpleNamespace(
                infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
                provider=SimpleNamespace(fallback_models=[], vision_models=["vision-a", "vision-b"]),
            ),
        )
        lane_manager = lane_mod.LaneManager(_ConversationManager())
        appended_artifacts = []
        original_append = lane_manager.append_visible_reply_artifact

        async def _capture_append(**kwargs):
            appended_artifacts.append(kwargs["artifact"].persistable_text)
            return await original_append(**kwargs)

        lane_manager.append_visible_reply_artifact = _capture_append
        gateway.set_lane_manager(lane_manager)
        lane_key = lane_mod.LaneKey(
            subsystem="bg",
            task_family="vision",
            scope_id="chat-1",
            scope_kind="chat",
        )

        result = asyncio.run(
            gateway.call_vision_task(
                image_data="image.png",
                prompt="Analyze",
                system_prompt="Return JSON",
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
            )
        )
        self.assertEqual(result["description"], "valid image")
        self.assertEqual(len(appended_artifacts), 1)
        self.assertNotIn('"description": ""', appended_artifacts[0])
        self.assertIn("valid image", appended_artifacts[0])

    def test_call_vision_task_skips_cooled_vision_model(self):
        context = _VisionContext(
            [
                '{"description": "a cat on the desk", "emotion_tags": ["calm"]}',
            ]
        )
        gateway = self.gateway_mod.GlobalModelGateway(
            context,
            SimpleNamespace(
                infra=SimpleNamespace(
                    max_concurrent_llm_calls=2,
                    llm_retries=0,
                    backoff_factor=1.5,
                    api_timeout=10,
                    rate_limit_model_cooldown_sec=120,
                    quota_model_cooldown_sec=1800,
                ),
                provider=SimpleNamespace(
                    fallback_models=["fallback-a"],
                    task_models=[],
                    agent_models=[],
                    vision_models=["vision-a", "vision-b"],
                ),
            ),
        )
        gateway._open_model_cooldown("vision", "vision-a", "Error code: 429 rate limit")

        async def _run():
            return await gateway.call_vision_task(
                image_data="image.png",
                prompt="Analyze",
                system_prompt="Return JSON",
            )

        result = asyncio.run(_run())

        self.assertEqual(result["description"], "a cat on the desk")
        self.assertEqual([call["chat_provider_id"] for call in context.calls], ["vision-b"])

    def test_call_vision_task_exhaustion_mentions_skipped_cooldown_models(self):
        context = _VisionContext(
            [
                '{"description": "", "emotion_tags": []}',
            ]
        )
        gateway = self.gateway_mod.GlobalModelGateway(
            context,
            SimpleNamespace(
                infra=SimpleNamespace(
                    max_concurrent_llm_calls=2,
                    llm_retries=0,
                    backoff_factor=1.5,
                    api_timeout=10,
                    rate_limit_model_cooldown_sec=120,
                    quota_model_cooldown_sec=1800,
                ),
                provider=SimpleNamespace(
                    fallback_models=["fallback-a"],
                    task_models=[],
                    agent_models=[],
                    vision_models=["vision-a", "vision-b"],
                ),
            ),
        )
        gateway._open_model_cooldown("vision", "vision-a", "Error code: 429 rate limit")

        async def _run():
            return await gateway.call_vision_task(
                image_data="image.png",
                prompt="Analyze",
                system_prompt="Return JSON",
            )

        with self.assertRaises(Exception) as caught:
            asyncio.run(_run())

        self.assertIn("skipped_cooldown_models", str(caught.exception))
        self.assertIn("vision-a", str(caught.exception))
        self.assertEqual([call["chat_provider_id"] for call in context.calls], ["vision-b"])

    def test_call_vision_task_propagates_structured_http_status_to_cooldown(self):
        class _Http503(RuntimeError):
            status_code = 503

        class _FailingVisionContext(_VisionContext):
            async def llm_generate(self, **kwargs):
                self.calls.append(kwargs)
                raise _Http503("upstream unavailable")

        context = _FailingVisionContext([])
        gateway = self.gateway_mod.GlobalModelGateway(
            context,
            SimpleNamespace(
                infra=SimpleNamespace(
                    max_concurrent_llm_calls=2,
                    llm_retries=0,
                    backoff_factor=1.5,
                    api_timeout=10,
                    server_error_model_cooldown_sec=300,
                    server_error_failure_threshold=1,
                    server_error_window_sec=60,
                ),
                provider=SimpleNamespace(
                    fallback_models=[],
                    task_models=[],
                    agent_models=[],
                    vision_models=["vision-a"],
                ),
            ),
        )

        with self.assertRaises(Exception):
            asyncio.run(
                gateway.call_vision_task(
                    image_data="image.png",
                    prompt="Analyze",
                    system_prompt="Return JSON",
                )
            )

        self.assertEqual(len(context.calls), 1)
        self.assertEqual(gateway._model_health[("vision", "vision-a")]["status"], "cooldown")

    def test_judge_and_mood_tasks_use_task_pool_and_workload_families(self):
        task_mod = importlib.import_module("astrmai.infrastructure.gateway.gateway_tasks")
        context_mod = importlib.import_module("astrmai.infrastructure.context_economy")

        class _ContextEconomy:
            def __init__(self):
                self.requests = []

            def build_request(self, **kwargs):
                self.requests.append(kwargs)
                return kwargs

            def resolve_policy(self, request):
                return {"family": request["family"]}

        class _Gateway(task_mod.GatewayTaskMixin):
            def __init__(self):
                self.context_economy = _ContextEconomy()
                self.elastic_calls = []

            def _task_models(self):
                return ["task-a", "task-b"]

            async def _elastic_call_result(self, *args, **kwargs):
                self.elastic_calls.append((args, kwargs))
                return SimpleNamespace(parsed_json={"ok": args[0], "models": list(args[3])}, text="unused")

        gateway = _Gateway()

        judge = asyncio.run(gateway.call_judge_task("judge prompt", system_prompt="judge system"))
        mood = asyncio.run(gateway.call_mood_task("mood prompt", system_prompt="mood system"))

        self.assertEqual(judge, {"ok": "task", "models": ["task-a", "task-b"]})
        self.assertEqual(mood, {"ok": "task", "models": ["task-a", "task-b"]})
        self.assertEqual(
            [request["family"] for request in gateway.context_economy.requests],
            [context_mod.WorkloadFamily.JUDGE, context_mod.WorkloadFamily.MOOD],
        )
        self.assertTrue(all(call[1]["is_json"] for call in gateway.elastic_calls))

    def test_direct_judge_call_records_stage_and_exact_model_attempt(self):
        ledger_mod = importlib.import_module("astrmai.infrastructure.runtime.turn_call_ledger")
        context = _VisionContext(['{"action": "REPLY"}'])
        gateway = self.gateway_mod.GlobalModelGateway(
            context,
            SimpleNamespace(
                infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
                provider=SimpleNamespace(fallback_models=[], task_models=["task-a"]),
            ),
        )
        event = _LedgerEvent()

        with ledger_mod.turn_telemetry_scope(event):
            result = asyncio.run(gateway.call_judge_task("judge prompt", system_prompt="judge system"))
            snapshot = ledger_mod.turn_telemetry_snapshot(event)

        self.assertEqual(result["action"], "REPLY")
        self.assertEqual(len(snapshot["llm_call_ledger"]), 1)
        call = snapshot["llm_call_ledger"][0]
        self.assertEqual(call["stage"], "attention.judge")
        self.assertEqual(call["status"], "success")
        self.assertEqual(call["attempts"], 1)
        self.assertEqual(call["model_attempts"][0]["model"], "task-a")
        self.assertGreaterEqual(call["model_attempts"][0]["elapsed_ms"], 0)

    def test_direct_vision_call_records_invalid_then_success_attempts(self):
        ledger_mod = importlib.import_module("astrmai.infrastructure.runtime.turn_call_ledger")
        context = _VisionContext(
            [
                '{"description": "", "emotion_tags": []}',
                '{"description": "valid image", "emotion_tags": ["calm"]}',
            ]
        )
        gateway = self.gateway_mod.GlobalModelGateway(
            context,
            SimpleNamespace(
                infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
                provider=SimpleNamespace(fallback_models=[], vision_models=["vision-a", "vision-b"]),
            ),
        )
        event = _LedgerEvent()

        with ledger_mod.turn_telemetry_scope(event):
            result = asyncio.run(
                gateway.call_vision_task(
                    image_data="image.png",
                    prompt="Analyze",
                    system_prompt="Return JSON",
                )
            )
            snapshot = ledger_mod.turn_telemetry_snapshot(event)

        self.assertEqual(result["description"], "valid image")
        calls = snapshot["llm_call_ledger"]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["stage"] == "multimodal.vision" for call in calls))
        self.assertEqual([call["status"] for call in calls], ["error", "success"])
        self.assertEqual([call["attempts"] for call in calls], [1, 1])
        self.assertEqual(
            [call["model_attempts"][0]["status"] for call in calls],
            ["invalid_output", "success"],
        )

    def test_data_process_and_proactive_tasks_dispatch_to_lane_or_elastic(self):
        task_mod = importlib.import_module("astrmai.infrastructure.gateway.gateway_tasks")
        context_mod = importlib.import_module("astrmai.infrastructure.context_economy")
        lane_mod = importlib.import_module("astrmai.infrastructure.runtime.lane_manager")

        class _ContextEconomy:
            def __init__(self):
                self.requests = []
                self.inferred = []

            def infer_workload_family(self, **kwargs):
                self.inferred.append(kwargs)
                return context_mod.WorkloadFamily.DATA_PROCESS

            def build_request(self, **kwargs):
                self.requests.append(kwargs)
                return kwargs

            def resolve_policy(self, request):
                return {"family": request["family"]}

        class _Gateway(task_mod.GatewayTaskMixin):
            def __init__(self):
                self.context_economy = _ContextEconomy()
                self.lane_calls = []
                self.elastic_calls = []

            def _task_models(self):
                return ["task-a"]

            async def chat_in_lane_result(self, **kwargs):
                self.lane_calls.append(kwargs)
                return SimpleNamespace(parsed_json={"lane": True}, text="lane text")

            async def _elastic_call_result(self, *args, **kwargs):
                self.elastic_calls.append((args, kwargs))
                return SimpleNamespace(parsed_json={"elastic": True}, text="elastic text")

        gateway = _Gateway()
        lane_key = lane_mod.LaneKey(subsystem="bg", task_family="memory", scope_id="chat-1", scope_kind="chat")

        lane_json = asyncio.run(gateway.call_data_process_task("prompt", is_json=True, lane_key=lane_key, base_origin="origin"))
        elastic_text = asyncio.run(
            gateway.call_data_process_task("prompt", is_json=False, base_origin="default:GroupMessage:group-1")
        )
        proactive_lane = asyncio.run(gateway.call_proactive_task("prompt", lane_key=lane_key))
        proactive_elastic = asyncio.run(gateway.call_proactive_task("prompt"))

        self.assertEqual(lane_json, {"lane": True})
        self.assertEqual(elastic_text, "elastic text")
        self.assertEqual(proactive_lane, "lane text")
        self.assertEqual(proactive_elastic, "elastic text")
        self.assertEqual(gateway.lane_calls[0]["lane_key"], lane_key)
        self.assertTrue(gateway.lane_calls[0]["is_json"])
        self.assertFalse(gateway.lane_calls[1]["is_json"])
        self.assertEqual(len(gateway.elastic_calls), 2)
        self.assertFalse(gateway.elastic_calls[1][1]["ledger_critical_path"])
        self.assertEqual(gateway.context_economy.requests[0]["scope_id"], "default:GroupMessage:group-1")
        self.assertEqual(gateway.context_economy.requests[0]["scope_kind"], "chat")

    def test_persona_task_dispatches_json_and_text_modes(self):
        task_mod = importlib.import_module("astrmai.infrastructure.gateway.gateway_tasks")
        context_mod = importlib.import_module("astrmai.infrastructure.context_economy")

        class _ContextEconomy:
            def __init__(self):
                self.requests = []

            def build_request(self, **kwargs):
                self.requests.append(kwargs)
                return kwargs

            def resolve_policy(self, request):
                return {"family": request["family"]}

        class _Gateway(task_mod.GatewayTaskMixin):
            def __init__(self):
                self.context_economy = _ContextEconomy()
                self.elastic_calls = []

            def _task_models(self):
                return ["task-a"]

            async def _elastic_call_result(self, *args, **kwargs):
                self.elastic_calls.append((args, kwargs))
                return SimpleNamespace(parsed_json={"persona": True}, text="persona text")

        gateway = _Gateway()

        json_result = asyncio.run(gateway.call_persona_task("prompt", is_json=True, persona_id="persona-1"))
        text_result = asyncio.run(gateway.call_persona_task("prompt", is_json=False, persona_id="persona-1"))

        self.assertEqual(json_result, {"persona": True})
        self.assertEqual(text_result, "persona text")
        self.assertEqual(gateway.context_economy.requests[0]["family"], context_mod.WorkloadFamily.PERSONA_SUMMARY)
        self.assertEqual(gateway.context_economy.requests[0]["persona_id"], "persona-1")
        self.assertTrue(all(not call[1]["ledger_critical_path"] for call in gateway.elastic_calls))

    def test_normalize_vision_failure_reason_handles_empty_and_valid_payloads(self):
        task_mod = importlib.import_module("astrmai.infrastructure.gateway.gateway_tasks")

        class _Gateway(task_mod.GatewayTaskMixin):
            @staticmethod
            def _classify_failure_kind(value):
                kind = "provider_failure_text" if "provider failed" in str(value).lower() else "unknown"
                return SimpleNamespace(value=kind)

        gateway = _Gateway()

        self.assertEqual(gateway._normalize_vision_failure_reason({}), (False, "empty_result"))
        self.assertEqual(gateway._normalize_vision_failure_reason(None), (False, "empty_result"))
        self.assertEqual(
            gateway._normalize_vision_failure_reason({"description": None}),
            (False, "empty_description"),
        )
        self.assertEqual(
            gateway._normalize_vision_failure_reason({"description": " none "}),
            (False, "empty_description"),
        )
        self.assertEqual(
            gateway._normalize_vision_failure_reason(
                {"description": "a cat", "emotion_tags": [" calm ", "curious"]}
            ),
            (True, ""),
        )
        self.assertEqual(
            gateway._normalize_vision_failure_reason(
                {"description": "provider failed before inference"}
            ),
            (False, "provider_failure_text"),
        )
        self.assertEqual(
            gateway._normalize_vision_failure_reason(
                {"description": "a cat", "emotion_tags": None}
            ),
            (True, ""),
        )
        self.assertEqual(
            gateway._normalize_vision_failure_reason(
                {"description": "a cat", "emotion_tags": [" ", ""]}
            ),
            (False, "invalid_emotion_tags"),
        )
        self.assertEqual(
            gateway._normalize_vision_failure_reason(
                {"description": "a cat", "emotion_tags": ["provider failed"]}
            ),
            (False, "provider_failure_text"),
        )
        self.assertEqual(
            gateway._normalize_vision_failure_reason(
                {"description": "a cat", "emotion_tags": "provider failed"}
            ),
            (False, "provider_failure_text"),
        )
        self.assertEqual(
            gateway._normalize_vision_failure_reason(
                {"description": "a cat", "emotion_tags": {"calm": True}}
            ),
            (False, "invalid_emotion_tags"),
        )

    def test_call_vision_task_uses_elastic_path_without_lane_manager(self):
        task_mod = importlib.import_module("astrmai.infrastructure.gateway.gateway_tasks")
        context_mod = importlib.import_module("astrmai.infrastructure.context_economy")
        lane_mod = importlib.import_module("astrmai.infrastructure.runtime.lane_manager")

        class _ContextEconomy:
            def __init__(self):
                self.requests = []

            def build_request(self, **kwargs):
                self.requests.append(kwargs)
                return kwargs

            @staticmethod
            def resolve_policy(request):
                return {"family": request["family"]}

        class _Gateway(task_mod.GatewayTaskMixin):
            def __init__(self):
                self.lane_manager = None
                self.context_economy = _ContextEconomy()
                self.elastic_calls = []

            @staticmethod
            def _vision_models():
                return ["vision-a"]

            @staticmethod
            def _filter_cooldown_attempt_queue(_pool_name, _primary, attempt_queue):
                return list(attempt_queue), [], False

            async def _elastic_call_result(self, **kwargs):
                self.elastic_calls.append(kwargs)
                return SimpleNamespace(
                    parsed_json={"description": "a cat", "emotion_tags": ["calm"]},
                    model_id="vision-a",
                )

            @staticmethod
            def _classify_failure_kind(_value):
                return SimpleNamespace(value="unknown")

            @staticmethod
            def _open_model_cooldown(*_args):
                raise AssertionError("valid vision result must not open cooldown")

        gateway = _Gateway()
        lane_key = lane_mod.LaneKey(
            subsystem="bg",
            task_family="vision",
            scope_id="chat-1",
            scope_kind="chat",
        )

        result = asyncio.run(
            gateway.call_vision_task(
                image_data="image-data",
                prompt="Analyze",
                lane_key=lane_key,
            )
        )

        self.assertEqual(result["description"], "a cat")
        self.assertEqual(len(gateway.elastic_calls), 1)
        self.assertEqual(gateway.elastic_calls[0]["models"], ["vision-a"])
        self.assertEqual(gateway.elastic_calls[0]["image_urls"], ["image-data"])
        self.assertEqual(
            gateway.context_economy.requests[0]["family"],
            context_mod.WorkloadFamily.VISION,
        )

    def test_call_vision_task_forwards_timeout_override(self):
        from astrmai.infrastructure.gateway.gateway_tasks import GatewayTaskMixin

        captured = {}

        class _Gateway(GatewayTaskMixin):
            lane_manager = None
            router = None
            context_economy = SimpleNamespace(
                build_request=lambda **kwargs: kwargs,
                resolve_policy=lambda request: request,
            )

            def _vision_models(self):
                return ["vision-model"]

            def _filter_cooldown_attempt_queue(self, *_args):
                return (["vision-model"], [], False)

            async def _elastic_call_result(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    parsed_json={"description": "ok", "emotion_tags": []},
                    model_id="vision-model",
                )

            def _is_model_cooldown(self, *_args):
                return False

            def _open_model_cooldown(self, *_args):
                return None

            def _classify_failure_kind(self, *_args):
                return SimpleNamespace(value="unknown")

        result = asyncio.run(
            _Gateway().call_vision_task(
                image_data="image.png",
                prompt="describe",
                timeout_override=12.5,
            )
        )

        self.assertEqual(result["description"], "ok")
        self.assertEqual(captured["timeout_override"], 12.5)

    def test_get_agent_models_combines_router_rankings_and_records_filter_state(self):
        task_mod = importlib.import_module("astrmai.infrastructure.gateway.gateway_tasks")

        class _Router:
            def __init__(self):
                self.calls = []

            def get_ranked_models(self, pool_name, models):
                self.calls.append((pool_name, list(models)))
                if pool_name == "agent":
                    return ["agent-b", "agent-a"]
                return ["fallback-a", "agent-a"]

        class _Gateway(task_mod.GatewayTaskMixin):
            def __init__(self):
                self.router = _Router()
                self.filter_calls = []

            @staticmethod
            def _agent_models():
                return ["agent-a", "agent-b"]

            @staticmethod
            def _fallback_models():
                return ["fallback-a", "agent-a"]

            def _filter_cooldown_attempt_queue(self, pool_name, primary, attempt_queue):
                self.filter_calls.append((pool_name, primary, attempt_queue))
                return ["agent-b", "fallback-a"], [{"model_id": "agent-a"}], True

        gateway = _Gateway()

        result = gateway.get_agent_models()

        self.assertEqual(result, ["agent-b", "fallback-a"])
        self.assertEqual(
            gateway.router.calls,
            [
                ("agent", ["agent-a", "agent-b"]),
                ("fallback", ["fallback-a", "agent-a"]),
            ],
        )
        self.assertEqual(
            gateway.filter_calls,
            [
                (
                    "agent",
                    ["agent-b", "agent-a"],
                    ["agent-b", "agent-a", "fallback-a"],
                )
            ],
        )
        self.assertEqual(
            gateway._last_agent_model_selection,
            {
                "skipped_cooldown_models": [{"model_id": "agent-a"}],
                "cooldown_overridden": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
