"""OPT-01 回归测试：群聊在途回复丢弃止血（ID-01 / TL-05 / ID-07）。

守护三个不变式：
1. TL-05: superseded_by_newer_activity 的 _same_thread/_unknown_thread 变体必须被识别为 stale，
   不得落入 executor 模型级联被当作模型失败重跑。
2. ID-01: 活动标记（mark_activity）与 freshness 检查两侧必须使用同一线程标识空间
   （ingress 即有的 turn thread id），线程隔离不再因签名恒空而失效。
3. ID-07: reply:* 键位的群聊等待，目标用户用纯文本跟进（无引用组件）时必须能复活。
"""

import asyncio
import time
import unittest
from types import SimpleNamespace

from astrmai.conversation.attention.gate import AttentionGate
from astrmai.conversation.execution.executor import ConcurrentExecutor
from astrmai.conversation.execution.reply_freshness import (
    ReplyFreshnessMixin,
    is_stale_reply_reason,
)
from astrmai.infrastructure.runtime.chat_runtime_coordinator import ChatRuntimeCoordinator
from astrmai.infrastructure.runtime.runtime_contracts import FreshnessState
from astrmai.state.group_wait.group_reply_wait_manager import GroupReplyWaitManager


class _MockEvent:
    def __init__(self, sender_id="111", sender_name="A", group_id="552752264", text="hi"):
        self._extras = {}
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._group_id = group_id
        self.message_str = text
        self.unified_msg_origin = f"ff:GroupMessage:{group_id}" if group_id else f"ff:FriendMessage:{sender_id}"
        self.message_obj = SimpleNamespace(message=[])
        self.timestamp = time.time()

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_group_id(self):
        return self._group_id


class _RecordingCoordinator:
    """只记录调用参数的协调器桩：不含 is_current_turn，绕开 generation 检查分支。"""

    def __init__(self):
        self.mark_activity_calls = []
        self.freshness_calls = []

    async def mark_activity(self, chat_id, timestamp, sender_id="", sender_name="", preview="", thread_signature=""):
        self.mark_activity_calls.append(
            {
                "chat_id": chat_id,
                "timestamp": timestamp,
                "sender_id": sender_id,
                "thread_signature": thread_signature,
            }
        )

    async def evaluate_reply_freshness(self, chat_id, focus_timestamp, **kwargs):
        self.freshness_calls.append({"chat_id": chat_id, "focus_timestamp": focus_timestamp, **kwargs})
        return FreshnessState.FRESH, ""

    async def get_latest_activity(self, chat_id):
        return 0.0, "", "", ""


class StaleReasonClassificationTests(unittest.TestCase):
    """TL-05：三种 reason 格式两套判据 → 统一宽前缀。"""

    def test_thread_variant_reasons_are_classified_stale(self):
        self.assertTrue(is_stale_reply_reason("superseded_by_newer_activity_unknown_thread:actor:5.0s"))
        self.assertTrue(is_stale_reply_reason("superseded_by_newer_activity_same_thread:actor:3.2s"))
        self.assertTrue(is_stale_reply_reason("superseded_by_newer_activity:actor:preview"))
        self.assertTrue(is_stale_reply_reason("reply_age_exceeded:94.6s>90.0s"))
        self.assertTrue(is_stale_reply_reason("stale_generation"))

    def test_non_stale_reasons_untouched(self):
        self.assertFalse(is_stale_reply_reason("transport failed"))
        self.assertFalse(is_stale_reply_reason(""))
        self.assertFalse(is_stale_reply_reason("empty tool reply"))


