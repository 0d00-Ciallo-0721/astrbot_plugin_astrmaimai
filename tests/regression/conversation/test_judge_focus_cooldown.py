"""G6 / RT-02 附带回归测试：judge 焦点冷却。

被 judge 判 IGNORE 的 focus 事件会被放回 attention window，下一批仍可能再次被选中
重复判决——线上实测同一 focus 150s 内判 10 次，judge 池 539 次调用中 521 次花在
最终被忽略的消息上。

守护不变式：
1. 被忽略过的事件在焦点评分中按轮次降权，新消息优先。
2. **强唤醒信号（@bot / 回复 bot / 点名 / 直接视觉）永不受冷却影响**——这是本改动
   最大的风险面（群聊唤醒灵敏度），必须锁死。
3. 冷却可配置关闭（默认开）；penalty=0 等同关闭。
4. gate 在 IGNORE 分支累加轮次计数，且开关关闭时不累加。
"""

import time
import unittest
from types import SimpleNamespace

from astrmai.conversation.attention.focus_selector import score_focus_candidate
from astrmai.conversation.attention.gate import AttentionGate


class _Event:
    def __init__(self, extras=None):
        self._extras = dict(extras or {})

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


class _Candidate:
    def __init__(
        self,
        *,
        index=0,
        sender_id="111",
        timestamp=None,
        ignored_rounds=0,
        is_at_bot=False,
        is_reply_to_bot=False,
        is_direct_wakeup=False,
        has_direct_vision=False,
        is_near_context_query=False,
    ):
        extras = {}
        if ignored_rounds:
            extras["astrmai_judge_ignored_rounds"] = ignored_rounds
        self.event = _Event(extras)
        self.index = index
        self.sender_id = sender_id
        self.timestamp = timestamp if timestamp is not None else time.time()
        self.is_self = False
        self.is_at_bot = is_at_bot
        self.is_reply_to_bot = is_reply_to_bot
        self.is_direct_wakeup = is_direct_wakeup
        self.has_direct_vision = has_direct_vision
        self.is_near_context_query = is_near_context_query


def _gate_config(**attention_kwargs):
    defaults = {
        "thread_same_speaker_followup_sec": 8,
        "thread_reply_priority_enabled": True,
        "judge_ignore_focus_penalty": 150,
    }
    defaults.update(attention_kwargs)
    return SimpleNamespace(config=SimpleNamespace(attention=SimpleNamespace(**defaults)))


