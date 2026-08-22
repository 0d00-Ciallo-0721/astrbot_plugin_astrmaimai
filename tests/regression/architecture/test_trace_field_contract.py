"""G9 / TG-04 收尾：executed trace 字段完整性契约。

c4aee57 宣称 "complete turn trace observability"，但没有任何测试锚定"executed trace
必须包含哪些字段"——于是 memory_funnel 在 executed 轮 64/67 缺失就这么溜进了生产
（运营者排查"记忆为什么没注入"时 96% 的轮次无数据，无法区分合理跳过与仪表故障）。

本测试把契约固化：executed trace 必须带齐 llm_call_ledger / stage_ledger /
context_block_stats / memory_funnel / reply_stats / budget。

注：memory_funnel 的补写发生在 prompt_refiner 的外包裹（OPT-11/G-TG04），此处从
trace 组装层验证——只要 event 上有这些 extra，trace 就必须原样带出。
"""

import time
import unittest

from astrmai.conversation.planning.planner import Planner
from astrmai.infrastructure.runtime.turn_call_ledger import (
    begin_llm_call,
    configure_turn_budget,
    finish_llm_call,
    observe_stage,
    record_context_block_stats,
    record_reply_stats,
    record_vision_observation,
    turn_telemetry_scope,
)


class _Event:
    def __init__(self, extras=None):
        self._extras = dict(extras or {})
        self.message_str = "测试消息"
        self.unified_msg_origin = "ff:GroupMessage:7000"

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_sender_id(self):
        return "111"

    def get_sender_name(self):
        return "阿甲"

    def get_group_id(self):
        return "7000"


