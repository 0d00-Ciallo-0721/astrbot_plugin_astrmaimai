"""G3 / ID-08 回归测试：撤回消息打墓碑。

线上 16h ≥5 条 group_recall notice，插件侧零处理——被撤回的消息原文继续留在
对话热区，bot 后续回复可能原样复述（隐私/尴尬）。

守护不变式：
1. store：`mark_recalled` 按 event_id 替换内容为 [已撤回]，**保留 speaker 与时序**；
   原文不再出现在任何渲染产物（warm 摘要/引用）中。
2. 路由：group_recall / friend_recall notice 被分类为 recall_notice（不再混进
   notice_passthrough 被直接丢弃），并调用 facade 打墓碑。
3. 边界：撤回消息不在热区（已压缩进冷区/从未入区）时返回 False 且不炸；
   重复撤回幂等；非撤回类 notice 路由不受影响。
"""

import asyncio
import time
import unittest
from types import SimpleNamespace

from astrmai.conversation.attention.group_dialogue_store import GroupDialogueStore
from astrmai.presentation.events import message_entry


async def _drive(result):
    """handle_global_message 是 async generator——必须用 async for 驱动，
    直接 await 会拿到未启动的生成器对象（AstrBot 插件调试常见坑）。"""
    import inspect

    if inspect.isasyncgen(result):
        async for _item in result:
            pass
        return None
    return await result


class _RecallEvent:
    def __init__(self, payload: dict, chat_id: str = "default:GroupMessage:7000"):
        self._extras: dict = {}
        self.unified_msg_origin = chat_id
        self.message_str = ""
        self.message_obj = SimpleNamespace(message=[], raw_message=payload, self_id="bot-1")

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_sender_id(self):
        return "111"

    def get_group_id(self):
        return "7000"


class RecallTombstoneStoreTests(unittest.TestCase):
    CHAT = "default:GroupMessage:7000"

    def _store_with_messages(self):
        store = GroupDialogueStore()
        # 时间戳须落在 warm zone TTL 内，否则渲染产物直接为空（前置条件不成立）
        now = time.time()

        async def _seed():
            await store.append_segment(
                self.CHAT,
                event_id="msg-1",
                speaker_id="111",
                speaker_name="阿甲",
                content="我的手机号是 13800001111",
                role="user",
                timestamp=now - 20.0,
            )
            await store.append_segment(
                self.CHAT,
                event_id="msg-2",
                speaker_id="222",
                speaker_name="阿乙",
                content="收到了",
                role="user",
                timestamp=now - 10.0,
            )
            return store

        return asyncio.run(_seed())

    def _segments(self, store):
        thread = store._get_thread(store._resolve_chat_key(self.CHAT))
        return list(thread.segments)

    def test_recall_replaces_content_but_keeps_speaker_and_order(self):
        store = self._store_with_messages()

        hit = asyncio.run(store.mark_recalled(self.CHAT, "msg-1"))

        self.assertTrue(hit)
        segments = self._segments(store)
        self.assertEqual([s.event_id for s in segments], ["msg-1", "msg-2"], "时序必须保留")
        recalled = segments[0]
        self.assertEqual(recalled.content, "[已撤回]")
        self.assertTrue(recalled.is_recalled)
        self.assertEqual(recalled.speaker_id, "111", "speaker 必须保留")
        self.assertEqual(recalled.speaker_name, "阿甲")
        self.assertLess(recalled.timestamp, segments[1].timestamp, "时序关系必须保留")
        # 未被撤回的消息不受影响
        self.assertEqual(segments[1].content, "收到了")

    def test_recalled_text_absent_from_rendered_context(self):
        store = self._store_with_messages()
        secret = "13800001111"

        async def _run():
            before = await store.get_warm_context_bundle(self.CHAT)
            await store.mark_recalled(self.CHAT, "msg-1")
            after = await store.get_warm_context_bundle(self.CHAT)
            return before, after

        before, after = asyncio.run(_run())

        rendered_before = f"{before.summary_text}\n{before.quote_text}\n{before.topic_preview}"
        rendered_after = f"{after.summary_text}\n{after.quote_text}\n{after.topic_preview}"
        self.assertIn(secret, rendered_before, "前置条件：撤回前原文确实会进入渲染产物")
        self.assertNotIn(secret, rendered_after, "撤回后原文不得出现在任何渲染产物中")

    def test_unknown_event_id_is_safe_noop(self):
        store = self._store_with_messages()

        self.assertFalse(asyncio.run(store.mark_recalled(self.CHAT, "not-in-hot-zone")))
        self.assertFalse(asyncio.run(store.mark_recalled(self.CHAT, "")))
        self.assertEqual([s.content for s in self._segments(store)], ["我的手机号是 13800001111", "收到了"])

    def test_repeat_recall_is_idempotent(self):
        store = self._store_with_messages()

        first = asyncio.run(store.mark_recalled(self.CHAT, "msg-1"))
        second = asyncio.run(store.mark_recalled(self.CHAT, "msg-1"))

        self.assertTrue(first)
        self.assertFalse(second, "已墓碑化的消息重复撤回应为 no-op")
        self.assertEqual(self._segments(store)[0].content, "[已撤回]")