class MarkActivityThreadIdentityTests(unittest.TestCase):
    """ID-01 标记侧：ingress 时刻 mark_activity 必须携带非空线程标识（turn thread id）。"""

    def _build_gate(self, coordinator):
        gate = AttentionGate.__new__(AttentionGate)
        gate.runtime_coordinator = coordinator
        gate.state_engine = SimpleNamespace(bot_id="999")

        async def _fake_session(chat_id):
            return SimpleNamespace(last_active_user_time=0.0)

        gate._get_or_create_session = _fake_session
        return gate

    def test_mark_activity_receives_turn_thread_id_at_ingress(self):
        coordinator = _RecordingCoordinator()
        gate = self._build_gate(coordinator)
        event = _MockEvent(sender_id="111")
        # message_entry 在 gate 之前绑定 turn 身份；此时 astrmai_thread_signature 尚不存在
        event.set_extra("astrmai_turn_thread_id", "sender:111")

        asyncio.run(gate._record_event_activity(event.unified_msg_origin, event, "111"))

        self.assertEqual(len(coordinator.mark_activity_calls), 1)
        recorded = coordinator.mark_activity_calls[0]["thread_signature"]
        self.assertEqual(recorded, "sender:111")

    def test_mark_activity_falls_back_to_resolver_without_turn_binding(self):
        coordinator = _RecordingCoordinator()
        gate = self._build_gate(coordinator)
        event = _MockEvent(sender_id="333")
        # 合成/注入事件可能没有 turn 绑定：应回退到 resolve_group_thread（sender: 前缀）

        asyncio.run(gate._record_event_activity(event.unified_msg_origin, event, "333"))

        recorded = coordinator.mark_activity_calls[0]["thread_signature"]
        self.assertEqual(recorded, "sender:333")


class ReplyFreshnessThreadIdentityTests(unittest.TestCase):
    """ID-01 检查侧：freshness 检查传给协调器的线程标识必须与标记侧同空间。"""

    class _Host(ReplyFreshnessMixin):
        def __init__(self, coordinator):
            self.runtime_coordinator = coordinator
            self.config = SimpleNamespace(timing=SimpleNamespace(reply_max_age_sec=450.0))

    def test_check_reply_freshness_prefers_turn_thread_id(self):
        coordinator = _RecordingCoordinator()
        host = self._Host(coordinator)
        event = _MockEvent(sender_id="777")
        event.set_extra("astrmai_timestamp", time.time() - 5.0)
        event.set_extra("astrmai_turn_thread_id", "sender:777")

        state, reason = asyncio.run(host._check_reply_freshness(event, event.unified_msg_origin))

        self.assertEqual(state, FreshnessState.FRESH)
        call = coordinator.freshness_calls[0]
        self.assertEqual(call.get("thread_signature"), "sender:777")
        self.assertTrue(call.get("allow_parallel_threads"))

    def test_check_reply_freshness_consistent_when_both_identities_present(self):
        coordinator = _RecordingCoordinator()
        host = self._Host(coordinator)
        event = _MockEvent(sender_id="777")
        event.set_extra("astrmai_timestamp", time.time() - 5.0)
        event.set_extra("astrmai_turn_thread_id", "sender:777")
        event.set_extra("astrmai_thread_signature", "sender:777")

        asyncio.run(host._check_reply_freshness(event, event.unified_msg_origin))

        call = coordinator.freshness_calls[0]
        self.assertEqual(call.get("thread_signature"), "sender:777")


class ExecutorFreshnessThreadIdentityTests(unittest.TestCase):
    """ID-01 执行器侧：预检也必须带线程标识 + 群聊放行并行线程。"""

    def _build_executor(self, coordinator):
        executor = ConcurrentExecutor.__new__(ConcurrentExecutor)
        executor.runtime_coordinator = coordinator
        executor.config = SimpleNamespace(timing=SimpleNamespace(reply_max_age_sec=450.0))
        return executor

    def test_executor_freshness_passes_thread_id_and_parallel_flag(self):
        coordinator = _RecordingCoordinator()
        executor = self._build_executor(coordinator)
        event = _MockEvent(sender_id="777")
        event.set_extra("astrmai_timestamp", time.time() - 3.0)
        event.set_extra("astrmai_turn_thread_id", "sender:777")

        state, reason = asyncio.run(
            executor._evaluate_execution_freshness(event, event.unified_msg_origin)
        )

        self.assertEqual(state, FreshnessState.FRESH)
        call = coordinator.freshness_calls[0]
        self.assertEqual(call.get("thread_signature"), "sender:777")
        self.assertTrue(call.get("allow_parallel_threads"))

    def test_executor_freshness_private_disallows_parallel(self):
        coordinator = _RecordingCoordinator()
        executor = self._build_executor(coordinator)
        event = _MockEvent(sender_id="777", group_id="")
        event.set_extra("astrmai_timestamp", time.time() - 3.0)
        event.set_extra("astrmai_turn_thread_id", "private:ff:FriendMessage:777")
        event.set_extra("is_private_chat", True)

        asyncio.run(executor._evaluate_execution_freshness(event, event.unified_msg_origin))

        call = coordinator.freshness_calls[0]
        self.assertFalse(call.get("allow_parallel_threads", False))


