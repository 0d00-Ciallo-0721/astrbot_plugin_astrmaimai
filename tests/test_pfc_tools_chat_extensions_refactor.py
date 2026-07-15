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
        self.assertEqual(api.calls, [])
        self.assertEqual(
            event.get_extra("astrmai_pending_actions")[0]["action"],
            "message_emoji_like",
        )
        self.assertEqual(
            event.get_extra("astrmai_pending_actions")[0]["payload"]["emoji_id"],
            self.mod.QQ_MESSAGE_EMOJI_OPTIONS["approve"][0],
        )
        self.assertEqual(
            event.get_extra("astrmai_tool_execution_trace", []),
            [],
        )

    def test_wait_tool_records_actual_execution(self):
        event = _FakeEvent(group_id="12345")

        result = asyncio.run(self.mod.WaitTool().call(_wrap_event(event)))

        self.assertEqual(result, "[SYSTEM_WAIT_SIGNAL]")
        self.assertEqual(
            event.get_extra("astrmai_tool_execution_trace"),
            [{"tool_name": "wait_and_listen", "status": "success"}],
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
        self.assertEqual(api.calls, [])
        self.assertEqual(event.get_extra("astrmai_pending_actions")[0]["action"], "group_sign")
        self.assertEqual(event.get_extra("astrmai_pending_actions")[0]["group_id"], "67890")

    def test_custom_face_catalog_query_accepts_wrapped_napcat_data(self):
        event = _FakeEvent(group_id="12345")
        api = _FakeApi(result={"data": ["cat_smile"]})
        event.bot.api = api

        result = asyncio.run(self.mod.CustomFaceCatalogQueryTool().call(_wrap_event(event), count=1))

        self.assertIn("cat_smile", result)

    def test_meme_tool_marks_explicit_send_as_forced(self):
        event = _FakeEvent(group_id="12345")

        asyncio.run(self.mod.ProactiveMemeTool(emotion_mapping=["happy: 开心"]).call(_wrap_event(event), emotion_tag="happy"))

        self.assertTrue(event.get_extra("astrmai_force_meme"))

    def test_textual_reaction_does_not_create_fake_qq_action(self):
        event = _FakeEvent(group_id="12345")

        result = asyncio.run(self.mod.MessageReactionTool().call(_wrap_event(event), reaction="赞同"))

        self.assertIn("文字回复", result)
        self.assertEqual(event.get_extra("astrmai_pending_actions", []), [])

    def test_withdraw_resolves_latest_bot_message_before_queueing(self):
        event = _FakeEvent(group_id="12345", self_id="90001")
        event.bot.api = _FakeApi(
            result={
                "data": {
                    "messages": [
                        {"message_id": 10, "sender": {"user_id": "user-1"}},
                        {"message_id": 11, "sender": {"user_id": "90001"}},
                    ]
                }
            }
        )

        result = asyncio.run(self.mod.RegretAndWithdrawTool().call(_wrap_event(event)))

        self.assertIn("加入待执行动作", result)
        self.assertEqual(event.get_extra("astrmai_pending_actions")[0]["message_id"], "11")
        self.assertEqual(event.bot.api.calls[0][0], "get_group_msg_history")

    def test_withdraw_does_not_queue_when_history_has_no_bot_message(self):
        event = _FakeEvent(group_id=None, self_id="90001")
        event.bot.api = _FakeApi(
            result={"messages": [{"message_id": 10, "user_id": "user-1"}]}
        )

        result = asyncio.run(self.mod.RegretAndWithdrawTool().call(_wrap_event(event)))

        self.assertIn("没有找到", result)
        self.assertEqual(event.get_extra("astrmai_pending_actions", []), [])
        self.assertEqual(event.bot.api.calls[0][0], "get_friend_msg_history")

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
