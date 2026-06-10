import unittest
import json
from pathlib import Path

from config import AstrMaiConfig, MemoryConfig


class ConfigStandaloneRefactorTests(unittest.TestCase):
    def test_astrmai_config_instantiates_with_expected_defaults(self):
        config = AstrMaiConfig()
        self.assertEqual(config.agent.max_steps, 5)
        self.assertTrue(hasattr(config.provider, "embedding_models"))
        self.assertTrue(hasattr(config.global_settings, "enable_private_chat"))
        self.assertTrue(hasattr(config.sys3, "enable_work_mode"))
        self.assertTrue(hasattr(config.vision, "use_native_main_reply_vision"))
        self.assertTrue(hasattr(config.vision, "native_main_reply_failure_cooldown_sec"))
        self.assertTrue(hasattr(config, "conversation"))
        self.assertEqual(config.conversation.compaction_trigger_segments, 40)
        self.assertEqual(config.conversation.compaction_keep_recent_segments, 16)

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

    def test_schema_json_is_parseable_and_contains_native_vision_fields(self):
        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
        vision_items = schema["vision"]["items"]
        self.assertIn("use_native_main_reply_vision", vision_items)
        self.assertIn("native_main_reply_failure_cooldown_sec", vision_items)
        self.assertEqual(schema["conversation"]["items"]["compaction_trigger_segments"]["default"], 40)
        self.assertEqual(schema["conversation"]["items"]["compaction_keep_recent_segments"]["default"], 16)


if __name__ == "__main__":
    unittest.main()