class CoordinatorThreadIsolationContractTests(unittest.TestCase):
    """ID-01 语义锁：两侧签名非空后，协调器的线程隔离行为契约。"""

    def test_other_thread_activity_is_ignored_for_groups(self):
        coordinator = ChatRuntimeCoordinator()

        async def _run():
            base = time.time()
            await coordinator.mark_activity(
                "chat-g", base + 10.0, sender_id="222", sender_name="B",
                preview="unrelated", thread_signature="sender:222",
            )
            return await coordinator.evaluate_reply_freshness(
                "chat-g",
                base,
                max_age_seconds=450.0,
                thread_signature="sender:111",
                allow_parallel_threads=True,
            )

        state, reason = asyncio.run(_run())
        self.assertEqual(state, FreshnessState.FRESH)
        self.assertEqual(reason, "newer_activity_other_thread_ignored")

    def test_same_thread_newer_activity_still_expires(self):
        coordinator = ChatRuntimeCoordinator()

        async def _run():
            base = time.time()
            await coordinator.mark_activity(
                "chat-g2", base + 30.0, sender_id="111", sender_name="A",
                preview="moved on", thread_signature="sender:111",
            )
            return await coordinator.evaluate_reply_freshness(
                "chat-g2",
                base,
                max_age_seconds=450.0,
                thread_signature="sender:111",
                allow_parallel_threads=True,
            )

        state, reason = asyncio.run(_run())
        self.assertEqual(state, FreshnessState.EXPIRED)
        self.assertTrue(reason.startswith("superseded_by_newer_activity_same_thread"))


class GroupWaitPureTextResumeTests(unittest.TestCase):
    """ID-07：reply:* 键位等待 + 目标纯文本跟进（仅 turn thread id）应复活。"""

    def _arm_reply_keyed_wait(self, manager):
        reply_event = _MockEvent(sender_id="999", sender_name="bot", group_id="7000")
        reply_event.set_extra("astrmai_turn_thread_id", "reply:msg-42")
        reply_event.set_extra("astrmai_wait_targets", ["222"])
        reply_event.set_extra("astrmai_wait_target_name", "B")
        self.assertTrue(manager.register_from_reply_event(reply_event))

    def test_pure_text_followup_from_target_resumes(self):
        manager = GroupReplyWaitManager(timeout_sec=60.0, message_budget=5, threaded_enabled=True)
        self._arm_reply_keyed_wait(manager)

        incoming = _MockEvent(sender_id="222", sender_name="B", group_id="7000", text="在的")
        incoming.set_extra("astrmai_turn_thread_id", "sender:222")
        # 纯文本跟进：无 Reply 组件、无 astrmai_thread_signature

        action = manager.handle_incoming_message(incoming)

        self.assertEqual(action, "RESUME")
        self.assertTrue(incoming.get_extra("astrmai_force_engage", False))

    def test_focus_signature_still_blocks_cross_thread_hijack(self):
        manager = GroupReplyWaitManager(timeout_sec=60.0, message_budget=5, threaded_enabled=True)
        self._arm_reply_keyed_wait(manager)

        # 目标用户在另一个明确线程里发言（带 focus 签名）——不应劫持 reply:* 等待
        incoming = _MockEvent(sender_id="222", sender_name="B", group_id="7000", text="另一件事")
        incoming.set_extra("astrmai_turn_thread_id", "sender:222")
        incoming.set_extra("astrmai_thread_signature", "topic:other-thread")

        action = manager.handle_incoming_message(incoming)

        self.assertNotEqual(action, "RESUME")


if __name__ == "__main__":
    unittest.main()
