import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from astrmai.conversation.contracts.conversation_event import ConversationEvent
from astrmai.conversation.contracts.reply_artifact import VisibleReplyArtifact
from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeConversation:
    def __init__(self, history=None):
        self.history = history or []


class _FakeConversationManager:
    def __init__(self):
        self.curr = {}
        self.conversations = {}
        self.counter = 0

    async def get_curr_conversation_id(self, unified_msg_origin):
        return self.curr.get(unified_msg_origin)

    async def new_conversation(self, unified_msg_origin, platform_id=None, content=None, title=None, persona_id=None):
        self.counter += 1
        cid = f"conv-{self.counter}"
        self.curr[unified_msg_origin] = cid
        self.conversations[cid] = _FakeConversation(history=content or [])
        return cid

    async def get_conversation(self, unified_msg_origin, conversation_id, create_if_not_exists=False):
        return self.conversations.get(conversation_id)

    async def update_conversation(self, unified_msg_origin, conversation_id=None, history=None, title=None, persona_id=None, token_usage=None):
        conversation_id = conversation_id or self.curr.get(unified_msg_origin)
        self.conversations[conversation_id] = _FakeConversation(history=history or [])


class _FakeResponse:
    def __init__(self, text):
        self.completion_text = text
        self.usage = SimpleNamespace(input=8, input_cached=4, output=3)


class _FakeContext:
    async def llm_generate(self, **kwargs):
        return _FakeResponse("ok")


class _FakeEvent:
    def __init__(self, canonical):
        self._extras = {"astrmai_conversation_event": canonical}

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)


class LaneStoresRawDialogueTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.runtime.lane_manager", None)
        sys.modules.pop("astrmai.infrastructure.gateway.model_gateway", None)
        self.lane_mod = importlib.import_module("astrmai.infrastructure.runtime.lane_manager")
        self.gateway_mod = importlib.import_module("astrmai.infrastructure.gateway.model_gateway")
        self.lane_mod = importlib.reload(self.lane_mod)
        self.gateway_mod = importlib.reload(self.gateway_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_lane_history_uses_raw_user_text_instead_of_wrapped_prompt(self):
        conversation_manager = _FakeConversationManager()
        lane_manager = self.lane_mod.LaneManager(conversation_manager)
        gateway = self.gateway_mod.GlobalModelGateway(
            _FakeContext(),
            SimpleNamespace(
                infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
                provider=SimpleNamespace(fallback_models=[]),
                global_settings=SimpleNamespace(debug_mode=False),
            ),
        )
        gateway.set_lane_manager(lane_manager)
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")

        async def _run():
            await gateway.chat_in_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prompt="（导演旁白：这是包装后的提示词）",
                raw_user_text="[Alice] 说: 为什么不可以",
                system_prompt="设定",
                models=["model-a"],
                prefix_hash="hash-1",
                use_fallback=False,
            )

        asyncio.run(_run())

        lane_umo = lane_manager.resolve_lane_umo("default:GroupMessage:group-1", lane_key)
        conversation_id = asyncio.run(conversation_manager.get_curr_conversation_id(lane_umo))
        history = conversation_manager.conversations[conversation_id].history

        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[0]["content"], "[Alice] 说: 为什么不可以")
        self.assertNotIn("导演旁白", history[0]["content"])

    def test_canonical_user_history_uses_explicit_visible_contract(self):
        conversation_manager = _FakeConversationManager()
        lane_manager = self.lane_mod.LaneManager(conversation_manager)
        gateway = self.gateway_mod.GlobalModelGateway(
            _FakeContext(),
            SimpleNamespace(
                infra=SimpleNamespace(max_concurrent_llm_calls=2, llm_retries=0, backoff_factor=1.5, api_timeout=10),
                provider=SimpleNamespace(fallback_models=[]),
                global_settings=SimpleNamespace(debug_mode=False),
            ),
        )
        gateway.set_lane_manager(lane_manager)
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")
        canonical = ConversationEvent(
            event_id="evt-42",
            chat_id="default:GroupMessage:group-1",
            chat_kind="group",
            timestamp=1.0,
            actor_id="10001",
            actor_name="Alice",
            visible_text="继续刚才的方案",
            rich_text="继续刚才的方案",
            message_kind="text",
            role="user",
            reply_target_event_id="evt-41",
            reply_target_actor_name="AstrMai",
            image_refs=("private-image-ref",),
        )
        event = _FakeEvent(canonical)
        artifact = VisibleReplyArtifact(
            visible_text="我继续说。",
            segments=["我继续说。"],
            persistable_text="我继续说。",
        )

        async def _run():
            history_user_text = gateway._build_history_user_text(
                event,
                raw_user_text="[事件=evt-42 | 发言人=Alice（ID:10001） | 角色=成员 | 类型=text | 来源=original]\n内容：继续刚才的方案",
                prompt="包装提示词",
            )
            await lane_manager.append_visible_reply_artifact(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                raw_user_text="[事件=evt-42 | 发言人=Alice（ID:10001） | 角色=成员 | 类型=text | 来源=original]\n内容：继续刚才的方案",
                history_user_text=history_user_text,
                artifact=artifact,
            )
            lane_umo = lane_manager.resolve_lane_umo("default:GroupMessage:group-1", lane_key)
            conversation_id = await conversation_manager.get_curr_conversation_id(lane_umo)
            return conversation_manager.conversations[conversation_id].history

        history = asyncio.run(_run())

        self.assertEqual([item["role"] for item in history], ["user", "assistant"])
        self.assertIn("继续刚才的方案", history[0]["content"])
        self.assertIn("[回复 AstrMai]", history[0]["content"])
        self.assertIn("[图片]", history[0]["content"])
        self.assertNotIn("事件=evt-42", history[0]["content"])
        self.assertNotIn("10001", history[0]["content"])
        self.assertNotIn("private-image-ref", history[0]["content"])
        self.assertEqual(history[1]["content"], "我继续说。")

    def test_empty_history_user_text_does_not_create_user_turn(self):
        lane_manager = self.lane_mod.LaneManager(_FakeConversationManager())
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")
        artifact = VisibleReplyArtifact(
            visible_text="仅保存助手回复",
            segments=["仅保存助手回复"],
            persistable_text="仅保存助手回复",
        )

        async def _run():
            return await lane_manager.append_visible_reply_artifact(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                raw_user_text="[事件=internal | 发言人=hidden | 角色=成员 | 类型=text | 来源=synthetic]",
                history_user_text="",
                artifact=artifact,
            )

        history = asyncio.run(_run())
        self.assertEqual([item["role"] for item in history], ["assistant"])

    def test_assistant_internal_envelope_stays_out_of_history(self):
        lane_manager = self.lane_mod.LaneManager(_FakeConversationManager())
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")
        artifact = VisibleReplyArtifact(
            visible_text="内部载荷",
            segments=["内部载荷"],
            persistable_text="[事件=evt-9 | 发言人=Bot（ID:bot-1） | 角色=机器人 | 类型=text | 来源=bot_echo]\n内容：内部载荷",
        )

        async def _run():
            return await lane_manager.append_visible_reply_artifact(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                raw_user_text="普通用户消息",
                history_user_text="普通用户消息",
                artifact=artifact,
            )

        history = asyncio.run(_run())
        self.assertEqual([item["role"] for item in history], ["user"])
        self.assertNotIn("内部载荷", str(history))


if __name__ == "__main__":
    unittest.main()
