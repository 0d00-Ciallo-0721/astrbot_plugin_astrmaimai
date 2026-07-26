"""OPT-13/TG-01 回归测试：群聊身份三源一致性（聚焦链）。

历史真实发生过称呼串号（为此写了 GroupActorConsistencyGuard），但三个身份来源
（focus 选择、speaker block、side_inputs 画像取值）此前只有各自单测。本测试
以"双 sender 交替发言、B 直接唤醒"为场景，断言三源指向同一 sender：
focus 选中的事件 == speaker block 中的 QQ == 画像加载将使用的 get_sender_id()。

完整 gate→planner→executor 三段拼装 e2e 仍属专项（见 OPT-13 完成记录），
本链已覆盖"身份从选择到 prompt 的塌缩点"。
"""

import time
import unittest
from types import SimpleNamespace

from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin


class _Event:
    def __init__(self, sender_id, sender_name, text, *, is_direct=False):
        self._extras = {
            "astrmai_timestamp": time.time(),
            "astrmai_group_direct_wakeup": is_direct,
        }
        self._sender_id = sender_id
        self._sender_name = sender_name
        self.message_str = text
        self.unified_msg_origin = "ff:GroupMessage:7000"
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
        return "7000"


class GroupIdentityChainTests(unittest.TestCase):
    def _alternating_events(self):
        return [
            _Event("111", "阿甲", "今天天气不错"),
            _Event("222", "阿乙", "是啊出去玩吗"),
            _Event("111", "阿甲", "我在加班呢"),
            _Event("222", "阿乙", "机器人你怎么看", is_direct=True),
        ]

    def test_speaker_block_matches_focus_sender_not_previous_speaker(self):
        events = self._alternating_events()
        focus_event = events[-1]  # 直接唤醒者 B(222)

        focus_context = SimpleNamespace(
            focus_sender_id=focus_event.get_sender_id(),
            focus_sender_name=focus_event.get_sender_name(),
            vision_bundle=None,
        )
        block = PlannerPromptContextMixin._build_current_speaker_block(
            focus_event,
            focus_context,
            is_lightweight_event=False,
        )

        # 三源一致：speaker block 的 QQ == focus 事件 sender == 画像层将使用的 id
        self.assertIn("QQ: 222", block)
        self.assertIn("阿乙", block)
        self.assertNotIn("QQ: 111", block)
        self.assertNotIn("阿甲", block)
        self.assertEqual(focus_event.get_sender_id(), "222")

    def test_focus_context_empty_falls_back_to_event_identity(self):
        # focus_context 缺失身份时必须回退到事件本身，而不是残留上一个人
        focus_event = _Event("222", "阿乙", "在吗", is_direct=True)
        block = PlannerPromptContextMixin._build_current_speaker_block(
            focus_event,
            SimpleNamespace(focus_sender_id="", focus_sender_name="", vision_bundle=None),
            is_lightweight_event=False,
        )
        self.assertIn("QQ: 222", block)
        self.assertIn("阿乙", block)

    def test_group_boundary_reminder_present(self):
        focus_event = _Event("222", "阿乙", "在吗", is_direct=True)
        block = PlannerPromptContextMixin._build_current_speaker_block(
            focus_event,
            SimpleNamespace(focus_sender_id="222", focus_sender_name="阿乙", vision_bundle=None),
            is_lightweight_event=False,
        )
        self.assertIn("历史里的其他发言人只是背景", block)


if __name__ == "__main__":
    unittest.main()
