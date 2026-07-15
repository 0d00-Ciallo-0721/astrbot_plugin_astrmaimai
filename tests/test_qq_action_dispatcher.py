import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _Api:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
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
        self.temp_dir.cleanup()

    def test_commits_native_actions_once_after_reply(self):
        event = _Event()
        event.set_extra("astrmai_turn_identity", SimpleNamespace())
        event.set_extra(
            "astrmai_pending_actions",
            [
                {"action": "poke", "target_id": "123", "group_id": "456"},
                {"action": "message_emoji_like", "message_id": "789", "payload": {"emoji_id": "66"}},
                {"action": "group_sign", "group_id": "456"},
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
                ("set_group_sign", {"group_id": "456"}),
            ],
        )
        statuses = [item["status"] for item in event.get_extra("astrmai_qq_action_results")]
        self.assertEqual(statuses, ["success", "success", "success", "skipped", "skipped", "skipped"])

    def test_stale_turn_never_calls_napcat(self):
        event = _Event()
        event.set_extra("astrmai_turn_identity", SimpleNamespace())
        event.set_extra("astrmai_pending_actions", [{"action": "poke", "target_id": "123"}])
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator(current=False))

        asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-2"))

        self.assertEqual(event.bot.api.calls, [])
        self.assertEqual(event.get_extra("astrmai_qq_action_results")[0]["detail"], "stale_turn")

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
        event.set_extra("astrmai_pending_actions", [{"action": "group_sign", "group_id": "456"}])
        self.config.conversation.qq_deferred_action_commit_enabled = False
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator())

        result = asyncio.run(dispatcher.commit(event, "ff:GroupMessage:456", send_key="turn-3"))

        self.assertEqual(result, [])
        self.assertEqual(event.bot.api.calls, [])
