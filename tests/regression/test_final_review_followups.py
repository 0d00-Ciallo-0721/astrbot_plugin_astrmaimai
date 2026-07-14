import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrmai.conversation.ingress import dedupe


class _Event:
    def __init__(self, *, message_id: str, text: str = "same text"):
        self.message_str = text
        self.message_obj = SimpleNamespace(message_id=message_id, message=text)
        self.unified_msg_origin = "default:GroupMessage:group-1"

    def get_sender_id(self):
        return "user-1"


class FinalReviewFollowupTests(unittest.TestCase):
    def setUp(self):
        dedupe._debounce_cache.clear()

    def tearDown(self):
        dedupe._debounce_cache.clear()

    def test_message_dedup_prefers_message_id_over_equal_content(self):
        first = dedupe.check_message_dedup(_Event(message_id="msg-1"))
        second = dedupe.check_message_dedup(_Event(message_id="msg-2"))
        duplicate = dedupe.check_message_dedup(_Event(message_id="msg-1"))

        self.assertFalse(first.should_stop)
        self.assertFalse(second.should_stop)
        self.assertTrue(duplicate.should_stop)

    def test_message_dedup_retains_content_fallback_without_message_id(self):
        first = dedupe.check_message_dedup(_Event(message_id="", text="fallback"))
        duplicate = dedupe.check_message_dedup(_Event(message_id="", text="fallback"))

        self.assertFalse(first.should_stop)
        self.assertTrue(duplicate.should_stop)

    def test_plugin_imports_as_package_without_plugin_root_on_sys_path(self):
        plugin_root = Path(__file__).resolve().parents[2]
        script = (
            "import sys; "
            f"sys.path.insert(0, {str(plugin_root.parent)!r}); "
            f"import {plugin_root.name}.main as plugin_main; "
            "assert plugin_main.AstrMaiPlugin.__name__ == 'AstrMaiPlugin'"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [sys.executable, "-I", "-c", script],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_global_event_handlers_accept_host_context_arguments(self):
        plugin_root = Path(__file__).resolve().parents[2]
        script = (
            "import inspect, sys; "
            f"sys.path.insert(0, {str(plugin_root.parent)!r}); "
            f"from {plugin_root.name}.main import AstrMaiPlugin; "
            "inspect.signature(AstrMaiPlugin.on_global_message).bind(object(), object(), 1, 2, 3); "
            "inspect.signature(AstrMaiPlugin.on_group_membership_notice).bind(object(), object(), 1, 2, 3)"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [sys.executable, "-I", "-c", script],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
