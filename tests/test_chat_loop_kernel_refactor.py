from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _FakeEvent:
    def __init__(self, *, umo: str, sender_id: str, sender_name: str, group_id: str = "", self_id: str = "bot-1", text: str = ""):
        self.unified_msg_origin = umo
        self.message_str = text
        self.message_obj = SimpleNamespace(self_id=self_id, message=[])
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._group_id = group_id
        self._self_id = self_id
        self._extra = {}

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_group_id(self):
        return self._group_id

    def get_self_id(self):
        return self._self_id

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def plain_result(self, text):
        return {"type": "plain", "text": text}


class ChatLoopKernelRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        for name in (
            "astrmai.conversation.loop.chat_loop_kernel",
            "astrmai.presentation.events.message_entry",
        ):
            sys.modules.pop(name, None)
        self.kernel_mod = importlib.import_module("astrmai.conversation.loop.chat_loop_kernel")
        self.message_entry_mod = importlib.import_module("astrmai.presentation.events.message_entry")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_message_and_heartbeat_ticks_create_and_reuse_state(self):
        calls = []

        async def _message_handler(event):
            calls.append(("message", event.unified_msg_origin))
            return "ENGAGED"

        async def _heartbeat_handler(chat_id, snapshot, decision):
            calls.append(("heartbeat", chat_id, decision.action))
            return {"chat_id": chat_id, "action": decision.action}

        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        kernel = self.kernel_mod.ChatLoopKernel(
            runtime_coordinator=_Coordinator(),
            message_handler=_message_handler,
            heartbeat_handler=_heartbeat_handler,
        )
        event = _FakeEvent(
            umo="default:GroupMessage:group-1",
            sender_id="user-1",
            sender_name="Alice",
            group_id="group-1",
            text="hello",
        )

        async def _run():
            first = await kernel.tick(chat_id="default:GroupMessage:group-1", trigger="message", event=event)
            second = await kernel.tick(chat_id="default:GroupMessage:group-1", trigger="heartbeat")
            status = await kernel.describe_status()
            return first, second, status

        first, second, status = asyncio.run(_run())

        self.assertEqual(first.decision.action, "INGRESS_MESSAGE")
        self.assertEqual(first.dispatch_result, "ENGAGED")
        self.assertEqual(second.decision.action, "NOOP")
        self.assertEqual(second.dispatch_result["action"], "NOOP")
        self.assertEqual(first.state.chat_id, second.state.chat_id)
        self.assertEqual(status["tracked_chats"], 1)
        self.assertEqual(status["decision_mode"], "single_primary_action")
        self.assertTrue(status["private_wait_visible_in_heartbeat"])
        self.assertTrue(status["heartflow_preview_readonly"])
        self.assertEqual(status["dream_scope"], "global_throttle")
        self.assertEqual(calls[0], ("message", "default:GroupMessage:group-1"))
        self.assertEqual(calls[1], ("heartbeat", "default:GroupMessage:group-1", "NOOP"))

    def test_busy_heartbeat_skips_dispatch(self):
        calls = []

        async def _heartbeat_handler(chat_id, snapshot, decision):
            calls.append(chat_id)
            return {}

        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 2, "wait_targets": ["u1"]}

        kernel = self.kernel_mod.ChatLoopKernel(
            runtime_coordinator=_Coordinator(),
            heartbeat_handler=_heartbeat_handler,
        )

        result = asyncio.run(kernel.tick(chat_id="chat-1", trigger="heartbeat"))

        self.assertEqual(result.decision.action, "SKIP_BUSY")
        self.assertFalse(result.decision.should_dispatch)
        self.assertEqual(result.decision.next_tick_delay, 5.0)
        self.assertEqual(result.decision.metadata["scheduler_bucket"], "fast_recheck")
        self.assertEqual(calls, [])

    def test_message_resume_wait_produces_resume_action(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        event = _FakeEvent(
            umo="default:GroupMessage:group-1",
            sender_id="user-1",
            sender_name="Alice",
            group_id="group-1",
            text="resume",
        )
        event.set_extra("astrmai_group_wait_resume", True)

        result = asyncio.run(kernel.tick(chat_id="default:GroupMessage:group-1", trigger="message", event=event))

        self.assertEqual(result.decision.action, "RESUME_WAIT")
        self.assertEqual(result.decision.reason, "wait_resumed")

    def test_group_wait_arm_forces_wait_during_heartbeat(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())

        async def _run():
            await kernel.arm_group_wait(
                "default:GroupMessage:group-1",
                {
                    "target_user_id": "user-1",
                    "target_name": "Alice",
                    "remaining_seconds": 20.0,
                    "remaining_messages": 3,
                    "reason": "bot_at_target",
                },
            )
            return await kernel.tick(chat_id="default:GroupMessage:group-1", trigger="heartbeat")

        result = asyncio.run(_run())

        self.assertEqual(result.decision.action, "WAIT")
        self.assertEqual(result.decision.metadata["wait_scope"], "group")
        self.assertGreaterEqual(result.decision.next_tick_delay, 1.0)
        self.assertLessEqual(result.decision.next_tick_delay, 20.0)
        self.assertEqual(result.decision.metadata["scheduler_bucket"], "wait_recheck")

    def test_message_interrupts_non_matching_wait_in_same_chat(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        calls = []

        async def _message_handler(event):
            calls.append(event.get_extra("astrmai_loop_action"))
            return "BUFFERED"

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator(), message_handler=_message_handler)
        event = _FakeEvent(
            umo="default:GroupMessage:group-1",
            sender_id="user-2",
            sender_name="Bob",
            group_id="group-1",
            text="new topic",
        )

        async def _run():
            await kernel.arm_group_wait(
                "default:GroupMessage:group-1",
                {
                    "target_user_id": "user-1",
                    "target_name": "Alice",
                    "remaining_seconds": 20.0,
                    "remaining_messages": 3,
                    "reason": "bot_at_target",
                },
            )
            return await kernel.tick(chat_id="default:GroupMessage:group-1", trigger="message", event=event)

        result = asyncio.run(_run())

        self.assertEqual(result.decision.action, "INTERRUPT_WAIT")
        self.assertEqual(calls, ["INTERRUPT_WAIT"])
        self.assertEqual(result.state.wait_status, "interrupted")

    def test_quiet_hours_blocks_wakeup_and_heartflow(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        class _WakeupService:
            config = SimpleNamespace(
                life=SimpleNamespace(proactive_quiet_hours=["00:00-23:59"]),
                reply=SimpleNamespace(base_frequency=0.7),
            )

            async def build_signal(self, chat_id, now=None):
                return {"eligible": True, "reason": "silence_threshold_reached", "wakeup_cooldown": 60.0}

        class _HeartflowManager:
            async def preview_chat(self, chat_id, snapshot=None, now=None):
                return {"eligible": True, "action_type": "prepare_reply", "blocked_reason": ""}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        kernel.bind_signal_sources(wakeup_service=_WakeupService(), heartflow_manager=_HeartflowManager())

        result = asyncio.run(kernel.tick(chat_id="chat-1", trigger="heartbeat"))

        self.assertEqual(result.decision.action, "NOOP")
        self.assertEqual(result.decision.reason, "quiet_hours")
        self.assertTrue(result.decision.metadata["quiet_active"])
        self.assertEqual(result.decision.metadata["quiet_blocks"], ["PROACTIVE_WAKEUP", "HEARTFLOW_EVALUATE"])
        self.assertEqual(result.decision.next_tick_delay, 300.0)
        self.assertEqual(result.decision.metadata["scheduler_bucket"], "idle_backoff")

    def test_per_action_cooldown_blocks_only_same_action(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        class _WakeupService:
            async def build_signal(self, chat_id, now=None):
                return {"eligible": True, "reason": "silence_threshold_reached", "wakeup_cooldown": 60.0}

        class _HeartflowManager:
            async def preview_chat(self, chat_id, snapshot=None, now=None):
                return {"eligible": True, "action_type": "prepare_reply", "blocked_reason": ""}

        async def _bridge(chat_id, snapshot, decision):
            return {"bridge": decision.action.lower()}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        kernel.bind_signal_sources(wakeup_service=_WakeupService(), heartflow_manager=_HeartflowManager())
        kernel.bind_dispatch_bridge("HEARTFLOW_EVALUATE", _bridge)

        async def _run():
            await kernel.set_cooldown("chat-1", "wakeup", 9999999999.0, reason="test")
            return await kernel.tick(chat_id="chat-1", trigger="heartbeat")

        result = asyncio.run(_run())

        self.assertEqual(result.decision.action, "HEARTFLOW_EVALUATE")

    def test_wakeup_candidate_in_cooldown_is_blocked_by_kernel(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        class _WakeupService:
            async def build_signal(self, chat_id, now=None):
                return {
                    "eligible": False,
                    "candidate_present": True,
                    "reason": "cooldown",
                    "next_wakeup_timestamp": 9999999999.0,
                    "wakeup_cooldown": 60.0,
                }

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        kernel.bind_signal_sources(wakeup_service=_WakeupService())

        result = asyncio.run(kernel.tick(chat_id="chat-1", trigger="heartbeat"))

        self.assertEqual(result.decision.action, "NOOP")
        self.assertEqual(result.decision.reason, "cooldown_blocked")
        self.assertEqual(result.decision.metadata["cooldown_blocks"], ["wakeup"])
        self.assertTrue(result.decision.metadata["wakeup_candidate_present"])
        self.assertEqual(result.decision.next_tick_delay, 300.0)
        self.assertGreater(result.decision.metadata["earliest_blocking_cooldown"], 0.0)

    def test_external_tick_reuses_state_and_records_external_source(self):
        calls = []

        async def _message_handler(event):
            calls.append(
                (
                    "external",
                    event.unified_msg_origin,
                    event.get_extra("astrmai_loop_source"),
                    event.get_extra("astrmai_loop_action"),
                )
            )
            return "BUFFERED"

        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        kernel = self.kernel_mod.ChatLoopKernel(
            runtime_coordinator=_Coordinator(),
            message_handler=_message_handler,
        )
        event = _FakeEvent(
            umo="default:GroupMessage:group-1",
            sender_id="bot-1",
            sender_name="Mai",
            group_id="group-1",
            text="synthetic hello",
        )
        event.set_extra("astrmai_loop_source", "proactive_dispatcher")

        async def _run():
            first = await kernel.tick(chat_id="default:GroupMessage:group-1", trigger="message", event=event)
            second = await kernel.tick(chat_id="default:GroupMessage:group-1", trigger="external", event=event)
            status = await kernel.describe_status()
            return first, second, status

        first, second, status = asyncio.run(_run())

        self.assertEqual(first.state.chat_id, second.state.chat_id)
        self.assertEqual(second.state.last_trigger, "external")
        self.assertEqual(second.state.last_decision, "INGRESS_EXTERNAL")
        self.assertEqual(second.decision.action, "INGRESS_EXTERNAL")
        self.assertEqual(second.decision.metadata["source"], "proactive_dispatcher")
        self.assertEqual(status["tracked_chats"], 1)
        self.assertEqual(calls[-1], ("external", "default:GroupMessage:group-1", "proactive_dispatcher", "INGRESS_EXTERNAL"))

    def test_wait_targets_force_wait_during_heartbeat(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": ["user-1"]}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        result = asyncio.run(kernel.tick(chat_id="chat-1", trigger="heartbeat"))
        self.assertEqual(result.decision.action, "WAIT")
        self.assertEqual(result.decision.reason, "wait_state:wait_targets")
        self.assertEqual(result.decision.metadata["wait_scope"], "runtime_wait_targets")

    def test_private_wait_forces_wait_during_heartbeat(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        class _PrivateChatManager:
            def get_session_info_by_chat_id(self, chat_id):
                return {"user_id": "user-1", "is_bot_waiting": True, "turn_count": 2}

        bridge_calls = []

        async def _bridge(chat_id, snapshot, decision):
            bridge_calls.append((chat_id, decision.action))
            return {"bridge": "unexpected"}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        kernel.bind_signal_sources(private_chat_manager=_PrivateChatManager())
        kernel.bind_dispatch_bridge("PROACTIVE_WAKEUP", _bridge)
        kernel.bind_dispatch_bridge("HEARTFLOW_EVALUATE", _bridge)
        kernel.bind_dispatch_bridge("DREAM_MAINTENANCE", _bridge)

        result = asyncio.run(kernel.tick(chat_id="default:FriendMessage:user-1", trigger="heartbeat"))

        self.assertEqual(result.decision.action, "WAIT")
        self.assertEqual(result.decision.reason, "wait_state:private_wait")
        self.assertEqual(result.decision.metadata["wait_scope"], "private")
        self.assertEqual(bridge_calls, [])

    def test_heartbeat_prefers_wakeup_signal_when_eligible(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        class _WakeupService:
            async def build_signal(self, chat_id, now=None):
                return {"eligible": True, "reason": "silence_threshold_reached"}

        bridge_calls = []

        async def _bridge(chat_id, snapshot, decision):
            bridge_calls.append((chat_id, decision.action, decision.reason))
            return {"bridge": "wakeup"}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        kernel.bind_signal_sources(wakeup_service=_WakeupService())
        kernel.bind_dispatch_bridge("PROACTIVE_WAKEUP", _bridge)

        result = asyncio.run(kernel.tick(chat_id="chat-1", trigger="heartbeat"))

        self.assertEqual(result.decision.action, "PROACTIVE_WAKEUP")
        self.assertEqual(result.dispatch_result, {"bridge": "wakeup"})
        self.assertEqual(bridge_calls, [("chat-1", "PROACTIVE_WAKEUP", "wakeup_signal")])
        self.assertEqual(result.decision.next_tick_delay, 15.0)
        self.assertEqual(result.decision.metadata["scheduler_bucket"], "post_dialogue")

    def test_heartbeat_prefers_heartflow_when_wakeup_not_ready(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        class _WakeupService:
            async def build_signal(self, chat_id, now=None):
                return {"eligible": False, "reason": "cooldown"}

        class _HeartflowManager:
            async def preview_chat(self, chat_id, snapshot=None, now=None):
                return {"eligible": True, "action_type": "prepare_reply", "blocked_reason": ""}

        bridge_calls = []

        async def _bridge(chat_id, snapshot, decision):
            bridge_calls.append((chat_id, decision.action, decision.reason))
            return {"bridge": "heartflow"}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        kernel.bind_signal_sources(wakeup_service=_WakeupService(), heartflow_manager=_HeartflowManager())
        kernel.bind_dispatch_bridge("HEARTFLOW_EVALUATE", _bridge)

        result = asyncio.run(kernel.tick(chat_id="chat-1", trigger="heartbeat"))

        self.assertEqual(result.decision.action, "HEARTFLOW_EVALUATE")
        self.assertEqual(result.dispatch_result, {"bridge": "heartflow"})
        self.assertEqual(bridge_calls, [("chat-1", "HEARTFLOW_EVALUATE", "heartflow_signal:prepare_reply")])
        self.assertEqual(result.decision.metadata["heartflow_preview_mode"], "readonly")
        self.assertEqual(result.decision.next_tick_delay, 15.0)

    def test_heartbeat_prefers_compaction_over_memory_and_dream(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        class _Compaction:
            async def get_trace_status(self, chat_id):
                return {"pending_eval_nodes_count": 1}

        class _MemorySummarizer:
            async def describe_session_eligibility(self, chat_id):
                return {"eligible": True, "candidate_present": True, "reason": "eligible", "pending_messages": 80}

        class _MemoryEngine:
            summarizer = _MemorySummarizer()

            async def describe_session_eligibility(self, chat_id):
                return await self.summarizer.describe_session_eligibility(chat_id)

        class _DreamScheduler:
            async def describe_session_eligibility(self, chat_id, now_ts):
                return {"eligible": True, "reason": "eligible", "throttle_scope": "global"}

        async def _bridge(chat_id, snapshot, decision):
            return {"bridge": decision.action}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        kernel.bind_signal_sources(
            context_compaction=_Compaction(),
            memory_service=_MemoryEngine(),
            dream_scheduler=_DreamScheduler(),
        )
        kernel.bind_dispatch_bridge("COMPACTION_EVALUATE", _bridge)
        kernel.bind_dispatch_bridge("MEMORY_MAINTENANCE", _bridge)
        kernel.bind_dispatch_bridge("DREAM_MAINTENANCE", _bridge)

        result = asyncio.run(kernel.tick(chat_id="chat-1", trigger="heartbeat"))

        self.assertEqual(result.decision.action, "COMPACTION_EVALUATE")
        self.assertEqual(result.decision.metadata["maintenance_priority_winner"], "compaction")
        self.assertEqual(
            result.decision.metadata["skipped_lower_priority_actions"],
            ["MEMORY_MAINTENANCE", "DREAM_MAINTENANCE"],
        )
        self.assertEqual(result.decision.next_tick_delay, 120.0)
        self.assertEqual(result.decision.metadata["scheduler_bucket"], "maintenance_backoff")

    def test_heartbeat_prefers_memory_over_dream_when_compaction_not_ready(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        class _Compaction:
            async def get_trace_status(self, chat_id):
                return {"pending_eval_nodes_count": 0}

        class _MemorySummarizer:
            async def describe_session_eligibility(self, chat_id):
                return {"eligible": True, "candidate_present": True, "reason": "eligible", "pending_messages": 80}

        class _MemoryEngine:
            summarizer = _MemorySummarizer()

            async def describe_session_eligibility(self, chat_id):
                return await self.summarizer.describe_session_eligibility(chat_id)

        class _DreamScheduler:
            async def describe_session_eligibility(self, chat_id, now_ts):
                return {"eligible": True, "reason": "eligible", "throttle_scope": "global"}

        async def _bridge(chat_id, snapshot, decision):
            return {"bridge": decision.action}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        kernel.bind_signal_sources(
            context_compaction=_Compaction(),
            memory_service=_MemoryEngine(),
            dream_scheduler=_DreamScheduler(),
        )
        kernel.bind_dispatch_bridge("MEMORY_MAINTENANCE", _bridge)
        kernel.bind_dispatch_bridge("DREAM_MAINTENANCE", _bridge)

        result = asyncio.run(kernel.tick(chat_id="chat-1", trigger="heartbeat"))

        self.assertEqual(result.decision.action, "MEMORY_MAINTENANCE")
        self.assertEqual(result.decision.metadata["maintenance_priority_winner"], "memory")
        self.assertEqual(result.decision.metadata["skipped_lower_priority_actions"], ["DREAM_MAINTENANCE"])
        self.assertEqual(result.dispatch_result, {"bridge": "MEMORY_MAINTENANCE"})
        self.assertEqual(result.decision.next_tick_delay, 120.0)

    def test_dream_summary_marks_global_throttle_reason(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        class _DreamScheduler:
            def describe_session_eligibility(self, session_id, now_ts):
                return {
                    "eligible": False,
                    "reason": "dream_global_cooldown",
                    "session_id": session_id,
                    "throttle_scope": "global",
                }

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        kernel.bind_signal_sources(dream_scheduler=_DreamScheduler())

        result = asyncio.run(kernel.tick(chat_id="chat-1", trigger="heartbeat"))

        self.assertEqual(result.decision.action, "NOOP")
        self.assertEqual(result.decision.metadata["dream_throttle_scope"], "global")
        self.assertEqual(result.decision.metadata["dream_reason"], "dream_global_cooldown")
        self.assertEqual(result.decision.next_tick_delay, 300.0)

    def test_select_due_chats_prefers_new_waiting_and_due_over_future_idle(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                snapshots = {
                    "chat-new": {"chat_id": "chat-new", "latest_activity_ts": 150.0},
                    "chat-wait": {"chat_id": "chat-wait", "latest_activity_ts": 120.0},
                    "chat-due": {"chat_id": "chat-due", "latest_activity_ts": 140.0},
                    "chat-future": {"chat_id": "chat-future", "latest_activity_ts": 160.0},
                }
                return snapshots.get(chat_id, {"chat_id": chat_id, "latest_activity_ts": 0.0})

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())

        async def _run():
            wait_state = await kernel.get_loop_state("chat-wait")
            wait_state.phase = "WAITING"
            wait_state.next_tick_at = 95.0
            await kernel._state_store.save(wait_state)

            due_state = await kernel.get_loop_state("chat-due")
            due_state.phase = "IDLE"
            due_state.next_tick_at = 99.0
            await kernel._state_store.save(due_state)

            future_state = await kernel.get_loop_state("chat-future")
            future_state.phase = "IDLE"
            future_state.next_tick_at = 140.0
            await kernel._state_store.save(future_state)

            return await kernel.select_due_chats(
                ["chat-future", "chat-due", "chat-new", "chat-wait"],
                now=100.0,
                horizon_seconds=2.0,
                max_batch=32,
            )

        due_chats = asyncio.run(_run())

        self.assertEqual(due_chats, ["chat-new", "chat-wait", "chat-due"])

    def test_describe_due_selection_applies_fairness_penalty_and_starvation_boost(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "latest_activity_ts": 100.0, "executor_pending": 0, "wait_targets": []}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())

        async def _run():
            hot = await kernel.get_loop_state("chat-hot")
            hot.phase = "ACTIVE"
            hot.next_tick_at = 90.0
            hot.last_selected_at = 99.0
            hot.consecutive_selected_count = 8
            await kernel._state_store.save(hot)

            maintenance = await kernel.get_loop_state("chat-maint")
            maintenance.phase = "MAINTENANCE"
            maintenance.next_tick_at = 90.0
            maintenance.pending_signals["maintenance_candidates_summary"] = {
                "memory": {"candidate_present": True, "reason": "eligible"}
            }
            await kernel._state_store.save(maintenance)

            return await kernel.describe_due_selection(
                ["chat-hot", "chat-maint"],
                now=100.0,
                horizon_seconds=2.0,
                max_batch=32,
            )

        report = asyncio.run(_run())

        self.assertEqual(report["selected"], ["chat-maint", "chat-hot"])
        self.assertEqual(report["poll_mode"], "FAST")
        self.assertEqual(report["maintenance_budget_total"], 0)
        self.assertGreater(report["score_breakdown"]["chat-hot"]["fairness_penalty"], 0.0)
        self.assertGreater(report["score_breakdown"]["chat-maint"]["maintenance_boost"], 0.0)

    def test_maintenance_budget_blocks_dispatch_even_when_candidate_is_ready(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        class _Compaction:
            async def get_trace_status(self, chat_id):
                return {"pending_eval_nodes_count": 1}

        bridge_calls = []

        async def _bridge(chat_id, snapshot, decision):
            bridge_calls.append((chat_id, decision.action))
            return {"bridge": "compaction"}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        kernel.bind_signal_sources(context_compaction=_Compaction())
        kernel.bind_dispatch_bridge("COMPACTION_EVALUATE", _bridge)
        kernel.set_heartbeat_scheduler_context(
            {
                "selected": ["chat-1"],
                "score_breakdown": {
                    "chat-1": {
                        "scheduler_score": 42.0,
                        "due_rank": 1,
                        "selected_reason": "selected_by_scheduler_score",
                        "pressure_components": {},
                    }
                },
                "poll_mode": "FAST",
                "maintenance_budget_total": 0,
                "maintenance_budget_used": 0,
                "maintenance_budget_remaining": 0,
                "maintenance_blocked_by_budget": [],
            }
        )

        result = asyncio.run(kernel.tick(chat_id="chat-1", trigger="heartbeat"))

        self.assertEqual(result.decision.action, "NOOP")
        self.assertEqual(result.decision.reason, "maintenance_budget_blocked")
        self.assertTrue(result.decision.metadata["maintenance_blocked_by_budget"])
        self.assertEqual(result.decision.metadata["maintenance_budget_state"]["remaining"], 0)
        self.assertEqual(result.state.phase, "MAINTENANCE")
        self.assertEqual(bridge_calls, [])

    def test_maintenance_budget_blocked_degrades_to_idle_after_three_rounds(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        class _Compaction:
            async def get_trace_status(self, chat_id):
                return {"pending_eval_nodes_count": 1}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        kernel.bind_signal_sources(context_compaction=_Compaction())

        async def _tick_once():
            kernel.set_heartbeat_scheduler_context(
                {
                    "selected": ["chat-1"],
                    "score_breakdown": {"chat-1": {"scheduler_score": 42.0, "due_rank": 1, "pressure_components": {}}},
                    "poll_mode": "FAST",
                    "maintenance_budget_total": 0,
                    "maintenance_budget_used": 0,
                    "maintenance_budget_remaining": 0,
                    "maintenance_blocked_by_budget": [],
                }
            )
            return await kernel.tick(chat_id="chat-1", trigger="heartbeat")

        async def _run():
            first = await _tick_once()
            first_phase = first.state.phase
            second = await _tick_once()
            second_phase = second.state.phase
            third = await _tick_once()
            third_phase = third.state.phase
            return (first, first_phase), (second, second_phase), (third, third_phase)

        (first, first_phase), (second, second_phase), (third, third_phase) = asyncio.run(_run())

        self.assertEqual(first_phase, "MAINTENANCE")
        self.assertEqual(second_phase, "MAINTENANCE")
        self.assertEqual(third_phase, "IDLE")
        self.assertTrue(third.decision.metadata["maintenance_phase_downgraded"])
        self.assertEqual(third.decision.metadata["maintenance_budget_blocked_rounds"], 3)
        self.assertEqual(third.decision.metadata["schedule_reason"], "maintenance_budget_blocked_idle_downgrade")

    def test_proactive_dispatch_marks_pending_heartflow_when_both_signals_exist(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        class _WakeupService:
            async def build_signal(self, chat_id, now=None):
                return {"eligible": True, "reason": "silence_threshold_reached"}

        class _HeartflowManager:
            async def preview_chat(self, chat_id, snapshot=None, now=None):
                return {"eligible": True, "action_type": "prepare_reply", "blocked_reason": ""}

        async def _bridge(chat_id, snapshot, decision):
            return {"bridge": decision.action.lower()}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        kernel.bind_signal_sources(wakeup_service=_WakeupService(), heartflow_manager=_HeartflowManager())
        kernel.bind_dispatch_bridge("PROACTIVE_WAKEUP", _bridge)

        result = asyncio.run(kernel.tick(chat_id="chat-1", trigger="heartbeat"))

        self.assertEqual(result.decision.action, "PROACTIVE_WAKEUP")
        self.assertTrue(result.decision.metadata["pending_heartflow"])
        self.assertEqual(result.state.pending_signals["pending_heartflow"], True)
        self.assertEqual(result.state.pending_signals["pending_heartflow_reason"], "heartflow_signal:prepare_reply")

    def test_pending_heartflow_is_replayed_on_next_heartbeat(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        class _HeartflowManager:
            async def preview_chat(self, chat_id, snapshot=None, now=None):
                return {"eligible": False, "action_type": "prepare_reply", "blocked_reason": ""}

        bridge_calls = []

        async def _bridge(chat_id, snapshot, decision):
            bridge_calls.append((chat_id, decision.action, decision.reason))
            return {"bridge": "heartflow"}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        kernel.bind_signal_sources(heartflow_manager=_HeartflowManager())
        kernel.bind_dispatch_bridge("HEARTFLOW_EVALUATE", _bridge)

        async def _run():
            state = await kernel.get_loop_state("chat-1")
            state.pending_signals["pending_heartflow"] = True
            state.pending_signals["pending_heartflow_reason"] = "heartflow_signal:prepare_reply"
            await kernel._state_store.save(state)
            return await kernel.tick(chat_id="chat-1", trigger="heartbeat")

        result = asyncio.run(_run())

        self.assertEqual(result.decision.action, "HEARTFLOW_EVALUATE")
        self.assertEqual(result.decision.reason, "heartflow_signal:prepare_reply")
        self.assertTrue(result.decision.metadata["pending_heartflow_consumed"])
        self.assertFalse(result.state.pending_signals["pending_heartflow"])
        self.assertEqual(bridge_calls, [("chat-1", "HEARTFLOW_EVALUATE", "heartflow_signal:prepare_reply")])

    def test_direct_heartbeat_maintenance_uses_fallback_budget_state_in_metadata(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        class _Compaction:
            async def get_trace_status(self, chat_id):
                return {"pending_eval_nodes_count": 1}

        async def _bridge(chat_id, snapshot, decision):
            return {"bridge": "compaction"}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        kernel.bind_signal_sources(context_compaction=_Compaction())
        kernel.bind_dispatch_bridge("COMPACTION_EVALUATE", _bridge)

        result = asyncio.run(kernel.tick(chat_id="chat-1", trigger="heartbeat"))

        self.assertEqual(result.decision.action, "COMPACTION_EVALUATE")
        self.assertEqual(result.decision.metadata["maintenance_budget_state"]["total"], 1)
        self.assertEqual(result.decision.metadata["maintenance_budget_state"]["remaining"], 1)
        self.assertEqual(result.state.pending_signals["maintenance_budget_state"]["total"], 1)
        self.assertEqual(result.state.pending_signals["maintenance_budget_state"]["remaining"], 1)

    def test_maintenance_budget_escalates_when_backlog_lives_in_skipped_by_batch(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "latest_activity_ts": 100.0, "executor_pending": 0, "wait_targets": []}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())

        async def _run():
            backlog = await kernel.get_loop_state("chat-maint")
            backlog.phase = "MAINTENANCE"
            backlog.next_tick_at = 90.0
            backlog.pending_signals["maintenance_candidates_summary"] = {
                "memory": {"candidate_present": True, "reason": "eligible"}
            }
            await kernel._state_store.save(backlog)

            first = await kernel.describe_due_selection(
                ["chat-maint"],
                now=100.0,
                horizon_seconds=2.0,
                max_batch=0,
            )
            second = await kernel.describe_due_selection(
                ["chat-maint"],
                now=101.0,
                horizon_seconds=2.0,
                max_batch=0,
            )
            return first, second

        first, second = asyncio.run(_run())

        self.assertEqual(first["selected"], [])
        self.assertEqual(first["skipped_by_batch"], ["chat-maint"])
        self.assertEqual(first["maintenance_budget_total"], 1)
        self.assertEqual(second["maintenance_budget_total"], 2)

    def test_forced_promotion_selects_starved_chat_before_hot_chat(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "latest_activity_ts": 100.0, "executor_pending": 0, "wait_targets": []}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())

        async def _run():
            hot = await kernel.get_loop_state("chat-hot")
            hot.phase = "ACTIVE"
            hot.next_tick_at = 90.0
            hot.last_selected_at = 99.0
            hot.consecutive_selected_count = 6
            await kernel._state_store.save(hot)

            starved = await kernel.get_loop_state("chat-starved")
            starved.phase = "MAINTENANCE"
            starved.next_tick_at = 90.0
            starved.missed_due_passes = kernel.STARVATION_PASS_THRESHOLDS["MAINTENANCE"]
            starved.pending_signals["maintenance_candidates_summary"] = {
                "memory": {"candidate_present": True, "reason": "eligible"}
            }
            await kernel._state_store.save(starved)

            report = await kernel.describe_due_selection(
                ["chat-hot", "chat-starved"],
                now=100.0,
                horizon_seconds=2.0,
                max_batch=1,
            )
            refreshed = await kernel.get_loop_state("chat-starved")
            return report, refreshed

        report, refreshed = asyncio.run(_run())

        self.assertEqual(report["selected"], ["chat-starved"])
        self.assertEqual(report["forced_promotions_selected"], ["chat-starved"])
        self.assertEqual(report["score_breakdown"]["chat-starved"]["selected_reason"], "selected_by_forced_promotion")
        self.assertTrue(report["score_breakdown"]["chat-starved"]["forced_promotion_eligible"])
        self.assertEqual(refreshed.missed_due_passes, kernel.STARVATION_PASS_THRESHOLDS["MAINTENANCE"])

    def test_due_selection_exposes_quota_and_backpressure_summary(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                pending = 1 if chat_id.startswith("busy") else 0
                return {"chat_id": chat_id, "latest_activity_ts": 100.0, "executor_pending": pending, "wait_targets": []}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())

        async def _run():
            chat_ids = []
            for index in range(18):
                state = await kernel.get_loop_state(f"busy-{index}")
                state.phase = "BUSY"
                state.next_tick_at = 90.0
                await kernel._state_store.save(state)
                chat_ids.append(f"busy-{index}")
            for index in range(10):
                state = await kernel.get_loop_state(f"maint-{index}")
                state.phase = "MAINTENANCE"
                state.next_tick_at = 90.0
                state.pending_signals["maintenance_candidates_summary"] = {
                    "compaction": {"eligible": True}
                }
                await kernel._state_store.save(state)
                chat_ids.append(f"maint-{index}")
            return await kernel.describe_due_selection(
                chat_ids,
                now=100.0,
                horizon_seconds=2.0,
                max_batch=20,
            )

        report = asyncio.run(_run())

        self.assertTrue(report["busy_backpressure_active"])
        self.assertEqual(report["batch_plan"]["dialogue_slots"], 16)
        self.assertGreater(report["batch_fill_rate"], 0.0)
        self.assertIn("busy_ratio", report["batch_pressure"])
        self.assertEqual(report["quota_skip_counts"]["skipped_by_maintenance_quota"], 6)
        self.assertEqual(len(report["maintenance_selected"]), 4)

    def test_describe_due_selection_is_observe_only_until_commit(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "latest_activity_ts": 100.0, "executor_pending": 0, "wait_targets": []}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())

        async def _run():
            starved = await kernel.get_loop_state("chat-starved")
            starved.phase = "MAINTENANCE"
            starved.next_tick_at = 90.0
            starved.missed_due_passes = 2
            starved.pending_signals["maintenance_candidates_summary"] = {
                "memory": {"candidate_present": True, "reason": "eligible"}
            }
            await kernel._state_store.save(starved)

            report = await kernel.describe_due_selection(
                ["chat-starved"],
                now=100.0,
                horizon_seconds=2.0,
                max_batch=0,
            )
            after_describe = (await kernel.get_loop_state("chat-starved")).missed_due_passes
            await kernel.commit_due_selection_counters(report)
            after_commit = (await kernel.get_loop_state("chat-starved")).missed_due_passes
            return report, after_describe, after_commit

        report, after_describe, after_commit = asyncio.run(_run())

        self.assertEqual(report["selected"], [])
        self.assertEqual(after_describe, 2)
        self.assertEqual(after_commit, 3)

    def test_maintenance_quota_is_hard_limit_even_with_overflow_capacity(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                pending = 1 if chat_id.startswith("busy") else 0
                return {"chat_id": chat_id, "latest_activity_ts": 100.0, "executor_pending": pending, "wait_targets": []}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())

        async def _run():
            chat_ids = []
            for index in range(24):
                state = await kernel.get_loop_state(f"busy-{index}")
                state.phase = "BUSY"
                state.next_tick_at = 90.0
                await kernel._state_store.save(state)
                chat_ids.append(f"busy-{index}")
            for index in range(8):
                state = await kernel.get_loop_state(f"maint-{index}")
                state.phase = "MAINTENANCE"
                state.next_tick_at = 90.0
                state.pending_signals["maintenance_candidates_summary"] = {
                    "compaction": {"eligible": True}
                }
                await kernel._state_store.save(state)
                chat_ids.append(f"maint-{index}")
            return await kernel.describe_due_selection(
                chat_ids,
                now=100.0,
                horizon_seconds=2.0,
                max_batch=32,
            )

        report = asyncio.run(_run())

        self.assertEqual(report["batch_plan"]["maintenance_slots"], 4)
        self.assertEqual(len(report["maintenance_selected"]), 4)
        self.assertEqual(report["quota_skip_counts"]["skipped_by_maintenance_quota"], 4)

    def test_dispatch_failure_still_commits_heartbeat_state_with_fast_recheck(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        class _WakeupService:
            async def build_signal(self, chat_id, now=None):
                return {"eligible": True, "reason": "silence_threshold_reached"}

        async def _bridge(chat_id, snapshot, decision):
            raise RuntimeError("bridge boom")

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        kernel.bind_signal_sources(wakeup_service=_WakeupService())
        kernel.bind_dispatch_bridge("PROACTIVE_WAKEUP", _bridge)

        async def _run():
            before = __import__("time").time()
            with self.assertRaises(RuntimeError):
                await kernel.tick(chat_id="chat-1", trigger="heartbeat")
            state = await kernel.get_loop_state("chat-1")
            return before, state

        before, state = asyncio.run(_run())

        self.assertEqual(state.last_decision, "PROACTIVE_WAKEUP")
        self.assertTrue(bool(state.phase))
        self.assertGreater(state.last_tick_at, 0.0)
        self.assertGreater(state.next_tick_at, before)
        self.assertEqual(state.pending_signals["schedule_reason"], "dispatch_failure_recheck")
        self.assertTrue(state.pending_signals["dispatch_failed"])
        self.assertEqual(state.pending_signals["dispatch_error_type"], "RuntimeError")
        self.assertEqual(state.pending_signals["dispatch_error_reason"], "bridge boom")
        self.assertEqual(state.pending_signals["dispatch_failure_backoff"], 5.0)

    def test_post_dispatch_cooldown_reason_is_not_overwritten_by_base_pending_signals(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "executor_pending": 0, "wait_targets": []}

        class _WakeupService:
            async def build_signal(self, chat_id, now=None):
                return {"eligible": True, "reason": "silence_threshold_reached"}

        async def _bridge(chat_id, snapshot, decision):
            return {
                "cooldown_until": __import__("time").time() + 60.0,
                "cooldown_reason": "dispatch_reason",
            }

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())
        kernel.bind_signal_sources(wakeup_service=_WakeupService())
        kernel.bind_dispatch_bridge("PROACTIVE_WAKEUP", _bridge)

        result = asyncio.run(kernel.tick(chat_id="chat-1", trigger="heartbeat"))

        self.assertEqual(result.state.pending_signals["wakeup_cooldown_reason"], "dispatch_reason")
        self.assertEqual(result.state.pending_signals["schedule_reason"], "proactive_wakeup")

    def test_message_entry_routes_through_kernel_after_guards(self):
        # handle_global_message 已重构为仅处理 facade 级守卫和委托；
        # kernel/record_user_message 调用已迁移至 PluginFacade.on_global_message。
        facade_calls = []

        runtime = SimpleNamespace(
            config=SimpleNamespace(
                global_settings=SimpleNamespace(debug_mode=False, whitelist_ids=[], admin_ids=[], enable_private_chat=True),
                system1=SimpleNamespace(extra_command_list=[]),
            ),
            group_reply_wait_manager=None,
            lifecycle=SimpleNamespace(manager=None),
            reflect_tracker=None,
            evolution=SimpleNamespace(record_user_message=lambda event: None),
            attention_gate=None,
            host_bridge=None,
            chat_loop_kernel=None,
        )

        async def _handle_group_reply_wait(event, scope):
            facade_calls.append(("handle_group_reply_wait", scope.chat_id))
            return "NONE"

        def _track_incoming(sender_id):
            facade_calls.append(("track_incoming_user_activity", sender_id))

        async def _try_consume_reflect_feedback(event):
            facade_calls.append(("try_consume_reflect_feedback", event.get_sender_id()))
            return None

        async def _record_and_dispatch_attention(event, scope):
            facade_calls.append(("record_and_dispatch_attention", scope.chat_id))
            return "BUFFERED"

        def _cancel_group_wait(event, result, status):
            facade_calls.append(("cancel_group_wait_if_interrupted", status))

        def _suppress(event, status, is_direct_call):
            facade_calls.append(("suppress_default_llm_if_engaged", status, is_direct_call))
            return None

        async def _handle_poke(event):
            from astrmai.presentation.dto.message_scope import IngressDecision
            return IngressDecision.allow()

        def _check_message_scope_access(scope):
            from astrmai.presentation.dto.message_scope import IngressDecision
            return IngressDecision.allow()

        facade = SimpleNamespace(
            is_framework_command=lambda msg: False,
            handle_poke=_handle_poke,
            check_message_scope_access=_check_message_scope_access,
            handle_group_reply_wait=_handle_group_reply_wait,
            is_debug_mode=lambda: False,
            track_incoming_user_activity=_track_incoming,
            try_consume_reflect_feedback=_try_consume_reflect_feedback,
            record_and_dispatch_attention=_record_and_dispatch_attention,
            cancel_group_wait_if_interrupted=_cancel_group_wait,
            suppress_default_llm_if_engaged=_suppress,
        )
        event = _FakeEvent(
            umo="default:GroupMessage:group-1",
            sender_id="user-1",
            sender_name="Alice",
            group_id="group-1",
            text="normal message",
        )

        async def _run():
            return [item async for item in self.message_entry_mod.handle_global_message(facade, event)]

        results = asyncio.run(_run())

        self.assertEqual(results, [])
        # 验证所有 facade 守卫均被调用
        self.assertIn(("handle_group_reply_wait", "default:GroupMessage:group-1"), facade_calls)
        self.assertIn(("track_incoming_user_activity", "user-1"), facade_calls)
        self.assertIn(("try_consume_reflect_feedback", "user-1"), facade_calls)
        self.assertIn(("record_and_dispatch_attention", "default:GroupMessage:group-1"), facade_calls)
        self.assertIn(("cancel_group_wait_if_interrupted", "BUFFERED"), facade_calls)
        self.assertIn(("suppress_default_llm_if_engaged", "BUFFERED", False), facade_calls)

    def test_message_entry_anonymous_sender_skips_user_activity_tracking(self):
        facade_calls = []

        async def _handle_group_reply_wait(event, scope):
            return "NONE"

        async def _try_consume_reflect_feedback(event):
            return None

        async def _record_and_dispatch_attention(event, scope):
            facade_calls.append(("record_and_dispatch_attention", scope.sender_id, scope.is_anonymous_sender))
            return "BUFFERED"

        async def _handle_poke(event):
            from astrmai.presentation.dto.message_scope import IngressDecision
            return IngressDecision.allow()

        def _check_message_scope_access(scope):
            from astrmai.presentation.dto.message_scope import IngressDecision
            return IngressDecision.allow()

        facade = SimpleNamespace(
            is_framework_command=lambda msg: False,
            handle_poke=_handle_poke,
            check_message_scope_access=_check_message_scope_access,
            handle_group_reply_wait=_handle_group_reply_wait,
            is_debug_mode=lambda: False,
            track_incoming_user_activity=lambda sender_id: facade_calls.append(("track", sender_id)),
            try_consume_reflect_feedback=_try_consume_reflect_feedback,
            record_and_dispatch_attention=_record_and_dispatch_attention,
            cancel_group_wait_if_interrupted=lambda event, result, status: None,
            suppress_default_llm_if_engaged=lambda event, status, is_direct_call: None,
        )
        event = _FakeEvent(
            umo="default:GroupMessage:group-1",
            sender_id="800000001234",
            sender_name="Anonymous",
            group_id="group-1",
            text="anonymous message",
        )

        async def _run():
            return [item async for item in self.message_entry_mod.handle_global_message(facade, event)]

        self.assertEqual(asyncio.run(_run()), [])
        self.assertNotIn(("track", "800000001234"), facade_calls)
        self.assertIn(("record_and_dispatch_attention", "800000001234", True), facade_calls)

    def test_group_decrease_notice_clears_only_runtime_state_for_bot_self(self):
        facade_mod = importlib.import_module("astrmai.app.plugin_facade")
        calls = []

        class _AsyncClear:
            def __init__(self, method_name):
                setattr(self, method_name, self._clear)

            async def _clear(self, chat_id):
                calls.append(chat_id)
                return True

        class _Heartflow:
            def clear_chat(self, chat_id):
                calls.append(chat_id)
                return True

        class _GroupWait:
            def cancel_wait(self, chat_id, reason=""):
                calls.append((chat_id, reason))
                return True

        facade = object.__new__(facade_mod.PluginFacade)
        facade.runtime = SimpleNamespace(
            attention_gate=_AsyncClear("clear_chat_state"),
            state_engine=_AsyncClear("clear_chat_state"),
            runtime_coordinator=_AsyncClear("clear_runtime_state"),
            dialogue_store=_AsyncClear("clear_chat"),
            chat_loop_kernel=_AsyncClear("clear_chat_state"),
            proactive_task=SimpleNamespace(heartflow_manager=_Heartflow()),
            group_reply_wait_manager=_GroupWait(),
        )
        event = SimpleNamespace(
            unified_msg_origin="default:GroupMessage:123456",
            raw_event={
                "post_type": "notice",
                "notice_type": "group_decrease",
                "group_id": "123456",
                "user_id": "bot-1",
                "self_id": "bot-1",
            },
            get_self_id=lambda: "bot-1",
        )

        handled = asyncio.run(facade.handle_group_membership_notice(event))

        self.assertTrue(handled)
        self.assertEqual(calls.count("default:GroupMessage:123456"), 6)
        self.assertIn(("default:GroupMessage:123456", "bot_left_group"), calls)

    def test_message_entry_self_message_stops_before_kernel(self):
        calls = []

        class _Kernel:
            async def tick(self, *, chat_id, trigger, event=None):
                calls.append((chat_id, trigger))
                return SimpleNamespace(dispatch_result="BUFFERED")

        runtime = SimpleNamespace(
            config=SimpleNamespace(
                global_settings=SimpleNamespace(debug_mode=False, whitelist_ids=[], admin_ids=[], enable_private_chat=True),
                system1=SimpleNamespace(extra_command_list=[]),
            ),
            group_reply_wait_manager=None,
            lifecycle=SimpleNamespace(manager=None),
            reflect_tracker=None,
            evolution=SimpleNamespace(record_user_message=lambda event: None),
            attention_gate=SimpleNamespace(process_event=lambda event: "BUFFERED"),
            host_bridge=SimpleNamespace(suppress_default_llm=lambda event: "(ghost)"),
            chat_loop_kernel=_Kernel(),
        )
        async def _handle_group_reply_wait(event, scope):
            return "NONE"

        async def _try_consume_reflect_feedback(event):
            return None

        async def _record_and_dispatch_attention(event, scope):
            return "BUFFERED"

        async def _handle_poke(event):
            from astrmai.presentation.dto.message_scope import IngressDecision
            return IngressDecision.allow()

        def _check_message_scope_access(scope):
            from astrmai.presentation.dto.message_scope import IngressDecision
            return IngressDecision.allow()

        facade = SimpleNamespace(
            is_framework_command=lambda msg: False,
            handle_poke=_handle_poke,
            check_message_scope_access=_check_message_scope_access,
            handle_group_reply_wait=_handle_group_reply_wait,
            is_debug_mode=lambda: False,
            track_incoming_user_activity=lambda sender_id: None,
            try_consume_reflect_feedback=_try_consume_reflect_feedback,
            record_and_dispatch_attention=_record_and_dispatch_attention,
            cancel_group_wait_if_interrupted=lambda event, result, status: None,
            suppress_default_llm_if_engaged=lambda event, status, is_direct_call: None,
        )
        event = _FakeEvent(
            umo="default:GroupMessage:group-1",
            sender_id="bot-1",
            sender_name="Mai",
            group_id="group-1",
            self_id="bot-1",
            text="self",
        )

        async def _run():
            return [item async for item in self.message_entry_mod.handle_global_message(facade, event)]

        results = asyncio.run(_run())

        self.assertEqual(results, [])
        self.assertEqual(calls, [])

    def test_peek_loop_state_does_not_create_state(self):
        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=SimpleNamespace())

        async def _run():
            before = await kernel._state_store.count()
            state = await kernel.peek_loop_state("chat-peek")
            after = await kernel._state_store.count()
            return before, state, after

        before, state, after = asyncio.run(_run())

        self.assertEqual(before, 0)
        self.assertIsNone(state)
        self.assertEqual(after, 0)

    def test_describe_status_sync_exposes_scheduler_policy_profiles(self):
        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=SimpleNamespace())

        status = kernel.describe_status_sync()

        policy = status["scheduler_policy"]
        self.assertEqual(policy["active_profile"], "balanced")
        self.assertIn("dialogue_first", policy["available_profiles"])
        self.assertIn("maintenance_friendly", policy["available_profiles"])
        self.assertEqual(policy["current"]["fairness_penalty_multiplier"], kernel.FAIRNESS_PENALTY_MULTIPLIER)
        self.assertEqual(
            policy["current"]["forced_promotion_pass_thresholds"]["MAINTENANCE"],
            kernel.STARVATION_PASS_THRESHOLDS["MAINTENANCE"],
        )

    def test_scheduler_policy_sync_reflects_active_testing_profile(self):
        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=SimpleNamespace())
        kernel.set_scheduler_profile_for_testing("maintenance_friendly")

        status = kernel.describe_status_sync()

        policy = status["scheduler_policy"]
        self.assertEqual(policy["active_profile"], "maintenance_friendly")
        self.assertEqual(policy["current"]["maintenance_batch_slots"], 6)
        self.assertEqual(policy["current"]["fairness_penalty_multiplier"], 10.0)

    def test_scheduler_policy_profiles_offer_distinct_tuning_matrix(self):
        profiles = self.kernel_mod.ChatLoopKernel.scheduler_policy_profiles_sync()

        self.assertLess(
            profiles["maintenance_friendly"]["maintenance_boost_divisor_seconds"],
            profiles["balanced"]["maintenance_boost_divisor_seconds"],
        )
        self.assertGreater(
            profiles["dialogue_first"]["fairness_penalty_multiplier"],
            profiles["balanced"]["fairness_penalty_multiplier"],
        )
        self.assertGreater(
            profiles["maintenance_friendly"]["maintenance_batch_slots"],
            profiles["dialogue_first"]["maintenance_batch_slots"],
        )
        self.assertGreater(
            profiles["dialogue_first"]["forced_promotion_pass_thresholds"]["IDLE"],
            profiles["balanced"]["forced_promotion_pass_thresholds"]["IDLE"],
        )

    def test_scheduler_profiles_change_due_selection_behavior(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                pending = 1 if chat_id.startswith("busy") else 0
                return {"chat_id": chat_id, "latest_activity_ts": 100.0, "executor_pending": pending, "wait_targets": []}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())

        async def _prepare():
            chat_ids = []
            for index in range(12):
                state = await kernel.get_loop_state(f"busy-{index}")
                state.phase = "BUSY"
                state.next_tick_at = 90.0
                await kernel._state_store.save(state)
                chat_ids.append(f"busy-{index}")
            for index in range(8):
                state = await kernel.get_loop_state(f"maint-{index}")
                state.phase = "MAINTENANCE"
                state.next_tick_at = 90.0
                state.pending_signals["maintenance_candidates_summary"] = {
                    "memory": {"candidate_present": True, "reason": "eligible"}
                }
                await kernel._state_store.save(state)
                chat_ids.append(f"maint-{index}")
            return chat_ids

        async def _run():
            chat_ids = await _prepare()
            kernel.set_scheduler_profile_for_testing("balanced")
            balanced = await kernel.describe_due_selection(
                chat_ids,
                now=100.0,
                horizon_seconds=2.0,
                max_batch=20,
            )
            kernel.set_scheduler_profile_for_testing("maintenance_friendly")
            maintenance_friendly = await kernel.describe_due_selection(
                chat_ids,
                now=100.0,
                horizon_seconds=2.0,
                max_batch=20,
            )
            return balanced, maintenance_friendly

        balanced, maintenance_friendly = asyncio.run(_run())

        self.assertEqual(len(balanced["maintenance_selected"]), 4)
        self.assertEqual(len(maintenance_friendly["maintenance_selected"]), 6)
        self.assertGreater(
            balanced["quota_skip_counts"]["skipped_by_maintenance_quota"],
            maintenance_friendly["quota_skip_counts"]["skipped_by_maintenance_quota"],
        )

    def test_scheduler_profiles_change_forced_promotion_threshold(self):
        class _Coordinator:
            async def get_activity_snapshot(self, chat_id):
                return {"chat_id": chat_id, "latest_activity_ts": 100.0, "executor_pending": 0, "wait_targets": []}

        kernel = self.kernel_mod.ChatLoopKernel(runtime_coordinator=_Coordinator())

        async def _run():
            state = await kernel.get_loop_state("chat-maint")
            state.phase = "MAINTENANCE"
            state.next_tick_at = 90.0
            state.missed_due_passes = 4
            state.pending_signals["maintenance_candidates_summary"] = {
                "memory": {"candidate_present": True, "reason": "eligible"}
            }
            await kernel._state_store.save(state)
            kernel.set_scheduler_profile_for_testing("dialogue_first")
            dialogue_first = await kernel.describe_due_selection(["chat-maint"], now=100.0, max_batch=1)
            kernel.set_scheduler_profile_for_testing("balanced")
            balanced = await kernel.describe_due_selection(["chat-maint"], now=100.0, max_batch=1)
            return dialogue_first, balanced

        dialogue_first, balanced = asyncio.run(_run())

        self.assertFalse(dialogue_first["score_breakdown"]["chat-maint"]["forced_promotion_eligible"])
        self.assertTrue(balanced["score_breakdown"]["chat-maint"]["forced_promotion_eligible"])


if __name__ == "__main__":
    unittest.main()
