"""OPT-08 回归测试：模型调用成本削减（RT-03 / RT-06 / RT-09 / ID-09）。

守护不变式：
1. RT-03: mood 后置开启时，判决前不再做独立情绪 LLM（旧行为 364 次/16h，
   88% 花在最终不回复的消息上）；关闭配置可回退旧行为。
2. RT-06: think1 消息不再无条件运行 cognitive_loop（8-35s 意图分类），
   仅长句/复杂度信号放行；门槛可配置。
3. RT-09: judge 固定 rubric 全部位于 JUDGE_STABLE_PREFIX（system 可缓存），
   动态 user prompt 不再内嵌固定段。
4. ID-09: 私聊默认跳过 judge（可配置关闭）。
"""

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrmai.conversation.attention.decision_router import AttentionDecisionRouter
from astrmai.conversation.attention.gate import AttentionGate
from astrmai.conversation.decision.judge_prompt import JUDGE_STABLE_PREFIX
from astrmai.conversation.planning.cognitive_loop import CognitiveLoop


class _Event:
    def __init__(self, text="hello", think_level=None):
        self._extras = {}
        if think_level is not None:
            self._extras["astrmai_think_level"] = think_level
        self.message_str = text
        self.unified_msg_origin = "ff:GroupMessage:7000"

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_sender_id(self):
        return "111"

    def get_sender_name(self):
        return "A"


def _gate_stub(post_judge_enabled: bool):
    recorded = []

    class _Judge:
        async def evaluate(self, *args, **kwargs):
            return SimpleNamespace(
                action="IGNORE",
                thought="",
                retrieve_keys=[],
                relevance=1,
                necessity=1.0,
                mood_tag="neutral",
                mood_delta=0.0,
                reason="test",
            )

    class _StateEngine:
        async def update_mood(self, chat_id, text):
            recorded.append((chat_id, text))
            return "neutral", 0.0

    gate = SimpleNamespace(
        judge=_Judge(),
        state_engine=_StateEngine(),
        config=SimpleNamespace(
            attention=SimpleNamespace(
                mood_post_judge_enabled=post_judge_enabled,
                judge_timeout=3.0,
            ),
            reply=None,
        ),
        _mood_post_judge_enabled=lambda: post_judge_enabled,
    )
    return gate, recorded


class MoodPostJudgeTests(unittest.TestCase):
    """RT-03：判决前的独立 mood LLM 受配置控制。"""

    def _evaluate(self, gate):
        router = AttentionDecisionRouter(gate)
        event = _Event(text="随便聊聊今天挺开心的")
        return asyncio.run(
            router.evaluate(
                "ff:GroupMessage:7000",
                event,
                None,
                [event],
                is_strong_wakeup=False,
            )
        )

    def test_post_judge_enabled_skips_pre_judge_mood(self):
        gate, recorded = _gate_stub(post_judge_enabled=True)
        try:
            self._evaluate(gate)
        except Exception:
            pass  # 判决深链路依赖 stub 之外的组件，这里只关心 mood 是否被前置调用
        self.assertEqual(recorded, [])

    def test_post_judge_disabled_keeps_pre_judge_mood(self):
        gate, recorded = _gate_stub(post_judge_enabled=False)
        try:
            self._evaluate(gate)
        except Exception:
            pass
        self.assertEqual(len(recorded), 1)


class GateConfigPlumbingTests(unittest.TestCase):
    """RT-03/ID-09：gate 配置帮助方法。"""

    def _gate(self, **attention_kwargs):
        gate = AttentionGate.__new__(AttentionGate)
        gate.config = SimpleNamespace(attention=SimpleNamespace(**attention_kwargs))
        return gate

    def test_mood_post_judge_default_on(self):
        gate = AttentionGate.__new__(AttentionGate)
        gate.config = SimpleNamespace(attention=None)
        self.assertTrue(gate._mood_post_judge_enabled())

    def test_private_skip_judge_respects_config(self):
        self.assertFalse(
            self._gate(private_skip_judge_enabled=False)._private_skip_judge_enabled()
        )
        self.assertTrue(
            self._gate(private_skip_judge_enabled=True)._private_skip_judge_enabled()
        )


class CognitiveLoopGateTests(unittest.TestCase):
    """RT-06：think1 不再无条件放行。"""

    def _loop(self, min_level=2):
        return CognitiveLoop(
            gateway=None,
            config=SimpleNamespace(
                attention=SimpleNamespace(cognitive_loop_min_think_level=min_level),
                timing=SimpleNamespace(),
            ),
        )

    def test_think1_short_message_skipped(self):
        loop = self._loop()
        result = loop.gate_decision(_Event(text="呜呜呜", think_level=1))
        self.assertFalse(result.should_run)

    def test_think1_long_message_still_runs(self):
        loop = self._loop()
        result = loop.gate_decision(
            _Event(text="帮我对比一下这两个方案的优缺点然后给出建议", think_level=1)
        )
        self.assertTrue(result.should_run)

    def test_think2_unconditionally_runs(self):
        loop = self._loop()
        result = loop.gate_decision(_Event(text="嗯", think_level=2))
        self.assertTrue(result.should_run)

    def test_min_level_one_restores_old_behavior(self):
        loop = self._loop(min_level=1)
        result = loop.gate_decision(_Event(text="呜呜呜", think_level=1))
        self.assertTrue(result.should_run)


class JudgePromptStructureTests(unittest.TestCase):
    """RT-09：固定 rubric 全在 stable prefix，动态段不再内嵌。"""

    _FIXED_MARKERS = [
        "可选的人格维度 Key",
        "mood_delta 为情绪变化值",
        "请严格按照以下 JSON 格式输出",
        "【思考与决策流】",
    ]

    def test_fixed_rubric_lives_in_stable_prefix(self):
        for marker in self._FIXED_MARKERS:
            self.assertIn(marker, JUDGE_STABLE_PREFIX)

    def test_judge_source_no_longer_embeds_fixed_rubric(self):
        source = Path("astrmai/conversation/decision/judge.py").read_text(encoding="utf-8")
        for marker in self._FIXED_MARKERS:
            self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
