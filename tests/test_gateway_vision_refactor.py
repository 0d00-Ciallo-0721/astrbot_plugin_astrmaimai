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
        elastic_text = asyncio.run(gateway.call_data_process_task("prompt", is_json=False))
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


if __name__ == "__main__":
    unittest.main()
