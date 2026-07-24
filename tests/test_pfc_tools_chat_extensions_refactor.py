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


class _MapApi:
    def __init__(self, results=None, exc=None):
        self.calls = []
        self.results = dict(results or {})
        self.exc = exc

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if self.exc is not None:
            raise self.exc
        result = self.results.get(action, self.results.get("*", {}))
        if callable(result):
            return result(action, kwargs)
        return result


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
        event.bot.api = _FakeApi(result={"data": [{"user_id": "222", "card": "Bob"}]})

        class _DbService:
            async def resolve_entity_spatio_temporal(self, **kwargs):
                return "222", "999"

        tool = self.mod.ConstructAtEventTool(db_service=_DbService())
        asyncio.run(tool.call(_wrap_event(event), target_name="Bob"))

        actions = event.get_extra("astrmai_pending_actions")
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[-1]["target_id"], "222")
        self.assertEqual(actions[-1]["group_id"], "111")
        self.assertTrue(actions[-1]["verified_current_group"])

    def test_construct_at_event_rejects_historical_identity_not_in_current_group(self):
        event = _FakeEvent(group_id="111", sender_id="user-1", sender_name="Alice")
        event.bot.api = _MapApi(
            results={
                "get_group_member_list": {"data": []},
                "get_group_member_info": {"data": None},
            }
        )

        class _DbService:
            async def resolve_entity_spatio_temporal(self, **kwargs):
                return "3650815443", "552752264"

        result = asyncio.run(
            self.mod.ConstructAtEventTool(db_service=_DbService()).call(
                _wrap_event(event),
                target_name="萤",
            )
        )

        self.assertIn("没有在当前群 111 中确认", result)
        self.assertEqual(event.get_extra("astrmai_pending_actions", []), [])

    def test_construct_at_event_can_verify_profile_identity_against_current_group(self):
        event = _FakeEvent(group_id="111", sender_id="user-1", sender_name="Alice")
        event.bot.api = _MapApi(
            results={
                "get_group_member_list": {"data": []},
                "get_group_member_info": {"data": {"user_id": "3650815443", "card": "萤"}},
            }
        )

        class _DbService:
            async def resolve_entity_spatio_temporal(self, **kwargs):
                return "3650815443", "552752264"

        result = asyncio.run(
            self.mod.ConstructAtEventTool(db_service=_DbService()).call(
                _wrap_event(event),
                target_name="萤",
            )
        )

        action = event.get_extra("astrmai_pending_actions")[0]
        self.assertIn("当前群确认", result)
        self.assertEqual(action["target_id"], "3650815443")
        self.assertEqual(action["group_id"], "111")

    def test_construct_at_event_does_not_use_fuzzy_name_for_side_effect(self):
        event = _FakeEvent(group_id="111", sender_id="user-1", sender_name="Alice")
        event.bot.api = _FakeApi(
            result={"data": [{"user_id": "888", "card": "萤火虫"}]}
        )

        result = asyncio.run(
            self.mod.ConstructAtEventTool(db_service=None).call(
                _wrap_event(event),
                target_name="萤",
            )
        )

        self.assertIn("没有在当前群 111 中确认", result)
        self.assertEqual(event.get_extra("astrmai_pending_actions", []), [])

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

    def test_qq_friend_lookup_matches_friend_remark_without_sending(self):
        event = _FakeEvent(group_id="12345")
        event.bot.api = _FakeApi(
            result={"data": [{"user_id": "1481314186", "nickname": "Ying", "remark": "萤"}]}
        )

        result = asyncio.run(self.mod.QQFriendLookupTool().call(_wrap_event(event), target="萤"))

        self.assertIn("找到匹配好友", result)
        self.assertIn("1481314186", result)
        self.assertEqual(event.bot.api.calls, [("get_friend_list", {})])

    def test_qq_group_presence_lookup_confirms_current_group_shared(self):
        event = _FakeEvent(group_id="777", sender_id="123")
        event.bot.api = _FakeApi(result={"data": [{"group_id": "777", "group_name": "测试群"}]})

        result = asyncio.run(self.mod.QQGroupPresenceLookupTool().call(_wrap_event(event), user_id="123"))

        self.assertIn("共同群判断", result)
        self.assertIn("测试群", result)
        self.assertEqual(event.bot.api.calls, [("get_group_list", {})])

    def test_qq_recent_contact_lookup_includes_cross_session_sends(self):
        event = _FakeEvent(group_id="777")
        event.set_extra(
            "astrmai_cross_session_sends",
            [{"target_id": "1481314186", "target_umo": "default:FriendMessage:1481314186", "message": "Alice让我转告你：你好"}],
        )

        result = asyncio.run(self.mod.QQRecentContactLookupTool().call(_wrap_event(event), count=3))

        self.assertIn("跨会话记录摘要", result)
        self.assertIn("1481314186", result)

    def test_qq_message_artifact_lookup_describes_current_image_segments(self):
        event = _FakeEvent(group_id="777", message_id="msg-9")
        event.message_obj.message = [
            {"type": "text", "data": {"text": "你看这个"}},
            {"type": "image", "data": {"file_id": "img-1", "url": "https://example.invalid/a.jpg"}},
        ]

        result = asyncio.run(self.mod.QQMessageArtifactLookupTool().call(_wrap_event(event)))

        self.assertIn("消息片段数量：2", result)
        self.assertIn("type=image", result)
        self.assertIn("file_id=img-1", result)

    def test_message_artifact_lookup_rejects_unbound_numeric_id(self):
        event = _FakeEvent(
            group_id="777",
            sender_id="3650815443",
            sender_name="6",
            message_id="msg-current",
        )
        event.message_obj.message = [{"type": "image", "data": {"file_id": "img-current"}}]
        event.message_str = "妃妃"
        event.bot.api = _FakeApi(result={"data": {"message_id": "1481314186"}})

        result = asyncio.run(
            self.mod.QQMessageArtifactLookupTool().call(
                _wrap_event(event),
                message_id="1481314186",
            )
        )

        self.assertIn("message_id_not_bound", result)
        self.assertIn("留空 message_id", result)
        self.assertEqual(event.bot.api.calls, [])

    def test_cross_chat_memory_query_uses_tool_service_without_current_session_scope(self):
        event = _FakeEvent(group_id="777")

        class _ToolService:
            def __init__(self):
                self.calls = []

            async def search_memory(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(items=[SimpleNamespace()])

            def render_result(self, result):
                return "记忆命中：妃爱棉花娃娃群仍未确认。"

        service = _ToolService()
        result = asyncio.run(
            self.mod.CrossChatMemoryQueryTool(memory_tool_service=service).call(
                _wrap_event(event),
                query="妃爱棉花娃娃群",
                scope="memory",
            )
        )

        self.assertIn("跨会话记忆", result)
        self.assertEqual(service.calls[0]["session_id"], "")

    def test_01_group_member_lookup_matches_card(self):
        event = _FakeEvent(group_id="777", sender_id="123")
        event.bot.api = _MapApi(
            {
                "get_group_member_list": {
                    "data": [
                        {"user_id": "123", "card": "萤", "nickname": "Ying", "role": "member"},
                    ]
                }
            }
        )

        result = asyncio.run(self.mod.QQGroupMemberLookupTool().call(_wrap_event(event), target="萤"))

        self.assertIn("QQ 123", result)
        self.assertIn("角色=member", result)

    def test_02_user_identity_lookup_combines_friend_and_group(self):
        event = _FakeEvent(group_id="777", sender_id="123")
        event.bot.api = _MapApi(
            {
                "get_friend_list": {"data": [{"user_id": "123", "nickname": "Ying", "remark": "萤"}]},
                "get_group_member_info": {"data": {"user_id": "123", "card": "萤", "role": "admin"}},
            }
        )

        result = asyncio.run(self.mod.QQUserIdentityLookupTool().call(_wrap_event(event), target="123"))

        self.assertIn("是机器人好友", result)
        self.assertIn("群身份", result)

    def test_03_forward_message_lookup_expands_forward_nodes(self):
        event = _FakeEvent(group_id="777")
        event.message_obj.message = [{"type": "forward", "data": {"id": "fw-1"}}]
        event.bot.api = _MapApi(
            {
                "get_forward_msg": {
                    "data": {
                        "messages": [
                            {"message_id": "m1", "sender": {"user_id": "1", "nickname": "A"}, "message": "你好"},
                        ]
                    }
                }
            }
        )

        result = asyncio.run(self.mod.QQForwardMessageLookupTool().call(_wrap_event(event)))

        self.assertIn("解析到 1 个节点", result)
        self.assertIn("你好", result)

    def test_04_vision_message_analyze_uses_existing_visual_extra(self):
        event = _FakeEvent(group_id="777")
        event.set_extra("astrmai_visual_context", {"type": "emoji", "description": "猫猫在挥手", "emotion_tags": ["开心"]})

        result = asyncio.run(self.mod.VisionMessageAnalyzeTool().call(_wrap_event(event)))

        self.assertIn("猫猫在挥手", result)
        self.assertIn("emoji", result)

    def test_04b_vision_message_analyze_uses_barrier_records(self):
        event = _FakeEvent(group_id="777")
        event.set_extra(
            "astrmai_vision_records",
            [
                {
                    "type": "emoji",
                    "description": "熊猫头低着头，文字为“我太难了”。通常用于自我调侃。",
                    "emotion_tags": ["无奈", "自嘲"],
                    "picid": "pic-1",
                }
            ],
        )

        result = asyncio.run(self.mod.VisionMessageAnalyzeTool().call(_wrap_event(event)))

        self.assertIn("熊猫头低着头", result)
        self.assertIn("emoji", result)
        self.assertIn("无奈", result)

    def test_04c_vision_message_rejects_qq_number_as_message_id(self):
        event = _FakeEvent(
            group_id="777",
            sender_id="3650815443",
            sender_name="6",
            message_id="msg-current",
        )
        event.message_obj.message = [{"type": "image", "data": {"file_id": "img-current"}}]
        event.message_str = "妃妃"
        event.bot.api = _FakeApi(result={})

        result = asyncio.run(
            self.mod.VisionMessageAnalyzeTool().call(
                _wrap_event(event),
                message_id="1481314186",
            )
        )

        self.assertIn("message_id_not_bound", result)
        self.assertEqual(event.bot.api.calls, [])

    def test_05_cross_session_reply_lookup_reads_friend_history(self):
        event = _FakeEvent(group_id="777")
        event.bot.api = _MapApi(
            {
                "get_friend_list": {"data": [{"user_id": "1481314186", "nickname": "Ying", "remark": "萤"}]},
                "get_friend_msg_history": {
                    "data": {
                        "messages": [
                            {"message_id": "m2", "sender": {"user_id": "1481314186", "nickname": "萤"}, "message": "开心呀"},
                        ]
                    }
                },
            }
        )

        result = asyncio.run(self.mod.CrossSessionReplyLookupTool().call(_wrap_event(event), target_name="萤"))

        self.assertIn("近期消息", result)
        self.assertIn("开心呀", result)

    def test_06_custom_face_send_queues_selected_face(self):
        event = _FakeEvent(group_id="777")
        event.bot.api = _MapApi({"fetch_custom_face": {"data": [{"id": "face-1", "name": "开心猫"}]}})

        result = asyncio.run(self.mod.QQCustomFaceSendTool().call(_wrap_event(event), keyword="开心"))

        self.assertIn("准备发送", result)
        action = event.get_extra("astrmai_pending_actions")[0]
        self.assertEqual(action["action"], "custom_face_send")
        self.assertEqual(action["payload"]["face"]["id"], "face-1")

    def test_07_quote_reply_action_queues_current_message_reply(self):
        event = _FakeEvent(group_id="777", message_id="m-current")

        result = asyncio.run(self.mod.QuoteReplyActionTool().call(_wrap_event(event), text="收到"))

        self.assertIn("引用消息", result)
        action = event.get_extra("astrmai_pending_actions")[0]
        self.assertEqual(action["action"], "quote_reply")
        self.assertEqual(action["message_id"], "m-current")

    def test_08_message_recall_lookup_filters_bot_messages(self):
        event = _FakeEvent(group_id="777", self_id="bot-1")
        event.bot.api = _MapApi(
            {
                "get_group_msg_history": {
                    "data": {
                        "messages": [
                            {"message_id": "u1", "sender": {"user_id": "user-1"}, "message": "用户消息"},
                            {"message_id": "b1", "sender": {"user_id": "bot-1"}, "message": "机器人回复"},
                        ]
                    }
                }
            }
        )

        result = asyncio.run(self.mod.QQMessageRecallLookupTool().call(_wrap_event(event), whose="bot"))

        self.assertIn("b1", result)
        self.assertNotIn("u1", result)

    def test_09_topic_thread_lookup_reads_prompt_envelope(self):
        event = _FakeEvent(group_id="777")
        envelope = SimpleNamespace(
            focus_message_text="刚才在聊棉花娃娃",
            direct_context_text="萤说买了娃娃",
            related_context_text="",
            ambient_background_text="",
        )
        event.set_extra("astrmai_prompt_envelope", envelope)

        result = asyncio.run(self.mod.TopicThreadLookupTool().call(_wrap_event(event), topic="娃娃"))

        self.assertIn("棉花娃娃", result)
        self.assertIn("萤说买了娃娃", result)

    def test_10_bot_capability_lookup_lists_registered_tools(self):
        event = _FakeEvent(group_id="777")

        result = asyncio.run(self.mod.BotCapabilityLookupTool().call(_wrap_event(event)))

        self.assertIn("qq_group_member_lookup", result)
        self.assertIn("contact_route_suggest_tool", result)

    def test_11_memory_write_correction_records_feedback(self):
        event = _FakeEvent(group_id="777")

        class _MemoryEngine:
            def __init__(self):
                self.calls = []

            async def record_cognitive_feedback(self, **kwargs):
                self.calls.append(kwargs)

        engine = _MemoryEngine()
        result = asyncio.run(
            self.mod.MemoryWriteCorrectionTool(memory_engine=engine).call(
                _wrap_event(event),
                wrong_fact="我叫小红",
                correct_fact="我叫小明",
            )
        )

        self.assertIn("已记录", result)
        self.assertEqual(engine.calls[0]["source"], "memory_correction_tool")
        self.assertEqual(event.get_extra("astrmai_memory_correction_reports")[0]["correct_fact"], "我叫小明")

    def test_12_unverified_report_record_marks_claim_uncertain(self):
        event = _FakeEvent(group_id="777")

        result = asyncio.run(
            self.mod.UnverifiedReportRecordTool().call(
                _wrap_event(event),
                claim="别的群有人卖娃娃",
                source_hint="群友转述",
            )
        )

        self.assertIn("未核实", result)
        self.assertEqual(event.get_extra("astrmai_unverified_reports")[0]["source_hint"], "群友转述")

    def test_13_persona_fact_check_uses_self_lore_query(self):
        event = _FakeEvent(group_id="777")

        class _ToolService:
            async def self_lore_query(self, **kwargs):
                return SimpleNamespace(items=[SimpleNamespace()])

            def render_result(self, result):
                return "没有授权棉花娃娃。"

        result = asyncio.run(
            self.mod.PersonaFactCheckTool(memory_tool_service=_ToolService()).call(
                _wrap_event(event),
                claim="妃爱授权棉花娃娃了吗",
            )
        )

        self.assertIn("没有授权", result)

    def test_14_group_activity_snapshot_counts_recent_senders(self):
        event = _FakeEvent(group_id="777")
        event.bot.api = _MapApi(
            {
                "get_group_msg_history": {
                    "data": {
                        "messages": [
                            {"message_id": "m1", "sender": {"user_id": "1", "nickname": "A"}, "message": "一"},
                            {"message_id": "m2", "sender": {"user_id": "1", "nickname": "A"}, "message": "二"},
                            {"message_id": "m3", "sender": {"user_id": "2", "nickname": "B"}, "message": "三"},
                        ]
                    }
                }
            }
        )

        result = asyncio.run(self.mod.GroupActivitySnapshotTool().call(_wrap_event(event), count=3))

        self.assertIn("A(2)", result)
        self.assertIn("B(1)", result)

    def test_15_contact_route_suggest_prefers_friend_private_send(self):
        event = _FakeEvent(group_id="777")
        event.bot.api = _MapApi({"get_friend_list": {"data": [{"user_id": "1481314186", "remark": "萤"}]}})

        result = asyncio.run(self.mod.ContactRouteSuggestTool().call(_wrap_event(event), target="萤", intent="传话"))

        self.assertIn("机器人好友", result)
        self.assertIn("space_transition_action", result)

    def test_dispatcher_commits_custom_face_send_action(self):
        event = _FakeEvent(group_id="777")
        event.bot.api = _MapApi({"send_msg": {"data": {"message_id": "out-1"}}})
        event.set_extra(
            "astrmai_pending_actions",
            [
                self.mod.PendingQQAction(
                    action_type="custom_face_send",
                    group_id="777",
                    payload={"face": {"id": "face-1"}},
                ).to_dict()
            ],
        )
        dispatcher_mod = importlib.import_module("astrmai.conversation.execution.qq_action_dispatcher")

        result = asyncio.run(dispatcher_mod.QQActionDispatcher().commit(event, "chat-1", send_key="send-1"))

        self.assertEqual(result[-1]["status"], "success")
        self.assertEqual(event.bot.api.calls[0][0], "send_msg")
        self.assertEqual(event.bot.api.calls[0][1]["message"][0]["type"], "mface")

    def test_dispatcher_commits_quote_reply_action(self):
        event = _FakeEvent(group_id="777")
        event.bot.api = _MapApi({"send_msg": {"data": {"message_id": "out-2"}}})
        event.set_extra(
            "astrmai_pending_actions",
            [
                self.mod.PendingQQAction(
                    action_type="quote_reply",
                    group_id="777",
                    message_id="m-current",
                    payload={"text": "收到"},
                ).to_dict()
            ],
        )
        dispatcher_mod = importlib.import_module("astrmai.conversation.execution.qq_action_dispatcher")

        result = asyncio.run(dispatcher_mod.QQActionDispatcher().commit(event, "chat-1", send_key="send-2"))

        self.assertEqual(result[-1]["status"], "success")
        segments = event.bot.api.calls[0][1]["message"]
        self.assertEqual(segments[0]["type"], "reply")
        self.assertEqual(segments[1]["data"]["text"], "收到")

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

    def test_proactive_poke_peer_poke_requires_explicit_actor_or_target(self):
        event = _FakeEvent(group_id="111", sender_id="user-1", sender_name="Alice")
        event.set_extra("astrmai_interaction_kind", "peer_poke")
        event.set_extra("astrmai_peer_poke_join_allowed", True)
        event.set_extra("astrmai_interaction_actor_id", "user-1")
        event.set_extra("astrmai_interaction_actor_name", "Alice")
        event.set_extra("astrmai_interaction_target_id", "user-2")
        event.set_extra("astrmai_interaction_target_name", "Bob")
        tool = self.mod.ProactivePokeTool(db_service=None)

        result = asyncio.run(tool.call(_wrap_event(event)))

        self.assertIn("必须明确选择", result)
        self.assertEqual(event.get_extra("astrmai_pending_actions", []), [])

    def test_proactive_poke_peer_poke_accepts_named_participant(self):
        event = _FakeEvent(group_id="111", sender_id="user-1", sender_name="Alice")
        event.set_extra("astrmai_interaction_kind", "peer_poke")
        event.set_extra("astrmai_peer_poke_join_allowed", True)
        event.set_extra("astrmai_interaction_actor_id", "user-1")
        event.set_extra("astrmai_interaction_actor_name", "Alice")
        event.set_extra("astrmai_interaction_target_id", "user-2")
        event.set_extra("astrmai_interaction_target_name", "Bob")
        tool = self.mod.ProactivePokeTool(db_service=None)

        result = asyncio.run(tool.call(_wrap_event(event), target_name="Bob"))

        self.assertIn("加入待执行动作", result)
        action = event.get_extra("astrmai_pending_actions")[0]
        self.assertEqual(action["action"], "poke")
        self.assertEqual(action["target_id"], "user-2")
        self.assertEqual(action["group_id"], "111")

    def test_proactive_poke_peer_poke_rejects_unrelated_target(self):
        event = _FakeEvent(group_id="111", sender_id="user-1", sender_name="Alice")
        event.set_extra("astrmai_interaction_kind", "peer_poke")
        event.set_extra("astrmai_peer_poke_join_allowed", True)
        event.set_extra("astrmai_interaction_actor_id", "user-1")
        event.set_extra("astrmai_interaction_actor_name", "Alice")
        event.set_extra("astrmai_interaction_target_id", "user-2")
        event.set_extra("astrmai_interaction_target_name", "Bob")

        class _DbService:
            async def resolve_entity_spatio_temporal(self, **kwargs):
                return "user-3", "111"

        tool = self.mod.ProactivePokeTool(db_service=_DbService())
        result = asyncio.run(tool.call(_wrap_event(event), target_name="Carol"))

        self.assertIn("只能戳发起者或被戳者", result)
        self.assertEqual(event.get_extra("astrmai_pending_actions", []), [])

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
