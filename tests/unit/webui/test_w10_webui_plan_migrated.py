import asyncio
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


class W10WebuiPlanMigratedTests(unittest.TestCase):
    def test_access_layer_uses_framework_identity_or_default(self):
        access_mod = importlib.import_module("astrmai.webui.backend.access")
        self.assertEqual(asyncio.run(access_mod.get_current_user("admin-user")), "admin-user")
        self.assertEqual(asyncio.run(access_mod.get_current_user(None)), "astrbot-plugin-page")

    def test_write_config_is_side_effect_free(self):
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            schema_path = Path(tmp_dir) / "schema.json"
            persona_path = Path(tmp_dir) / "persona.json"
            schema_path.write_text("{}", encoding="utf-8")
            persona_path.write_text("{}", encoding="utf-8")
            adapter = adapter_mod.PluginApiAdapter(
                facade=None,
                config_path=str(config_path),
                schema_path=str(schema_path),
                persona_cache_path=str(persona_path),
            )

            payload = {"global_settings": {"debug_mode": True}, "persona": {"persona_id": "alpha"}}
            asyncio.run(adapter.write_config(payload))
            written_before = config_path.read_text(encoding="utf-8")
            written_after = config_path.read_text(encoding="utf-8")
            self.assertEqual(json.loads(written_before), payload)
            self.assertEqual(written_before, written_after)

    def test_reset_all_uses_schema_defaults_without_legacy_auth_fields(self):
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")
        service_mod = importlib.import_module("astrmai.webui.backend.services.settings_ui_service")
        previous_facade = adapter_mod.get_active_facade()

        try:
            adapter_mod.set_active_facade(None)
            with tempfile.TemporaryDirectory() as tmp_dir:
                config_path = Path(tmp_dir) / "config.json"
                schema_path = Path(tmp_dir) / "schema.json"
                persona_path = Path(tmp_dir) / "persona.json"
                schema_path.write_text(
                    json.dumps(
                        {
                            "reply": {
                                "type": "object",
                                "items": {
                                    "enabled": {"type": "bool", "default": True},
                                },
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                config_path.write_text(
                    json.dumps(
                        {
                            "global_settings": {"debug_mode": True},
                            "reply": {"enabled": False},
                        }
                    ),
                    encoding="utf-8",
                )
                persona_path.write_text("{}", encoding="utf-8")

                adapter = adapter_mod.PluginApiAdapter(
                    facade=None,
                    config_path=str(config_path),
                    schema_path=str(schema_path),
                    persona_cache_path=str(persona_path),
                )
                service = service_mod.SettingsUiService(adapter)
                asyncio.run(service.reset_all())
                saved = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertNotIn("global_settings", saved)
                self.assertEqual(saved["reply"]["enabled"], True)
        finally:
            adapter_mod.set_active_facade(previous_facade)

    def test_persona_get_and_update_round_trip_current_persona(self):
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")
        persona_mod = importlib.import_module("astrmai.webui.backend.services.persona_ui_service")

        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            schema_path = Path(tmp_dir) / "schema.json"
            persona_path = Path(tmp_dir) / "persona.json"
            config_path.write_text(json.dumps({"persona": {"persona_id": "alpha"}}), encoding="utf-8")
            schema_path.write_text("{}", encoding="utf-8")
            persona_path.write_text(
                json.dumps(
                    {
                        "alpha": {
                            "summary": "old-summary",
                            "first_person_rewrite": "old-first",
                            "style": "calm",
                        },
                        "beta": {"summary": "keep-me"},
                    }
                ),
                encoding="utf-8",
            )

            adapter = adapter_mod.PluginApiAdapter(
                facade=None,
                config_path=str(config_path),
                schema_path=str(schema_path),
                persona_cache_path=str(persona_path),
            )
            service = persona_mod.PersonaUiService(adapter)

            current = asyncio.run(service.get_persona())
            self.assertEqual(current["summary"], "old-summary")
            self.assertEqual(current["first_person_rewrite"], "old-first")

            updated = asyncio.run(
                service.update_persona(
                    {
                        "summary": "new-summary",
                        "first_person_rewrite": "new-first",
                        "custom_field": "preserved",
                    }
                )
            )
            self.assertEqual(updated["summary"], "new-summary")
            reloaded = asyncio.run(service.get_persona())
            self.assertEqual(reloaded["summary"], "new-summary")
            self.assertEqual(reloaded["first_person_rewrite"], "new-first")

            saved = json.loads(persona_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["beta"]["summary"], "keep-me")
            self.assertEqual(saved["alpha"]["custom_field"], "preserved")

    def test_backend_routes_drop_memory_feedback_delete_alias_and_use_admin_service(self):
        routes_mod = importlib.import_module("astrmai.webui.backend.routes")
        router = routes_mod.build_api_router()
        paths = {route.path for route in router.routes}
        self.assertIn("/memory-feedback/{feedback_id}/disable", paths)
        self.assertNotIn("/memory-feedback/{feedback_id}", paths)

        repo_root = Path(__file__).resolve().parents[3]
        proactive = (repo_root / "astrmai" / "webui" / "backend" / "routes" / "proactive_routes.py").read_text(encoding="utf-8")
        chats = (repo_root / "astrmai" / "webui" / "backend" / "routes" / "chats_routes.py").read_text(encoding="utf-8")
        feedback = (repo_root / "astrmai" / "webui" / "backend" / "routes" / "memory_feedback_routes.py").read_text(encoding="utf-8")
        for content in (proactive, chats, feedback):
            self.assertIn("from ..services.admin_ui_service import AdminUiService", content)
            self.assertNotIn("from ..services.chatruntimeservice import ChatRuntimeService", content)

    def test_plugin_pages_do_not_register_memory_feedback_delete_alias(self):
        plugin_pages_mod = importlib.import_module("astrmai.webui.plugin_pages")

        registered = []

        class _Context:
            def register_web_api(self, path, handler, methods, description):
                registered.append((path, tuple(methods)))

        plugin_pages_mod.register_astrmai_admin_pages(_Context(), SimpleNamespace(runtime=None))
        self.assertIn((f"{plugin_pages_mod.PLUGIN_API_PREFIX}/memory-feedback/{{feedback_id}}/disable", ("POST",)), registered)
        self.assertNotIn((f"{plugin_pages_mod.PLUGIN_API_PREFIX}/memory-feedback/{{feedback_id}}", ("DELETE",)), registered)

    def test_chat_runtime_service_no_longer_delegates_to_admin_ui_service(self):
        repo_root = Path(__file__).resolve().parents[3]
        content = (
            repo_root
            / "astrmai"
            / "webui"
            / "backend"
            / "services"
            / "chatruntimeservice.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("from .admin_ui_service import AdminUiService", content)

    def test_server_cors_origins_reads_env(self):
        server_mod = importlib.import_module("astrmai.webui.backend.server")
        original = os.environ.get("ASTRMAI_CORS_ORIGINS")
        try:
            os.environ["ASTRMAI_CORS_ORIGINS"] = "http://localhost:8765, http://127.0.0.1:8787"
            self.assertEqual(
                server_mod._cors_origins(),
                ["http://localhost:8765", "http://127.0.0.1:8787"],
            )
        finally:
            if original is None:
                os.environ.pop("ASTRMAI_CORS_ORIGINS", None)
            else:
                os.environ["ASTRMAI_CORS_ORIGINS"] = original

    def test_standalone_auth_module_is_removed(self):
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("astrmai.webui.backend.auth")

    def test_plugin_page_is_auth_free_and_uses_astrbot_bridge(self):
        repo_root = Path(__file__).resolve().parents[3]
        index_html = (repo_root / "pages" / "admin" / "index.html").read_text(encoding="utf-8")
        app_js = (repo_root / "pages" / "admin" / "app.js").read_text(encoding="utf-8")

        self.assertIn("window.AstrBotPluginPage", app_js)
        self.assertIn("readyBridge", app_js)
        self.assertIn("pluginEndpoint", app_js)
        self.assertNotIn("/auth/login", app_js)
        self.assertNotIn("/auth/logout", app_js)
        self.assertNotIn("/auth/verify", app_js)
        self.assertNotIn("localStorage", app_js)
        self.assertNotIn("sessionStorage", app_js)
        self.assertNotIn("currentPage: 'login'", index_html)

    def test_remote_image_allowlist_artifacts_are_removed(self):
        repo_root = Path(__file__).resolve().parents[3]
        config_py = (repo_root / "config.py").read_text(encoding="utf-8")
        schema_json = (repo_root / "_conf_schema.json").read_text(encoding="utf-8")

        self.assertNotIn("remote_image_host_suffixes", config_py)
        self.assertNotIn('"remote_image_host_suffixes"', schema_json)
        self.assertFalse((repo_root / "REMOTE_IMAGE_ALLOWLIST.md").exists())

    def test_standalone_auth_routes_are_not_exposed_by_aggregated_router(self):
        repo_root = Path(__file__).resolve().parents[3]
        routes_py = (repo_root / "astrmai" / "webui" / "backend" / "routes.py").read_text(encoding="utf-8")
        self.assertNotIn("auth_router", routes_py)
        self.assertIn("from .routes import api_router, build_api_router", routes_py)
        self.assertNotIn("router.include_router(", routes_py)


if __name__ == "__main__":
    unittest.main()
