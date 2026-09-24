import importlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from tests.helpers.astrbot_stubs import install_astrbot_stubs
from tests.test_workmode_router_refactor import _install_workmode_stubs


class CronAgentCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        _install_workmode_stubs()
        sys.modules.pop("astrmai.workmode.subagents.cron_agent", None)
        self.cron_mod = importlib.import_module("astrmai.workmode.subagents.cron_agent")
        self.cron_mod._CRON_TOOLS_CACHE = None

    def tearDown(self):
        self.cron_mod._CRON_TOOLS_CACHE = None
        sys.modules.pop("astrmai.workmode.subagents.cron_agent", None)
        self.temp_dir.cleanup()

    def _cron_tools_module(self, **exports):
        module = types.ModuleType("astrbot.core.tools.cron_tools")
        for name, value in exports.items():
            setattr(module, name, value)
        return module

    def test_modern_future_task_tool_exposes_supported_actions(self):
        schema = {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "edit", "delete", "list"],
                }
            },
            "required": ["action"],
        }

        class FutureTaskTool:
            name = "future_task"
            parameters = schema

        module = self._cron_tools_module(FutureTaskTool=FutureTaskTool)
        with patch.object(self.cron_mod, "import_module", return_value=module):
            tools, error = self.cron_mod._load_cron_tools()

        self.assertIsNone(error)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].name, "future_task")
        self.assertIn("action", tools[0].parameters["required"])
        self.assertEqual(
            tools[0].parameters["properties"]["action"]["enum"],
            ["create", "edit", "delete", "list"],
        )

        self.cron_mod._CRON_TOOLS_CACHE = None
        with patch.object(self.cron_mod, "import_module", return_value=module):
            tool_set = self._run_get_tool_set()
        self.assertEqual([tool.name for tool in tool_set.tools], ["future_task"])

    def test_legacy_three_tool_interface_is_preserved(self):
        expected = [
            types.SimpleNamespace(name="create_future_task"),
            types.SimpleNamespace(name="delete_future_task"),
            types.SimpleNamespace(name="list_future_tasks"),
        ]
        module = self._cron_tools_module(
            CREATE_CRON_JOB_TOOL=expected[0],
            DELETE_CRON_JOB_TOOL=expected[1],
            LIST_CRON_JOBS_TOOL=expected[2],
        )
        with patch.object(self.cron_mod, "import_module", return_value=module):
            tools, error = self.cron_mod._load_cron_tools()

        self.assertIsNone(error)
        self.assertEqual(list(tools), expected)

    def test_missing_cron_tools_module_reports_unavailable_interface(self):
        with patch.object(
            self.cron_mod,
            "import_module",
            side_effect=ModuleNotFoundError("cron_tools missing"),
        ):
            tools, error = self.cron_mod._load_cron_tools()
            self.assertEqual(list(tools), [])
            self.assertIn("cron_tools", error)
            self.assertIn("当前宿主未提供可识别的 Cron 工具接口", error)
            self.assertIn(
                "当前宿主未提供可识别的 Cron 工具接口",
                self._run_decline_reason(),
            )

            import asyncio

            result = asyncio.run(
                self.cron_mod.CronAgent().call(
                    types.SimpleNamespace(
                        context=types.SimpleNamespace(
                            context=types.SimpleNamespace(),
                            event=types.SimpleNamespace(unified_msg_origin="chat-1"),
                        )
                    ),
                    query="set a reminder",
                )
            )
            self.assertIn("[SUBAGENT_DECLINE]", result)
            self.assertIn("当前宿主未提供可识别的 Cron 工具接口", result)

    def test_cron_tools_module_initialization_error_is_reported(self):
        with patch.object(
            self.cron_mod,
            "import_module",
            side_effect=RuntimeError("cron_tools initialization failed"),
        ):
            tools, error = self.cron_mod._load_cron_tools()

        self.assertEqual(list(tools), [])
        self.assertIn("cron_tools initialization failed", error)
        self.assertIn("当前宿主未提供可识别的 Cron 工具接口", error)

    def test_failed_probe_is_retried_on_a_later_request(self):
        module = self._cron_tools_module(
            CREATE_CRON_JOB_TOOL=object(),
            DELETE_CRON_JOB_TOOL=object(),
            LIST_CRON_JOBS_TOOL=object(),
        )
        with patch.object(
            self.cron_mod,
            "import_module",
            side_effect=[ModuleNotFoundError("temporarily unavailable"), module],
        ):
            first_tools, first_error = self.cron_mod._load_cron_tools()
            second_tools, second_error = self.cron_mod._load_cron_tools()

        self.assertEqual(list(first_tools), [])
        self.assertIsNotNone(first_error)
        self.assertEqual(len(second_tools), 3)
        self.assertIsNone(second_error)

    def test_unknown_exports_report_unavailable_interface(self):
        module = self._cron_tools_module(OTHER_TOOL=object())
        with patch.object(self.cron_mod, "import_module", return_value=module):
            tools, error = self.cron_mod._load_cron_tools()

        self.assertEqual(list(tools), [])
        self.assertIn("当前宿主未提供可识别的 Cron 工具接口", error)

    def test_future_tool_initialization_failure_falls_back_to_legacy_tools(self):
        legacy = [
            types.SimpleNamespace(name="create_future_task"),
            types.SimpleNamespace(name="delete_future_task"),
            types.SimpleNamespace(name="list_future_tasks"),
        ]

        class FutureTaskTool:
            def __init__(self):
                raise RuntimeError("tool initialization failed")

        module = self._cron_tools_module(
            FutureTaskTool=FutureTaskTool,
            CREATE_CRON_JOB_TOOL=legacy[0],
            DELETE_CRON_JOB_TOOL=legacy[1],
            LIST_CRON_JOBS_TOOL=legacy[2],
        )
        with patch.object(self.cron_mod, "import_module", return_value=module):
            tools, error = self.cron_mod._load_cron_tools()

        self.assertIsNone(error)
        self.assertEqual(list(tools), legacy)

    def test_future_tool_initialization_failure_reports_unavailable_interface(self):
        class FutureTaskTool:
            def __init__(self):
                raise RuntimeError("tool initialization failed")

        module = self._cron_tools_module(FutureTaskTool=FutureTaskTool)
        with patch.object(self.cron_mod, "import_module", return_value=module):
            tools, error = self.cron_mod._load_cron_tools()

        self.assertEqual(list(tools), [])
        self.assertIn("tool initialization failed", error)
        self.assertIn("当前宿主未提供可识别的 Cron 工具接口", error)

    def _run_get_tool_set(self):
        import asyncio

        return asyncio.run(self.cron_mod.CronAgent().get_tool_set(None, None))

    def _run_decline_reason(self):
        import asyncio

        return asyncio.run(self.cron_mod.CronAgent()._get_decline_reason())


if __name__ == "__main__":
    unittest.main()
