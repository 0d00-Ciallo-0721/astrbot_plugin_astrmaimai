import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
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


class ContextEconomyBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        for module_name in [
            "astrmai.infrastructure.runtime.context_economy_benchmark",
            "astrmai.infrastructure.runtime.context_economy_benchmark_store",
            "astrmai.infrastructure.runtime.lane_manager",
            "astrmai.infrastructure.gateway.model_gateway",
        ]:
            sys.modules.pop(module_name, None)
        self.benchmark_mod = importlib.import_module("astrmai.infrastructure.runtime.context_economy_benchmark")
        self.store_mod = importlib.import_module("astrmai.infrastructure.runtime.context_economy_benchmark_store")
        self.lane_mod = importlib.import_module("astrmai.infrastructure.runtime.lane_manager")
        self.gateway_mod = importlib.import_module("astrmai.infrastructure.gateway.model_gateway")
        self.benchmark_mod = importlib.reload(self.benchmark_mod)
        self.store_mod = importlib.reload(self.store_mod)
        self.lane_mod = importlib.reload(self.lane_mod)
        self.gateway_mod = importlib.reload(self.gateway_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_sample_store_appends_and_reads_jsonl(self):
        store = self.store_mod.ContextEconomyBenchmarkSampleStore(Path(self.temp_dir.name), run_id="run-1")
        asyncio.run(
            store.append(
                {
                    "workload_family": "memory_global_summary",
                    "template_id": "memory_global_summary",
                    "template_version": "v1",
                }
            )
        )
        items = store.read_all_sync()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source_run_id"], "run-1")
        self.assertEqual(items[0]["template_id"], "memory_global_summary")

    def test_aggregate_samples_tracks_reuse_rotate_and_problem_templates(self):
        samples = [
            {
                "source_run_id": "run-1",
                "created_at": 1.0,
                "workload_family": "memory_global_summary",
                "template_id": "memory_global_summary",
                "template_version": "v1",
                "template_key": "memory_global_summary@v1",
                "model_id": "model-a",
                "provider_family": "openai",
                "input_tokens": 20,
                "cached_input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 25,
                "provider_session_id": "session-a",
                "lane_rotated": False,
                "lane_rotate_reason": "",
                "stable_prefix_length": 100,
                "dynamic_payload_length": 30,
                "primary_hit": True,
                "fallback_used": False,
            },
            {
                "source_run_id": "run-1",
                "created_at": 2.0,
                "workload_family": "memory_global_summary",
                "template_id": "memory_global_summary",
                "template_version": "v1",
                "template_key": "memory_global_summary@v1",
                "model_id": "model-a",
                "provider_family": "openai",
                "input_tokens": 18,
                "cached_input_tokens": 12,
                "output_tokens": 6,
                "total_tokens": 24,
                "provider_session_id": "session-a",
                "lane_rotated": True,
                "lane_rotate_reason": "template_version_changed,schema_changed",
                "stable_prefix_length": 100,
                "dynamic_payload_length": 35,
                "primary_hit": True,
                "fallback_used": False,
            },
            {
                "source_run_id": "run-1",
                "created_at": 3.0,
                "workload_family": "persona_summary",
                "template_id": "persona_core_identity",
                "template_version": "v2",
                "template_key": "persona_core_identity@v2",
                "model_id": "model-b",
                "provider_family": "openai",
                "input_tokens": 40,
                "cached_input_tokens": 0,
                "output_tokens": 10,
                "total_tokens": 50,
                "provider_session_id": "session-b",
                "lane_rotated": True,
                "lane_rotate_reason": "template_version_changed",
                "stable_prefix_length": 220,
                "dynamic_payload_length": 60,
                "primary_hit": False,
                "fallback_used": True,
            },
        ]
        summary = self.benchmark_mod.aggregate_benchmark_samples(samples)

        overview = summary["overview"]
        family = summary["by_workload_family"]["memory_global_summary"]
        template = summary["by_template"]["memory_global_summary@v1"]

        self.assertEqual(overview["call_count"], 3)
        self.assertEqual(overview["total_tokens"], 99)
        self.assertEqual(family["provider_session_reuse_rate"], 0.5)
        self.assertEqual(template["rotate_reasons"]["template_version_changed"], 1)
        self.assertEqual(template["rotate_reasons"]["schema_changed"], 1)
        self.assertEqual(summary["high_rotate_templates"][0]["template_key"], "persona_core_identity@v2")
        self.assertEqual(summary["low_reuse_templates"][0]["template_key"], "persona_core_identity@v2")
        self.assertEqual(summary["high_traffic_templates"][0]["template_key"], "memory_global_summary@v1")

    def test_gateway_success_records_benchmark_sample(self):
        fake_context = _FakeContext()
        config = SimpleNamespace(
            infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
            provider=SimpleNamespace(fallback_models=[]),
        )
        gateway = self.gateway_mod.GlobalModelGateway(fake_context, config)
        gateway.set_lane_manager(self.lane_mod.LaneManager(_FakeConversationManager()))
        gateway.benchmark_sample_store = self.store_mod.ContextEconomyBenchmarkSampleStore(Path(self.temp_dir.name), run_id="run-2")
        lane_key = self.lane_mod.LaneKey(subsystem="bg", task_family="memory", scope_id="global", scope_kind="global")

        async def _run():
            return await gateway.chat_in_lane_result(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prompt="hello",
                system_prompt="stable prompt",
                models=["model-a"],
            )

        result = asyncio.run(_run())
        self.assertTrue(result.ok)

        sample_path = Path(self.temp_dir.name) / "context_economy_benchmark_samples.jsonl"
        items = [json.loads(line) for line in sample_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source_run_id"], "run-2")
        self.assertEqual(items[0]["workload_family"], "memory_global_summary")
        self.assertEqual(items[0]["input_tokens"], 12)
        self.assertEqual(items[0]["cached_input_tokens"], 6)
        self.assertEqual(items[0]["total_tokens"], 16)

    def test_runner_writes_json_and_markdown_artifacts(self):
        sample_path = Path(self.temp_dir.name) / "context_economy_benchmark_samples.jsonl"
        sample_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "source_run_id": "run-3",
                            "created_at": 1.0,
                            "workload_family": "memory_global_summary",
                            "template_id": "memory_global_summary",
                            "template_version": "v1",
                            "template_key": "memory_global_summary@v1",
                            "model_id": "model-a",
                            "provider_family": "openai",
                            "input_tokens": 10,
                            "cached_input_tokens": 5,
                            "output_tokens": 2,
                            "total_tokens": 12,
                            "provider_session_id": "session-a",
                            "lane_rotated": False,
                            "lane_rotate_reason": "",
                            "stable_prefix_length": 100,
                            "dynamic_payload_length": 20,
                            "primary_hit": True,
                            "fallback_used": False,
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            encoding="utf-8",
        )

        samples = self.benchmark_mod.load_benchmark_samples(sample_path)
        summary = self.benchmark_mod.aggregate_benchmark_samples(samples)
        meta = self.benchmark_mod.build_benchmark_meta(sample_path=sample_path, sample_count=len(samples))
        run_dir = self.benchmark_mod.write_benchmark_artifacts(
            output_root=Path(self.temp_dir.name) / "artifacts",
            summary=summary,
            meta=meta,
            label="baseline",
            repo_root=Path(__file__).resolve().parents[1],
        )

        self.assertTrue((run_dir / "samples_meta.json").exists())
        self.assertTrue((run_dir / "benchmark_summary.json").exists())
        markdown = (run_dir / "benchmark_summary.md").read_text(encoding="utf-8")
        self.assertIn("Context Economy Benchmark Baseline", markdown)
        self.assertIn("High Rotate Templates", markdown)

    def test_replay_seed_builder_ignores_wakeup_guidance_template(self):
        replay_dir = Path(self.temp_dir.name) / "kimi_replay" / "run-1"
        replay_dir.mkdir(parents=True, exist_ok=True)
        report_path = replay_dir / "report.jsonl"
        report_path.write_text(
            "\n".join(
                [
                    json.dumps({"kind": "case", "case_id": "poke_case", "reply_preview": "poke reply"}, ensure_ascii=False),
                    json.dumps({"kind": "case", "case_id": "group_non_direct_chat", "reply_preview": "chat reply"}, ensure_ascii=False),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        builder_mod = importlib.import_module("tests.manual.context_economy_replay_seed_builder")
        builder_mod = importlib.reload(builder_mod)

        samples, versions = builder_mod.build_samples_from_replay(replay_dir.parent)

        self.assertTrue(any(item["template_id"] == "chat_dialog" for item in samples))
        self.assertFalse(any(item["template_id"] == "proactive_wakeup_opening" for item in samples))
        self.assertIn("persona_version", versions)

    def test_replay_seed_builder_groups_dialog_sessions_by_private_vs_group(self):
        replay_dir = Path(self.temp_dir.name) / "kimi_replay" / "run-2"
        replay_dir.mkdir(parents=True, exist_ok=True)
        report_path = replay_dir / "report.jsonl"
        report_path.write_text(
            "\n".join(
                [
                    json.dumps({"kind": "case", "case_id": "normal_private", "reply_preview": "private reply"}, ensure_ascii=False),
                    json.dumps({"kind": "case", "case_id": "pushback_strict", "reply_preview": "group reply"}, ensure_ascii=False),
                    json.dumps({"kind": "case", "case_id": "group_non_direct", "reply_preview": "group passive"}, ensure_ascii=False),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        builder_mod = importlib.import_module("tests.manual.context_economy_replay_seed_builder")
        builder_mod = importlib.reload(builder_mod)

        samples, _ = builder_mod.build_samples_from_replay(replay_dir.parent)
        chat_samples = [item for item in samples if item.get("template_id") == "chat_dialog"]

        private_sessions = {
            item["provider_session_id"]
            for item in chat_samples
            if item["provider_session_id"].startswith("chat-private-")
        }
        group_sessions = {
            item["provider_session_id"]
            for item in chat_samples
            if item["provider_session_id"].startswith("chat-group-")
        }

        self.assertEqual(private_sessions, {"chat-private-run-2"})
        self.assertEqual(group_sessions, {"chat-group-run-2"})

    def test_replay_seed_builder_reuses_global_persona_session_across_runs(self):
        root = Path(self.temp_dir.name) / "kimi_replay"
        for run_name in ("run-a", "run-b"):
            replay_dir = root / run_name
            replay_dir.mkdir(parents=True, exist_ok=True)
            (replay_dir / "report.jsonl").write_text(
                json.dumps({"kind": "case", "case_id": "tool_intent", "reply_preview": "tool reply"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

        builder_mod = importlib.import_module("tests.manual.context_economy_replay_seed_builder")
        builder_mod = importlib.reload(builder_mod)

        samples, _ = builder_mod.build_samples_from_replay(root)
        persona_samples = [item for item in samples if item.get("template_id") == "persona_core_identity"]
        persona_sessions = {item["provider_session_id"] for item in persona_samples}

        self.assertTrue(persona_samples)
        self.assertEqual(persona_sessions, {"persona-global"})

    def test_replay_seed_builder_reuses_shared_memory_session_across_runs(self):
        root = Path(self.temp_dir.name) / "kimi_replay"
        for run_name, case_id in (("run-a", "deep_memory"), ("run-b", "zh_memory_intent")):
            replay_dir = root / run_name
            replay_dir.mkdir(parents=True, exist_ok=True)
            (replay_dir / "report.jsonl").write_text(
                json.dumps({"kind": "case", "case_id": case_id, "reply_preview": "memory reply"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

        builder_mod = importlib.import_module("tests.manual.context_economy_replay_seed_builder")
        builder_mod = importlib.reload(builder_mod)

        samples, _ = builder_mod.build_samples_from_replay(root)
        memory_samples = [item for item in samples if item.get("template_id") in {"memory_global_summary", "memory_structured_extraction"}]
        memory_sessions = {item["provider_session_id"] for item in memory_samples}

        self.assertTrue(memory_samples)
        self.assertEqual(memory_sessions, {"memory-shared"})

    def test_replay_seed_builder_reuses_shared_dream_session_across_runs(self):
        root = Path(self.temp_dir.name) / "kimi_replay"
        for run_name in ("run-a", "run-b"):
            replay_dir = root / run_name
            replay_dir.mkdir(parents=True, exist_ok=True)
            (replay_dir / "report.jsonl").write_text(
                json.dumps({"kind": "case", "case_id": "normal_private", "reply_preview": "chat reply"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

        builder_mod = importlib.import_module("tests.manual.context_economy_replay_seed_builder")
        builder_mod = importlib.reload(builder_mod)

        samples, _ = builder_mod.build_samples_from_replay(root)
        dream_samples = [item for item in samples if item.get("template_id") == "dream_generation"]
        dream_sessions = {item["provider_session_id"] for item in dream_samples}

        self.assertTrue(dream_samples)
        self.assertEqual(dream_sessions, {"dream-shared"})

    def test_replay_seed_builder_reuses_shared_compaction_session_across_runs(self):
        root = Path(self.temp_dir.name) / "kimi_replay"
        for run_name in ("run-a", "run-b"):
            replay_dir = root / run_name
            replay_dir.mkdir(parents=True, exist_ok=True)
            (replay_dir / "report.jsonl").write_text(
                json.dumps({"kind": "case", "case_id": "normal_private", "reply_preview": "chat reply"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

        builder_mod = importlib.import_module("tests.manual.context_economy_replay_seed_builder")
        builder_mod = importlib.reload(builder_mod)

        samples, _ = builder_mod.build_samples_from_replay(root)
        compaction_samples = [item for item in samples if item.get("template_id") == "compaction_summary_v2"]
        compaction_sessions = {item["provider_session_id"] for item in compaction_samples}

        self.assertTrue(compaction_samples)
        self.assertEqual(compaction_sessions, {"compaction-shared"})
