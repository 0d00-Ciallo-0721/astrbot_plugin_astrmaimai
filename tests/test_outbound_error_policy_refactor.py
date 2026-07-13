import asyncio
import unittest


class _FakeHostBridge:
    def is_ghost_sentinel(self, message_str):
        return message_str == "[[ghost]]"

    def should_intercept_error(self, message_str, enabled=True):
        return "Traceback" in message_str

    def build_admin_alert(self, event, message_str):
        return f"alert:{message_str}"

    def admin_targets(self, admin_ids):
        return list(admin_ids)


class _FakeApi:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))


class _FakeBot:
    def __init__(self):
        self.api = _FakeApi()


class _FakeResult:
    def __init__(self, text):
        self.chain = []
        self._text = text


class _FakeEvent:
    def __init__(self, text):
        self.bot = _FakeBot()
        self._result = _FakeResult(text)
        self._stopped = False

    def get_result(self):
        return self._result

    def set_result(self, value):
        self._result = value

    def stop_event(self):
        self._stopped = True


class RefactoredOutboundErrorPolicyTests(unittest.TestCase):
    @staticmethod
    def _runtime(mode="block_only", admin_ids=None):
        return type(
            "Runtime",
            (),
            {
                "host_bridge": _FakeHostBridge(),
                "config": type(
                    "Config",
                    (),
                    {
                        "global_settings": type(
                            "GlobalSettings",
                            (),
                            {
                                "enable_error_interception": True,
                                "error_interception_mode": mode,
                                "admin_ids": list(admin_ids or []),
                            },
                        )()
                    },
                )(),
            },
        )()

    def test_ghost_message_is_dropped(self):
        policy_mod = __import__(
            "astrmai.conversation.execution.outbound_error_policy",
            fromlist=["intercept_outbound_error"],
        )
        runtime = type(
            "Runtime",
            (),
            {
                "host_bridge": _FakeHostBridge(),
                "config": type(
                    "Config",
                    (),
                    {"global_settings": type("GlobalSettings", (), {"enable_error_interception": True, "admin_ids": []})()},
                )(),
            },
        )()
        event = _FakeEvent("[[ghost]]")

        async def _extract_result_text(result):
            return result._text

        policy_mod.extract_result_text = lambda result: result._text
        asyncio.run(policy_mod.intercept_outbound_error(runtime, event))

        self.assertIsNone(event.get_result())
        self.assertFalse(event._stopped)

    def test_error_message_is_intercepted_and_alerted(self):
        policy_mod = __import__(
            "astrmai.conversation.execution.outbound_error_policy",
            fromlist=["intercept_outbound_error"],
        )
        runtime = type(
            "Runtime",
            (),
            {
                "host_bridge": _FakeHostBridge(),
                "config": type(
                    "Config",
                    (),
                    {
                        "global_settings": type(
                            "GlobalSettings",
                            (),
                            {
                                "enable_error_interception": True,
                                "error_interception_mode": "block_and_stop",
                                "admin_ids": ["1001"],
                            },
                        )()
                    },
                )(),
            },
        )()
        event = _FakeEvent("Traceback: boom")
        policy_mod.extract_result_text = lambda result: result._text

        asyncio.run(policy_mod.intercept_outbound_error(runtime, event))

        self.assertIsNone(event.get_result())
        self.assertTrue(event._stopped)
        self.assertEqual(
            event.bot.api.calls,
            [("send_private_msg", {"user_id": 1001, "message": "alert:Traceback: boom"})],
        )

    def test_error_interception_modes_keep_distinct_clear_and_stop_semantics(self):
        policy_mod = __import__(
            "astrmai.conversation.execution.outbound_error_policy",
            fromlist=["intercept_outbound_error"],
        )
        policy_mod.extract_result_text = lambda result: result._text

        expectations = {
            "log_only": (False, False),
            "block_only": (True, False),
            "block_and_stop": (True, True),
        }
        for mode, (cleared, stopped) in expectations.items():
            with self.subTest(mode=mode):
                event = _FakeEvent("Traceback: boom")
                asyncio.run(policy_mod.intercept_outbound_error(self._runtime(mode), event))
                self.assertEqual(event.get_result() is None, cleared)
                self.assertEqual(event._stopped, stopped)


if __name__ == "__main__":
    unittest.main()