class ExecutedTraceFieldContractTests(unittest.TestCase):
    #  executed trace 的必备字段——缺任何一个都会让线上排查失去依据
    REQUIRED_FIELDS = (
        "llm_call_ledger",
        "stage_ledger",
        "context_block_stats",
        "memory_funnel",
        "reply_stats",
        "budget",
    )

    def _executed_event(self):
        """用**真实仪表 API** 造一轮 executed。

        注意：trace 组装优先读 telemetry 快照，只有快照缺失才回落 event extras；
        直接塞 astrmai_llm_call_ledger 等 extra 会被快照分支整个盖掉，测出来是假绿。
        """
        now = time.time()
        event = _Event({"astrmai_turn_created_at": now, "astrmai_memory_funnel": {
            "status": "skipped",
            "skip_reason": "think_level_0",
        }})
        with turn_telemetry_scope(event):
            call_id = begin_llm_call(event, stage="gateway.chat", pool="judge", prompt="判断一下")
            finish_llm_call(event, call_id, status="success", model="m", output="是")
            with observe_stage(event, "reply.send"):
                pass
            record_context_block_stats(
                event,
                stage="planner.final_prompt_transmitted",
                blocks={"persona": "人设文本"},
                total_chars=9000,
            )
            record_reply_stats(
                event,
                segment_count=1,
                segment_lengths=[42],
                total_chars=42,
                send_status="sent",
                sent_segment_count=1,
            )
        return event

    def _build(self, event, status="executed", reply_text="回复内容"):
        """走真实组装路径 `_remember_turn_trace`，用收集型 store 截获落盘项。"""
        import asyncio

        captured = []

        class _Store:
            async def append(self, item):
                captured.append(item)

        planner = Planner.__new__(Planner)
        planner.turn_trace_store = _Store()
        planner.raw_trace_store = None
        planner.turn_trace_history = []
        with turn_telemetry_scope(event):
            asyncio.run(
                planner._remember_turn_trace(
                    "ff:GroupMessage:7000",
                    event,
                    status=status,
                    reply_text=reply_text,
                )
            )
        self.assertTrue(captured, "trace 必须被写入 store")
        return captured[-1]

    def test_executed_trace_carries_all_observability_fields(self):
        item = self._build(self._executed_event())

        missing = [field for field in self.REQUIRED_FIELDS if field not in item]
        self.assertEqual(missing, [], f"executed trace 缺少观测字段: {missing}")

    def test_ledger_contents_are_not_dropped(self):
        item = self._build(self._executed_event())

        self.assertEqual(len(item["llm_call_ledger"]), 1)
        self.assertEqual(item["llm_call_ledger"][0]["pool"], "judge")
        self.assertEqual(len(item["context_block_stats"]), 1)
        self.assertEqual(item["reply_stats"]["total_chars"], 42)

    def test_memory_funnel_skip_reason_survives(self):
        # TG-04 的核心：跳过路径也必须留下 funnel，让"合理跳过"与"仪表坏了"可区分
        item = self._build(self._executed_event())

        self.assertEqual(item["memory_funnel"]["status"], "skipped")
        self.assertEqual(item["memory_funnel"]["skip_reason"], "think_level_0")

    def test_legacy_topic_observation_carries_semantic_activity_contract(self):
        event = self._executed_event()
        event.set_extra("astrmai_topic_activity_valid", True)
        event.set_extra("astrmai_topic_activity_kind", "message")
        event.set_extra("astrmai_topic_activity_reason", "semantic_topic_text")
        event.set_extra("astrmai_topic_activity_source", "attention_gate")
        event.set_extra("astrmai_effective_user_response", True)
        event.set_extra("astrmai_topic_activity_state_transition_status", "persisted")
        event.set_extra("astrmai_topic_activity_state_before", {"unanswered_count": 1})
        event.set_extra("astrmai_topic_activity_state_after", {"unanswered_count": 0})

        item = self._build(event)
        observation = item["topic_observation"]

        self.assertTrue(observation["valid"])
        self.assertEqual(observation["kind"], "message")
        self.assertEqual(observation["reason"], "semantic_topic_text")
        self.assertEqual(observation["source"], "attention_gate")
        self.assertTrue(observation["effective_user_response"])
        self.assertEqual(observation["state_transition_status"], "persisted")
        self.assertEqual(observation["state_before"]["unanswered_count"], 1)
        self.assertEqual(observation["state_after"]["unanswered_count"], 0)

    def test_vision_observation_survives_trace_assembly(self):
        event = self._executed_event()
        with turn_telemetry_scope(event):
            record_vision_observation(
                event,
                {
                    "vision_path": "direct",
                    "vision_call_status": "success",
                    "image_count": 1,
                    "analyzed_count": 1,
                    "visual_memory_ids": ["vm_opaque"],
                    "description": "不应进入 trace 的原始图片描述",
                },
            )

        item = self._build(event)

        self.assertEqual(item["vision_observation"]["vision_path"], "direct")
        self.assertEqual(item["vision_observation"]["vision_call_status"], "success")
        self.assertEqual(item["image_count"], 1)
        self.assertEqual(item["visual_memory_ids"], ["vm_opaque"])
        self.assertNotIn("原始图片描述", str(item["vision_observation"]))

    def test_learning_context_observation_records_selection_and_model_visibility(self):
        event = self._executed_event()
        event.set_extra(
            "astrmai_learning_context_trace",
            {
                "mode": "fast",
                "budget_chars": 180,
                "rendered_chars": 42,
                "selected_jargon_chars": 30,
                "selected_expression_chars": 40,
                "trimmed_sections": ["expression"],
                "model_visible_jargon": True,
                "model_visible_expression": False,
            },
        )
        event.set_extra(
            "astrmai_jargon_route_trace",
            {
                "selected_ids": ["mem-jargon-1"],
                "matched_terms": ["不应进入 trace 的原始词"],
                "injected": True,
            },
        )
        event.set_extra(
            "astrmai_expression_pattern_trace",
            type("Trace", (), {"selected_ids": ["mem-expression-1"]})(),
        )

        item = self._build(event)
        observation = item["learning_context_observation"]

        self.assertEqual(observation["mode"], "fast")
        self.assertEqual(observation["jargon_selected_ids"], ["mem-jargon-1"])
        self.assertEqual(observation["expression_selected_ids"], ["mem-expression-1"])
        self.assertEqual(observation["jargon_matched_term_count"], 1)
        self.assertTrue(observation["model_visible_jargon"])
        self.assertFalse(observation["model_visible_expression"])
        self.assertNotIn("原始词", str(observation))

    def test_decision_observation_present_for_every_status(self):
        for status in ("executed", "skipped_wait", "skipped_ignore", "stale_drop"):
            with self.subTest(status=status):
                item = self._build(self._executed_event(), status=status, reply_text=None)
                self.assertIn("decision_observation", item)
                self.assertEqual(item["decision_observation"]["status"], status)

    def test_snapshot_refresh_does_not_duplicate_raw_trace_events(self):
        import asyncio

        snapshots = []
        raw_appends = []

        class _SnapshotStore:
            async def append(self, item):
                snapshots.append(item)

        class _RawStore:
            async def append_many(self, chat_id, items):
                raw_appends.append((chat_id, list(items)))

        planner = Planner.__new__(Planner)
        planner.turn_trace_store = _SnapshotStore()
        planner.raw_trace_store = _RawStore()
        planner.turn_trace_history = []
        event = self._executed_event()
        configure_turn_budget(
            event,
            total_budget_sec=120,
            main_reply_reserve_sec=30,
        )

        asyncio.run(
            planner.record_turn_trace(
                event.unified_msg_origin,
                event,
                status="skipped_ignore",
            )
        )
        first_elapsed_ms = snapshots[-1]["turn_total_elapsed_ms"]
        first_timing_coverage = snapshots[-1]["timing_coverage"]
        first_budget = snapshots[-1]["budget"]
        time.sleep(0.08)
        event.set_extra("astrmai_judge_validation_status", "success")
        asyncio.run(
            planner.record_turn_trace(
                event.unified_msg_origin,
                event,
                status="skipped_ignore",
                refresh_snapshot_only=True,
            )
        )

        self.assertEqual(len(snapshots), 2)
        self.assertEqual(len(raw_appends), 1)
        self.assertEqual(snapshots[-1]["judge_decision"]["validation_status"], "success")
        self.assertEqual(snapshots[-1]["turn_total_elapsed_ms"], first_elapsed_ms)
        self.assertEqual(snapshots[-1]["timing_coverage"], first_timing_coverage)
        self.assertEqual(snapshots[-1]["budget"], first_budget)
        self.assertGreater(first_budget["remaining_ms"], 0)


class MemoryFunnelWrapperContractTests(unittest.TestCase):
    """锚定 OPT-11 的外包裹实现：每条 early-return 都要留下 skipped funnel。"""

    def test_prompt_refiner_wraps_all_early_returns(self):
        from pathlib import Path

        source = Path("astrmai/conversation/planning/prompt_refiner.py").read_text(encoding="utf-8")
        self.assertIn("_decide_memory_injection_inner", source, "内部实现必须被外包裹")
        wrapper_start = source.find("async def _decide_memory_injection(")
        inner_start = source.find("async def _decide_memory_injection_inner(")
        self.assertGreater(wrapper_start, 0)
        self.assertGreater(inner_start, wrapper_start, "包裹函数必须在内部实现之前")
        wrapper_body = source[wrapper_start:inner_start]
        self.assertIn("astrmai_memory_funnel", wrapper_body, "包裹层必须补写 funnel")
        self.assertIn("skipped", wrapper_body)


if __name__ == "__main__":
    unittest.main()
