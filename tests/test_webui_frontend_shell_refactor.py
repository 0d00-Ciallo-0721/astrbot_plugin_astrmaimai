from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "astrmai" / "webui" / "frontend"


class WebuiFrontendShellRefactorTests(unittest.TestCase):
    def test_index_uses_shell_mounts_and_template_loader(self):
        content = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('src="js/template_loader.js"', content)
        self.assertIn('id="layout-overlays"', content)
        self.assertIn('id="layout-sidebar"', content)
        self.assertIn('id="page-slot-dashboard"', content)
        self.assertNotIn('page-dashboard" x-show=', content)

    def test_page_partials_exist_for_core_views(self):
        expected = [
            ROOT / "pages" / "login" / "index.html",
            ROOT / "pages" / "dashboard" / "index.html",
            ROOT / "pages" / "learning" / "index.html",
            ROOT / "pages" / "settings" / "index.html",
            ROOT / "pages" / "reviews" / "index.html",
            ROOT / "pages" / "memories" / "index.html",
            ROOT / "pages" / "users" / "index.html",
            ROOT / "pages" / "persona" / "index.html",
            ROOT / "components" / "overlays.html",
            ROOT / "components" / "sidebar.html",
        ]
        for path in expected:
            self.assertTrue(path.exists(), str(path))

    def test_frontend_contract_helpers_match_backend_api_shape(self):
        api_js = (ROOT / "js" / "api.js").read_text(encoding="utf-8")
        app_js = (ROOT / "js" / "app.js").read_text(encoding="utf-8")
        config_js = (ROOT / "js" / "pages" / "config.js").read_text(encoding="utf-8")
        settings_html = (ROOT / "pages" / "settings" / "index.html").read_text(encoding="utf-8")
        dashboard_html = (ROOT / "pages" / "dashboard" / "index.html").read_text(encoding="utf-8")
        learning_js = (ROOT / "js" / "pages" / "learning.js").read_text(encoding="utf-8")
        learning_html = (ROOT / "pages" / "learning" / "index.html").read_text(encoding="utf-8")
        dashboard_js = (ROOT / "js" / "pages" / "dashboard.js").read_text(encoding="utf-8")

        self.assertIn("segment: (value) => encodeURIComponent(String(value))", api_js)
        self.assertIn("window.app =", app_js)
        self.assertIn("confirm:", app_js)
        self.assertIn("sectionFields(section)", config_js)
        self.assertIn("section?.def?.items || section?.def?.keys", config_js)
        self.assertIn("sectionFields(section)", settings_html)
        self.assertNotIn("x-collapse", settings_html)
        self.assertNotIn("x-collapse", dashboard_html)
        self.assertIn("/runtime/capabilities", dashboard_js)
        self.assertIn("/tools/policy", dashboard_js)
        self.assertIn("/cognition/chats/${encodedChat}/recent-decisions?limit=20", dashboard_js)
        self.assertIn("/tools/chats/${encodedChat}/recent-calls?limit=20", dashboard_js)
        self.assertIn("openChatTraceDetails(chat.chat_id)", dashboard_html)
        self.assertIn("/memory-feedback/sources", learning_js)
        self.assertIn("/chats/${window.api.segment(chatId)}/runtime", learning_js)
        self.assertIn("/runtime/clear", learning_js)
        self.assertIn("/memory-feedback/${window.api.segment(feedbackId)}/disable", learning_js)
        self.assertIn("clearChatRuntime(chat.chat_id)", learning_html)

    def test_settings_page_exposes_advanced_config_actions(self):
        config_js = (ROOT / "js" / "pages" / "config.js").read_text(encoding="utf-8")
        settings_html = (ROOT / "pages" / "settings" / "index.html").read_text(encoding="utf-8")

        self.assertIn("window.api.put('/config', this.config)", config_js)
        self.assertIn("window.api.post('/config/reset')", config_js)
        self.assertIn("window.api.get('/config')", config_js)
        self.assertIn("saveAllConfig()", settings_html)
        self.assertIn("resetAllConfig()", settings_html)
        self.assertIn("openRawConfig()", settings_html)
        self.assertIn("meta.config_path", settings_html)
        self.assertIn("meta.schema_path", settings_html)


if __name__ == "__main__":
    unittest.main()
