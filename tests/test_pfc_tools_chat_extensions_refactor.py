import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _FakeApi:
    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result
        self.exc = exc

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.result


class _FakeEvent:
    def __init__(self, *, group_id=None, sender_id="user-1", self_id="bot-1", sender_name="Alice", message_id="msg-1"):
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self._sender_name = sender_name
        self._extra = {}
        self.message_obj = SimpleNamespace(message_id=message_id)
        self.bot = SimpleNamespace(api=None)

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id

    def get_sender_name(self):
        return self._sender_name

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


def _wrap_event(event):
    return SimpleNamespace(context=SimpleNamespace(event=event, context=SimpleNamespace()))


class PfcToolsChatExtensionsRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.conversation.planning.tools.pfc_tools", None)
        self.mod = importlib.import_module("astrmai.conversation.planning.tools.pfc_tools")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_message_emoji_like_uses_builtin_pool_and_current_message(self):
        event = _FakeEvent(group_id="12345")
        api = _FakeApi()
        event.bot.api = api
        tool = self.mod.MessageEmojiLikeTool()

        with patch.object(self.mod.random, "choice", side_effect=lambda seq: seq[0]):
            result = asyncio.run(tool.call(_wrap_event(event), tone="approve"))

        self.assertIn("QQ", result)
        self.assertEqual(
            api.calls,
            [("set_msg_emoji_like", {"message_id": "msg-1", "emoji_id": self.mod.QQ_MESSAGE_EMOJI_OPTIONS["approve"][0]})],
        )

    def test_group_sign_tool_blocks_private_chat(self):
        event = _FakeEvent(group_id=None)
        api = _FakeApi()
        event.bot.api = api
        tool = self.mod.GroupSignTool()

        result = asyncio.run(tool.call(_wrap_event(event)))

        self.assertIn("当前不是群聊", result)
        self.assertEqual(api.calls, [])

    def test_construct_at_event_ignores_dirty_none_target_when_deduping(self):
        event = _FakeEvent(group_id="111", sender_id="user-1", sender_name="Alice")
        event.set_extra("astrmai_pending_actions", [{"action": "at", "target_id": None}])

        class _DbService:
            async def resolve_entity_spatio_temporal(self, **kwargs):
                return "user-2", "111"

        tool = self.mod.ConstructAtEventTool(db_service=_DbService())
        asyncio.run(tool.call(_wrap_event(event), target_name="Bob"))

        actions = event.get_extra("astrmai_pending_actions")
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[-1]["target_id"], "user-2")

    def test_group_sign_tool_calls_current_group_only(self):
        event = _FakeEvent(group_id="67890")
        api = _FakeApi()
        event.bot.api = api
        tool = self.mod.GroupSignTool()

        result = asyncio.run(tool.call(_wrap_event(event)))

        self.assertIn("群签到", result)
        self.assertEqual(api.calls, [("set_group_sign", {"group_id": "67890"})])

    def test_custom_face_catalog_query_returns_preview(self):
        event = _FakeEvent(group_id="12345")
        api = _FakeApi(result=["cat_smile", "dog_wave"])
        event.bot.api = api
        tool = self.mod.CustomFaceCatalogQueryTool()

        result = asyncio.run(tool.call(_wrap_event(event), count=2))

        self.assertIn("cat_smile", result)
        self.assertIn("dog_wave", result)
        self.assertEqual(api.calls, [("fetch_custom_face", {"count": 2})])

    def test_proactive_poke_rejects_cross_group_target(self):
        event = _FakeEvent(group_id="111", sender_id="user-1", sender_name="Alice")
        api = _FakeApi()
        event.bot.api = api

        class _DbService:
            async def resolve_entity_spatio_temporal(self, **kwargs):
                return "user-2", "222"

        tool = self.mod.ProactivePokeTool(db_service=_DbService())
        result = asyncio.run(tool.call(_wrap_event(event), target_name="Bob"))

        self.assertIn("不在当前群聊", result)
        self.assertEqual(api.calls, [])

    def test_proactive_poke_rejects_private_arbitrary_numeric_target(self):
        event = _FakeEvent(group_id=None, sender_id="user-1", sender_name="Alice")
        api = _FakeApi()
        event.bot.api = api

        class _DbService:
            async def resolve_entity_spatio_temporal(self, **kwargs):
                return "987654321", None

        tool = self.mod.ProactivePokeTool(db_service=_DbService())
        result = asyncio.run(tool.call(_wrap_event(event), target_name="987654321"))

        self.assertIn("不在当前私聊", result)
        self.assertEqual(api.calls, [])
