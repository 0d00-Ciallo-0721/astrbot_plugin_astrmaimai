from pathlib import Path
from types import SimpleNamespace
import asyncio
import unittest


class PluginPagesAdminRefactorTests(unittest.TestCase):
    def setUp(self):
        from astrmai.webui.backend.adapters.plugin_api import get_active_facade

        self._original_active_facade = get_active_facade()

    def tearDown(self):
        from astrmai.webui.backend.adapters.plugin_api import set_active_facade

        set_active_facade(self._original_active_facade)

    def test_native_admin_api_registers_core_routes(self):
        from astrmai.webui.plugin_pages import PLUGIN_API_PREFIX, register_astrmai_admin_pages

        registered = []

        class _Context:
            def register_web_api(self, path, handler, methods, description):
                registered.append((path, handler, tuple(methods), description))

        register_astrmai_admin_pages(_Context(), SimpleNamespace(runtime=None))

        paths = {path for path, _, _, _ in registered}
        self.assertIn(f"{PLUGIN_API_PREFIX}/dashboard", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/runtime/capabilities", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/tools/policy", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/recent-decisions", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/recent-turns", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/chats/{{chat_id}}/turns", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/chats/{{chat_id}}/trace-events", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/chats/<chat_id>/trace-events", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/chats/{{chat_id}}/unified-timeline", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/observability/overview", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/observability/timeline", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/observability/chats/{{chat_id}}", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/observability/errors", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/observability/search", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/scheduler/status", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/scheduler/due-selection", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/scheduler/chats/{{chat_id}}", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/heartflow/chats", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/heartflow/impulses", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/heartflow/chats/{{chat_id}}/impulses", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/heartflow/timeline", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/heartflow/chats/{{chat_id}}/timeline", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/heartflow/topic-digests", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/proactive/intents", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/learning/status", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/memory-feedback/sources", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/reviews/{{id}}/submit", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/reviews/<id>/submit", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/memories/events/{{id}}", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/memories/events/<id>", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/users/{{user_id}}/slices/{{index}}", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/users/<user_id>/slices/<index>", paths)
        self.assertIn(f"{PLUGIN_API_PREFIX}/persona/slices", paths)
        self.assertNotIn(f"{PLUGIN_API_PREFIX}/persona", paths)
        self.assertFalse(any(path.startswith(f"{PLUGIN_API_PREFIX}/config") for path in paths))

        mutating = {(path, methods) for path, _, methods, _ in registered if methods != ("GET",)}
        self.assertIn((f"{PLUGIN_API_PREFIX}/reviews/{{id}}/submit", ("POST",)), mutating)
        self.assertIn((f"{PLUGIN_API_PREFIX}/reviews/<id>/submit", ("POST",)), mutating)
        self.assertIn((f"{PLUGIN_API_PREFIX}/reviews/{{id}}/delete", ("POST",)), mutating)
        self.assertIn((f"{PLUGIN_API_PREFIX}/memories/events/{{id}}/delete", ("POST",)), mutating)
        self.assertIn((f"{PLUGIN_API_PREFIX}/memories/events/<id>/delete", ("POST",)), mutating)
        self.assertIn((f"{PLUGIN_API_PREFIX}/memories/reflections/{{date}}/delete", ("POST",)), mutating)
        self.assertIn((f"{PLUGIN_API_PREFIX}/memories/nodes/{{id}}/delete", ("POST",)), mutating)
        self.assertIn((f"{PLUGIN_API_PREFIX}/memories/jargon/{{id}}/delete", ("POST",)), mutating)
        self.assertIn((f"{PLUGIN_API_PREFIX}/users/{{user_id}}", ("POST",)), mutating)
        self.assertIn((f"{PLUGIN_API_PREFIX}/users/{{user_id}}/delete", ("POST",)), mutating)
        self.assertIn((f"{PLUGIN_API_PREFIX}/users/{{user_id}}/slices/{{index}}/delete", ("POST",)), mutating)
        self.assertIn((f"{PLUGIN_API_PREFIX}/users/<user_id>/slices/<index>/delete", ("POST",)), mutating)
        self.assertNotIn((f"{PLUGIN_API_PREFIX}/persona/save", ("POST",)), mutating)

    def test_registered_plugin_page_handlers_accept_astrbot_path_kwargs(self):
        from astrmai.webui.plugin_pages import PLUGIN_API_PREFIX, register_astrmai_admin_pages

        registered = {}

        class _Context:
            def register_web_api(self, path, handler, methods, description):
                registered[path] = handler

        register_astrmai_admin_pages(_Context(), SimpleNamespace(runtime=None))
        response = asyncio.run(registered[f"{PLUGIN_API_PREFIX}/persona/slices"](object()))
        self.assertIsInstance(response, dict)
        self.assertEqual(response.get("status"), "ok")
        self.assertIn("data", response)

    def test_plugin_page_handlers_sanitize_path_values(self):
        from pathlib import Path

        from astrmai.webui.plugin_pages import _page_handler

        async def _handler(_request):
            return {"path": Path("C:/tmp/demo"), "nested": {"items": [Path("C:/tmp/child")]}}

        response = asyncio.run(_page_handler(_handler)())
        self.assertEqual(response["path"], "C:\\tmp\\demo")
        self.assertEqual(response["nested"]["items"][0], "C:\\tmp\\child")

    def test_admin_plugin_page_uses_astrbot_bridge_and_relative_assets(self):
        root = Path(__file__).resolve().parents[1] / "pages" / "admin"
        index_html = (root / "index.html").read_text(encoding="utf-8")
        app_js = (root / "app.js").read_text(encoding="utf-8")

        self.assertIn('href="./style.css"', index_html)
        self.assertIn('src="./app.js"', index_html)
        self.assertIn("window.AstrBotPluginPage", app_js)
        self.assertIn("Turn Context", app_js)
        self.assertIn("Think Level Budget", app_js)
        self.assertIn("Follow-up", app_js)
        self.assertIn("renderFollowUpSummary", app_js)
        self.assertIn("Side Inputs Timings", app_js)
        self.assertIn("renderSideInputTimings", app_js)
        self.assertIn("side_inputs", app_js)
        self.assertIn("think_level", app_js)
        self.assertIn("think_reason", app_js)
        self.assertIn("cognitive_loop_skipped_reason", app_js)
        self.assertIn("readonly_tools_allowed", app_js)
        self.assertIn("记忆裁决 Memory", app_js)
        self.assertIn("/cognition/recent-turns", app_js)
        self.assertIn("/cognition/chats/${segment(chatId)}/turns", app_js)
        self.assertIn("/cognition/observability/overview", app_js)
        self.assertIn("/cognition/observability/timeline", app_js)
        self.assertIn("/cognition/observability/search", app_js)
        self.assertIn("Scheduler Diagnostics", app_js)
        self.assertIn("/cognition/scheduler/status", app_js)
        self.assertIn("/cognition/scheduler/due-selection", app_js)
        self.assertIn("/cognition/scheduler/chats/${segment(targetChat)}", app_js)
        self.assertIn("暂无 loop state。该 chat 尚未进入 scheduler 跟踪。", app_js)
        self.assertIn("SCHEDULER_POLL_INTERVAL_MS = 5000", app_js)
        self.assertIn('state.current === "dashboard" && state.dashboardTab === "cognition"', app_js)
        self.assertIn("startSchedulerPolling()", app_js)
        self.assertIn("stopSchedulerPolling()", app_js)
        self.assertIn("${renderSchedulerDiagnosticsSection()}", app_js)
        self.assertNotIn('insertAdjacentHTML("afterbegin", renderSchedulerDiagnosticsSection())', app_js)
        self.assertIn('if (state.dashboardTab === "cognition") {\n    await renderDashboardCognition();\n    startSchedulerPolling();\n    return;\n  }', app_js)
        cognition_render_start = app_js.index("async function renderDashboardCognition()")
        cognition_render_end = app_js.index("function renderThinkLevelSummary", cognition_render_start)
        cognition_render = app_js[cognition_render_start:cognition_render_end]
        scheduler_idx = cognition_render.index("${renderSchedulerDiagnosticsSection()}")
        observability_idx = cognition_render.index("Global Observability Timeline")
        cognition_idx = cognition_render.index("主动决策池 Cognition")
        turn_context_idx = cognition_render.index("Turn Context")
        self.assertLess(scheduler_idx, cognition_idx)
        self.assertLess(scheduler_idx, observability_idx)
        self.assertLess(observability_idx, cognition_idx)
        self.assertLess(cognition_idx, turn_context_idx)
        self.assertIn("Impulse Safety", app_js)
        self.assertIn("/heartflow/impulses", app_js)
        self.assertIn("/heartflow/chats/${segment(chatId)}/impulses", app_js)
        self.assertIn("Heartflow Timeline", app_js)
        self.assertIn("Heartflow Sessions", app_js)
        self.assertIn("Proactive Intents", app_js)
        self.assertIn("topic-digests", app_js)
        self.assertIn("/heartflow/timeline", app_js)
        self.assertIn("/heartflow/chats/${segment(chatId)}/timeline", app_js)
        self.assertIn("/heartflow/topic-digests", app_js)
        self.assertIn("主动意图轨迹", app_js)
        self.assertIn("/proactive/intents", app_js)
        self.assertIn("removed_by_energy", app_js)
        self.assertIn("removed_by_cooldown", app_js)
        self.assertIn("removed_by_social_intent", app_js)
        self.assertIn("apiGet", app_js)
        self.assertIn("apiPost", app_js)
        self.assertIn("/persona/slices", app_js)
        self.assertIn("角色切片", app_js)
        self.assertIn("renderPersonaSlices", app_js)
        self.assertIn("renderShardCards", app_js)
        self.assertIn("角色切片读取失败", app_js)
        self.assertIn("Bridge 初始化中", app_js)
        self.assertIn("Bridge 连接失败", app_js)
        self.assertNotIn("/config/replace", app_js)
        self.assertNotIn("/config/", app_js)
        self.assertNotIn('"/config"', app_js)
        self.assertNotIn("/persona\"", app_js)
        self.assertNotIn("/persona/save", app_js)
        self.assertNotIn("raw_preview", app_js)
        self.assertNotIn("查看原始人格预览", app_js)
        self.assertNotIn("renderConfigField", app_js)
        self.assertNotIn("collectSectionValue", app_js)
        self.assertNotIn("data-config-field", app_js)
        self.assertNotIn("apiPut", app_js)
        self.assertNotIn("apiDelete", app_js)
        self.assertNotIn("api.put", app_js)
        self.assertNotIn("api.delete", app_js)
        self.assertIn('const API_PREFIX = "admin"', app_js)
        self.assertIn("pluginEndpoint", app_js)
        self.assertIn("readyBridge", app_js)
        self.assertIn("waitForBridge", app_js)
        self.assertIn("DOMContentLoaded", app_js)
        self.assertNotIn('const API_PREFIX = "/astrmai/admin"', app_js)
        self.assertNotIn("/astrmai/admin", app_js)

        forbidden = [
            "localStorage.getItem('token')",
            'localStorage.getItem("token")',
            "/auth/login",
            "/auth/verify",
            "base: '/api'",
            'base: "/api"',
            "currentPage: 'login'",
        ]
        for marker in forbidden:
            self.assertNotIn(marker, app_js)
            self.assertNotIn(marker, index_html)

    def test_admin_page_exposes_core_management_tabs(self):
        app_js = (Path(__file__).resolve().parents[1] / "pages" / "admin" / "app.js").read_text(encoding="utf-8")
        for label in ["Dashboard", "主动学习", "表达审核", "记忆网络", "用户画像", "角色切片"]:
            self.assertIn(label, app_js)
        self.assertNotIn('"系统配置"', app_js)
        self.assertNotIn('"人格设定"', app_js)
        for label in [
            "运行概览",
            "心智流 Heartflow",
            "主动决策池 Cognition",
            "工具链观测 Tools",
            "主动组件调度",
            "记忆反馈",
            "待审队列",
            "全量库查阅",
            "记忆碎片 Events",
            "每日反思 Reflections",
            "实体图谱 Nodes",
            "黑话字典 Jargon",
            "基础画像",
            "长期记忆点",
            "角色切片诊断 Persona Slices",
            "核心摘要 Summary",
            "第一人称自觉 First Person Rewrite",
            "八维角色切片",
        ]:
            self.assertIn(label, app_js)

    def test_memory_ui_service_uses_package_relative_memory_import(self):
        service_path = (
            Path(__file__).resolve().parents[1]
            / "astrmai"
            / "webui"
            / "backend"
            / "services"
            / "memory_ui_service.py"
        )
        content = service_path.read_text(encoding="utf-8")
        self.assertIn("from ....memory.contracts.memory_query import MemoryWriteRequest", content)
        self.assertNotIn("from astrmai.memory.contracts.memory_query import MemoryWriteRequest", content)


if __name__ == "__main__":
    unittest.main()
