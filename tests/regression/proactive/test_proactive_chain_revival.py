"""OPT-03 回归测试：主动行为链复活（PL-01/ID-02、ID-03、ID-06、ID-10、PL-02）。

守护不变式：
1. PL-01/ID-03: 带 astrmai_is_proactive_event / astrmai_interaction_kind 标记的合成事件
   不得被传感器的"组件文本为空"过滤绞杀（它们只有 message_str 无消息组件）。
2. ID-06: 主动候选事件不生成"当前发言人归因锁"（否则模型对着幽灵用户
   astrmai_proactive_candidate 说"你"）。
3. ID-10: poke 目标缺失时不得伪装成"戳 bot"（无端回戳+好感误结算），应跳过互动处理。
4. PL-02: pre-planner 终结路径必须填充 trace 的 proactive 快照（否则主动链死因不可见）。
"""

import asyncio
import time
import unittest
from types import SimpleNamespace

from astrmai.conversation.attention.gate import AttentionGate
from astrmai.conversation.contracts.turn_context import ensure_turn_context
from astrmai.conversation.ingress.sensors import PreFilters
from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin


class _Event:
    def __init__(self, sender_id="111", self_id="999", group_id="7000", text=""):
        self._extras = {}
        self._sender_id = sender_id
        self._self_id = self_id
        self._group_id = group_id
        self.message_str = text
        self.unified_msg_origin = f"ff:GroupMessage:{group_id}" if group_id else f"ff:FriendMessage:{sender_id}"
        self.message_obj = SimpleNamespace(message=[], self_id=self_id, raw_message=None)
        self.timestamp = time.time()

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return "某人"

    def get_self_id(self):
        return self._self_id

    def get_group_id(self):
        return self._group_id


def _build_prefilters() -> PreFilters:
    pf = PreFilters.__new__(PreFilters)

    async def _noop_load():
        return None

    pf._load_foreign_commands = _noop_load
    pf.foreign_commands = set()
    pf.config = SimpleNamespace(
        vision=SimpleNamespace(enable_vision=False, image_recognition_probability=1.0),
        system1=SimpleNamespace(nicknames=[]),
    )
    return pf


class SensorSyntheticEventExemptionTests(unittest.TestCase):
    """PL-01/ID-03：合成事件放行（此前 14/14 主动候选 + 30/30 peer poke 全灭于此）。"""

    def test_proactive_candidate_passes_sensor(self):
        pf = _build_prefilters()
        event = _Event(text="[主动开口候选]\n这不是用户消息……")
        event.set_extra("astrmai_is_proactive_event", True)

        allowed = asyncio.run(pf.should_process_message(event))

        self.assertTrue(allowed)

    def test_peer_poke_interaction_passes_sensor(self):
        pf = _build_prefilters()
        event = _Event(text="A 戳了 B 一下，这是群友之间的轻互动")
        event.set_extra("astrmai_interaction_kind", "peer_poke")

        allowed = asyncio.run(pf.should_process_message(event))

        self.assertTrue(allowed)

    def test_plain_empty_component_message_still_filtered(self):
        pf = _build_prefilters()
        event = _Event(text="")

        allowed = asyncio.run(pf.should_process_message(event))

        self.assertFalse(allowed)


class ProactiveSpeakerBlockTests(unittest.TestCase):
    """ID-06：主动候选不得生成发言人归因锁。"""

    def _focus_context(self):
        return SimpleNamespace(
            focus_sender_id="astrmai_proactive_candidate",
            focus_sender_name="主动开口候选",
            vision_bundle=None,
        )

    def test_speaker_block_empty_for_proactive_event(self):
        event = _Event()
        event.set_extra("astrmai_is_proactive_event", True)

        block = PlannerPromptContextMixin._build_current_speaker_block(
            event,
            self._focus_context(),
            is_lightweight_event=False,
        )

        self.assertEqual(block, "")

    def test_speaker_block_kept_for_real_user(self):
        event = _Event()

        block = PlannerPromptContextMixin._build_current_speaker_block(
            event,
            SimpleNamespace(focus_sender_id="111", focus_sender_name="真人", vision_bundle=None),
            is_lightweight_event=False,
        )

        self.assertIn("QQ: 111", block)


class PokeUnknownTargetTests(unittest.TestCase):
    """ID-10：目标缺失的 poke 应跳过处理，而不是伪装成'戳 bot'。"""

    def test_missing_target_skips_interaction(self):
        pf = PreFilters.__new__(PreFilters)
        event = _Event(sender_id="111", self_id="999")
        # raw payload：poke 事件但 target_id 缺失
        event.message_obj.raw_message = {
            "sub_type": "poke",
            "notice_type": "notify",
            "user_id": "111",
            "target_id": "",
            "group_id": "7000",
        }

        asyncio.run(pf.process_poke_event(event, None, None))

        # 跳过整个互动：不生成叙事、不打互动标记（旧行为会当作"戳 bot"回戳+结算好感）
        self.assertTrue(event.get_extra("astrmai_interaction_target_unknown", False))
        self.assertIsNone(event.get_extra("astrmai_interaction_kind"))
        self.assertEqual(event.message_str, "")


class PrePlannerProactiveTraceTests(unittest.TestCase):
    """PL-02：pre-planner 终结路径填充 proactive 快照。"""

    def test_finalize_fills_proactive_snapshot(self):
        gate = AttentionGate.__new__(AttentionGate)
        captured = {}

        async def _trace_callback(chat_id, event, *, status, reply_text=None):
            captured["status"] = status

        gate.turn_trace_callback = _trace_callback

        event = _Event()
        event.set_extra("astrmai_is_proactive_event", True)
        event.set_extra("astrmai_proactive_source", "wakeup")
        event.set_extra("astrmai_proactive_intent_id", "intent-1")
        event.set_extra("astrmai_proactive_reason", "quiet_too_long")

        asyncio.run(
            gate._finalize_pre_planner_turn(
                event,
                event.unified_msg_origin,
                status="skipped_sensor_filter",
            )
        )

        snapshot = ensure_turn_context(event).proactive
        self.assertTrue(snapshot.is_proactive)
        self.assertEqual(snapshot.source, "wakeup")
        self.assertEqual(snapshot.intent_id, "intent-1")
        self.assertEqual(snapshot.dispatch_status, "skipped_sensor_filter")
        self.assertEqual(snapshot.blocked_reason, "skipped_sensor_filter")
        self.assertEqual(captured["status"], "skipped_sensor_filter")


if __name__ == "__main__":
    unittest.main()
