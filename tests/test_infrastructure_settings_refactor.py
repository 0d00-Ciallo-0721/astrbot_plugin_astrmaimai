import importlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class InfrastructureSettingsRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_build_infrastructure_settings_collects_gateway_lane_and_flags(self):
        defaults_mod = importlib.import_module(
            "astrmai.shared.constants.defaults"
        )
        config = SimpleNamespace(
            provider=SimpleNamespace(
                task_models=["task-a"],
                agent_models=["agent-a"],
                fallback_models=["fb-a"],
                vision_models=["vision-a"],
            ),
            infra=SimpleNamespace(
                max_concurrent_llm_calls=9,
                llm_retries=4,
                backoff_factor=2.0,
                api_timeout=21.0,
            ),
            global_settings=SimpleNamespace(debug_mode=True, enable_private_chat=True),
            system1=SimpleNamespace(nicknames=["Mai"]),
            sys3=SimpleNamespace(enable_work_mode=True),
            vision=SimpleNamespace(enable_vision=False),
            life=SimpleNamespace(enable_proactive=False, dream_visible=True),
            reply=SimpleNamespace(meme_probability=35),
        )

        settings = defaults_mod.build_infrastructure_settings(config)
        self.assertEqual(settings.gateway.max_concurrent_llm_calls, 9)
        self.assertEqual(settings.gateway.task_models, ("task-a",))
        self.assertEqual(settings.lane.nicknames, ("Mai",))
        self.assertTrue(settings.features.work_mode_enabled)
        self.assertTrue(settings.features.private_chat_enabled)
        self.assertFalse(settings.features.vision_enabled)
        self.assertFalse(settings.features.proactive_enabled)
        self.assertTrue(settings.features.dream_visible)
        self.assertTrue(settings.features.meme_enabled)

    def test_gateway_and_lane_manager_can_use_local_settings_views(self):
        defaults_mod = importlib.import_module(
            "astrmai.shared.constants.defaults"
        )
        lane_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.lane_manager"
        )
        gateway_mod = importlib.import_module(
            "astrmai.infrastructure.gateway.model_gateway"
        )

        settings = defaults_mod.build_infrastructure_settings(
            SimpleNamespace(
                provider=SimpleNamespace(task_models=["task"], agent_models=["agent"], fallback_models=["fb"], vision_models=["vision"]),
                infra=SimpleNamespace(max_concurrent_llm_calls=5, llm_retries=3, backoff_factor=1.2, api_timeout=18.0),
                global_settings=SimpleNamespace(debug_mode=True, enable_private_chat=False),
                system1=SimpleNamespace(nicknames=["Mai"]),
                sys3=SimpleNamespace(enable_work_mode=False),
                vision=SimpleNamespace(enable_vision=True),
                life=SimpleNamespace(enable_proactive=True, dream_visible=False),
                reply=SimpleNamespace(meme_probability=0),
            )
        )

        fake_context = SimpleNamespace()
        gateway = gateway_mod.GlobalModelGateway(fake_context, SimpleNamespace(), settings=settings.gateway)
        lane_manager = lane_mod.LaneManager(SimpleNamespace(), settings=settings.lane)

        self.assertEqual(gateway._api_timeout(), 18.0)
        self.assertEqual(gateway._task_models(), ["task"])
        self.assertEqual(lane_manager.settings.nicknames, ("Mai",))
        self.assertTrue(gateway._debug_mode())

    def test_proactive_rhythm_defaults_cross_midnight_quiet_hours(self):
        rhythm_mod = importlib.import_module("astrmai.proactive.rhythm")
        config = SimpleNamespace(life=SimpleNamespace(), reply=SimpleNamespace(base_frequency=0.3))
        quiet_ts = time.mktime((2026, 5, 11, 23, 45, 0, 0, 0, -1))
        morning_ts = time.mktime((2026, 5, 12, 8, 30, 0, 0, 0, -1))

        quiet = rhythm_mod.evaluate_proactive_rhythm(config, now=quiet_ts)
        morning = rhythm_mod.evaluate_proactive_rhythm(config, now=morning_ts)

        self.assertTrue(quiet.quiet_hours)
        self.assertEqual(quiet.time_bucket, "quiet")
        self.assertFalse(morning.quiet_hours)
        self.assertEqual(morning.time_bucket, "morning")
        self.assertGreater(quiet.base_frequency_factor, 1.0)

    def test_proactive_rhythm_empty_quiet_hours_disables_quiet_mode(self):
        rhythm_mod = importlib.import_module("astrmai.proactive.rhythm")
        config = SimpleNamespace(
            life=SimpleNamespace(proactive_quiet_hours=[]),
            reply=SimpleNamespace(base_frequency=0.7),
        )
        quiet_ts = time.mktime((2026, 5, 11, 23, 45, 0, 0, 0, -1))

        quiet = rhythm_mod.evaluate_proactive_rhythm(config, now=quiet_ts)

        self.assertFalse(quiet.quiet_hours)
        self.assertEqual(quiet.quiet_ranges, ())

    def test_proactive_rhythm_without_config_has_no_quiet_hours(self):
        rhythm_mod = importlib.import_module("astrmai.proactive.rhythm")
        quiet_ts = time.mktime((2026, 5, 11, 23, 45, 0, 0, 0, -1))

        quiet = rhythm_mod.evaluate_proactive_rhythm(None, now=quiet_ts)

        self.assertFalse(quiet.quiet_hours)
        self.assertEqual(quiet.quiet_ranges, ())

    def test_proactive_rhythm_logs_timezone_diagnostic_only_once(self):
        rhythm_mod = importlib.reload(importlib.import_module("astrmai.proactive.rhythm"))
        calls = []
        original_info = rhythm_mod.logger.info
        rhythm_mod._TZ_DIAGNOSTIC_LOGGED = False
        rhythm_mod.logger.info = lambda message: calls.append(message)
        config = SimpleNamespace(
            life=SimpleNamespace(proactive_quiet_hours=["23:30-07:30"]),
            reply=SimpleNamespace(base_frequency=0.7),
        )
        quiet_ts = time.mktime((2026, 5, 11, 23, 45, 0, 0, 0, -1))
        morning_ts = time.mktime((2026, 5, 12, 8, 30, 0, 0, 0, -1))

        try:
            quiet = rhythm_mod.evaluate_proactive_rhythm(config, now=quiet_ts)
            morning = rhythm_mod.evaluate_proactive_rhythm(config, now=morning_ts)
        finally:
            rhythm_mod.logger.info = original_info

        self.assertTrue(quiet.quiet_hours)
        self.assertFalse(morning.quiet_hours)
        self.assertEqual(len(calls), 1)
        self.assertIn("local timezone diagnostic", calls[0])
        self.assertIn("quiet_ranges=['23:30-07:30']", calls[0])

    def test_astrmai_config_preserves_runtime_config_fields(self):
        config_mod = importlib.import_module("config")

        parsed = config_mod.AstrMaiConfig(
            life={"proactive_quiet_hours": ["00:00-01:00"]},
            global_settings={"debug_mode": True},
        )

        self.assertEqual(parsed.life.proactive_quiet_hours, ["00:00-01:00"])
        self.assertTrue(parsed.global_settings.debug_mode)
        self.assertEqual(parsed.conversation.compaction_trigger_segments, 40)

    def test_astrmai_config_normalizes_legacy_memory_namespace(self):
        config_mod = importlib.import_module("config")

        parsed = config_mod.AstrMaiConfig(
            global_settings={"maintenance_hot_beta": 0.3},
        )

        self.assertEqual(parsed.memory.maintenance_hot_beta, 0.3)

    def test_project_schema_exposes_runtime_config_fields(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        memory_items = schema["memory"]["items"]
        global_items = schema["global_settings"]["items"]

        self.assertNotIn("webui_password", global_items)
        self.assertIn("proactive_quiet_hours", schema["life"]["items"])
        self.assertIn("conversation", schema)
        self.assertIn("deep_temporal_alpha", memory_items)
        self.assertIn("maintenance_temporal_stale_hot_threshold", memory_items)
        self.assertNotIn("deep_temporal_alpha", global_items)
        self.assertNotIn("maintenance_temporal_stale_hot_threshold", global_items)


class PersistenceBoundaryRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.persistence.orm_models", None)
        sys.modules.pop("astrmai.infrastructure.persistence.persistence_manager", None)
        sys.modules.pop("astrmai.infrastructure.persistence.database_service", None)
        sys.modules.pop("astrmai.infrastructure.persistence", None)
        sys.modules.pop("astrmai.infrastructure.persistence.persistence_manager", None)
        sys.modules.pop("astrmai.infrastructure.persistence.database_service", None)
        sys.modules.pop("astrmai.infrastructure.persistence.orm_models", None)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_local_persistence_adapters_are_not_legacy_subclasses(self):
        local_pm_mod = importlib.import_module(
            "astrmai.infrastructure.persistence.persistence_manager"
        )
        local_db_mod = importlib.import_module(
            "astrmai.infrastructure.persistence.database_service"
        )
        self.assertEqual(local_pm_mod.PersistenceManager.__module__, local_pm_mod.__name__)
        self.assertEqual(local_db_mod.DatabaseService.__module__, local_db_mod.__name__)
        self.assertNotIn("Legacy", local_pm_mod.PersistenceManager.__name__)
        self.assertNotIn("Legacy", local_db_mod.DatabaseService.__name__)

    def test_orm_models_no_longer_export_brain_action_plan(self):
        orm_mod = importlib.import_module(
            "astrmai.infrastructure.persistence.orm_models"
        )
        action_plan_mod = importlib.import_module(
            "astrmai.conversation.decision.action_plan"
        )

        self.assertFalse(hasattr(orm_mod, "BrainActionPlan"))
        plan = action_plan_mod.BrainActionPlan(action="REPLY", necessity=1.0)
        self.assertTrue(plan.should_act())


if __name__ == "__main__":
    unittest.main()