class FocusCooldownScoringTests(unittest.TestCase):
    def test_ignored_event_is_downweighted_per_round(self):
        gate = _gate_config()
        fresh = _Candidate(index=0)
        once = _Candidate(index=0, ignored_rounds=1)
        twice = _Candidate(index=0, ignored_rounds=2)

        fresh_score, fresh_reason = score_focus_candidate(gate, fresh, [fresh])
        once_score, once_reason = score_focus_candidate(gate, once, [once])
        twice_score, _ = score_focus_candidate(gate, twice, [twice])

        self.assertEqual(fresh_reason, "latest_user_message")
        self.assertEqual(once_reason, "judge_ignored_cooldown")
        self.assertEqual(fresh_score - once_score, 150)
        self.assertEqual(once_score - twice_score, 150, "降权按被忽略轮次线性累加")

    def test_fresh_message_outranks_previously_ignored_one(self):
        gate = _gate_config()
        now = time.time()
        ignored = _Candidate(index=0, sender_id="111", timestamp=now - 1.0, ignored_rounds=2)
        fresh = _Candidate(index=1, sender_id="222", timestamp=now)
        events = [ignored, fresh]

        ignored_score, _ = score_focus_candidate(gate, ignored, events)
        fresh_score, _ = score_focus_candidate(gate, fresh, events)

        self.assertGreater(fresh_score, ignored_score, "新消息必须优先于已被忽略的消息")

    def test_strong_wakeup_signals_are_exempt(self):
        # 风险面锁死：强唤醒不受冷却影响，否则群聊唤醒灵敏度受损
        gate = _gate_config()
        for kwargs, label in (
            ({"is_at_bot": True}, "at_bot"),
            ({"is_reply_to_bot": True}, "reply_to_bot"),
            ({"is_direct_wakeup": True}, "direct_wakeup"),
            ({"has_direct_vision": True}, "direct_vision_request"),
        ):
            with self.subTest(signal=label):
                plain = _Candidate(index=0, **kwargs)
                ignored = _Candidate(index=0, ignored_rounds=3, **kwargs)

                plain_score, plain_reason = score_focus_candidate(gate, plain, [plain])
                ignored_score, ignored_reason = score_focus_candidate(gate, ignored, [ignored])

                self.assertEqual(plain_score, ignored_score, "强唤醒不得被降权")
                self.assertEqual(ignored_reason, plain_reason)
                self.assertNotEqual(ignored_reason, "judge_ignored_cooldown")

    def test_zero_penalty_disables_downweighting(self):
        gate = _gate_config(judge_ignore_focus_penalty=0)
        plain = _Candidate(index=0)
        ignored = _Candidate(index=0, ignored_rounds=5)

        plain_score, _ = score_focus_candidate(gate, plain, [plain])
        ignored_score, _ = score_focus_candidate(gate, ignored, [ignored])

        self.assertEqual(plain_score, ignored_score)

    def test_malformed_counter_is_ignored(self):
        gate = _gate_config()
        broken = _Candidate(index=0)
        broken.event.set_extra("astrmai_judge_ignored_rounds", "not-a-number")
        plain = _Candidate(index=0)

        broken_score, _ = score_focus_candidate(gate, broken, [broken])
        plain_score, _ = score_focus_candidate(gate, plain, [plain])

        self.assertEqual(broken_score, plain_score)


class GateIgnoreCounterTests(unittest.TestCase):
    def _gate(self, enabled=True):
        gate = AttentionGate.__new__(AttentionGate)
        gate.config = SimpleNamespace(
            attention=SimpleNamespace(judge_ignore_focus_cooldown_enabled=enabled)
        )
        return gate

    def test_cooldown_switch_defaults_on(self):
        gate = AttentionGate.__new__(AttentionGate)
        gate.config = SimpleNamespace(attention=None)
        self.assertTrue(gate._judge_ignore_cooldown_enabled())

    def test_switch_respects_config(self):
        self.assertTrue(self._gate(True)._judge_ignore_cooldown_enabled())
        self.assertFalse(self._gate(False)._judge_ignore_cooldown_enabled())

    def test_counter_accumulates_only_when_enabled(self):
        # 模拟 gate IGNORE 分支的计数逻辑（与 gate.py 内实现同构）
        def _bump(gate, event):
            if gate._judge_ignore_cooldown_enabled():
                try:
                    previous = int(event.get_extra("astrmai_judge_ignored_rounds", 0) or 0)
                except (TypeError, ValueError):
                    previous = 0
                event.set_extra("astrmai_judge_ignored_rounds", previous + 1)

        enabled_gate, disabled_gate = self._gate(True), self._gate(False)
        event = _Event()
        _bump(enabled_gate, event)
        _bump(enabled_gate, event)
        self.assertEqual(event.get_extra("astrmai_judge_ignored_rounds"), 2)

        off_event = _Event()
        _bump(disabled_gate, off_event)
        self.assertIsNone(off_event.get_extra("astrmai_judge_ignored_rounds"))


class GateSourceContractTests(unittest.TestCase):
    """装配断言：gate 的 IGNORE 分支确实累加计数（防止实现被摘掉）。"""

    def test_ignore_branch_bumps_counter(self):
        from pathlib import Path

        source = Path("astrmai/conversation/attention/gate.py").read_text(encoding="utf-8")
        ignore_index = source.find('elif judge_action == "IGNORE":')
        self.assertGreater(ignore_index, 0)
        window = source[ignore_index : ignore_index + 900]
        self.assertIn("_judge_ignore_cooldown_enabled()", window)
        self.assertIn("astrmai_judge_ignored_rounds", window)


if __name__ == "__main__":
    unittest.main()
