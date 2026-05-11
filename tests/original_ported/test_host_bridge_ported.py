import unittest
from types import SimpleNamespace

from astrmai.infrastructure.runtime.host_bridge import HostBridge


class HostBridgeTests(unittest.TestCase):
    def test_suppress_default_llm_returns_ghost_sentinel(self):
        bridge = HostBridge()
        event = SimpleNamespace(call_llm=False)
        sentinel = bridge.suppress_default_llm(event)
        self.assertTrue(event.call_llm)
        self.assertEqual(sentinel, HostBridge.GHOST_SENTINEL)
        self.assertTrue(bridge.is_ghost_sentinel(sentinel))

    def test_error_alert_contains_chat_context(self):
        bridge = HostBridge()
        event = SimpleNamespace(
            get_group_id=lambda: "123",
            get_sender_id=lambda: "456",
            get_sender_name=lambda: "测试用户",
        )
        message = bridge.build_admin_alert(event, "All chat models failed")
        self.assertIn("群聊(123)", message)
        self.assertIn("测试用户", message)
        self.assertTrue(bridge.should_intercept_error("All chat models failed"))


if __name__ == "__main__":
    unittest.main()
