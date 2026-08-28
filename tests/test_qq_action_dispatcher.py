import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs
from astrmai.infrastructure.runtime.outbound_send_guard import OUTBOUND_SEND_GATE


class _Api:
    def __init__(self, result=None):
        self.calls = []
        self.result = {"status": "ok"} if result is None else result

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return self.result


class _TimeoutSendApi(_Api):
    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "send_msg":
            raise TimeoutError("request timed out after remote acceptance")
        return {"status": "ok"}


class _Event:
    def __init__(self):
        self._extra = {}
        self.bot = SimpleNamespace(api=_Api())

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class _Coordinator:
    def __init__(self, *, current=True, previous=None):
        self.current = current
        self.previous = list(previous or [])

    async def is_current_turn(self, _turn):
        return self.current

    async def get_latest_committed_outbound(self, _chat_id, *, exclude_send_key=""):
        return self.previous


class QQActionDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.conversation.execution.qq_action_dispatcher", None)
        self.mod = importlib.import_module("astrmai.conversation.execution.qq_action_dispatcher")
        self.config = SimpleNamespace(
            conversation=SimpleNamespace(
                qq_native_tools_enabled=True,
                qq_deferred_action_commit_enabled=True,
            )
        )

    def tearDown(self):
        OUTBOUND_SEND_GATE.close()
        self.temp_dir.cleanup()

    def test_shutdown_gate_rejects_deferred_actions_before_napcat(self):
        event = _Event()
        event.set_extra("astrmai_pending_actions", [{"action": "poke", "target_id": "123"}])
        OUTBOUND_SEND_GATE.open()
        OUTBOUND_SEND_GATE.close(enforce_provider=True)
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator())

        result = asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-shutdown"))

        self.assertEqual(event.bot.api.calls, [])
        self.assertEqual(result[-1]["status"], "skipped")
        self.assertEqual(result[-1]["detail"], "shutdown_rejected")
        self.assertTrue(result[-1]["action_id"])
        self.assertTrue(result[-1]["transport_idempotency_key"])
        self.assertEqual(result[-1]["failure_kind"], "shutdown")

    def test_commits_native_actions_once_after_reply(self):
        event = _Event()
        event.set_extra("astrmai_turn_identity", SimpleNamespace())
        event.set_extra(
            "astrmai_pending_actions",
            [
                {"action": "poke", "target_id": "123", "group_id": "456"},
                {"action": "message_emoji_like", "message_id": "789", "payload": {"emoji_id": "66"}},
            ],
        )
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator())

        asyncio.run(dispatcher.commit(event, "ff:GroupMessage:456", send_key="turn-1"))
        asyncio.run(dispatcher.commit(event, "ff:GroupMessage:456", send_key="turn-1"))

        self.assertEqual(
            event.bot.api.calls,
            [
                ("send_poke", {"user_id": 123, "group_id": 456}),
                ("set_msg_emoji_like", {"message_id": 789, "emoji_id": "66", "set": True}),
            ],
        )
        statuses = [item["status"] for item in event.get_extra("astrmai_qq_action_results")]
        self.assertEqual(statuses, ["sending", "sent", "sending", "sent", "skipped", "skipped"])

    def test_stale_turn_never_calls_napcat(self):
        event = _Event()
        event.set_extra("astrmai_turn_identity", SimpleNamespace())
        event.set_extra("astrmai_pending_actions", [{"action": "poke", "target_id": "123"}])
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator(current=False))

        asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-2"))

        self.assertEqual(event.bot.api.calls, [])
        self.assertEqual(event.get_extra("astrmai_qq_action_results")[0]["detail"], "stale_turn")
        self.assertTrue(event.get_extra("astrmai_qq_action_results")[0]["action_id"])
        self.assertEqual(event.get_extra("astrmai_qq_action_results")[0]["failure_kind"], "stale_turn")

    def test_api_unavailable_result_keeps_action_identity(self):
        event = _Event()
        event.bot.api = None
        event.set_extra("astrmai_pending_actions", [{"action": "like", "target_id": "123"}])
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator())

        result = asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-no-api"))

        self.assertEqual(result[-1]["status"], "failed")
        self.assertTrue(result[-1]["action_id"])
        self.assertTrue(result[-1]["transport_idempotency_key"])
        self.assertEqual(result[-1]["failure_kind"], "api_unavailable")

    def test_withdraw_targets_previous_committed_reply(self):
        event = _Event()
        event.set_extra("astrmai_pending_actions", [{"action": "withdraw"}])
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator(previous=["991", "992"]))

        asyncio.run(dispatcher.commit(event, "ff:GroupMessage:456", send_key="current-turn"))

        self.assertEqual(event.bot.api.calls, [("delete_msg", {"message_id": 992})])

    def test_withdraw_prefers_message_id_resolved_before_reply_send(self):
        event = _Event()
        event.set_extra("astrmai_pending_actions", [{"action": "withdraw", "message_id": "812"}])
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator(previous=["991"]))

        asyncio.run(dispatcher.commit(event, "ff:GroupMessage:456", send_key="current-turn"))

        self.assertEqual(event.bot.api.calls, [("delete_msg", {"message_id": 812})])

    def test_disabled_dispatcher_does_not_construct_side_effects(self):
        event = _Event()
        event.set_extra("astrmai_pending_actions", [{"action": "poke", "target_id": "123"}])
        self.config.conversation.qq_deferred_action_commit_enabled = False
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator())

        result = asyncio.run(dispatcher.commit(event, "ff:GroupMessage:456", send_key="turn-3"))

        self.assertEqual(result, [])
        self.assertEqual(event.bot.api.calls, [])

    def test_sent_result_contains_canonical_trace_and_transport_key(self):
        event = _Event()
        event.set_extra("astrmai_pending_actions", [{"action": "like", "target_id": "123", "payload": {"times": 1}}])
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator())

        result = asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-meta"))

        sent = next(item for item in result if item["status"] == "sent")
        self.assertEqual(sent["canonical_tool_name"], "proactive_like_action")
        self.assertTrue(sent["action_id"].startswith("qqact_"))
        self.assertTrue(sent["transport_idempotency_key"])
        self.assertGreater(sent["sent_at"], 0)

    def test_identical_action_instances_are_both_sent_but_retry_is_deduplicated(self):
        from astrmai.conversation.contracts.qq_action import PendingQQAction

        event = _Event()
        first = PendingQQAction(action_type="like", target_id="123", payload={"times": 1})
        second = PendingQQAction(action_type="like", target_id="123", payload={"times": 1})
        self.assertNotEqual(first.action_instance_id, second.action_instance_id)
        event.set_extra("astrmai_pending_actions", [first.to_dict(), second.to_dict()])
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator())

        asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-duplicate"))
        self.assertEqual(len(event.bot.api.calls), 2)

        asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-duplicate"))
        self.assertEqual(len(event.bot.api.calls), 2)

    def test_nonzero_retcode_is_failed_even_when_status_ok(self):
        event = _Event()
        event.bot.api = _Api(result={"status": "ok", "retcode": 100})
        event.set_extra("astrmai_pending_actions", [{"action": "like", "target_id": "123"}])
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator())

        result = asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-retcode"))

        self.assertEqual(len(event.bot.api.calls), 1)
        self.assertEqual(result[-1]["status"], "failed")
        self.assertIn("retcode=100", result[-1]["detail"])

    def test_quote_send_timeout_does_not_fallback_and_duplicate(self):
        event = _Event()
        event.bot.api = _TimeoutSendApi()
        event.set_extra(
            "astrmai_pending_actions",
            [{"action": "quote_reply", "target_id": "123", "message_id": "88", "payload": {"text": "hi"}}],
        )
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator())

        result = asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-timeout"))

        self.assertEqual([name for name, _ in event.bot.api.calls], ["send_msg"])
        self.assertEqual(result[-1]["status"], "failed")