class RecallNoticeRoutingTests(unittest.TestCase):
    def test_group_and_friend_recall_are_routed_as_recall_notice(self):
        for notice_type in ("group_recall", "friend_recall"):
            with self.subTest(notice_type=notice_type):
                event = _RecallEvent({"notice_type": notice_type, "message_id": "msg-1"})
                route, payload = message_entry._classify_event_route(event)
                self.assertEqual(route, "recall_notice")
                self.assertEqual(payload.get("message_id"), "msg-1")

    def test_other_notices_keep_existing_routes(self):
        poke = _RecallEvent({"notice_type": "notify", "sub_type": "poke", "user_id": "111"})
        self.assertEqual(message_entry._classify_event_route(poke)[0], "poke_notice")

        other = _RecallEvent({"notice_type": "group_upload", "user_id": "111"})
        self.assertEqual(message_entry._classify_event_route(other)[0], "notice_passthrough")

    def test_recall_notice_calls_facade_and_marks_non_conversational(self):
        calls = []

        class _Facade:
            async def handle_message_recall(self, chat_id, event_id):
                calls.append((chat_id, event_id))
                return True

        event = _RecallEvent({"notice_type": "group_recall", "message_id": "msg-1"})

        asyncio.run(_drive(message_entry.handle_global_message(_Facade(), event)))

        self.assertEqual(calls, [("default:GroupMessage:7000", "msg-1")])
        self.assertTrue(event.get_extra("astrmai_non_conversational", False))
        self.assertTrue(event.get_extra("astrmai_recall_tombstoned", False))
        self.assertEqual(event.get_extra("astrmai_recalled_message_id"), "msg-1")

    def test_recall_without_message_id_does_not_call_facade(self):
        calls = []

        class _Facade:
            async def handle_message_recall(self, chat_id, event_id):
                calls.append((chat_id, event_id))
                return True

        event = _RecallEvent({"notice_type": "group_recall"})

        asyncio.run(_drive(message_entry.handle_global_message(_Facade(), event)))

        self.assertEqual(calls, [])
        self.assertFalse(event.get_extra("astrmai_recall_tombstoned", True))


class FacadeRecallDelegationTests(unittest.TestCase):
    def test_facade_delegates_to_dialogue_store(self):
        from astrmai.app.plugin_facade import PluginFacade

        marked = []

        class _Store:
            async def mark_recalled(self, chat_id, event_id):
                marked.append((chat_id, event_id))
                return True

        facade = PluginFacade.__new__(PluginFacade)
        facade.runtime = SimpleNamespace(dialogue_store=_Store())

        result = asyncio.run(facade.handle_message_recall("chat-1", "msg-9"))

        self.assertTrue(result)
        self.assertEqual(marked, [("chat-1", "msg-9")])

    def test_facade_survives_missing_store(self):
        from astrmai.app.plugin_facade import PluginFacade

        facade = PluginFacade.__new__(PluginFacade)
        facade.runtime = SimpleNamespace(dialogue_store=None)

        self.assertFalse(asyncio.run(facade.handle_message_recall("chat-1", "msg-9")))


if __name__ == "__main__":
    unittest.main()
