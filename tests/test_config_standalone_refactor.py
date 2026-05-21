import unittest
import json
from pathlib import Path

from config import AstrMaiConfig


class ConfigStandaloneRefactorTests(unittest.TestCase):
    def test_astrmai_config_instantiates_with_expected_defaults(self):
        config = AstrMaiConfig()
        self.assertEqual(config.agent.max_steps, 5)
        self.assertTrue(hasattr(config.provider, "embedding_models"))
        self.assertTrue(hasattr(config.global_settings, "enable_private_chat"))
        self.assertTrue(hasattr(config.sys3, "enable_work_mode"))
        self.assertTrue(hasattr(config.vision, "use_native_main_reply_vision"))
        self.assertTrue(hasattr(config.vision, "native_main_reply_failure_cooldown_sec"))

    def test_schema_json_is_parseable_and_contains_native_vision_fields(self):
        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
        vision_items = schema["vision"]["items"]
        self.assertIn("use_native_main_reply_vision", vision_items)
        self.assertIn("native_main_reply_failure_cooldown_sec", vision_items)


if __name__ == "__main__":
    unittest.main()
