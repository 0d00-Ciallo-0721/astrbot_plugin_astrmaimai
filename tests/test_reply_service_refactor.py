import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.helpers.astrbot_stubs import install_astrbot_stubs
from tests.helpers.reply_engine_stubs import (
    FakeEvent,
    FakeStateEngine,
    install_reply_engine_stubs,
)


class _FakeRuntimeCoordinator:
    def __init__(self, latest_activity):
        self.latest_activity = latest_activity

    async def get_latest_activity(self, chat_id):
        return self.latest_activity


class _StaleTurnRuntimeCoordinator:
    async def is_current_turn(self, _turn):
        return False


class _ClaimingRuntimeCoordinator:
    def __init__(self):
        self.claims = set()
        self.commits = []
        self.claim_calls = []

    async def is_current_turn(self, _turn):
        return True

    async def claim_send(self, _chat_id, send_key):
        self.claim_calls.append(send_key)
        if send_key in self.claims:
            return False
        self.claims.add(send_key)
        return True

    async def commit_send(self, chat_id, send_key, outbound_message_ids=None):
        self.commits.append((chat_id, send_key, list(outbound_message_ids or [])))

    async def mark_send_failed(self, chat_id, send_key, error=""):
        self.commits.append((chat_id, send_key, [f"failed:{error}"]))

    async def get_latest_committed_outbound(self, _chat_id, *, exclude_send_key=""):
        return ["previous-message"]


async def _noop_post_send(*args, **kwargs):
    return None


class RefactoredReplyServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        install_reply_engine_stubs()
        sys.modules.pop("astrmai.Brain.reply_engine", None)
        sys.modules.pop("astrmai.conversation.execution.reply_service", None)
        reply_mod = importlib.import_module("astrmai.conversation.execution.reply_service")
        self.reply_mod = importlib.reload(reply_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _service(self, *, min_len=8, max_len=48):
        state_engine = FakeStateEngine()
        state_engine.config.reply.segment_min_len = min_len
        state_engine.config.reply.no_segment_max_len = max_len
        state_engine.config.reply.typing_speed_factor = 0.1
        return self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
        )

    def _enable_tts(self, service, **overrides):
        values = {
            "enabled": True,
            "plugin_name": "astrbot_plugin_tts_llm",
            "enable_private": True,
            "enable_group": False,
            "group_probability": 10,
            "group_require_direct_trigger": True,
            "send_text_with_audio": True,
            "min_text_length": 2,
            "max_text_length": 120,
            "silent_on_failure": True,
        }
        values.update(overrides)
        service.config.tts = SimpleNamespace(**values)
        service.tts_bridge.refresh_config(service.config)
        return service.config.tts

    def _install_tts_plugin(self, service, plugin):
        metadata = SimpleNamespace(
            root_dir_name="astrbot_plugin_tts_llm",
            name="astrbot_plugin_tts_llm",
            star_cls=plugin,
        )
        service.state_engine.gateway.context.get_all_stars = lambda: [metadata]

    def test_tts_disabled_preserves_text_only_reply(self):
        service = self._service()
        plugin = SimpleNamespace(hiy_tts_from_text=AsyncMock(return_value=["voice"]))
        self._install_tts_plugin(service, plugin)
        event = FakeEvent("user-1", "Alice", "hello")

        artifact = asyncio.run(service.handle_reply(event, "你好呀", event.unified_msg_origin))

        self.assertTrue(artifact.sent)
        self.assertEqual(len(service.state_engine.gateway.context.sent), 1)
        plugin.hiy_tts_from_text.assert_not_called()
        stages = event.get_extra("astrmai_stage_ledger", [])
        self.assertEqual(
            [stage["stage"] for stage in stages],
            ["reply.prepare", "reply.send", "reply.commit"],
        )
        self.assertTrue(all(stage["status"] == "success" for stage in stages))

    def test_private_overdue_reply_is_allowed_without_newer_activity(self):
        service = self._service()
        event_ts = time.time() - 10.0
        service.config.reply.stale_reply_max_age_sec = 1.0
        service.runtime_coordinator = _FakeRuntimeCoordinator((event_ts, "user-1", "Alice", "旧消息"))
        event = FakeEvent("user-1", "Alice", "问题")
        event.unified_msg_origin = "default:FriendMessage:user-1"
        event.set_extra("is_private_chat", True)
        event.set_extra("astrmai_timestamp", event_ts)

        state, reason = asyncio.run(
            service._check_reply_freshness(event, event.unified_msg_origin)
        )

        self.assertEqual(state, self.reply_mod.FreshnessState.FRESH)
        self.assertEqual(reason, "")

    def test_private_tts_appends_voice_after_text(self):
        service = self._service()
        self._enable_tts(service, send_text_with_audio=True)
        plugin = SimpleNamespace(hiy_tts_from_text=AsyncMock(return_value=["voice"]))
        self._install_tts_plugin(service, plugin)
        event = FakeEvent("user-1", "Alice", "hello")
        event.unified_msg_origin = "default:FriendMessage:user-1"

        artifact = asyncio.run(service.handle_reply(event, "你好呀", event.unified_msg_origin))

        self.assertTrue(artifact.sent)
        self.assertTrue(artifact.metadata["tts_sent"])
        plugin.hiy_tts_from_text.assert_awaited_once_with(event=event, text="你好呀", visible_text="")
        self.assertEqual(len(service.state_engine.gateway.context.sent), 2)
        text_chain = service.state_engine.gateway.context.sent[0][1].chain
        voice_chain = service.state_engine.gateway.context.sent[1][1].chain
        self.assertEqual(getattr(text_chain[0], "text", ""), "你好呀")
        self.assertEqual(voice_chain, ["voice"])

    def test_private_tts_rechecks_freshness_immediately_before_voice_send(self):
        service = self._service()
        self._enable_tts(service, send_text_with_audio=True)
        plugin = SimpleNamespace(hiy_tts_from_text=AsyncMock(return_value=["voice"]))
        self._install_tts_plugin(service, plugin)
        event = FakeEvent("user-1", "Alice", "hello")
        event.unified_msg_origin = "default:FriendMessage:user-1"
        service._check_reply_freshness = AsyncMock(
            side_effect=[
                (self.reply_mod.FreshnessState.FRESH, ""),
                (self.reply_mod.FreshnessState.FRESH, ""),
                (
                    self.reply_mod.FreshnessState.EXPIRED,
                    "proactive_generation_superseded",
                ),
            ]
        )

        artifact = asyncio.run(
            service.handle_reply(event, "你好呀", event.unified_msg_origin)
        )

        self.assertTrue(artifact.sent)
        self.assertFalse(artifact.metadata["tts_sent"])
        self.assertEqual(
            artifact.metadata["tts_skip_reason"],
            "proactive_generation_superseded",
        )
        self.assertEqual(len(service.state_engine.gateway.context.sent), 1)
        self.assertEqual(service._check_reply_freshness.await_count, 3)

    def test_group_tts_requires_direct_trigger(self):
        service = self._service()
        self._enable_tts(service, enable_group=True, group_probability=100, group_require_direct_trigger=True)
        plugin = SimpleNamespace(hiy_tts_from_text=AsyncMock(return_value=["voice"]))
        self._install_tts_plugin(service, plugin)
        event = FakeEvent("user-1", "Alice", "hello")

        artifact = asyncio.run(service.handle_reply(event, "你好呀", event.unified_msg_origin))

        self.assertTrue(artifact.sent)
        self.assertNotIn("tts_sent", artifact.metadata)
        plugin.hiy_tts_from_text.assert_not_called()
        self.assertEqual(len(service.state_engine.gateway.context.sent), 1)

    def test_group_tts_direct_trigger_can_send_voice(self):
        service = self._service()
        self._enable_tts(service, enable_group=True, group_probability=100, group_require_direct_trigger=True)
        plugin = SimpleNamespace(hiy_tts_from_text=AsyncMock(return_value=["voice"]))
        self._install_tts_plugin(service, plugin)
        event = FakeEvent("user-1", "Alice", "hello")
        event.set_extra("astrmai_is_direct_call", True)

        artifact = asyncio.run(service.handle_reply(event, "你好呀", event.unified_msg_origin))

        self.assertTrue(artifact.sent)
        self.assertTrue(artifact.metadata["tts_sent"])
        plugin.hiy_tts_from_text.assert_awaited_once()
        self.assertEqual(len(service.state_engine.gateway.context.sent), 2)

    def test_tts_send_failure_does_not_break_text_reply(self):
        service = self._service()
        self._enable_tts(service, send_text_with_audio=True)
        plugin = SimpleNamespace(hiy_tts_from_text=AsyncMock(return_value=["voice"]))
        self._install_tts_plugin(service, plugin)
        event = FakeEvent("user-1", "Alice", "hello")
        event.unified_msg_origin = "default:FriendMessage:user-1"
        sent = []

        async def _send_message(_umo, chain):
            sent.append(chain)
            if len(sent) == 2:
                raise RuntimeError("voice transport failed")
            return None

        service.state_engine.gateway.context.send_message = _send_message

        artifact = asyncio.run(service.handle_reply(event, "你好呀", event.unified_msg_origin))

        self.assertTrue(artifact.sent)
        self.assertFalse(artifact.metadata["tts_sent"])
        self.assertEqual(len(sent), 2)
        self.assertEqual(getattr(sent[0].chain[0], "text", ""), "你好呀")

    def _build_memory_summarizer(self, *, threshold=2):
        sys.modules.pop("astrmai.memory.services.summarizer", None)
        summarizer_mod = importlib.import_module("astrmai.memory.services.summarizer")
        summarizer_mod = importlib.reload(summarizer_mod)
        gateway = SimpleNamespace(context=SimpleNamespace(astrmai=None))
        config = SimpleNamespace(memory=SimpleNamespace(summary_threshold=threshold, cleanup_interval=3600))
        return summarizer_mod.ChatHistorySummarizer(
            context=SimpleNamespace(astrmai_plugin=None),
            gateway=gateway,
            engine=SimpleNamespace(),
            config=config,
        )

    def _build_memory_engine_runtime(self, *, threshold=2):
        summarizer = self._build_memory_summarizer(threshold=threshold)

        class _Pipeline:
            def __init__(self, summarizer_ref):
                self._session_history_buffer = {}
                self._instant_llm_last_check = {}
                self._summarizer = summarizer_ref

            def build_turn(self, **kwargs):
                return SimpleNamespace(**kwargs, instant_gate_hit=False, instant_memory_id="")

            async def record_turn(self, turn):
                if turn.is_proactive:
                    return {"performed": False, "reason": "proactive_ignored", "pending_messages": 0}
                self._session_history_buffer.setdefault(
                    turn.chat_id,
                    {"buffer": [], "last_update": 0.0, "cooldown_until": 0.0, "failures": 0, "last_run_at": 0.0},
                )
                session = self._session_history_buffer[turn.chat_id]
                session["buffer"].extend([f"用户/旁白：{turn.user_text}", f"Bot：{turn.assistant_text}"])
                session["last_update"] = time.time()
                return {"performed": True, "reason": "recorded", "pending_messages": len(session["buffer"])}

            async def process_instant_gate(self, turn):
                return SimpleNamespace(hit=False, memory_id="")

            async def publish_turn_committed(self, turn):
                return None

            async def describe_session_eligibility(self, chat_id):
                session = self._session_history_buffer.get(chat_id) or {}
                pending = len(session.get("buffer", []) or [])
                threshold_messages = int(getattr(self._summarizer.config.memory, "summary_threshold", 2) or 2) * 2
                eligible = pending > 0 and pending >= threshold_messages
                return {
                    "eligible": eligible,
                    "candidate_present": pending > 0,
                    "reason": "eligible" if eligible else ("below_threshold" if pending > 0 else "no_buffer"),
                    "pending_messages": pending,
                    "history_size": pending,
                    "threshold_messages": threshold_messages,
                    "cooldown_until": float(session.get("cooldown_until", 0.0) or 0.0),
                    "last_memory_run_at": float(session.get("last_run_at", 0.0) or 0.0),
                    "last_update": float(session.get("last_update", 0.0) or 0.0),
                }

        pipeline = _Pipeline(summarizer)
        summarizer.engine.memory_pipeline = pipeline
        return SimpleNamespace(
            session_summarizer=summarizer,
            memory_pipeline=pipeline,
            instant_gate=SimpleNamespace(process_committed_turn=lambda turn: asyncio.sleep(0, result=SimpleNamespace(hit=False, memory_id=""))),
        )

    def test_memory_turn_ingest_passes_real_group_sender_id(self):
        captured = {}

        class _Pipeline:
            def build_turn(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(**kwargs, instant_gate_hit=False, instant_memory_id="")

            async def record_turn(self, _turn):
                return {"performed": False}

        service = self._service()
        service.memory_engine = SimpleNamespace(
            memory_pipeline=_Pipeline(),
            instant_gate=SimpleNamespace(),
        )
        event = SimpleNamespace(
            get_sender_id=lambda: "group-user-42",
            get_extra=lambda _key, default=None: default,
        )

        asyncio.run(service._ingest_memory_turn(event, "group-1", "我叫甲", "记住了"))

        self.assertEqual(captured["sender_id"], "group-user-42")

    def test_stale_reply_is_still_skipped(self):
        state_engine = FakeStateEngine()
        base_ts = time.time() - 12.0
        coordinator = _FakeRuntimeCoordinator((base_ts + 10.0, "user-2", "Bob", "later message"))
        engine = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            runtime_coordinator=coordinator,
        )
        event = FakeEvent("user-1", "Alice", "old question")
        event.set_extra("astrmai_timestamp", base_ts)

        artifact = asyncio.run(engine.handle_reply(event, "this reply is stale", event.unified_msg_origin))

        self.assertEqual(state_engine.gateway.context.sent, [])
        self.assertFalse(event.get_extra("astrmai_reply_sent", False))
        self.assertFalse(artifact.sent)
        self.assertTrue(artifact.blocked_reason)

    def test_stale_turn_generation_is_skipped_even_without_timestamp(self):
        state_engine = FakeStateEngine()
        engine = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            runtime_coordinator=_StaleTurnRuntimeCoordinator(),
        )
        event = FakeEvent("user-1", "Alice", "old question")
        event.set_extra(
            "astrmai_turn_identity",
            SimpleNamespace(chat_id=event.unified_msg_origin, thread_id=event.unified_msg_origin, generation=1),
        )

        artifact = asyncio.run(engine.handle_reply(event, "this reply is stale", event.unified_msg_origin))

        self.assertEqual(state_engine.gateway.context.sent, [])
        self.assertFalse(event.get_extra("astrmai_reply_sent", False))
        self.assertFalse(artifact.sent)
        self.assertTrue(artifact.blocked_reason)
        trace_log = event.get_extra("astrmai_trace_log", [])
        self.assertTrue(any(item.get("stage") == "reply.blocked_stale_generation" for item in trace_log))
        stale_records = [item for item in trace_log if item.get("stage") == "reply.blocked_stale_generation"]
        self.assertNotIn("this reply is stale", str(stale_records))

    def test_generation_flag_off_preserves_legacy_send_for_stale_turn(self):
        state_engine = FakeStateEngine()
        state_engine.config.conversation = SimpleNamespace(conversation_generation_enabled=False)
        engine = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            runtime_coordinator=_StaleTurnRuntimeCoordinator(),
        )
        event = FakeEvent("user-1", "Alice", "old question")
        event.set_extra(
            "astrmai_turn_identity",
            SimpleNamespace(chat_id=event.unified_msg_origin, thread_id=event.unified_msg_origin, generation=1),
        )

        asyncio.run(engine.handle_reply(event, "legacy answer", event.unified_msg_origin))

        self.assertEqual(len(state_engine.gateway.context.sent), 1)
        self.assertTrue(event.get_extra("astrmai_reply_sent", False))

    def test_duplicate_final_send_for_same_turn_is_blocked(self):
        from astrmai.conversation.contracts.turn_identity import TurnIdentity, build_turn_send_key

        state_engine = FakeStateEngine()
        coordinator = _ClaimingRuntimeCoordinator()
        engine = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            runtime_coordinator=coordinator,
        )
        event = FakeEvent("user-1", "Alice", "question")
        turn = TurnIdentity(
            mode="group",
            chat_id=event.unified_msg_origin,
            thread_id=event.unified_msg_origin,
            generation=1,
        )
        event.set_extra("astrmai_turn_identity", turn)

        asyncio.run(engine.handle_reply(event, "first answer", event.unified_msg_origin))
        asyncio.run(engine.handle_reply(event, "duplicate answer", event.unified_msg_origin))

        self.assertEqual(len(state_engine.gateway.context.sent), 1)
        self.assertEqual(coordinator.commits[0][1], build_turn_send_key(turn, "final"))
        trace_log = event.get_extra("astrmai_trace_log", [])
        stages = [item.get("stage") for item in trace_log]
        self.assertIn("reply.send_claimed", stages)
        self.assertIn("reply.send_committed", stages)
        self.assertIn("reply.duplicate_final_blocked", stages)
        claim_records = [item for item in trace_log if str(item.get("stage", "")).startswith("reply.")]
        self.assertNotIn("first answer", str(claim_records))
        self.assertNotIn("duplicate answer", str(claim_records))

    def test_qq_action_is_committed_only_after_visible_reply_send(self):
        from astrmai.conversation.contracts.turn_identity import TurnIdentity

        state_engine = FakeStateEngine()
        coordinator = _ClaimingRuntimeCoordinator()
        order = []

        async def _send_message(_umo, _chain):
            order.append("reply")
            return "reply-message"

        class _Api:
            async def call_action(self, action, **kwargs):
                order.append(action)
                return {"status": "ok"}

        state_engine.gateway.context.send_message = _send_message
        engine = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            runtime_coordinator=coordinator,
        )
        event = FakeEvent("user-1", "Alice", "question")
        event.bot = SimpleNamespace(api=_Api())
        event.set_extra(
            "astrmai_turn_identity",
            TurnIdentity(
                mode="group",
                chat_id=event.unified_msg_origin,
                thread_id=event.unified_msg_origin,
                generation=1,
            ),
        )
        event.set_extra(
            "astrmai_pending_actions",
            [{"action": "poke", "target_id": "user-1", "group_id": "group-1"}],
        )

        asyncio.run(engine.handle_reply(event, "answer", event.unified_msg_origin))

        self.assertEqual(order, ["reply", "send_poke"])
        self.assertEqual(event.get_extra("astrmai_qq_action_results")[0]["status"], "success")

    def test_boolean_context_send_result_is_not_recorded_as_message_id(self):
        from astrmai.conversation.contracts.turn_identity import TurnIdentity

        state_engine = FakeStateEngine()
        coordinator = _ClaimingRuntimeCoordinator()

        async def _send_message(_umo, _chain):
            return True

        state_engine.gateway.context.send_message = _send_message
        engine = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            runtime_coordinator=coordinator,
        )
        event = FakeEvent("user-1", "Alice", "question")
        event.set_extra(
            "astrmai_turn_identity",
            TurnIdentity(
                mode="group",
                chat_id=event.unified_msg_origin,
                thread_id=event.unified_msg_origin,
                generation=1,
            ),
        )

        asyncio.run(engine.handle_reply(event, "answer", event.unified_msg_origin))

        self.assertEqual(coordinator.commits[0][2], [])
        self.assertEqual(event.get_extra("astrmai_reply_outbound_message_ids", []), [])

    def test_send_claim_flag_off_preserves_legacy_duplicate_send_path(self):
        from astrmai.conversation.contracts.turn_identity import TurnIdentity

        state_engine = FakeStateEngine()
        state_engine.config.conversation = SimpleNamespace(reply_send_claim_enabled=False)
        coordinator = _ClaimingRuntimeCoordinator()
        engine = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            runtime_coordinator=coordinator,
        )
        event = FakeEvent("user-1", "Alice", "question")
        event.set_extra(
            "astrmai_turn_identity",
            TurnIdentity(
                mode="group",
                chat_id=event.unified_msg_origin,
                thread_id=event.unified_msg_origin,
                generation=1,
            ),
        )

        asyncio.run(engine.handle_reply(event, "first answer", event.unified_msg_origin))
        asyncio.run(engine.handle_reply(event, "second answer", event.unified_msg_origin))

        self.assertEqual(len(state_engine.gateway.context.sent), 2)
        self.assertEqual(coordinator.claim_calls, [])
        self.assertEqual(coordinator.commits, [])

    def test_send_exception_marks_claim_failed_before_reraising(self):
        from astrmai.conversation.contracts.turn_identity import TurnIdentity

        state_engine = FakeStateEngine()
        coordinator = _ClaimingRuntimeCoordinator()

        async def _raise_send(*_args, **_kwargs):
            raise RuntimeError("transport failed")

        state_engine.gateway.context.send_message = _raise_send
        engine = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            runtime_coordinator=coordinator,
        )
        event = FakeEvent("user-1", "Alice", "question")
        event.set_extra(
            "astrmai_turn_identity",
            TurnIdentity(
                mode="group",
                chat_id=event.unified_msg_origin,
                thread_id=event.unified_msg_origin,
                generation=1,
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "transport failed"):
            asyncio.run(engine.handle_reply(event, "answer", event.unified_msg_origin))

        self.assertEqual(len(coordinator.commits), 1)
        self.assertIn("failed:transport failed", coordinator.commits[0][2])

    def test_send_exception_records_unconfirmed_claim_failure_during_shutdown(self):
        from astrmai.conversation.contracts.turn_identity import TurnIdentity

        state_engine = FakeStateEngine()
        coordinator = _ClaimingRuntimeCoordinator()

        async def _raise_send(*_args, **_kwargs):
            raise RuntimeError("transport failed")

        async def _mark_unconfirmed(*_args, **_kwargs):
            return False

        state_engine.gateway.context.send_message = _raise_send
        coordinator.mark_send_failed = _mark_unconfirmed
        engine = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            runtime_coordinator=coordinator,
        )
        event = FakeEvent("user-1", "Alice", "question")
        event.set_extra(
            "astrmai_turn_identity",
            TurnIdentity(
                mode="group",
                chat_id=event.unified_msg_origin,
                thread_id=event.unified_msg_origin,
                generation=1,
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "transport failed"):
            asyncio.run(engine.handle_reply(event, "answer", event.unified_msg_origin))

        traces = event.get_extra("astrmai_conversation_concurrency_trace", [])
        failed = [item for item in traces if item.get("action") == "send_claim"]
        self.assertTrue(failed)
        self.assertEqual(failed[-1]["claim_status"], "failed_unconfirmed")

    def test_reply_below_segment_limit_splits_complete_sentences(self):
        service = self._service(max_len=80)

        artifact = service._build_visible_reply_artifact("I get what you mean! We can keep this simple for now?")

        self.assertEqual(len(artifact.segments), 2)
        self.assertEqual(artifact.metadata["segment_reason"], "natural_segmenter")

    def test_natural_chinese_reply_splits_into_two_chat_bubbles(self):
        service = self._service(min_len=15, max_len=120)
        text = "晚上好呀～✨ 妃爱刚还在想哥哥会不会来找我呢，结果你就来了！唉嘿嘿♡ 今天辛苦啦，有没有好好吃晚饭呀？"

        artifact = service._build_visible_reply_artifact(text)

        self.assertEqual(
            artifact.segments,
            [
                "晚上好呀～✨ 妃爱刚还在想哥哥会不会来找我呢，结果你就来了！",
                "唉嘿嘿♡ 今天辛苦啦，有没有好好吃晚饭呀？",
            ],
        )

    def test_reply_below_segment_limit_can_split_and_caps_at_three(self):
        service = self._service(max_len=80)
        text = ("A" * 60) + "。不过" + ("B" * 15) + "。"

        artifact = service._build_visible_reply_artifact(text)

        self.assertGreater(len(artifact.segments), 1)
        self.assertLessEqual(len(artifact.segments), 3)
        self.assertEqual(artifact.metadata["segment_reason"], "natural_segmenter")

    def test_reply_at_or_above_segment_limit_stays_single(self):
        service = self._service(max_len=80)

        at_limit = service._build_visible_reply_artifact("A" * 80)
        above_limit = service._build_visible_reply_artifact("B" * 81)

        self.assertEqual(at_limit.segments, ["A" * 80])
        self.assertEqual(above_limit.segments, ["B" * 81])
        self.assertEqual(at_limit.metadata["segment_reason"], "at_or_above_segment_limit")
        self.assertEqual(above_limit.metadata["segment_reason"], "at_or_above_segment_limit")

    def test_reply_below_minimum_segment_length_stays_single(self):
        service = self._service(min_len=15, max_len=120)

        artifact = service._build_visible_reply_artifact("短句不会拆分")

        self.assertEqual(artifact.segments, ["短句不会拆分"])
        self.assertEqual(artifact.metadata["segment_reason"], "below_segment_min")

    def test_micro_utterance_reply_is_capped_and_sent_as_one_bubble(self):
        service = self._service(min_len=8, max_len=120)
        service.config.reply.humanlike_short_reply_enabled = True
        service.config.reply.short_reply_max_chars = 80
        service.config.reply.short_reply_max_sentences = 2
        service.config.reply.short_reply_allow_followup_question = False
        event = FakeEvent("user-1", "Alice", "哼哼哼")
        text = "哼什么呀！妃爱才没有闹。你是不是又在笑我？"

        artifact = service._build_visible_reply_artifact(text, event=event)

        self.assertEqual(len(artifact.segments), 1)
        self.assertEqual(artifact.metadata["segment_reason"], "humanlike_short_single")
        self.assertTrue(artifact.metadata["humanlike_short_reply_applied"])
        self.assertNotIn("你是不是又在笑我", artifact.visible_text)

    def test_micro_reply_constraint_does_not_touch_tool_result(self):
        service = self._service(min_len=8, max_len=120)
        event = FakeEvent("user-1", "Alice", "行")
        event.set_extra(
            "astrmai_reply_shape_policy",
            {
                "enabled": True,
                "mode": "micro",
                "reason": "known_micro_utterance",
                "max_chars": 40,
                "max_sentences": 1,
                "allow_followup_question": False,
            },
        )
        event.set_extra("astrmai_tool_execution_trace", [{"tool": "lookup", "status": "success"}])
        text = "工具已经查到第一条结果。第二条结果也需要完整保留。"

        artifact = service._build_visible_reply_artifact(text, event=event)

        self.assertIn("第二条结果也需要完整保留", artifact.visible_text)
        self.assertFalse(artifact.metadata["humanlike_short_reply_applied"])

    def test_micro_reply_constraint_is_disabled_for_emotional_support(self):
        service = self._service(min_len=8, max_len=120)
        event = FakeEvent("user-1", "Alice", "累困")
        text = "累了就先休息一下。妃爱会在这里陪着你。等缓过来再慢慢说也可以。"

        artifact = service._build_visible_reply_artifact(
            text,
            event=event,
            reply_mode=self.reply_mod.ReplyMode.EMOTIONAL_SUPPORT,
        )

        self.assertIn("等缓过来再慢慢说也可以", artifact.visible_text)
        self.assertFalse(artifact.metadata["humanlike_short_reply_applied"])

    def test_forced_paragraph_boundary_can_split_below_single_limit(self):
        service = self._service(max_len=200)

        artifact = service._build_visible_reply_artifact("First paragraph.\n\nSecond paragraph.")

        self.assertEqual(len(artifact.segments), 2)
        self.assertEqual(artifact.metadata["segment_reason"], "natural_segmenter")

    def test_forced_paragraph_boundary_splits_above_segment_limit(self):
        service = self._service(max_len=80)
        text = ("A" * 90) + "\n\n" + ("B" * 90)

        artifact = service._build_visible_reply_artifact(text)

        self.assertEqual(len(artifact.segments), 2)
        self.assertEqual(artifact.metadata["segment_reason"], "natural_segmenter")

    def test_segmenter_preserves_code_url_and_decimal_fragments(self):
        service = self._service(max_len=34)
        text = "Version is 3.14.15, check https://example.com/a.b?x=1, and keep ```a.b()``` intact."

        artifact = service._build_visible_reply_artifact(text)
        visible = "\n".join(artifact.segments)

        self.assertIn("3.14.15", visible)
        self.assertIn("https://example.com/a.b?x=1", visible)
        self.assertIn("```a.b()```", visible)

    def test_reply_modes_apply_human_segment_limits(self):
        service = self._service(max_len=32)
        text = "I can stay here with you for a bit. You do not need to explain everything at once. Catch your breath first."

        emotional = service._build_visible_reply_artifact(
            text,
            reply_mode=self.reply_mod.ReplyMode.EMOTIONAL_SUPPORT,
        )
        playful = service._build_visible_reply_artifact(
            text,
            reply_mode=self.reply_mod.ReplyMode.PLAYFUL_INTERACTION,
        )

        self.assertLessEqual(len(emotional.segments), 2)
        self.assertEqual(emotional.metadata["delay_profile"], "gentle")
        self.assertEqual(len(playful.segments), 1)

    def test_proactive_reply_defaults_to_low_segment_count(self):
        service = self._service(max_len=28)
        text = "I just thought of one more thing. We do not need to rush to a conclusion. We can keep this light."

        artifact = service._build_visible_reply_artifact(text, is_proactive=True)

        self.assertLessEqual(len(artifact.segments), 2)
        self.assertEqual(artifact.metadata["delay_profile"], "proactive")

    def test_guarded_stance_clamps_first_reply_length_and_trailing_question(self):
        service = self._service(max_len=200)
        event = FakeEvent("user-1", "Alice", "question")
        event.set_extra("astrmai_stance", "guarded")
        event.set_extra("astrmai_social_intent", "answer")
        text = "I can help with that. Let me lay out the key point first. Do you want me to keep going?"

        artifact = service._build_visible_reply_artifact(text, event=event)

        self.assertTrue(artifact.metadata["stance_clamp_applied"])
        self.assertEqual(artifact.metadata["stance"], "guarded")
        self.assertEqual(artifact.metadata["stance_social_intent"], "answer")
        self.assertLess(len(artifact.visible_text), len(text))
        self.assertNotIn("Do you want me to keep going?", artifact.visible_text)

    def test_neutral_stance_does_not_apply_first_reply_clamp(self):
        service = self._service(max_len=200)
        event = FakeEvent("user-1", "Alice", "question")
        event.set_extra("astrmai_stance", "neutral")
        text = "I can help with that. Let me lay out the key point first. Do you want me to keep going?"

        artifact = service._build_visible_reply_artifact(text, event=event)

        self.assertNotIn("stance_clamp_applied", artifact.metadata)
        self.assertIn("Do you want me to keep going?", artifact.visible_text)

    def test_guarded_boundary_uses_tighter_first_reply_caps_than_guarded_answer(self):
        service = self._service(max_len=200)
        text = "I can help with that. Let me lay out the key point first. Do you want me to keep going?"
        guarded_answer = FakeEvent("user-1", "Alice", "question")
        guarded_answer.set_extra("astrmai_stance", "guarded")
        guarded_answer.set_extra("astrmai_social_intent", "answer")
        guarded_boundary = FakeEvent("user-1", "Alice", "question")
        guarded_boundary.set_extra("astrmai_stance", "guarded")
        guarded_boundary.set_extra("astrmai_social_intent", "boundary")

        answer_artifact = service._build_visible_reply_artifact(text, event=guarded_answer)
        boundary_artifact = service._build_visible_reply_artifact(text, event=guarded_boundary)

        self.assertTrue(boundary_artifact.metadata["stance_clamp_applied"])
        self.assertEqual(boundary_artifact.metadata["stance_social_intent"], "boundary")
        self.assertLess(boundary_artifact.metadata["stance_char_cap"], answer_artifact.metadata["stance_char_cap"])
        self.assertLessEqual(len(boundary_artifact.visible_text), len(answer_artifact.visible_text))
        self.assertLessEqual(boundary_artifact.metadata["stance_sentence_cap"], answer_artifact.metadata["stance_sentence_cap"])
        self.assertEqual(boundary_artifact.metadata["stance_char_cap"], 28)
        self.assertEqual(answer_artifact.metadata["stance_char_cap"], 38)

    def test_cool_comfort_keeps_looser_cap_than_cool_answer_while_trimming_tail_question(self):
        service = self._service(max_len=200)
        text = "I hear you. Let me stay with the key part first. We can keep going gently. Do you want me to keep going?"
        cool_answer = FakeEvent("user-1", "Alice", "question")
        cool_answer.set_extra("astrmai_stance", "cool")
        cool_answer.set_extra("astrmai_social_intent", "answer")
        cool_comfort = FakeEvent("user-1", "Alice", "question")
        cool_comfort.set_extra("astrmai_stance", "cool")
        cool_comfort.set_extra("astrmai_social_intent", "comfort")

        answer_artifact = service._build_visible_reply_artifact(text, event=cool_answer)
        comfort_artifact = service._build_visible_reply_artifact(text, event=cool_comfort)

        self.assertTrue(answer_artifact.metadata["stance_clamp_applied"])
        self.assertTrue(comfort_artifact.metadata["stance_clamp_applied"])
        self.assertEqual(comfort_artifact.metadata["stance_social_intent"], "comfort")
        self.assertGreater(comfort_artifact.metadata["stance_char_cap"], answer_artifact.metadata["stance_char_cap"])
        self.assertNotIn("Do you want me to keep going?", comfort_artifact.visible_text)
        self.assertGreaterEqual(len(comfort_artifact.visible_text), len(answer_artifact.visible_text))
        self.assertEqual(answer_artifact.metadata["stance_char_cap"], 60)
        self.assertEqual(comfort_artifact.metadata["stance_char_cap"], 72)

    def test_successful_reply_feeds_memory_buffer_after_send(self):
        state_engine = FakeStateEngine()
        state_engine.config.reply.typing_speed_factor = 0.0
        memory_engine = self._build_memory_engine_runtime(threshold=100)
        pipeline = memory_engine.memory_pipeline
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            memory_engine=memory_engine,
        )
        service._settle_post_send = _noop_post_send

        first = FakeEvent("user-1", "Alice", "turn-1")
        second = FakeEvent("user-1", "Alice", "turn-2")

        async def _run():
            await service.handle_reply(first, "reply-1", first.unified_msg_origin)
            after_first = await pipeline.describe_session_eligibility(first.unified_msg_origin)
            await service.handle_reply(second, "reply-2", second.unified_msg_origin)
            memory_engine.session_summarizer.config.memory.summary_threshold = 2
            after_second = await pipeline.describe_session_eligibility(second.unified_msg_origin)
            return after_first, after_second

        after_first, after_second = asyncio.run(_run())

        self.assertEqual(after_first["reason"], "below_threshold")
        self.assertTrue(after_first["candidate_present"])
        self.assertEqual(after_second["reason"], "eligible")
        self.assertTrue(after_second["eligible"])

    def test_failed_send_does_not_feed_memory_buffer(self):
        state_engine = FakeStateEngine()
        memory_engine = self._build_memory_engine_runtime(threshold=2)
        pipeline = memory_engine.memory_pipeline
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            memory_engine=memory_engine,
        )

        async def _send_fail(*args, **kwargs):
            return False

        service._send_segments = _send_fail
        service._settle_post_send = _noop_post_send
        event = FakeEvent("user-1", "Alice", "send-failed")

        mirrored = []

        async def _mirror(**kwargs):
            mirrored.append(kwargs)

        service._sync_native_history_mirror = _mirror
        artifact = asyncio.run(service.handle_reply(event, "will-not-write-memory", event.unified_msg_origin))

        self.assertNotIn(event.unified_msg_origin, pipeline._session_history_buffer)
        self.assertEqual(mirrored, [])
        self.assertFalse(artifact.sent)
        self.assertEqual(artifact.blocked_reason, "send_failed")

    def test_successful_send_commits_group_dialogue_after_delivery(self):
        from astrmai.conversation.attention.group_dialogue_store import (
            GroupDialogueStore,
        )

        state_engine = FakeStateEngine()
        state_engine.config.reply.typing_speed_factor = 0.0
        store = GroupDialogueStore()
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            dialogue_store=store,
        )
        service._settle_post_send = _noop_post_send
        event = FakeEvent("user-1", "Alice", "question")

        async def _run():
            artifact = await service.handle_reply(
                event,
                "delivered reply",
                event.unified_msg_origin,
            )
            turns = await store.get_recent_bot_turns(
                event.unified_msg_origin,
                target_sender_id="user-1",
            )
            return artifact, turns

        artifact, turns = asyncio.run(_run())

        self.assertTrue(artifact.sent)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].reply_text, "delivered reply")
        self.assertEqual(turns[0].target_sender_id, "user-1")
        self.assertEqual(
            event.get_extra("astrmai_reply_commit_id"),
            turns[0].turn_id,
        )

    def test_image_only_vision_failure_skips_semantic_persistence_consumers(self):
        from astrmai.conversation.attention.group_dialogue_store import (
            GroupDialogueStore,
        )

        state_engine = FakeStateEngine()
        state_engine.config.reply.typing_speed_factor = 0.0
        store = GroupDialogueStore()
        memory_engine = self._build_memory_engine_runtime(threshold=2)
        evolution_manager = SimpleNamespace(process_bot_reply=AsyncMock())
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            dialogue_store=store,
            memory_engine=memory_engine,
            evolution_manager=evolution_manager,
        )
        service._settle_post_send = _noop_post_send
        service._sync_native_history_mirror = AsyncMock()
        event = FakeEvent("user-1", "Alice", "[图片]")
        event.set_extra("astrmai_media_status_nonsemantic", True)
        event.set_extra("astrmai_media_only_failure", True)

        async def _run():
            artifact = await service.handle_reply(
                event,
                "暂时无法确认图片内容",
                event.unified_msg_origin,
            )
            counts = await store.snapshot_counts(event.unified_msg_origin)
            return artifact, counts

        artifact, counts = asyncio.run(_run())

        self.assertTrue(artifact.sent)
        self.assertEqual(counts["segments"], 0)
        self.assertNotIn(
            event.unified_msg_origin,
            memory_engine.memory_pipeline._session_history_buffer,
        )
        service._sync_native_history_mirror.assert_not_awaited()
        evolution_manager.process_bot_reply.assert_not_awaited()
        self.assertEqual(
            artifact.metadata["commit_consumer_status"],
            {
                "group_dialogue": "skipped_nonsemantic_media",
                "native_history": "skipped_nonsemantic_media",
                "memory": "skipped_nonsemantic_media",
                "learning": "skipped_nonsemantic_media",
            },
        )

    def test_successful_proactive_reply_updates_bot_watermark_after_commit(self):
        state_engine = FakeStateEngine()
        state_engine.config.reply.typing_speed_factor = 0.0
        state_engine.record_committed_bot_reply = AsyncMock()
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
        )
        service._settle_post_send = _noop_post_send
        event = FakeEvent("user-1", "Alice", "proactive")
        event.set_extra("astrmai_is_proactive_event", True)

        artifact = asyncio.run(
            service.handle_reply(
                event,
                "主动问候",
                event.unified_msg_origin,
            )
        )

        self.assertTrue(artifact.sent)
        commit_id = event.get_extra("astrmai_reply_commit_id")
        self.assertTrue(commit_id)
        state_engine.record_committed_bot_reply.assert_awaited_once()
        call = state_engine.record_committed_bot_reply.await_args
        self.assertEqual(call.args[0], event.unified_msg_origin)
        self.assertTrue(call.kwargs["is_proactive"])
        self.assertEqual(call.kwargs["commit_id"], commit_id)

    def test_failed_send_never_commits_group_dialogue(self):
        from astrmai.conversation.attention.group_dialogue_store import (
            GroupDialogueStore,
        )

        state_engine = FakeStateEngine()
        store = GroupDialogueStore()
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            dialogue_store=store,
        )

        async def _send_fail(*args, **kwargs):
            return False

        service._send_segments = _send_fail
        service._settle_post_send = _noop_post_send
        event = FakeEvent("user-1", "Alice", "question")

        async def _run():
            artifact = await service.handle_reply(
                event,
                "undelivered draft",
                event.unified_msg_origin,
            )
            counts = await store.snapshot_counts(event.unified_msg_origin)
            return artifact, counts

        artifact, counts = asyncio.run(_run())

        self.assertFalse(artifact.sent)
        self.assertEqual(counts["segments"], 0)
        self.assertIsNone(event.get_extra("astrmai_reply_commit_id"))

    def test_partial_segment_send_is_committed_and_does_not_trigger_model_retry(self):
        from astrmai.conversation.contracts.turn_identity import TurnIdentity

        state_engine = FakeStateEngine()
        state_engine.config.reply.typing_speed_factor = 0.0
        coordinator = _ClaimingRuntimeCoordinator()
        calls = []

        async def _partial_send(*args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == 2:
                raise RuntimeError("second segment failed")
            return "msg-1"

        state_engine.gateway.context.send_message = _partial_send
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            runtime_coordinator=coordinator,
        )
        service._settle_post_send = _noop_post_send
        event = FakeEvent("user-1", "Alice", "question")
        event.set_extra(
            "astrmai_turn_identity",
            TurnIdentity(
                mode="group",
                chat_id=event.unified_msg_origin,
                thread_id=event.unified_msg_origin,
                generation=1,
            ),
        )

        artifact = service._build_visible_reply_artifact("first\n\nsecond", event=event)
        service._build_visible_reply_artifact = lambda *args, **kwargs: artifact
        result = asyncio.run(service.handle_reply(event, "first\n\nsecond", event.unified_msg_origin))

        self.assertTrue(result.sent)
        self.assertEqual(result.metadata["send_status"], "partial_sent")
        self.assertEqual(result.persistable_text, "first")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(coordinator.commits), 1)
        self.assertEqual(coordinator.commits[0][2], ["msg-1"])

    def test_partial_segment_send_commits_only_delivered_group_text(self):
        from astrmai.conversation.attention.group_dialogue_store import (
            GroupDialogueStore,
        )

        state_engine = FakeStateEngine()
        state_engine.config.reply.typing_speed_factor = 0.0
        store = GroupDialogueStore()
        calls = 0

        async def _partial_send(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("second segment failed")
            return "msg-1"

        state_engine.gateway.context.send_message = _partial_send
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            dialogue_store=store,
        )
        service._settle_post_send = _noop_post_send
        event = FakeEvent("user-1", "Alice", "question")
        artifact = service._build_visible_reply_artifact(
            "first\n\nsecond",
            event=event,
        )
        service._build_visible_reply_artifact = lambda *args, **kwargs: artifact

        async def _run():
            result = await service.handle_reply(
                event,
                "first\n\nsecond",
                event.unified_msg_origin,
            )
            turns = await store.get_recent_bot_turns(
                event.unified_msg_origin,
                target_sender_id="user-1",
            )
            return result, turns

        result, turns = asyncio.run(_run())

        self.assertTrue(result.sent)
        self.assertEqual(result.metadata["send_status"], "partial_sent")
        self.assertEqual(result.persistable_text, "first")
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].reply_text, "first")

    def test_stale_during_segmented_send_persists_only_delivered_text(self):
        state_engine = FakeStateEngine()
        state_engine.config.reply.typing_speed_factor = 0.0
        sent = []

        async def _send(_origin, chain):
            sent.append(chain)
            return f"msg-{len(sent)}"

        state_engine.gateway.context.send_message = _send
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
        )
        service._settle_post_send = _noop_post_send
        freshness_checks = 0

        async def _freshness(*args, **kwargs):
            nonlocal freshness_checks
            freshness_checks += 1
            if freshness_checks >= 3:
                return self.reply_mod.FreshnessState.EXPIRED, "newer_turn"
            return self.reply_mod.FreshnessState.FRESH, ""

        service._check_reply_freshness = _freshness
        event = FakeEvent("user-1", "Alice", "question")
        mirrored = []

        async def _mirror(**kwargs):
            mirrored.append(kwargs)

        service._sync_native_history_mirror = _mirror
        artifact = service._build_visible_reply_artifact("first\n\nsecond", event=event)
        service._build_visible_reply_artifact = lambda *args, **kwargs: artifact

        result = asyncio.run(service.handle_reply(event, "first\n\nsecond", event.unified_msg_origin))

        self.assertTrue(result.sent)
        self.assertEqual(result.metadata["send_status"], "partial_sent")
        self.assertEqual(result.metadata["sent_segment_count"], 1)
        self.assertEqual(result.persistable_text, "first")
        self.assertEqual(len(sent), 1)
        self.assertEqual(mirrored[0]["assistant_text"], "first")

    def test_final_and_follow_up_have_independent_exactly_once_claims(self):
        from astrmai.conversation.contracts.turn_identity import TurnIdentity

        state_engine = FakeStateEngine()
        state_engine.config.reply.typing_speed_factor = 0.0
        coordinator = _ClaimingRuntimeCoordinator()
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            runtime_coordinator=coordinator,
        )
        event = FakeEvent("user-1", "Alice", "question")
        event.set_extra(
            "astrmai_turn_identity",
            TurnIdentity(
                mode="group",
                chat_id=event.unified_msg_origin,
                thread_id=event.unified_msg_origin,
                generation=7,
            ),
        )

        async def _run():
            first = service._build_visible_reply_artifact("first", event=event)
            first_sent = await service._send_segments(event, event.unified_msg_origin, first, [])
            event.set_extra("astrmai_response_kind", "follow_up")
            follow = service._build_visible_reply_artifact("follow", event=event)
            follow_sent = await service._send_segments(event, event.unified_msg_origin, follow, [])
            duplicate = service._build_visible_reply_artifact("duplicate", event=event)
            duplicate_sent = await service._send_segments(event, event.unified_msg_origin, duplicate, [])
            return first_sent, follow_sent, duplicate_sent

        first_sent, follow_sent, duplicate_sent = asyncio.run(_run())

        self.assertTrue(first_sent)
        self.assertTrue(follow_sent)
        self.assertFalse(duplicate_sent)
        self.assertEqual(len(coordinator.commits), 2)
        self.assertTrue(coordinator.claim_calls[0].endswith(":final"))
        self.assertTrue(coordinator.claim_calls[1].endswith(":follow_up"))
        self.assertEqual(coordinator.claim_calls[1], coordinator.claim_calls[2])

    def test_failed_send_triggers_light_no_send_affection_settlement(self):
        state_engine = FakeStateEngine()
        observed = {}
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
        )

        async def _send_fail(*args, **kwargs):
            return False

        async def _capture_no_send_affection(**kwargs):
            observed.update(kwargs)
            return True

        state_engine.settle_no_send_affection = _capture_no_send_affection
        service._send_segments = _send_fail
        event = FakeEvent("user-1", "Alice", "你这个废物")

        asyncio.run(service.handle_reply(event, "visible reply", event.unified_msg_origin))

        self.assertEqual(observed["user_id"], "user-1")
        self.assertEqual(observed["group_id"], event.unified_msg_origin)
        self.assertEqual(observed["message_text"], "你这个废物")
        self.assertEqual(observed["skipped_reason"], "send_failed")

    def test_proactive_reply_does_not_feed_memory_buffer(self):
        state_engine = FakeStateEngine()
        memory_engine = self._build_memory_engine_runtime(threshold=2)
        pipeline = memory_engine.memory_pipeline
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            memory_engine=memory_engine,
        )
        service._settle_post_send = _noop_post_send
        event = FakeEvent("user-1", "Alice", "proactive-message")
        event.set_extra("astrmai_is_proactive_event", True)

        asyncio.run(service.handle_reply(event, "proactive-reply", event.unified_msg_origin))

        self.assertNotIn(event.unified_msg_origin, pipeline._session_history_buffer)

    def test_publish_turn_committed_failure_still_keeps_memory_buffer(self):
        state_engine = FakeStateEngine()
        state_engine.config.reply.typing_speed_factor = 0.0
        memory_engine = self._build_memory_engine_runtime(threshold=100)
        pipeline = memory_engine.memory_pipeline
        published_turns = []

        async def _publish_fail(turn):
            published_turns.append(turn.chat_id)
            raise RuntimeError("queue dropped")

        pipeline.publish_turn_committed = _publish_fail
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            memory_engine=memory_engine,
        )
        service._settle_post_send = _noop_post_send
        event = FakeEvent("user-1", "Alice", "turn-publish-fail")

        asyncio.run(service.handle_reply(event, "reply-visible", event.unified_msg_origin))

        self.assertEqual(published_turns, [event.unified_msg_origin])
        self.assertIn(event.unified_msg_origin, pipeline._session_history_buffer)
        self.assertEqual(
            pipeline._session_history_buffer[event.unified_msg_origin]["buffer"],
            ["用户/旁白：Alice: turn-publish-fail", "Bot：reply-visible"],
        )

    def test_instant_gate_failure_does_not_break_visible_reply_or_buffer(self):
        state_engine = FakeStateEngine()
        state_engine.config.reply.typing_speed_factor = 0.0
        memory_engine = self._build_memory_engine_runtime(threshold=100)
        pipeline = memory_engine.memory_pipeline
        sent = []

        async def _instant_gate_fail(_turn):
            raise RuntimeError("instant gate failed")

        async def _capture_send(origin, reply_chain):
            sent.append((origin, reply_chain))
            return True

        pipeline.process_instant_gate = _instant_gate_fail
        state_engine.gateway.context.send_message = _capture_send
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            memory_engine=memory_engine,
        )
        service._settle_post_send = _noop_post_send
        event = FakeEvent("user-1", "Alice", "turn-instant-fail")

        asyncio.run(service.handle_reply(event, "reply-visible", event.unified_msg_origin))

        self.assertTrue(event.get_extra("astrmai_reply_sent", False))
        self.assertTrue(sent)
        self.assertIn(event.unified_msg_origin, pipeline._session_history_buffer)
        self.assertEqual(len(pipeline._session_history_buffer[event.unified_msg_origin]["buffer"]), 2)

    def test_post_send_affection_uses_anchor_message_text_for_event_classification(self):
        state_engine = FakeStateEngine()
        observed = {}

        async def _capture_affection(**kwargs):
            observed.update(kwargs)
            return None

        state_engine.calculate_and_update_affection = _capture_affection
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
        )
        class _PrivateEvent(FakeEvent):
            def __init__(self, sender_id, sender_name, text):
                super().__init__(sender_id, sender_name, text)
                self.unified_msg_origin = "default:FriendMessage:user-1"

            def get_group_id(self):
                return None

        event = _PrivateEvent("user-1", "Alice", "assistant-visible-reply")
        event.set_extra("astrmai_primary_mood_tag", "happy")
        anchor = _PrivateEvent("user-1", "Alice", "thank you, you are amazing")

        asyncio.run(
            service._settle_post_send(
                event,
                event.unified_msg_origin,
                bypassed_tag="happy",
                window_events=[event],
                anchor_event=anchor,
            )
        )

        self.assertEqual(observed["user_id"], "user-1")
        self.assertEqual(observed["mood_tag"], "happy")
        self.assertEqual(observed["message_text"], "thank you, you are amazing")

    def test_post_send_proactive_event_does_not_mutate_affection_with_synthetic_text(self):
        state_engine = FakeStateEngine()
        observed = {"calls": 0}

        async def _capture_affection(**kwargs):
            observed["calls"] += 1
            observed["payload"] = kwargs
            return None

        state_engine.calculate_and_update_affection = _capture_affection
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
        )
        event = FakeEvent("user-1", "Alice", "你可以去关心一下她今天的状态")
        event.set_extra("astrmai_is_proactive_event", True)

        asyncio.run(
            service._settle_post_send(
                event,
                event.unified_msg_origin,
                bypassed_tag="happy",
                window_events=[event],
                anchor_event=None,
            )
        )

        self.assertEqual(observed["calls"], 0)

    def test_post_send_meme_tag_uses_configured_probability(self):
        state_engine = FakeStateEngine()
        state_engine.config.reply.meme_probability = 37
        state_engine.config.reply.emotion_mapping = ["excited: 兴奋、期待、庆祝"]
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
        )
        event = FakeEvent("user-1", "Alice", "今天值得庆祝")

        async def _no_affection_target(*_args, **_kwargs):
            return None

        service._collect_affection_target = _no_affection_target

        send_meme = AsyncMock(return_value=True)
        with patch("astrmai.conversation.execution.reply_post_send.send_meme", new=send_meme):
            asyncio.run(
                service._settle_post_send(
                    event,
                    event.unified_msg_origin,
                    bypassed_tag="excited",
                    window_events=[event],
                    anchor_event=event,
                )
            )

        self.assertEqual(send_meme.await_args.kwargs["emotion_tag"], "excited")
        self.assertEqual(send_meme.await_args.kwargs["probability"], 37)
        self.assertEqual(event.get_extra("astrmai_meme_tag_validation"), "configured")

    def test_post_send_unknown_meme_tag_is_suppressed(self):
        state_engine = FakeStateEngine()
        state_engine.config.reply.emotion_mapping = ["happy: 开心"]
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
        )
        event = FakeEvent("user-1", "Alice", "随便发一个")

        async def _no_affection_target(*_args, **_kwargs):
            return None

        service._collect_affection_target = _no_affection_target
        send_meme = AsyncMock(return_value=True)
        with patch("astrmai.conversation.execution.reply_post_send.send_meme", new=send_meme):
            asyncio.run(
                service._settle_post_send(
                    event,
                    event.unified_msg_origin,
                    bypassed_tag="not-configured",
                    window_events=[event],
                    anchor_event=event,
                )
            )

        send_meme.assert_not_awaited()
        self.assertEqual(event.get_extra("astrmai_meme_tag"), "neutral")
        self.assertEqual(event.get_extra("astrmai_meme_tag_validation"), "unknown_tag")

    def test_handle_reply_does_not_use_primary_mood_tag_as_bot_expression(self):
        state_engine = FakeStateEngine()
        state_engine.config.reply.meme_probability = 80
        state_engine.config.reply.emotion_mapping = ["surprised: 惊讶、震惊、意外"]
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
        )
        event = FakeEvent("user-1", "Alice", "真的假的")
        event.set_extra("astrmai_primary_mood_tag", "surprised")

        async def _no_affection_target(*_args, **_kwargs):
            return None

        service._collect_affection_target = _no_affection_target
        send_meme = AsyncMock(return_value=True)
        with patch("astrmai.conversation.execution.reply_post_send.send_meme", new=send_meme):
            asyncio.run(service.handle_reply(event, "太意外了", event.unified_msg_origin))

        send_meme.assert_not_awaited()
        self.assertEqual(event.get_extra("astrmai_expression_disposition"), "no_expression_decision")

    def test_handle_reply_does_not_turn_user_happy_mood_into_meme(self):
        state_engine = FakeStateEngine()
        state_engine.config.reply.meme_probability = 80
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
        )
        event = FakeEvent("user-1", "Alice", "今天真开心")
        event.set_extra("astrmai_primary_mood_tag", "happy")

        async def _no_affection_target(*_args, **_kwargs):
            return None

        service._collect_affection_target = _no_affection_target
        send_meme = AsyncMock(return_value=True)
        with patch("astrmai.conversation.execution.reply_post_send.send_meme", new=send_meme):
            asyncio.run(
                service.handle_reply(
                    event,
                    "我也很开心呀",
                    event.unified_msg_origin,
                )
            )

        send_meme.assert_not_awaited()
        self.assertEqual(event.get_extra("astrmai_expression_source"), "none")

    def test_handle_reply_sends_only_explicit_bot_expression_decision(self):
        state_engine = FakeStateEngine()
        state_engine.config.reply.meme_probability = 80
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
        )
        event = FakeEvent("user-1", "Alice", "nice work")
        event.set_extra(
            "astrmai_bot_expression_decision",
            {"expression_tag": "happy", "source": "explicit_tool", "force": True},
        )

        async def _no_affection_target(*_args, **_kwargs):
            return None

        service._collect_affection_target = _no_affection_target
        send_meme = AsyncMock(return_value=True)
        with patch("astrmai.conversation.execution.reply_post_send.send_meme", new=send_meme):
            asyncio.run(service.handle_reply(event, "thanks", event.unified_msg_origin))

        self.assertEqual(send_meme.await_args.kwargs["emotion_tag"], "happy")
        self.assertEqual(send_meme.await_args.kwargs["probability"], 100)
        self.assertEqual(event.get_extra("astrmai_expression_source"), "explicit_tool")

    def test_expression_decision_is_suppressed_by_cooldown_or_long_reply(self):
        state_engine = FakeStateEngine()
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
        )
        event = FakeEvent("user-1", "Alice", "hello")
        event.set_extra(
            "astrmai_bot_expression_decision",
            {"expression_tag": "happy", "source": "explicit_tool", "force": True},
        )
        event.set_extra("astrmai_cooldown_tags", ["meme"])

        tag, force, source, disposition = service._resolve_expression_decision(event, None, "short reply")
        self.assertEqual((tag, force, source, disposition), ("neutral", False, "explicit_tool", "cooldown"))

        event.set_extra("astrmai_cooldown_tags", [])
        tag, force, source, disposition = service._resolve_expression_decision(event, None, "x" * 160)
        self.assertEqual((tag, force, source, disposition), ("neutral", False, "explicit_tool", "long_reply"))

    def test_handle_reply_prefers_bypassed_mood_tag_over_primary_mood_tag(self):
        state_engine = FakeStateEngine()
        state_engine.config.reply.meme_probability = 80
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
        )
        event = FakeEvent("user-1", "Alice", "今天真开心")
        event.set_extra("astrmai_bypass_mood_analysis", "angry")
        event.set_extra("astrmai_primary_mood_tag", "happy")

        async def _no_affection_target(*_args, **_kwargs):
            return None

        service._collect_affection_target = _no_affection_target
        send_meme = AsyncMock(return_value=True)
        with patch("astrmai.conversation.execution.reply_post_send.send_meme", new=send_meme):
            asyncio.run(
                service.handle_reply(
                    event,
                    "我知道啦",
                    event.unified_msg_origin,
                )
            )

        self.assertEqual(send_meme.await_args.kwargs["emotion_tag"], "angry")
        self.assertEqual(send_meme.await_args.kwargs["probability"], 80)

    def test_explicit_meme_force_uses_one_hundred_percent_probability(self):
        state_engine = FakeStateEngine()
        state_engine.config.reply.meme_probability = 37
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
        )
        event = FakeEvent("user-1", "Alice", "给我发张开心的表情包")
        event.set_extra("astrmai_force_meme", True)

        async def _no_affection_target(*_args, **_kwargs):
            return None

        service._collect_affection_target = _no_affection_target
        send_meme = AsyncMock(return_value=True)
        with patch("astrmai.conversation.execution.reply_post_send.send_meme", new=send_meme):
            asyncio.run(
                service._settle_post_send(
                    event,
                    event.unified_msg_origin,
                    bypassed_tag="happy",
                    window_events=[event],
                    anchor_event=event,
                )
            )

        self.assertEqual(send_meme.await_args.kwargs["probability"], 100)

    def test_merge_wait_targets_preserves_existing_targets_before_pending_actions(self):
        service = self._service()
        event = FakeEvent("user-1", "Alice", "question")
        event.set_extra("astrmai_wait_targets", ["user-1"])
        pending_actions = [
            {"action": "at", "target_id": "user-2", "target_name": "Bob", "group_id": "group-1", "verified_current_group": True},
            {"action": "at", "target_id": "user-1", "target_name": "Alice", "group_id": "group-1", "verified_current_group": True},
        ]

        merged = service._merge_wait_targets(event, pending_actions)

        self.assertEqual(merged, ["user-1", "user-2"])
        self.assertEqual(event.get_extra("astrmai_wait_targets"), ["user-1", "user-2"])
        self.assertEqual(event.get_extra("astrmai_wait_target_name"), "Bob")

    def test_native_at_removes_duplicate_text_marker_for_same_target(self):
        service = self._service()
        event = FakeEvent("user-1", "Alice", "question")
        event.set_extra(
            "astrmai_pending_actions",
            [
                {
                    "action": "at",
                    "target_id": "3650815443",
                    "target_name": "萤",
                    "requested_target_name": "萤",
                }
            ],
        )

        cleaned = service._strip_duplicate_native_at_text(
            event,
            "找到啦～ @萤 快出来冒个泡吧！",
            ["3650815443"],
        )

        self.assertEqual(cleaned, "找到啦～ 快出来冒个泡吧！")

    def test_merge_wait_targets_ignores_unverified_or_other_group_at_actions(self):
        service = self._service()
        event = FakeEvent("user-1", "Alice", "question")

        merged = service._merge_wait_targets(
            event,
            [
                {"action": "at", "target_id": "1", "group_id": "group-1"},
                {"action": "at", "target_id": "2", "group_id": "other", "verified_current_group": True},
            ],
        )

        self.assertEqual(merged, [])


if __name__ == "__main__":
    unittest.main()
