from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from astrmai.conversation.attention.context_compaction import ContextCompactionEngine
from astrmai.conversation.attention.group_dialogue_store import GroupDialogueStore
from astrmai.conversation.contracts.focus_context import FocusThreadContext
from astrmai.infrastructure.context_economy import ContextEconomyCenter
from astrmai.infrastructure.context_economy.models import WorkloadTrace
from astrmai.infrastructure.runtime.lane_manager import LaneManager


class GroupDialogueStoreAndCompactionTests(unittest.TestCase):
    def test_compaction_engine_exposes_split_helper_components(self):
        engine = ContextCompactionEngine(dialogue_store=None)
        self.assertTrue(hasattr(engine, "safety_analyzer"))
        self.assertTrue(hasattr(engine, "window_selector"))
        self.assertTrue(hasattr(engine, "compaction_executor"))

    def test_warm_transcript_and_cold_summary_lifecycle(self):
        async def run():
            store = GroupDialogueStore(hot_zone_ttl_seconds=30.0, warm_zone_ttl_seconds=300.0, warm_zone_max_tokens=1200)
            await store.append_segment(
                "chat-1",
                event_id="e1",
                speaker_id="u1",
                speaker_name="Alice",
                content="Hello there",
                role="user",
                message_kind="text",
            )
            await store.append_segment(
                "chat-1",
                event_id="e2",
                speaker_id="b1",
                speaker_name="Bot",
                content="Hi",
                role="assistant",
                message_kind="text",
                is_bot=True,
            )
            warm = await store.get_warm_transcript("chat-1")
            self.assertIn("Alice: Hello there", warm)
            self.assertIn("Bot: Hi", warm)
            self.assertEqual((await store.snapshot_counts("chat-1"))["tokens"], 6)
            await store.set_cold_summary("chat-1", "summary line")
            self.assertEqual(await store.get_cold_summary("chat-1"), "summary line")

        asyncio.run(run())

    def test_warm_context_bundle_keeps_structure_and_excludes_cold_summary(self):
        async def run():
            store = GroupDialogueStore()
            await store.set_cold_summary("chat-1", "older summary")
            await store.append_segment(
                "chat-1",
                event_id="e1",
                speaker_id="u1",
                speaker_name="Alice",
                content="look at this image",
                role="user",
                message_kind="image",
                reply_target_sender_id="u2",
                reply_target_sender_name="Bob",
                is_at_bot=True,
                is_reply_to_bot=True,
                has_direct_vision=True,
                is_image_only=True,
            )
            bundle = await store.get_warm_context_bundle("chat-1")
            warm = "\n".join(part for part in (bundle.summary_text, bundle.quote_text) if part)
            self.assertNotIn("older summary", warm)
            self.assertIn("Alice", warm)
            self.assertIn("Bob", warm)
            self.assertTrue(bundle.topic_preview)

        asyncio.run(run())

    def test_warm_summary_uses_topic_units_instead_of_simple_counts(self):
        async def run():
            store = GroupDialogueStore()
            await store.append_segment(
                "chat-1",
                event_id="u1",
                speaker_id="u1",
                speaker_name="Alice",
                content="Is this cache-hit plan still not fully closed out?",
                role="user",
                is_at_bot=True,
            )
            await store.append_segment(
                "chat-1",
                event_id="a1",
                speaker_id="b1",
                speaker_name="Bot",
                content="I just finished sorting out the trigger conditions on the main path.",
                role="assistant",
                is_bot=True,
            )
            await store.append_segment(
                "chat-1",
                event_id="u2",
                speaker_id="u1",
                speaker_name="Alice",
                content="So are we still missing the focus tail overlap part?",
                role="user",
                is_reply_to_bot=True,
            )
            bundle = await store.get_warm_context_bundle("chat-1")
            self.assertTrue(bundle.summary_text)
            self.assertNotIn("Alice:", bundle.summary_text)
            self.assertNotIn("Bot:", bundle.summary_text)
            self.assertIn("topic:", bundle.topic_preview)
            self.assertIn("event:", bundle.topic_preview)

        asyncio.run(run())

    def test_warm_summary_keeps_bot_directed_mainline_over_later_smalltalk(self):
        async def run():
            store = GroupDialogueStore()
            await store.append_segment(
                "chat-1",
                event_id="a1",
                speaker_id="b1",
                speaker_name="Bot",
                content="I can help debug the compaction logic.",
                role="assistant",
                is_bot=True,
            )
            await store.append_segment(
                "chat-1",
                event_id="u1",
                speaker_id="u1",
                speaker_name="Alice",
                content="So should we still keep recent fallback?",
                role="user",
                is_reply_to_bot=True,
            )
            for idx in range(6):
                await store.append_segment(
                    "chat-1",
                    event_id=f"s{idx}",
                    speaker_id="u2",
                    speaker_name="Bob",
                    content=f"background smalltalk {idx}",
                    role="user",
                )
            bundle = await store.get_warm_context_bundle("chat-1")
            self.assertIn("still keep", bundle.summary_text)
            self.assertNotIn("background smalltalk 5", bundle.summary_text)
            self.assertIn("recent fallback", bundle.quote_text)

        asyncio.run(run())

    def test_warm_quotes_keep_latest_direct_question_when_visual_context_is_present(self):
        async def run():
            store = GroupDialogueStore()
            await store.append_segment(
                "chat-1",
                event_id="i1",
                speaker_id="u1",
                speaker_name="Alice",
                content="Here is the screenshot.",
                role="user",
                message_kind="image",
                has_direct_vision=True,
                is_image_only=True,
            )
            await store.append_segment(
                "chat-1",
                event_id="a1",
                speaker_id="b1",
                speaker_name="AstrMai",
                content="I can keep following the compaction mainline.",
                role="assistant",
                is_bot=True,
            )
            await store.append_segment(
                "chat-1",
                event_id="u2",
                speaker_id="u1",
                speaker_name="Alice",
                content="Can you keep the compaction mainline while I send the screenshot?",
                role="user",
                is_at_bot=True,
                has_direct_vision=True,
                message_kind="mixed",
            )
            bundle = await store.get_warm_context_bundle("chat-1")
            self.assertIn("keep the compaction mainline", bundle.quote_text)
            self.assertIn("screenshot", bundle.quote_text.lower())

        asyncio.run(run())

    def test_warm_quotes_do_not_promote_plain_group_question_over_bot_directed_question(self):
        async def run():
            store = GroupDialogueStore()
            await store.append_segment(
                "chat-1",
                event_id="a1",
                speaker_id="b1",
                speaker_name="AstrMai",
                content="We should keep following the compaction mainline.",
                role="assistant",
                is_bot=True,
            )
            await store.append_segment(
                "chat-1",
                event_id="u1",
                speaker_id="u1",
                speaker_name="Alice",
                content="Can you keep the compaction mainline while this tail is still live?",
                role="user",
                is_at_bot=True,
            )
            await store.append_segment(
                "chat-1",
                event_id="u2",
                speaker_id="u2",
                speaker_name="Bob",
                content="Should we order lunch first?",
                role="user",
            )
            bundle = await store.get_warm_context_bundle("chat-1", max_tokens=80)
            self.assertIn("compaction mainline", bundle.quote_text)
            self.assertNotIn("order lunch", bundle.quote_text.lower())

        asyncio.run(run())

    def test_compaction_not_ready_before_80_messages(self):
        async def run():
            store = GroupDialogueStore()
            for idx in range(79):
                await store.append_segment("chat-1", event_id=f"s{idx}", speaker_id="u1", speaker_name="Alice", content=f"m{idx}")
            engine = ContextCompactionEngine(store, compaction_keep_recent_segments=1, compaction_summary_max_tokens=200)
            result = await engine.maybe_compact("chat-1")
            self.assertFalse(result.triggered)
            self.assertEqual(result.state, "NOT_READY")
            self.assertEqual(result.message_count_since_last_compaction, 79)

        asyncio.run(run())

    def test_compaction_waits_for_next_node_at_80_when_score_not_enough(self):
        async def run():
            store = GroupDialogueStore()
            for idx in range(80):
                await store.append_segment("chat-1", event_id=f"m{idx}", speaker_id="u1", speaker_name="Alice", content=f"m{idx}")
            engine = ContextCompactionEngine(store, compaction_keep_recent_segments=1)
            result = await engine.maybe_compact("chat-1")
            self.assertFalse(result.triggered)
            self.assertEqual(result.state, "WAIT_NEXT_NODE")
            self.assertEqual(result.next_eval_at_count, 90)

        asyncio.run(run())

    def test_pending_task_still_queues_crossed_eval_nodes(self):
        async def run():
            store = GroupDialogueStore()
            for idx in range(79):
                await store.append_segment("chat-1", event_id=f"m{idx}", speaker_id="u1", speaker_name="Alice", content=f"m{idx}")
            engine = ContextCompactionEngine(store, compaction_keep_recent_segments=1)
            pending = asyncio.get_running_loop().create_future()
            engine._pending_tasks["chat-1"] = pending
            queued = await engine.schedule_compaction_evaluation("chat-1", message_source="user")
            self.assertEqual(queued.skipped_reason, "evaluation_already_scheduled")
            self.assertEqual(engine._state_for_chat("chat-1")["pending_eval_nodes"], [80])
            pending.cancel()
            engine._pending_tasks.pop("chat-1", None)
            result = await engine.maybe_compact("chat-1")
            self.assertEqual(result.evaluation_count, 80)
            self.assertEqual(result.current_message_count, 80)
            self.assertEqual(result.state, "WAIT_NEXT_NODE")

        asyncio.run(run())

    def test_multiple_crossed_eval_nodes_are_preserved_in_queue(self):
        async def run():
            store = GroupDialogueStore()
            for idx in range(79):
                await store.append_segment("chat-1", event_id=f"m{idx}", speaker_id="u1", speaker_name="Alice", content=f"m{idx}")
            engine = ContextCompactionEngine(store, compaction_keep_recent_segments=1)
            pending = asyncio.get_running_loop().create_future()
            engine._pending_tasks["chat-1"] = pending
            for idx in range(21):
                queued = await engine.schedule_compaction_evaluation("chat-1", message_source="user")
                self.assertEqual(queued.skipped_reason, "evaluation_already_scheduled")
                await store.append_segment("chat-1", event_id=f"x{idx}", speaker_id="u1", speaker_name="Alice", content=f"x{idx}")
            self.assertEqual(engine._state_for_chat("chat-1")["pending_eval_nodes"], [80, 90, 100])
            pending.cancel()
            engine._pending_tasks.pop("chat-1", None)

        asyncio.run(run())

    def test_compaction_forced_at_120_when_safe(self):
        async def run():
            store = GroupDialogueStore()
            for idx in range(120):
                await store.append_segment("chat-1", event_id=f"l{idx}", speaker_id="u1", speaker_name="Alice", content=f"normal message {idx}")
            engine = ContextCompactionEngine(store, compaction_keep_recent_segments=1)
            result = await engine.maybe_compact("chat-1")
            self.assertTrue(result.triggered)
            self.assertEqual(result.state, "COOLDOWN")
            self.assertEqual(result.message_count_since_last_compaction, 0)

        asyncio.run(run())

    def test_compaction_enters_forced_pending_when_120_but_chain_active(self):
        async def run():
            store = GroupDialogueStore()
            for idx in range(117):
                await store.append_segment("chat-1", event_id=f"u{idx}", speaker_id="u1", speaker_name="Alice", content=f"m{idx}")
            await store.append_segment("chat-1", event_id="a1", speaker_id="b1", speaker_name="Bot", content="reply", role="assistant", is_bot=True)
            await store.append_segment("chat-1", event_id="u118", speaker_id="u1", speaker_name="Alice", content="?", is_at_bot=True)
            await store.append_segment("chat-1", event_id="u119", speaker_id="u1", speaker_name="Alice", content="??", is_reply_to_bot=True)
            engine = ContextCompactionEngine(store, compaction_keep_recent_segments=1)
            result = await engine.maybe_compact("chat-1")
            self.assertFalse(result.triggered)
            self.assertEqual(result.state, "FORCED_PENDING")
            self.assertEqual(result.reason, "awaiting_followup_chain")

        asyncio.run(run())

    def test_compaction_skips_when_focus_thread_overlaps_old_zone_tail(self):
        async def run():
            store = GroupDialogueStore()
            for idx in range(120):
                await store.append_segment("chat-1", event_id=f"e{idx}", speaker_id="u1", speaker_name="Alice", content=f"m{idx}")
            focus_event = SimpleNamespace(
                message_obj=SimpleNamespace(message_id="e118"),
                timestamp=118.0,
                message_str="m118",
                get_sender_id=lambda: "u1",
            )
            focus_context = FocusThreadContext(focus_event=focus_event, core_events=[focus_event])
            engine = ContextCompactionEngine(store, compaction_keep_recent_segments=1)
            result = await engine.maybe_compact("chat-1", focus_context=focus_context)
            self.assertFalse(result.triggered)
            self.assertEqual(result.state, "FORCED_PENDING")
            self.assertEqual(result.reason, "focus_tail_overlap")
            self.assertTrue(result.focus_tail_overlap)

        asyncio.run(run())

    def test_compaction_prefers_provider_summary_when_available(self):
        class FakeContext:
            async def get_current_chat_provider_id(self, _chat_id):
                return "chat-provider"

            async def llm_generate(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(completion_text="[topics]\n- around cache hit strategy\n[decisions]\n- keep dual fallback\n[open_items]\n- still need focus tail overlap")

        async def run():
            fake_context = FakeContext()
            store = GroupDialogueStore()
            for idx in range(120):
                await store.append_segment("chat-1", event_id=f"e{idx}", speaker_id="u1", speaker_name="Alice", content=f"m{idx}")
            engine = ContextCompactionEngine(
                store,
                compaction_keep_recent_segments=1,
                compaction_summary_max_tokens=200,
                gateway=SimpleNamespace(context=fake_context),
            )
            result = await engine.maybe_compact("chat-1")
            self.assertTrue(result.triggered)
            self.assertIn("[topics]", result.summary)
            self.assertEqual(fake_context.kwargs["chat_provider_id"], "chat-provider")

        asyncio.run(run())

    def test_compaction_falls_back_to_rule_summary_when_provider_fails(self):
        class FakeContext:
            async def get_current_chat_provider_id(self, _chat_id):
                return "chat-provider"

            async def llm_generate(self, **kwargs):
                raise RuntimeError("provider down")

        async def run():
            store = GroupDialogueStore()
            for idx in range(120):
                await store.append_segment("chat-1", event_id=f"e{idx}", speaker_id="u1", speaker_name="Alice", content=f"m{idx}")
            engine = ContextCompactionEngine(
                store,
                compaction_keep_recent_segments=1,
                compaction_summary_max_tokens=200,
                gateway=SimpleNamespace(context=FakeContext()),
            )
            result = await engine.maybe_compact("chat-1")
            self.assertTrue(result.triggered)
            self.assertIn("[topics]", result.summary)

        asyncio.run(run())

    def test_compaction_provider_kwargs_use_dedicated_lane_and_reuse_session(self):
        async def run():
            store = GroupDialogueStore()
            economy = ContextEconomyCenter()
            engine = ContextCompactionEngine(
                store,
                gateway=SimpleNamespace(
                    context=SimpleNamespace(),
                    context_economy=economy,
                    lane_manager=LaneManager(SimpleNamespace()),
                ),
            )
            kwargs1, trace1 = engine._compaction_provider_kwargs(
                "chat-1",
                "dify-agent",
                "stable-shell",
                "dynamic-payload",
                "compaction_summary_v2",
                "v2",
                "section_summary",
                "stable-shell",
                "dynamic-payload",
            )
            kwargs2, trace2 = engine._compaction_provider_kwargs(
                "chat-1",
                "dify-agent",
                "stable-shell",
                "dynamic-payload",
                "compaction_summary_v2",
                "v2",
                "section_summary",
                "stable-shell",
                "dynamic-payload",
            )

            self.assertIn("session_id", kwargs1)
            self.assertEqual(kwargs1["session_id"], kwargs2["session_id"])
            self.assertIn("@@astrmai:bg:compaction:", str(kwargs1["session_id"]))
            self.assertTrue(str(kwargs1["session_id"]).endswith("compaction_summary_v2:v2:section_summary"))
            self.assertEqual(trace1["lane_scope_id"], "chat-1")
            self.assertEqual(trace1["template_id"], "compaction_summary_v2")
            self.assertEqual(trace1["template_version"], "v2")

            economy.record_trace(WorkloadTrace(**trace1))
            economy.record_trace(WorkloadTrace(**trace2))
            snapshot = economy.snapshot_metrics()
            template_stats = snapshot["_templates"]["compaction_summary_v2@v2"]
            self.assertEqual(template_stats["provider_session_usage_rate"], 1.0)
            self.assertEqual(template_stats["provider_session_reuse_rate"], 0.5)

        asyncio.run(run())

    def test_compaction_persists_structured_cold_summary(self):
        async def run():
            store = GroupDialogueStore()
            await store.append_segment("chat-1", event_id="e1", speaker_id="u1", speaker_name="Alice", content="We should push this plan first", role="user")
            await store.append_segment("chat-1", event_id="e2", speaker_id="u2", speaker_name="Bob", content="Do we still need focus tail overlap?", role="user")
            for idx in range(118):
                await store.append_segment("chat-1", event_id=f"e{idx+3}", speaker_id="u1", speaker_name="Alice", content=f"m{idx}")
            engine = ContextCompactionEngine(store, compaction_keep_recent_segments=1)
            result = await engine.maybe_compact("chat-1")
            self.assertTrue(result.triggered)
            structure = await store.get_cold_summary_structure("chat-1")
            self.assertIsNotNone(structure)
            counts = structure.section_counts()
            self.assertGreaterEqual(counts["topics"], 1)
            self.assertGreaterEqual(counts["open_items"], 1)

        asyncio.run(run())

    def test_cold_merge_closes_open_item_when_decision_resolves_it(self):
        async def run():
            store = GroupDialogueStore()
            engine = ContextCompactionEngine(store)
            current = engine._structure_from_summary_text(
                "[open_items]\n- Do we still need focus tail overlap?"
            )
            addition = engine._structure_from_summary_text(
                "[decisions]\n- We no longer need focus tail overlap."
            )
            merged = engine._merge_cold_structure(current, addition)
            self.assertEqual([unit.text for unit in merged.open_items], [])
            self.assertEqual(len(merged.decisions), 1)

        asyncio.run(run())

    def test_compaction_save_failure_enters_cooldown_without_losing_segments(self):
        class BrokenStore(GroupDialogueStore):
            async def set_cold_summary(self, chat_id: str, summary: str) -> None:
                raise RuntimeError("save failed")

        async def run():
            store = BrokenStore()
            for idx in range(120):
                await store.append_segment("chat-1", event_id=f"e{idx}", speaker_id="u1", speaker_name="Alice", content=f"m{idx}")
            engine = ContextCompactionEngine(store, compaction_keep_recent_segments=1, compaction_summary_max_tokens=200)
            before = await store.snapshot_counts("chat-1")
            first = await engine.maybe_compact("chat-1")
            after_first = await store.snapshot_counts("chat-1")
            second = await engine.maybe_compact("chat-1")
            self.assertEqual(first.skipped_reason, "summary_save_failed")
            self.assertEqual(after_first["segments"], before["segments"])
            self.assertEqual(second.skipped_reason, "cooldown")

        asyncio.run(run())

    def test_compaction_summary_empty_keeps_segments(self):
        class EmptySummaryEngine(ContextCompactionEngine):
            async def _build_summary_with_provider_v2(self, chat_id: str, drained_segments) -> str:
                return ""

            def _build_summary_v2(self, drained_segments) -> str:
                return ""

        async def run():
            store = GroupDialogueStore()
            for idx in range(120):
                await store.append_segment("chat-1", event_id=f"e{idx}", speaker_id="u1", speaker_name="Alice", content=f"m{idx}")
            engine = EmptySummaryEngine(store, compaction_keep_recent_segments=1, compaction_summary_max_tokens=200)
            before = await store.snapshot_counts("chat-1")
            result = await engine.maybe_compact("chat-1")
            after = await store.snapshot_counts("chat-1")
            self.assertEqual(result.skipped_reason, "summary_empty")
            self.assertEqual(after["segments"], before["segments"])

        asyncio.run(run())

    def test_warm_transcript_prefers_recent_messages_under_token_budget(self):
        async def run():
            store = GroupDialogueStore(warm_zone_max_tokens=14)
            await store.append_segment("chat-1", speaker_id="u1", speaker_name="Alice", content="1111")
            await store.append_segment("chat-1", speaker_id="u1", speaker_name="Alice", content="2222")
            await store.append_segment("chat-1", speaker_id="u1", speaker_name="Alice", content="3333")
            bundle = await store.get_warm_context_bundle("chat-1", max_tokens=14)
            self.assertNotIn("1111", bundle.quote_text)
            self.assertIn("2222", bundle.quote_text)
            self.assertIn("3333", bundle.quote_text)

        asyncio.run(run())

    def test_forced_pending_compacts_after_20_more_messages(self):
        async def run():
            store = GroupDialogueStore()
            for idx in range(117):
                await store.append_segment("chat-1", event_id=f"e{idx}", speaker_id="u1", speaker_name="Alice", content=f"m{idx}")
            await store.append_segment("chat-1", event_id="a1", speaker_id="b1", speaker_name="Bot", content="reply", role="assistant", is_bot=True)
            await store.append_segment("chat-1", event_id="u118", speaker_id="u1", speaker_name="Alice", content="?", is_at_bot=True)
            await store.append_segment("chat-1", event_id="u119", speaker_id="u1", speaker_name="Alice", content="??", is_reply_to_bot=True)
            engine = ContextCompactionEngine(store, compaction_keep_recent_segments=1)
            first = await engine.maybe_compact("chat-1")
            self.assertFalse(first.triggered)
            self.assertEqual(first.state, "FORCED_PENDING")
            for idx in range(20):
                await store.append_segment("chat-1", event_id=f"n{idx}", speaker_id="u1", speaker_name="Alice", content=f"followup {idx}", is_reply_to_bot=True)
            focus_event = SimpleNamespace(
                message_obj=SimpleNamespace(message_id="n18"),
                timestamp=138.0,
                message_str="followup 18",
                get_sender_id=lambda: "u1",
            )
            focus_context = FocusThreadContext(focus_event=focus_event, core_events=[focus_event])
            second = await engine.maybe_compact("chat-1", focus_context=focus_context)
            self.assertFalse(second.triggered)
            self.assertEqual(second.state, "FORCED_PENDING")
            self.assertTrue(second.force_execute_on_next_safe_hook)
            self.assertEqual(second.reason, "forced_waiting_for_safe_hook")
            for idx in range(3):
                await store.append_segment("chat-1", event_id=f"b{idx}", speaker_id="u2", speaker_name="Bob", content=f"background {idx}")
            third = await engine.maybe_compact("chat-1")
            self.assertTrue(third.triggered)
            self.assertEqual(third.state, "COOLDOWN")

        asyncio.run(run())

    def test_deferred_state_can_compact_when_safe_window_reopens_before_next_node(self):
        async def run():
            store = GroupDialogueStore()
            for idx in range(101):
                await store.append_segment("chat-1", event_id=f"e{idx}", speaker_id="u1", speaker_name="Alice", content=f"normal message {idx}")
            engine = ContextCompactionEngine(store, compaction_keep_recent_segments=1)
            state = engine._state_for_chat("chat-1")
            state["current_state"] = "DEFERRED_FOR_STABILITY"
            state["message_count_since_last_compaction"] = 101
            result = await engine.maybe_compact("chat-1")
            self.assertTrue(result.triggered)
            self.assertEqual(result.state, "COOLDOWN")
            self.assertGreaterEqual(result.last_safe_window_seen_at_count, 101)

        asyncio.run(run())

    def test_trace_status_exposes_signal_buckets_and_recovery_rounds(self):
        async def run():
            store = GroupDialogueStore()
            for idx in range(100):
                await store.append_segment("chat-1", event_id=f"e{idx}", speaker_id="u1", speaker_name="Alice", content=f"normal message {idx}")
            engine = ContextCompactionEngine(store, compaction_keep_recent_segments=1)
            result = await engine.maybe_compact("chat-1")
            self.assertEqual(result.state, "WAIT_NEXT_NODE")
            trace = await engine.get_trace_status("chat-1")
            self.assertIn("closure_signals", trace)
            self.assertIn("tail_activity_signals", trace)
            self.assertIn("topic_density_signals", trace)
            self.assertIn("stability_signals", trace)
            self.assertIn("benefit_signals", trace)
            self.assertIn("evaluation_count", trace)
            self.assertIn("current_message_count", trace)
            self.assertIn("pending_eval_nodes", trace)
            self.assertIn("force_execute_on_next_safe_hook", trace)
            self.assertEqual(trace["post_compaction_recovery_rounds"], 0)

        asyncio.run(run())

    def test_post_compaction_recovery_rounds_decrease_on_user_messages(self):
        async def run():
            store = GroupDialogueStore()
            for idx in range(120):
                await store.append_segment("chat-1", event_id=f"e{idx}", speaker_id="u1", speaker_name="Alice", content=f"normal message {idx}")
            engine = ContextCompactionEngine(store, compaction_keep_recent_segments=1)
            result = await engine.maybe_compact("chat-1")
            self.assertTrue(result.triggered)
            self.assertEqual(result.post_compaction_recovery_rounds, 2)
            await engine.schedule_compaction_evaluation("chat-1", message_source="user")
            trace = await engine.get_trace_status("chat-1")
            self.assertEqual(trace["post_compaction_recovery_rounds"], 1)

        asyncio.run(run())

    def test_trace_status_does_not_mutate_compaction_state(self):
        async def run():
            store = GroupDialogueStore()
            for idx in range(120):
                await store.append_segment("chat-1", event_id=f"e{idx}", speaker_id="u1", speaker_name="Alice", content=f"m{idx}")
            engine = ContextCompactionEngine(store, compaction_keep_recent_segments=1)
            first = await engine.maybe_compact("chat-1")
            self.assertTrue(first.triggered)
            self.assertEqual(engine._state_for_chat("chat-1")["last_state"], "COOLDOWN")
            trace_status = await engine.get_trace_status("chat-1")
            self.assertEqual(trace_status["state"], "COOLDOWN")
            self.assertEqual(engine._state_for_chat("chat-1")["last_state"], "COOLDOWN")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
