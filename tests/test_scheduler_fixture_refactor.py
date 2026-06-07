import asyncio
import importlib
import os
import sqlite3
import unittest
from pathlib import Path


class SchedulerFixtureRefactorTests(unittest.TestCase):
    def setUp(self):
        plugin_api_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")
        self._plugin_api_mod = plugin_api_mod
        self._original_active_facade = plugin_api_mod.get_active_facade()
        self._original_env = {
            "ASTRMAI_DB_PATH": os.environ.get("ASTRMAI_DB_PATH"),
            "ASTRMAI_CONFIG_PATH": os.environ.get("ASTRMAI_CONFIG_PATH"),
            "ASTRMAI_PERSONA_CACHE_PATH": os.environ.get("ASTRMAI_PERSONA_CACHE_PATH"),
        }

    def tearDown(self):
        self._plugin_api_mod.set_active_facade(self._original_active_facade)
        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _activate_fixture(self, profile: str):
        fixture_mod = importlib.import_module("tests.helpers.scheduler_webui_fixture")
        summary = fixture_mod.ensure_fixture_files(profile=profile)
        os.environ["ASTRMAI_DB_PATH"] = summary["db_path"]
        os.environ["ASTRMAI_CONFIG_PATH"] = summary["config_path"]
        os.environ["ASTRMAI_PERSONA_CACHE_PATH"] = summary["persona_cache_path"]
        facade = fixture_mod.build_fixture_facade_sync(profile=profile)
        self._plugin_api_mod.set_active_facade(facade)
        return fixture_mod, summary, facade

    def test_fixture_profiles_reset_summary_and_assets_are_repeatable(self):
        fixture_mod = importlib.import_module("tests.helpers.scheduler_webui_fixture")
        scheduler_summary = fixture_mod.ensure_fixture_files(profile="scheduler_only")
        admin_summary = fixture_mod.ensure_fixture_files(profile="admin_full")
        host_summary = fixture_mod.ensure_fixture_files(profile="acceptance_host")

        self.assertEqual(scheduler_summary["profile"], "scheduler_only")
        self.assertEqual(admin_summary["profile"], "admin_full")
        self.assertEqual(host_summary["profile"], "acceptance_host")
        self.assertLess(scheduler_summary["user_count"], admin_summary["user_count"])
        self.assertLess(scheduler_summary["memory_event_count"], admin_summary["memory_event_count"])
        self.assertGreater(host_summary["runtime_snapshots"], admin_summary["runtime_snapshots"] - 1)
        self.assertTrue(Path(admin_summary["db_path"]).exists())
        self.assertTrue(Path(admin_summary["config_path"]).exists())
        self.assertTrue(Path(admin_summary["persona_cache_path"]).exists())
        self.assertTrue(Path(admin_summary["direct_open_harness_path"]).exists())
        baseline_doc = Path(admin_summary["acceptance_baseline_dir"]) / "acceptance_baseline.md"
        self.assertTrue(baseline_doc.exists())
        self.assertIn("iframe", baseline_doc.read_text(encoding="utf-8"))

        with sqlite3.connect(admin_summary["db_path"]) as conn:
            conn.execute("INSERT INTO UserProfile(user_id) VALUES ('extra-user')")
            conn.commit()
            count_dirty = conn.execute("SELECT COUNT(*) FROM UserProfile").fetchone()[0]
        self.assertGreater(count_dirty, admin_summary["table_counts"]["UserProfile"])

        reset_summary = fixture_mod.ensure_fixture_files(profile="admin_full")
        with sqlite3.connect(reset_summary["db_path"]) as conn:
            count_reset = conn.execute("SELECT COUNT(*) FROM UserProfile").fetchone()[0]
        self.assertEqual(count_reset, reset_summary["table_counts"]["UserProfile"])

    def test_admin_full_fixture_supports_backend_service_views(self):
        _, summary, facade = self._activate_fixture("admin_full")
        dashboard_mod = importlib.import_module("astrmai.webui.backend.services.dashboard_service")
        admin_mod = importlib.import_module("astrmai.webui.backend.services.admin_ui_service")
        review_mod = importlib.import_module("astrmai.webui.backend.services.review_ui_service")
        memory_mod = importlib.import_module("astrmai.webui.backend.services.memory_ui_service")
        user_mod = importlib.import_module("astrmai.webui.backend.services.user_ui_service")
        persona_mod = importlib.import_module("astrmai.webui.backend.services.persona_ui_service")
        db_mod = importlib.import_module("astrmai.webui.backend.db")

        adapter = self._plugin_api_mod.PluginApiAdapter(
            facade=facade,
            config_path=summary["config_path"],
            persona_cache_path=summary["persona_cache_path"],
        )
        dashboard = asyncio.run(dashboard_mod.DashboardService(adapter, db_mod.get_db).get_snapshot())
        admin_status = asyncio.run(admin_mod.AdminUiService(adapter, db_mod.get_db).scheduler_status_view())
        pending_reviews = asyncio.run(review_mod.ReviewUiService(adapter, db_mod.get_db).list_pending())
        all_reviews = asyncio.run(review_mod.ReviewUiService(adapter, db_mod.get_db).list_reviews(page_size=10))
        memory_service = memory_mod.MemoryUiService(db_mod.get_db, adapter)
        events = asyncio.run(memory_service.list_events())
        reflections = asyncio.run(memory_service.list_reflections(""))
        nodes = asyncio.run(memory_service.list_nodes())
        jargon = asyncio.run(memory_service.list_jargon(status="", group_id="group-fixture", query=""))
        canonical = asyncio.run(memory_service.list_canonical())
        users = asyncio.run(user_mod.UserUiService(db_mod.get_db).list_users())
        persona = asyncio.run(persona_mod.PersonaUiService(adapter).get_persona_slices())

        self.assertEqual(dashboard["total_users"], 3)
        self.assertGreaterEqual(dashboard["total_canonical_memories"], 6)
        self.assertEqual(admin_status["data"]["scheduler_policy"]["active_profile"], "balanced")
        self.assertGreaterEqual(admin_status["data"]["overview"]["due_chat_count"], 1)
        self.assertEqual(len(pending_reviews), 1)
        self.assertGreaterEqual(all_reviews["total"], 2)
        self.assertGreaterEqual(len(events), 3)
        self.assertGreaterEqual(len(reflections), 2)
        self.assertGreaterEqual(len(nodes), 2)
        self.assertGreaterEqual(len(jargon), 2)
        self.assertGreaterEqual(canonical["total"], 6)
        self.assertEqual(len(users), 3)
        self.assertEqual(persona["status"], "ok")
        self.assertTrue(persona["data"]["summary"])
        self.assertEqual(persona["data"]["persona_id"], "fixture-persona")

    def test_acceptance_host_direct_open_harness_contains_bridge_stub_contract(self):
        fixture_mod = importlib.import_module("tests.helpers.scheduler_webui_fixture")
        summary = fixture_mod.ensure_fixture_files(profile="acceptance_host")
        harness = Path(summary["direct_open_harness_path"]).read_text(encoding="utf-8")

        self.assertIn("window.AstrBotPluginPage", harness)
        self.assertIn("ready: async", harness)
        self.assertIn("apiGet: async", harness)
        self.assertIn("apiPost: async", harness)
        self.assertIn('const fixtureBase = "http://127.0.0.1:8765"', harness)
        self.assertIn('href="http://127.0.0.1:8766/pages/admin/style.css"', harness)
        self.assertIn('src="http://127.0.0.1:8766/pages/admin/app.js"', harness)
        self.assertNotIn('/api/auth/login', harness)
        self.assertNotIn('password: "astrmai_admin"', harness)
        self.assertNotIn('"Authorization": token ? `Bearer ${token}` : ""', harness)
        self.assertIn('clean.startsWith("admin/") ? clean.slice("admin/".length) : clean', harness)
        self.assertIn("绕开 AstrBot 宿主页 iframe 边界", harness)
        self.assertIn("AstrMai 管理台直开验收页", harness)


if __name__ == "__main__":
    unittest.main()
