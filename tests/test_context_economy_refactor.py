import asyncio
import importlib
import sys
import tempfile
import time
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
        if create_if_not_exists and conversation_id not in self.conversations:
            self.conversations[conversation_id] = _FakeConversation(history=[])
        return self.conversations.get(conversation_id)

    async def update_conversation(self, unified_msg_origin, conversation_id=None, history=None, title=None, persona_id=None, token_usage=None):
        conversation_id = conversation_id or self.curr.get(unified_msg_origin)
        self.conversations[conversation_id] = _FakeConversation(history=history or [])


class _FakeResponse:
    def __init__(self, text="ok"):
        self.completion_text = text
        self.usage = SimpleNamespace(input=12, input_cached=6, output=4)


class _FakeContext:
    def __init__(self):
        self.calls = []

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse()


class ContextEconomyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.context_economy.center", None)
        sys.modules.pop("astrmai.infrastructure.context_economy.models", None)
        self.center_mod = importlib.import_module("astrmai.infrastructure.context_economy.center")
        self.models_mod = importlib.import_module("astrmai.infrastructure.context_economy.models")
        self.center_mod = importlib.reload(self.center_mod)
        self.models_mod = importlib.reload(self.models_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_cache_priority_policy_uses_stable_shell_hash(self):
        center = self.center_mod.ContextEconomyCenter()
        family = self.models_mod.WorkloadFamily.PERSONA_SUMMARY
        req1 = center.build_request(
            family=family,
            pool_name="task",
            prompt="dynamic-a",
            system_prompt="stable-shell",
            models=["model-a"],
            persona_id="persona-1",
            scope_id="persona-1",
        )
        req2 = center.build_request(
            family=family,
            pool_name="task",
            prompt="dynamic-b",
            system_prompt="stable-shell",
            models=["model-a"],
            persona_id="persona-1",
            scope_id="persona-1",
        )

        policy1 = center.resolve_policy(req1)
        policy2 = center.resolve_policy(req2)

        self.assertTrue(policy1.cache_priority)
        self.assertEqual(policy1.stable_prefix_hash, policy2.stable_prefix_hash)
        self.assertEqual(policy1.effective_prefix_hash, policy2.effective_prefix_hash)
        self.assertEqual(policy1.primary_model, "model-a")

    def test_dialog_policy_keeps_existing_prefix_hash(self):
        center = self.center_mod.ContextEconomyCenter()
        family = self.models_mod.WorkloadFamily.CHAT_DIALOG
        req = center.build_request(
            family=family,
            pool_name="dialog",
            prompt="hello",
            system_prompt="stable",
            models=["model-a"],
            prefix_hash="prefix-user",
            scope_id="chat-1",
        )

        policy = center.resolve_policy(req)

        self.assertFalse(policy.cache_priority)
        self.assertEqual(policy.effective_prefix_hash, "prefix-user")

    def test_cache_priority_lane_prompt_identity_tracks_template_version(self):
        center = self.center_mod.ContextEconomyCenter()
        lane_key = self.center_mod.LaneKey(subsystem="bg", task_family="memory", scope_id="global", scope_kind="global")

        req_v1 = center.build_request(
            family=self.models_mod.WorkloadFamily.MEMORY_GLOBAL_SUMMARY,
            pool_name="task",
            prompt="payload-a",
            system_prompt="stable-shell",
            models=["model-a"],
            lane_key=lane_key,
            scope_id="global",
            template_id="memory_global_summary",
            template_version="v1",
            schema_id="text",
        )
        req_v2 = center.build_request(
            family=self.models_mod.WorkloadFamily.MEMORY_GLOBAL_SUMMARY,
            pool_name="task",
            prompt="payload-b",
            system_prompt="stable-shell",
            models=["model-a"],
            lane_key=lane_key,
            scope_id="global",
            template_id="memory_global_summary",
            template_version="v2",
            schema_id="text",
        )

        policy_v1 = center.resolve_policy(req_v1)
        policy_v2 = center.resolve_policy(req_v2)

        self.assertEqual(policy_v1.lane_key.prompt_version, "memory_global_summary:v1:text")
        self.assertEqual(policy_v2.lane_key.prompt_version, "memory_global_summary:v2:text")
        self.assertNotEqual(policy_v1.lane_key.prompt_version, policy_v2.lane_key.prompt_version)

    def test_dialog_lane_prompt_version_is_not_forced_by_template_version(self):
        center = self.center_mod.ContextEconomyCenter()
        lane_key = self.center_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1", prompt_version="v1")
        req = center.build_request(
            family=self.models_mod.WorkloadFamily.CHAT_DIALOG,
            pool_name="dialog",
            prompt="hello",
            system_prompt="stable",
            models=["model-a"],
            lane_key=lane_key,
            prefix_hash="prefix-user",
            scope_id="group-1",
            template_id="chat_dialog",
            template_version="v9",
            schema_id="text",
        )

        policy = center.resolve_policy(req)

        self.assertEqual(policy.lane_key.prompt_version, "v1")

    def test_template_version_change_counts_as_rotate_in_metrics(self):
        center = self.center_mod.ContextEconomyCenter()
        lane_key = self.center_mod.LaneKey(subsystem="bg", task_family="memory", scope_id="global", scope_kind="global")

        req_v1 = center.build_request(
            family=self.models_mod.WorkloadFamily.MEMORY_GLOBAL_SUMMARY,
            pool_name="task",
            prompt="payload-a",
            system_prompt="stable-shell",
            models=["model-a"],
            lane_key=lane_key,
            scope_id="global",
            template_id="memory_global_summary",
            template_version="v1",
            schema_id="text",
        )
        policy_v1 = center.resolve_policy(req_v1)
        trace_v1 = center.build_trace(
            policy=policy_v1,
            lane_umo="lane-v1",
            actual_model="model-a",
            provider_session_enabled=True,
            provider_session_id="session-v1",
        )
        center.record_trace(trace_v1)

        req_v2 = center.build_request(
            family=self.models_mod.WorkloadFamily.MEMORY_GLOBAL_SUMMARY,
            pool_name="task",
            prompt="payload-b",
            system_prompt="stable-shell",
            models=["model-a"],
            lane_key=lane_key,
            scope_id="global",
            template_id="memory_global_summary",
            template_version="v2",
            schema_id="text",
        )
        policy_v2 = center.resolve_policy(req_v2)
        self.assertTrue(policy_v2.synthetic_lane_rotated)
        self.assertEqual(policy_v2.synthetic_lane_rotate_reason, "template_version_changed")

        trace_v2 = center.build_trace(
            policy=policy_v2,
            lane_umo="lane-v2",
            actual_model="model-a",
            provider_session_enabled=True,
            provider_session_id="session-v2",
        )
        center.record_trace(trace_v2)

        snapshot = center.snapshot_metrics()
        family_stats = snapshot["memory_global_summary"]
        template_stats = snapshot["_templates"]["memory_global_summary@v2"]

        self.assertEqual(family_stats["lane_rotate_count"], 1)
        self.assertEqual(family_stats["rotate_reasons"]["template_version_changed"], 1)
        self.assertEqual(template_stats["lane_rotate_count"], 1)

    def test_provider_session_reuse_and_split_rotate_reasons_are_counted_correctly(self):
        center = self.center_mod.ContextEconomyCenter()
        lane_key = self.center_mod.LaneKey(subsystem="bg", task_family="memory", scope_id="global", scope_kind="global")
        req = center.build_request(
            family=self.models_mod.WorkloadFamily.MEMORY_GLOBAL_SUMMARY,
            pool_name="task",
            prompt="payload",
            system_prompt="stable-shell",
            models=["model-a"],
            lane_key=lane_key,
            scope_id="global",
            template_id="memory_global_summary",
            template_version="v1",
            schema_id="text",
        )
        policy = center.resolve_policy(req)

        trace_first = center.build_trace(
            policy=policy,
            lane_umo="lane-1",
            actual_model="model-a",
            provider_session_enabled=True,
            provider_session_id="session-1",
        )
        center.record_trace(trace_first)

        trace_second = center.build_trace(
            policy=policy,
            lane_umo="lane-1",
            actual_model="model-a",
            lane_rotated=True,
            lane_rotate_reason="template_version_changed,schema_changed,template_version_changed",
            provider_session_enabled=True,
            provider_session_id="session-1",
        )
        center.record_trace(trace_second)

        snapshot = center.snapshot_metrics()
        family_stats = snapshot["memory_global_summary"]

        self.assertEqual(family_stats["provider_session_usage_rate"], 1.0)
        self.assertEqual(family_stats["provider_session_reuse_rate"], 0.5)
        self.assertEqual(family_stats["rotate_reasons"]["template_version_changed"], 1)
        self.assertEqual(family_stats["rotate_reasons"]["schema_changed"], 1)

    def test_global_scope_fallback_is_marked_for_cache_priority_memory_and_dream(self):
        center = self.center_mod.ContextEconomyCenter()
        lane_key = self.center_mod.LaneKey(subsystem="bg", task_family="dream", scope_id="global", scope_kind="global")
        req = center.build_request(
            family=self.models_mod.WorkloadFamily.DREAM_GENERATION,
            pool_name="task",
            prompt="dream payload",
            system_prompt="dream shell",
            models=["model-a"],
            lane_key=lane_key,
            scope_id="global",
            template_id="dream_generation",
            template_version="v1",
            schema_id="text",
        )

        policy = center.resolve_policy(req)

        self.assertEqual(policy.lane_scope_id, "global")
        self.assertEqual(policy.cache_affinity_reason, "global_scope_fallback")

    def test_persona_core_identity_template_defaults_to_v3(self):
        templates_mod = importlib.import_module("astrmai.infrastructure.context_economy.prompt_templates")
        templates_mod = importlib.reload(templates_mod)
        registry = templates_mod.PromptTemplateRegistry()
        envelope = registry.render_template(
            templates_mod.PromptTemplateId.PERSONA_CORE_IDENTITY,
            {
                "original_prompt": "persona prompt",
                "cache_key": "persona-1",
            },
        )
        self.assertEqual(envelope.template_id, "persona_core_identity")
        self.assertEqual(envelope.template_version, "v3")
        self.assertEqual(envelope.schema_id, "text")

    def test_dream_template_and_fallback_share_stable_system_shell_wording(self):
        templates_mod = importlib.import_module("astrmai.infrastructure.context_economy.prompt_templates")
        templates_mod = importlib.reload(templates_mod)
        registry = templates_mod.PromptTemplateRegistry()
        envelope = registry.render_template(
            templates_mod.PromptTemplateId.DREAM_GENERATION,
            {
                "persona_name": "Mai",
                "style": "奇幻冒险",
                "dream_log": "log",
            },
        )
        self.assertEqual(
            envelope.system_prompt.split("\n\n")[0],
            "你是一个善于幻想与创作的写作助手，擅长用诗意的语言描述梦境。",
        )

    def test_memory_topic_summary_keeps_segment_count_out_of_system_prompt(self):
        templates_mod = importlib.import_module("astrmai.infrastructure.context_economy.prompt_templates")
        templates_mod = importlib.reload(templates_mod)
        registry = templates_mod.PromptTemplateRegistry()

        envelope_two = registry.render_template(
            templates_mod.PromptTemplateId.MEMORY_TOPIC_SUMMARY,
            {
                "segment_count": 2,
                "combined_segments": "A\nB",
            },
        )
        envelope_five = registry.render_template(
            templates_mod.PromptTemplateId.MEMORY_TOPIC_SUMMARY,
            {
                "segment_count": 5,
                "combined_segments": "A\nB\nC\nD\nE",
            },
        )

        self.assertNotIn("当前共有 2 个话题段", envelope_two.system_prompt)
        self.assertNotIn("当前共有 5 个话题段", envelope_five.system_prompt)
        self.assertIn("[Segment Count]\n2", envelope_two.prompt)
        self.assertIn("[Segment Count]\n5", envelope_five.prompt)
        self.assertEqual(envelope_two.stable_prefix_text, envelope_five.stable_prefix_text)

    def test_dream_generation_keeps_style_and_persona_name_out_of_system_prompt(self):
        templates_mod = importlib.import_module("astrmai.infrastructure.context_economy.prompt_templates")
        templates_mod = importlib.reload(templates_mod)
        registry = templates_mod.PromptTemplateRegistry()

        envelope_a = registry.render_template(
            templates_mod.PromptTemplateId.DREAM_GENERATION,
            {
                "persona_name": "Mai",
                "style": "奇幻冒险",
                "dream_log": "log-a",
            },
        )
        envelope_b = registry.render_template(
            templates_mod.PromptTemplateId.DREAM_GENERATION,
            {
                "persona_name": "Astra",
                "style": "安静悬疑",
                "dream_log": "log-b",
            },
        )

        self.assertNotIn("Mai", envelope_a.system_prompt)
        self.assertNotIn("Astra", envelope_b.system_prompt)
        self.assertNotIn("奇幻冒险", envelope_a.system_prompt)
        self.assertNotIn("安静悬疑", envelope_b.system_prompt)
        self.assertIn("[Persona Name]\nMai", envelope_a.prompt)
        self.assertIn("[Dream Style]\n奇幻冒险", envelope_a.prompt)
        self.assertIn("[Persona Name]\nAstra", envelope_b.prompt)
        self.assertIn("[Dream Style]\n安静悬疑", envelope_b.prompt)
        self.assertEqual(envelope_a.stable_prefix_text, envelope_b.stable_prefix_text)

    def test_cache_priority_hash_stays_stable_when_template_payload_parameters_change(self):
        templates_mod = importlib.import_module("astrmai.infrastructure.context_economy.prompt_templates")
        templates_mod = importlib.reload(templates_mod)
        center = self.center_mod.ContextEconomyCenter()

        topic_env_a = center.templates.render_template(
            templates_mod.PromptTemplateId.MEMORY_TOPIC_SUMMARY,
            {"segment_count": 1, "combined_segments": "alpha"},
        )
        topic_env_b = center.templates.render_template(
            templates_mod.PromptTemplateId.MEMORY_TOPIC_SUMMARY,
            {"segment_count": 9, "combined_segments": "alpha\nbeta"},
        )

        req_a = center.build_request(
            family=self.models_mod.WorkloadFamily.MEMORY_TOPIC_SUMMARY,
            pool_name="task",
            prompt=topic_env_a.prompt,
            system_prompt=topic_env_a.system_prompt,
            models=["model-a"],
            scope_id="global",
            template_envelope=topic_env_a,
        )
        req_b = center.build_request(
            family=self.models_mod.WorkloadFamily.MEMORY_TOPIC_SUMMARY,
            pool_name="task",
            prompt=topic_env_b.prompt,
            system_prompt=topic_env_b.system_prompt,
            models=["model-a"],
            scope_id="global",
            template_envelope=topic_env_b,
        )

        policy_a = center.resolve_policy(req_a)
        policy_b = center.resolve_policy(req_b)

        self.assertEqual(policy_a.stable_prefix_hash, policy_b.stable_prefix_hash)
        self.assertEqual(len(policy_a.stable_prefix_text), len(policy_b.stable_prefix_text))
        self.assertNotEqual(len(policy_a.dynamic_payload_text), len(policy_b.dynamic_payload_text))

        dream_env_a = center.templates.render_template(
            templates_mod.PromptTemplateId.DREAM_GENERATION,
            {"persona_name": "Mai", "style": "奇幻冒险", "dream_log": "log-a"},
        )
        dream_env_b = center.templates.render_template(
            templates_mod.PromptTemplateId.DREAM_GENERATION,
            {"persona_name": "Nova", "style": "冷调科幻", "dream_log": "log-b"},
        )

        dream_req_a = center.build_request(
            family=self.models_mod.WorkloadFamily.DREAM_GENERATION,
            pool_name="task",
            prompt=dream_env_a.prompt,
            system_prompt=dream_env_a.system_prompt,
            models=["model-a"],
            scope_id="global",
            template_envelope=dream_env_a,
        )
        dream_req_b = center.build_request(
            family=self.models_mod.WorkloadFamily.DREAM_GENERATION,
            pool_name="task",
            prompt=dream_env_b.prompt,
            system_prompt=dream_env_b.system_prompt,
            models=["model-a"],
            scope_id="global",
            template_envelope=dream_env_b,
        )

        dream_policy_a = center.resolve_policy(dream_req_a)
        dream_policy_b = center.resolve_policy(dream_req_b)

        self.assertEqual(dream_policy_a.stable_prefix_hash, dream_policy_b.stable_prefix_hash)
        self.assertEqual(len(dream_policy_a.stable_prefix_text), len(dream_policy_b.stable_prefix_text))
        self.assertNotEqual(len(dream_policy_a.dynamic_payload_text), len(dream_policy_b.dynamic_payload_text))


class ContextEconomyGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.runtime.lane_manager", None)
        sys.modules.pop("astrmai.infrastructure.gateway.model_gateway", None)
        self.lane_mod = importlib.import_module("astrmai.infrastructure.runtime.lane_manager")
        self.gateway_mod = importlib.import_module("astrmai.infrastructure.gateway.model_gateway")
        self.router_mod = importlib.import_module("astrmai.infrastructure.gateway.model_router")
        self.lane_mod = importlib.reload(self.lane_mod)
        self.gateway_mod = importlib.reload(self.gateway_mod)
        self.router_mod = importlib.reload(self.router_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_chat_in_lane_result_contains_economy_trace(self):
        fake_context = _FakeContext()
        config = SimpleNamespace(
            infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
            provider=SimpleNamespace(fallback_models=[]),
        )
        gateway = self.gateway_mod.GlobalModelGateway(fake_context, config)
        gateway.set_lane_manager(self.lane_mod.LaneManager(_FakeConversationManager()))
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")

        async def _run():
            return await gateway.chat_in_lane_result(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prompt="hello",
                system_prompt="stable prompt",
                models=["model-a"],
                prefix_hash="hash-1",
                use_fallback=False,
            )

        result = asyncio.run(_run())

        self.assertEqual(result.economy["workload_family"], "chat_dialog")
        self.assertEqual(result.economy["primary_model"], "model-a")
        self.assertIn("stable_prefix_length", result.economy)
        stats = gateway.get_context_economy_stats()
        self.assertIn("chat_dialog", stats)
        self.assertEqual(stats["chat_dialog"]["call_count"], 1)

    def test_sticky_router_keeps_primary_model_pinned(self):
        router = self.router_mod.ModelRouter()
        ranked1 = router.get_ranked_models("task", ["model-a", "model-b"], sticky_key="persona:1", sticky_preferred="model-a")
        ranked2 = router.get_ranked_models("task", ["model-a", "model-b"], sticky_key="persona:1", sticky_preferred="model-a")

        self.assertEqual(ranked1[0], "model-a")
        self.assertEqual(ranked2[0], "model-a")

        router.report_failure("task", "model-a", is_fatal=True)
        ranked3 = router.get_ranked_models("task", ["model-a", "model-b"], sticky_key="persona:1", sticky_preferred="model-a")
        self.assertEqual(ranked3[0], "model-b")

        pool = router._pools["task"]
        pool.models["model-a"].cooldown_until = time.time() - 1
        ranked4 = router.get_ranked_models("task", ["model-a", "model-b"], sticky_key="persona:1", sticky_preferred="model-a")
        self.assertEqual(ranked4[0], "model-a")


if __name__ == "__main__":
    unittest.main()
