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
        self.unified_msg_origin = f"default:{'GroupMessage' if group_id else 'FriendMessage'}:{group_id or sender_id}"

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


class _FakeMessageChain:
    def __init__(self):
        self.text = ""

    def message(self, text):
        self.text = str(text)
        return self


class _FakeAstrContext:
    def __init__(self, *, send_exc=None, send_result=None):
        self.shared_dict = {}
        self.sent = []
        self.send_exc = send_exc
        self.send_result = send_result

    async def send_message(self, umo, chain):
        if self.send_exc is not None:
            raise self.send_exc
        self.sent.append((umo, chain.text))
        return self.send_result


def _wrap_event(event, astr_ctx=None):
    return SimpleNamespace(
        context=SimpleNamespace(
            event=event,
            context=astr_ctx or SimpleNamespace(),
        )
    )


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

    def test_space_transition_relays_to_real_bot_friend_and_records_summary(self):
        event = _FakeEvent(group_id="777", sender_id="123", sender_name="Alice")
        event.bot.api = _FakeApi(
            result={"data": [{"user_id": 456, "nickname": "Bob", "remark": "小明"}]}
        )
        astr_ctx = _FakeAstrContext()

        with patch.object(self.mod, "MessageChain", _FakeMessageChain):
            result = asyncio.run(
                self.mod.SpaceTransitionTool().call(
                    _wrap_event(event, astr_ctx),
                    target_name="小明",
                    message="明天十点见",
                    context_summary="Alice 在测试群里委托我通知小明见面时间。",
                    delivery_mode="relay",
                )
            )

        self.assertIn("发送成功", result)
        self.assertEqual(astr_ctx.sent, [("default:FriendMessage:456", "Alice让我转告你：明天十点见")])
        jump = astr_ctx.shared_dict["astrmai_space_jumps"]["456"]
        self.assertEqual(jump["source_umo"], "default:GroupMessage:777")
        self.assertEqual(jump["source_sender_name"], "Alice")
        self.assertEqual(jump["delivery_mode"], "relay")
        self.assertIn("委托", jump["context_summary"])

    def test_space_transition_can_proactively_send_from_private_session(self):
        event = _FakeEvent(group_id=None, sender_id="123", sender_name="Alice")
        event.bot.api = _FakeApi(result=[{"user_id": "456", "nickname": "Bob"}])
        astr_ctx = _FakeAstrContext()

        with patch.object(self.mod, "MessageChain", _FakeMessageChain):
            result = asyncio.run(
                self.mod.SpaceTransitionTool().call(
                    _wrap_event(event, astr_ctx),
                    target_name="456",
                    message="突然想问问你今天过得怎么样。",
                    context_summary="我在和 Alice 私聊时想起 Bob，决定主动问候。",
                    delivery_mode="proactive",
                )
            )

        self.assertIn("发送成功", result)
        self.assertEqual(astr_ctx.sent[0][1], "突然想问问你今天过得怎么样。")
        self.assertEqual(
            astr_ctx.shared_dict["astrmai_space_jumps"]["456"]["source_umo"],
            "default:FriendMessage:123",
        )

    def test_space_transition_uses_runtime_handoff_store_without_shared_dict(self):
        store_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.cross_session_handoff_store"
        )
        store = store_mod.CrossSessionHandoffStore()
        event = _FakeEvent(group_id=None, sender_id="516779421", sender_name="恸")
        event.bot.api = _FakeApi(result=[{"user_id": "1481314186", "nickname": "萤"}])
        astr_ctx = _FakeAstrContext()
        del astr_ctx.shared_dict

        async def _run():
            with patch.object(self.mod, "MessageChain", _FakeMessageChain):
                result = await self.mod.SpaceTransitionTool(
                    handoff_store=store
                ).call(
                    _wrap_event(event, astr_ctx),
                    target_name="1481314186",
                    message="和妃爱聊天开不开心？",
                    context_summary="恸委托我询问萤的聊天感受。",
                    delivery_mode="relay",
                )
            handoff = await store.peek_for_recipient("default", "1481314186")
            return result, handoff

        result, handoff = asyncio.run(_run())

        self.assertIn("发送成功", result)
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff.source_sender_id, "516779421")
        self.assertEqual(handoff.source_sender_name, "恸")
        self.assertEqual(handoff.target_id, "1481314186")

    def test_space_transition_treats_false_send_result_as_failure(self):
        event = _FakeEvent(group_id=None, sender_id="516779421", sender_name="恸")
        event.bot.api = _FakeApi(result=[{"user_id": "1481314186", "nickname": "萤"}])
        astr_ctx = _FakeAstrContext(send_result=False)

        with patch.object(self.mod, "MessageChain", _FakeMessageChain):
            result = asyncio.run(
                self.mod.SpaceTransitionTool().call(
                    _wrap_event(event, astr_ctx),
                    target_name="1481314186",
                    message="测试消息",
                    context_summary="验证发送失败不会被报告为成功。",
                    delivery_mode="relay",
                )
            )

        self.assertIn("发送失败", result)
        self.assertNotIn("发送成功", result)
        self.assertEqual(
            event.get_extra("astrmai_tool_execution_trace")[-1]["status"],
            "failed",
        )

    def test_space_transition_blocks_stale_turn_before_sending(self):
        event = _FakeEvent(group_id=None, sender_id="516779421", sender_name="恸")
        event.bot.api = _FakeApi(result=[{"user_id": "1481314186", "nickname": "萤"}])
        astr_ctx = _FakeAstrContext()

        class _Coordinator:
            async def is_current_turn(self, turn):
                return False

        with patch.object(self.mod, "MessageChain", _FakeMessageChain):
            result = asyncio.run(
                self.mod.SpaceTransitionTool(
                    runtime_coordinator=_Coordinator()
                ).call(
                    _wrap_event(event, astr_ctx),
                    target_name="1481314186",
                    message="测试消息",
                    context_summary="验证过期请求不会产生跨会话副作用。",
                    delivery_mode="relay",
                )
            )

        self.assertIn("请求已经过期", result)
        self.assertEqual(astr_ctx.sent, [])

    def test_space_transition_reports_missing_friend_in_origin_session(self):
        event = _FakeEvent(group_id="777")
        event.bot.api = _FakeApi(result=[{"user_id": "456", "nickname": "Bob"}])
        astr_ctx = _FakeAstrContext()

        result = asyncio.run(
            self.mod.SpaceTransitionTool().call(
                _wrap_event(event, astr_ctx),
                target_name="不存在的人",
                message="你好",
                context_summary="测试不存在好友时的错误反馈。",
                delivery_mode="relay",
            )
        )

        self.assertIn("好友列表中没有找到", result)
        self.assertIn("消息未发送", result)
        self.assertEqual(astr_ctx.sent, [])

    def test_space_transition_deduplicates_same_send_within_turn(self):
        event = _FakeEvent(group_id="777", sender_name="Alice")
        event.bot.api = _FakeApi(result=[{"user_id": "456", "nickname": "Bob"}])
        astr_ctx = _FakeAstrContext()
        kwargs = {
            "target_name": "Bob",
            "message": "明天见",
            "context_summary": "Alice 委托我约 Bob 明天见。",
            "delivery_mode": "relay",
        }

        with patch.object(self.mod, "MessageChain", _FakeMessageChain):
            first = asyncio.run(self.mod.SpaceTransitionTool().call(_wrap_event(event, astr_ctx), **kwargs))
            second = asyncio.run(self.mod.SpaceTransitionTool().call(_wrap_event(event, astr_ctx), **kwargs))

        self.assertIn("发送成功", first)
        self.assertIn("不再重复发送", second)
        self.assertEqual(len(astr_ctx.sent), 1)

    def test_space_transition_reports_send_failure_without_fake_success(self):
        event = _FakeEvent(group_id="777")
        event.bot.api = _FakeApi(result=[{"user_id": "456", "nickname": "Bob"}])
        astr_ctx = _FakeAstrContext(send_exc=RuntimeError("network down"))

        with patch.object(self.mod, "MessageChain", _FakeMessageChain):
            result = asyncio.run(
                self.mod.SpaceTransitionTool().call(
                    _wrap_event(event, astr_ctx),
                    target_name="Bob",
                    message="你好",
                    context_summary="测试发送失败反馈。",
                    delivery_mode="proactive",
                )
            )

        self.assertIn("发送失败", result)
        self.assertIn("network down", result)
        self.assertNotIn("发送成功", result)

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

    def test_proactive_poke_accepts_current_private_sender_name(self):
        event = _FakeEvent(group_id=None, sender_id="516779421", sender_name="恸")

        class _DbService:
            async def resolve_entity_spatio_temporal(self, **kwargs):
                raise AssertionError("current private sender should not require database resolution")

        tool = self.mod.ProactivePokeTool(db_service=_DbService())
        result = asyncio.run(tool.call(_wrap_event(event), target_name="恸"))

        self.assertIn("加入待执行动作", result)
        action = event.get_extra("astrmai_pending_actions")[0]
        self.assertEqual(action["target_id"], "516779421")
        self.assertEqual(action["group_id"], "")

    def test_proactive_poke_accepts_current_private_sender_id(self):
        event = _FakeEvent(group_id=None, sender_id="516779421", sender_name="恸")
        tool = self.mod.ProactivePokeTool(db_service=None)

        result = asyncio.run(tool.call(_wrap_event(event), target_name="516779421"))

        self.assertIn("加入待执行动作", result)
        self.assertEqual(
            event.get_extra("astrmai_pending_actions")[0]["target_id"],
            "516779421",
        )

    def test_proactive_poke_accepts_private_session_scope_from_resolver(self):
        event = _FakeEvent(group_id=None, sender_id="516779421", sender_name="恸")

        class _DbService:
            async def resolve_entity_spatio_temporal(self, **kwargs):
                return "516779421", event.unified_msg_origin

        tool = self.mod.ProactivePokeTool(db_service=_DbService())
        result = asyncio.run(tool.call(_wrap_event(event), target_name="当前聊天对象"))

        self.assertIn("加入待执行动作", result)
        self.assertEqual(
            event.get_extra("astrmai_pending_actions")[0]["group_id"],
            "",
        )

    def test_proactive_poke_defaults_to_current_private_sender(self):
        event = _FakeEvent(group_id=None, sender_id="516779421", sender_name="恸")
        tool = self.mod.ProactivePokeTool(db_service=None)

        result = asyncio.run(tool.call(_wrap_event(event)))

        self.assertIn("加入待执行动作", result)
        self.assertEqual(
            event.get_extra("astrmai_pending_actions")[0]["target_id"],
            "516779421",
        )

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
