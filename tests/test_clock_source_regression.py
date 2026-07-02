"""Regression tests for clock source unification fix (R4).

Verifies that group_reply_wait_manager and private_chat_manager use
time.monotonic() consistently instead of mixing monotonic() with time.time().
"""

import asyncio
import importlib
import types
import sys
import unittest
from time import monotonic


def _install_pm_stubs():
    """Install minimal stubs for AstrBot so state modules can be imported."""
    api_mod = types.ModuleType("astrbot.api")
    api_mod.logger = type("Logger", (), {
        "info": lambda *a, **kw: None,
        "debug": lambda *a, **kw: None,
        "warning": lambda *a, **kw: None,
        "error": lambda *a, **kw: None,
        "exception": lambda *a, **kw: None,
    })
    sys.modules["astrbot.api"] = api_mod

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event_mod.filter = type("Filter", (), {
        "EventMessageType": type("EventMessageType", (), {
            "ALL": "ALL",
        }),
        "PermissionType": type("PermissionType", (), {}),
        "PlatformAdapterType": type("PlatformAdapterType", (), {}),
    })
    sys.modules["astrbot.api.event"] = event_mod


class ClockSourceRegressionTests(unittest.TestCase):
    """Regression tests for R4: clock source unification."""

    @classmethod
    def setUpClass(cls):
        _install_pm_stubs()

    # ── group_reply_wait_manager ──

    def test_group_wait_expires_at_uses_monotonic(self):
        """expires_at is set with monotonic() so timeout comparisons work."""
        mgr_mod = importlib.import_module(
            "astrmai.state.group_wait.group_reply_wait_manager"
        )

        # Read the source to verify monotonic() is used for expires_at
        import inspect
        source = inspect.getsource(mgr_mod.GroupReplyWaitManager.register_from_reply_event)
        self.assertIn("expires_at=monotonic()", source,
                      "expires_at must use monotonic()")

    def test_group_wait_handle_incoming_uses_monotonic(self):
        """handle_incoming_message must use monotonic() for 'now', not time.time()."""
        mgr_mod = importlib.import_module(
            "astrmai.state.group_wait.group_reply_wait_manager"
        )
        import inspect
        source = inspect.getsource(mgr_mod.GroupReplyWaitManager.handle_incoming_message)
        self.assertNotIn("time.time()", source,
                         "handle_incoming_message must not use time.time()")
        self.assertIn("monotonic()", source,
                      "handle_incoming_message must use monotonic()")

    def test_group_wait_timeout_not_immediate(self):
        """A freshly-armed wait state should not expire immediately."""
        mgr_mod = importlib.import_module(
            "astrmai.state.group_wait.group_reply_wait_manager"
        )
        mgr = mgr_mod.GroupReplyWaitManager()

        # Create a simple mock event that triggers wait registration
        class MockEvent:
            def get_sender_id(self):
                return "user_a"
            def get_group_id(self):
                return "group_1"
            def get_sender_name(self):
                return "sender_a"
            _extra = {}
            def get_extra(self, key, default=None):
                return self._extra.get(key, default)
            def set_extra(self, key, value):
                self._extra[key] = value
            @property
            def unified_msg_origin(self):
                return "group_1"

        # register_from_reply_event requires astrmai_wait_targets extra to arm a wait
        event = MockEvent()
        event._extra["astrmai_wait_targets"] = ["user_b"]
        event._extra["astrmai_wait_target_name"] = "user_b"
        mgr.register_from_reply_event(event)

        # Immediately check — should NOT be expired (RESUMED_TIMEOUT or OBSERVED confirms non-expired state)
        check_event = MockEvent()
        result = mgr.handle_incoming_message(check_event)
        self.assertIn(result, ("RESUMED_TIMEOUT", "OBSERVED"),
                      f"Fresh wait state should not expire immediately; "
                      f"got {result} — this indicates clock source mismatch")

    # ── private_chat_manager ──

    def test_private_chat_last_message_uses_monotonic(self):
        """signal_new_message must use monotonic() for last_message_time."""
        pm_mod = importlib.import_module(
            "astrmai.state.private_chat.private_chat_manager"
        )
        import inspect
        source = inspect.getsource(pm_mod.PrivateChatManager.signal_new_message)
        self.assertIn("last_message_time = monotonic()", source,
                      "signal_new_message must use monotonic()")

    def test_private_chat_silence_uses_monotonic(self):
        """get_session_info silence_sec must use monotonic() not time.time()."""
        pm_mod = importlib.import_module(
            "astrmai.state.private_chat.private_chat_manager"
        )
        import inspect
        source = inspect.getsource(pm_mod.PrivateChatManager.get_session_info)
        self.assertNotIn("time.time()", source,
                         "get_session_info must not use time.time()")

    def test_private_chat_cleanup_uses_monotonic(self):
        """cleanup_stale_sessions must use monotonic() for 'now'."""
        pm_mod = importlib.import_module(
            "astrmai.state.private_chat.private_chat_manager"
        )
        import inspect
        source = inspect.getsource(pm_mod.PrivateChatManager.cleanup_stale_sessions)
        self.assertNotIn("time.time()", source,
                         "cleanup_stale_sessions must not use time.time()")

    def test_private_chat_no_immediate_stale(self):
        """A fresh session should not appear stale immediately."""
        pm_mod = importlib.import_module(
            "astrmai.state.private_chat.private_chat_manager"
        )
        mgr = pm_mod.PrivateChatManager()

        async def _run():
            await mgr.signal_new_message("test_user", message_str="hello")
            session_info = mgr.get_session_info("test_user")
            return session_info

        session_info = asyncio.run(_run())
        self.assertIsNotNone(session_info)
        # silence_sec should be very small (just created), not in the billions
        silence = session_info["silence_sec"]
        self.assertLess(silence, 10.0,
                        f"Fresh session silence_sec={silence} is too large; "
                        f"indicates monotonic/time.time mismatch")


if __name__ == "__main__":
    unittest.main()
