import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _Event:
    def __init__(self, *, sender_id="user-1", self_id="bot-1", text="hello"):
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self.message_str = text
        self.message_obj = SimpleNamespace(self_id=self_id, message=[])
        self._sender_id = sender_id
        self._self_id = self_id
        self._extra = {}
        self.stopped = False

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return "Alice"

    def get_group_id(self):
        return "group-1"

    def get_self_id(self):
        return self._self_id

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def stop_event(self):
        self.stopped = True

    @staticmethod
    def plain_result(text):
        return {"type": "plain", "text": text}


class _Facade:
    def __init__(self):
        self.calls = []

    async def handle_poke(self, _event):
        self.calls.append("poke")
        return self.decision_type.allow()

    @staticmethod
    def is_framework_command(_message):
        return False

    def check_message_scope_access(self, _scope):
        self.calls.append("scope_access")
        return self.decision_type.allow()

    async def handle_group_reply_wait(self, _event, _scope):
        self.calls.append("group_wait")
        return "NONE"

    @staticmethod
    def is_debug_mode():
        return False

    def track_incoming_user_activity(self, sender_id):
        self.calls.append(("track", sender_id))

    async def try_consume_reflect_feedback(self, _event):
        self.calls.append("reflect")
        return None

    async def record_and_dispatch_attention(self, _event, _scope):
        self.calls.append("attention")
        return "BUFFERED"

    def cancel_group_wait_if_interrupted(self, _event, _group_wait_result, status):
        self.calls.append(("cancel_wait", status))

    @staticmethod
    def suppress_default_llm_if_engaged(_event, _status, _is_direct_call):
        return None

    @staticmethod
    def get_runtime_config():
        return SimpleNamespace(reply=SimpleNamespace(fallback_text="runtime fallback"))


class MessageEntryGapCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.presentation.events.message_entry", None)
        self.entry_mod = importlib.import_module("astrmai.presentation.events.message_entry")
        self.entry_mod.logger = SimpleNamespace(
            debug=lambda *_args, **_kwargs: None,
            info=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
            exception=lambda *_args, **_kwargs: None,
        )
        _Facade.decision_type = self.entry_mod.IngressDecision

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _collect(self, facade, event):
        async def _run():
            return [
                item
                async for item in self.entry_mod.handle_global_message(facade, event)
            ]

        with patch.object(
            self.entry_mod,
            "check_message_dedup",
            return_value=self.entry_mod.IngressDecision.allow(),
        ), patch.object(self.entry_mod, "is_direct_call_event", return_value=False):
            return asyncio.run(_run())

    def test_duplicate_message_stops_event_before_facade_guards(self):
        facade = _Facade()
        event = _Event()

        async def _run():
            with patch.object(
                self.entry_mod,
                "check_message_dedup",
                return_value=self.entry_mod.IngressDecision.stop("duplicate"),
            ):
                return [
                    item
                    async for item in self.entry_mod.handle_global_message(facade, event)
                ]

        self.assertEqual(asyncio.run(_run()), [])
        self.assertTrue(event.stopped)
        self.assertEqual(facade.calls, [])

    def test_self_message_stops_before_poke_handling(self):
        facade = _Facade()
        event = _Event(sender_id="bot-1", self_id="bot-1")

        self.assertEqual(self._collect(facade, event), [])
        self.assertTrue(event.stopped)
        self.assertEqual(facade.calls, [])

    def test_framework_command_exception_is_caught_and_processing_continues(self):
        facade = _Facade()
        event = _Event()

        with patch.object(
            self.entry_mod,
            "check_framework_command",
            side_effect=RuntimeError("command guard failed"),
        ):
            result = self._collect(facade, event)

        self.assertEqual(result, [])
        self.assertFalse(event.stopped)
        self.assertIn("scope_access", facade.calls)
        self.assertIn("attention", facade.calls)

    def test_poke_exception_is_caught_and_processing_continues(self):
        facade = _Facade()
        event = _Event()

        async def _raise(_event):
            raise RuntimeError("poke handler failed")

        facade.handle_poke = _raise

        self.assertEqual(self._collect(facade, event), [])
        self.assertFalse(event.stopped)
        self.assertIn("scope_access", facade.calls)
        self.assertIn("attention", facade.calls)

    def test_framework_command_decision_stops_event(self):
        facade = _Facade()
        event = _Event(text="/help")

        with patch.object(
            self.entry_mod,
            "check_framework_command",
            return_value=self.entry_mod.IngressDecision.stop("framework_command"),
        ):
            result = self._collect(facade, event)

        self.assertEqual(result, [])
        self.assertTrue(event.stopped)
        self.assertNotIn("scope_access", facade.calls)

    def test_scope_access_exception_is_caught_and_denied(self):
        facade = _Facade()
        event = _Event()

        def _raise(_scope):
            raise RuntimeError("permission backend failed")

        facade.check_message_scope_access = _raise

        self.assertEqual(self._collect(facade, event), [])
        self.assertTrue(event.stopped)
        self.assertNotIn("group_wait", facade.calls)

    def test_group_wait_exception_yields_error_and_stops_event(self):
        facade = _Facade()
        event = _Event()

        async def _raise(_event, _scope):
            raise RuntimeError("wait manager failed")

        facade.handle_group_reply_wait = _raise

        result = self._collect(facade, event)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "plain")
        self.assertTrue(result[0]["text"])
        self.assertTrue(event.stopped)
        self.assertNotIn("reflect", facade.calls)

    def test_attention_error_yields_runtime_fallback_text(self):
        facade = _Facade()
        event = _Event()

        async def _attention(_event, _scope):
            return "error"

        facade.record_and_dispatch_attention = _attention

        self.assertEqual(
            self._collect(facade, event),
            [{"type": "plain", "text": "runtime fallback"}],
        )
        self.assertFalse(event.stopped)

    def test_reflect_feedback_yields_response_and_stops_event(self):
        facade = _Facade()
        event = _Event()

        async def _feedback(_event):
            return "review accepted"

        facade.try_consume_reflect_feedback = _feedback

        self.assertEqual(
            self._collect(facade, event),
            [{"type": "plain", "text": "review accepted"}],
        )
        self.assertTrue(event.stopped)
        self.assertNotIn("attention", facade.calls)


if __name__ == "__main__":
    unittest.main()
