import unittest
import json
from pathlib import Path

from config import AstrMaiConfig, MemoryConfig


class ConfigStandaloneRefactorTests(unittest.TestCase):
    def test_astrmai_config_instantiates_with_expected_defaults(self):
        config = AstrMaiConfig()
        self.assertEqual(config.agent.max_steps, 5)
        self.assertTrue(hasattr(config.provider, "embedding_models"))
        self.assertTrue(hasattr(config, "performance"))
        self.assertEqual(config.performance.summary_threshold, 300)
        self.assertTrue(hasattr(config.global_settings, "enable_private_chat"))
        self.assertTrue(hasattr(config.sys3, "enable_work_mode"))
        self.assertTrue(hasattr(config.vision, "use_native_main_reply_vision"))
        self.assertTrue(hasattr(config.vision, "native_main_reply_failure_cooldown_sec"))
        self.assertTrue(hasattr(config, "conversation"))
        self.assertEqual(config.conversation.compaction_trigger_segments, 40)
        self.assertEqual(config.conversation.compaction_keep_recent_segments, 16)
        self.assertTrue(config.memory.memory_query_builder_enabled)
        self.assertFalse(config.memory.intent_rerank_enabled)
        self.assertFalse(config.memory.adaptive_top_k_enabled)
        self.assertFalse(config.memory.memory_retrieval_debug_trace_enabled)
        self.assertEqual(config.attention.judge_timeout, 3.0)
        self.assertEqual(config.sys3.max_steps, 30)
        self.assertEqual(config.sys3.tool_timeout, 120)
        self.assertEqual(config.evolution.jargon_min_count, 2)

    def test_astrmai_config_accepts_conversation_and_memory_namespace_fields(self):
        config = AstrMaiConfig(
            conversation={
                "compaction_trigger_segments": 64,
                "compaction_keep_recent_segments": 20,
            },
            memory={
                "deep_temporal_alpha": 0.5,
                "maintenance_hot_beta": 0.25,
            },
        )

        self.assertEqual(config.conversation.compaction_trigger_segments, 64)
        self.assertEqual(config.conversation.compaction_keep_recent_segments, 20)
        self.assertEqual(config.memory.deep_temporal_alpha, 0.5)
        self.assertEqual(config.memory.maintenance_hot_beta, 0.25)

    def test_astrmai_config_migrates_legacy_global_memory_fields(self):
        config = AstrMaiConfig(
            global_settings={
                "debug_mode": True,
                "maintenance_hot_beta": 0.3,
                "deep_temporal_alpha": 0.4,
            }
        )

        self.assertTrue(config.global_settings.debug_mode)
        self.assertEqual(config.memory.maintenance_hot_beta, 0.3)
        self.assertEqual(config.memory.deep_temporal_alpha, 0.4)

    def test_memory_namespace_overrides_legacy_global_fields(self):
        config = AstrMaiConfig(
            global_settings={"maintenance_hot_beta": 0.3},
            memory={"maintenance_hot_beta": 0.6},
        )

        self.assertEqual(config.memory.maintenance_hot_beta, 0.6)

    def test_memory_config_instance_is_preserved(self):
        config = AstrMaiConfig(
            memory=MemoryConfig(
                deep_temporal_alpha=0.9,
                maintenance_hot_beta=0.1,
            )
        )

        self.assertEqual(config.memory.deep_temporal_alpha, 0.9)
        self.assertEqual(config.memory.maintenance_hot_beta, 0.1)

    def test_astrmai_config_accepts_performance_and_evolution_fields(self):
        config = AstrMaiConfig(
            performance={"summary_threshold": 512},
            evolution={"jargon_min_count": 5},
        )

        self.assertEqual(config.performance.summary_threshold, 512)
        self.assertEqual(config.evolution.jargon_min_count, 5)

    def test_schema_json_is_parseable_and_contains_runtime_config_fields(self):
        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
        performance_items = schema["performance"]["items"]
        vision_items = schema["vision"]["items"]
        evolution_items = schema["evolution"]["items"]
        self.assertIn("summary_threshold", performance_items)
        self.assertIn("use_native_main_reply_vision", vision_items)
        self.assertIn("native_main_reply_failure_cooldown_sec", vision_items)
        self.assertIn("jargon_min_count", evolution_items)
        self.assertEqual(performance_items["summary_threshold"]["default"], 300)
        self.assertEqual(evolution_items["jargon_min_count"]["default"], 2)
        self.assertEqual(schema["conversation"]["items"]["compaction_trigger_segments"]["default"], 40)
        self.assertEqual(schema["conversation"]["items"]["compaction_keep_recent_segments"]["default"], 16)
        memory_items = schema["memory"]["items"]
        self.assertTrue(memory_items["memory_query_builder_enabled"]["default"])
        self.assertFalse(memory_items["intent_rerank_enabled"]["default"])
        self.assertFalse(memory_items["adaptive_top_k_enabled"]["default"])
        self.assertFalse(memory_items["memory_retrieval_debug_trace_enabled"]["default"])
        self.assertEqual(schema["attention"]["items"]["judge_timeout"]["default"], 3.0)
        self.assertEqual(schema["sys3"]["items"]["max_steps"]["default"], 30)
        self.assertEqual(schema["sys3"]["items"]["tool_timeout"]["default"], 120)
        self.assertEqual(schema["life"]["items"]["energy_exhaustion"]["default"], 0.1)


if __name__ == "__main__":
    unittest.main()
