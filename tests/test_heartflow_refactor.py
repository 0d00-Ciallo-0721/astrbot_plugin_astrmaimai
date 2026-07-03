import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _FakeStateEngine:
    def __init__(self, energy=0.8, mood=0.0):
        self.energy = energy
        self.mood = mood

    async def get_state(self, chat_id):
        return SimpleNamespace(chat_id=chat_id, energy=self.energy, mood=self.mood)


class _FakeMemory:
    def __init__(self):
        self.feedback_calls = []

    async def record_cognitive_feedback(self, **kwargs):
        self.feedback_calls.append(kwargs)


class _FakeGateway:
    def __init__(self):
        self.calls = []

    async def call_data_process_task(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, "kwargs": kwargs})
        return {
            "action": "reply",
            "memory_policy": "light",
            "social_intent": "answer",
            "action_tier": "chat",
        }


class _FakeEvent:
    def __init__(self, text="what should we do now?"):
        self.message_str = text
        self.unified_msg_origin = "chat-1"
        self._extra = {}

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_sender_id(self):
        return "user-1"

    def get_sender_name(self):
        return "Alice"


class HeartflowRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        for name in [
            "astrmai.infrastructure.runtime.chat_runtime_coordinator",
            "astrmai.proactive.heartflow.models",
            "astrmai.proactive.heartflow.feedback_bridge",
            "astrmai.proactive.heartflow.manager",
            "astrmai.proactive.heartflow",
            "astrmai.proactive.rhythm",
            "astrmai.conversation.planning.cognitive_loop",
        ]:
            sys.modules.pop(name, None)
        self.coordinator_mod = importlib.import_module("astrmai.infrastructure.runtime.chat_runtime_coordinator")
        self.heartflow_mod = importlib.import_module("astrmai.proactive.heartflow")
        self.loop_mod = importlib.import_module("astrmai.conversation.planning.cognitive_loop")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_heartflow_tick_ignores_empty_runtime(self):
        coordinator = self.coordinator_mod.ChatRuntimeCoordinator()
        manager = self.heartflow_mod.HeartflowManager(
            runtime_coordinator=coordinator,
            state_engine=_FakeStateEngine(),
            memory_engine=_FakeMemory(),
        )

        asyncio.run(manager.tick(now=time.time()))

        status = manager.describe_status()
        self.assertEqual(status["active_chats"], 0)
        self.assertEqual(status["pending_pulses"], 0)

    def test_heartflow_builds_state_for_active_chat(self):
        coordinator = self.coordinator_mod.ChatRuntimeCoordinator()
        now = time.time()

        async def _seed():
            await coordinator.mark_activity("chat-1", now - 50, "u1", "Alice", "hello")
            await coordinator.mark_activity("chat-1", now - 35, "u1", "Alice", "still here")
            await coordinator.mark_activity("chat-1", now - 20, "u1", "Alice", "what now?")

        asyncio.run(_seed())
        manager = self.heartflow_mod.HeartflowManager(
            runtime_coordinator=coordinator,
            state_engine=_FakeStateEngine(energy=0.8, mood=0.2),
            memory_engine=_FakeMemory(),
        )

        asyncio.run(manager.tick(now=now))

        state = manager.get_state("chat-1")
        self.assertIsNotNone(state)
        self.assertGreater(state.interest, 0.70)
        self.assertGreater(state.engagement, 0.50)

    def test_no_reply_counter_decays_after_threshold(self):
        manager_mod = importlib.import_module("astrmai.proactive.heartflow.manager")
        models_mod = importlib.import_module("astrmai.proactive.heartflow.models")
        manager = manager_mod.HeartflowManager(
            runtime_coordinator=self.coordinator_mod.ChatRuntimeCoordinator(),
            state_engine=_FakeStateEngine(),
            memory_engine=_FakeMemory(),
        )
        manager._sessions["chat-1"] = models_mod.HeartflowSessionState(
            chat_id="chat-1",
            started_at=100.0,
            last_activity_ts=100.0,
            last_tick_ts=100.0,
            expires_at=1000.0,
            consecutive_no_reply_count=3,
        )
        decision = models_mod.HeartflowActionDecision(
            chat_id="chat-1",
            timestamp=120.0,
            action_type="no_reply",
            reason="insert pressure",
            guidance="stay quiet",
            blocked_reason="insert_pressure",
        )

        manager._remember_action_decision(decision)

        self.assertEqual(manager.get_session("chat-1").consecutive_no_reply_count, 2)

    def test_no_reply_counter_decays_on_non_reply_actions(self):
        manager_mod = importlib.import_module("astrmai.proactive.heartflow.manager")
        models_mod = importlib.import_module("astrmai.proactive.heartflow.models")
        manager = manager_mod.HeartflowManager(
            runtime_coordinator=self.coordinator_mod.ChatRuntimeCoordinator(),
            state_engine=_FakeStateEngine(),
            memory_engine=_FakeMemory(),
        )
        manager._sessions["chat-1"] = models_mod.HeartflowSessionState(
            chat_id="chat-1",
            started_at=100.0,
            last_activity_ts=100.0,
            last_tick_ts=100.0,
            expires_at=1000.0,
            consecutive_no_reply_count=4,
        )

        for action in ("observe", "wait", "cool_down", "complete_topic"):
            manager._remember_action_decision(
                models_mod.HeartflowActionDecision(
                    chat_id="chat-1",
                    timestamp=120.0,
                    action_type=action,
                    reason="release",
                    guidance="release silence",
                )
            )

        self.assertEqual(manager.get_session("chat-1").consecutive_no_reply_count, 0)

    def test_heartflow_creates_session_and_records_hidden_action(self):
        coordinator = self.coordinator_mod.ChatRuntimeCoordinator()
        now = time.time()

        async def _seed():
            await coordinator.mark_activity("chat-1", now - 40, "u1", "Alice", "hello")
            await coordinator.mark_activity("chat-1", now - 20, "u1", "Alice", "still here?")

        asyncio.run(_seed())
        manager = self.heartflow_mod.HeartflowManager(
            runtime_coordinator=coordinator,
            state_engine=_FakeStateEngine(energy=0.8, mood=0.1),
            memory_engine=_FakeMemory(),
        )

        asyncio.run(manager.tick(now=now))

        session = manager.get_session("chat-1")
        action = manager.get_latest_action_decision("chat-1")
        self.assertIsNotNone(session)
        self.assertIsNotNone(action)
        self.assertEqual(session.tick_count, 1)
        self.assertGreater(session.topic_heat, 0.0)
        self.assertIn(action.action_type, {"observe", "prepare_reply"})
        hidden = manager.get_hidden_context("chat-1")
        self.assertIn("session=", hidden)
        self.assertIn("latest_heartflow_action=", hidden)

    def test_heartflow_low_cost_session_does_not_dispatch_candidate(self):
        class _Dispatcher:
            def __init__(self):
                self.intents = []

            async def dispatch(self, intent):
                self.intents.append(intent)
                return SimpleNamespace(allowed=True, synthetic_event_queued=True, safety_checks={})

        coordinator = self.coordinator_mod.ChatRuntimeCoordinator()
        now = time.time()

        async def _seed():
            await coordinator.mark_activity("chat-1", now - 900, "u1", "Alice", "old but interesting?")

        asyncio.run(_seed())
        dispatcher = _Dispatcher()
        manager = self.heartflow_mod.HeartflowManager(
            runtime_coordinator=coordinator,
            state_engine=_FakeStateEngine(energy=0.9, mood=0.2),
            memory_engine=_FakeMemory(),
            dispatcher=dispatcher,
        )

        asyncio.run(manager.tick(now=now))

        session = manager.get_session("chat-1")
        action = manager.get_latest_action_decision("chat-1")
        self.assertTrue(session.low_cost_retained)
        self.assertIn(action.action_type, {"observe", "complete_topic"})
        self.assertFalse(action.should_dispatch_candidate)
        self.assertEqual(len(dispatcher.intents), 0)

    def test_heartflow_low_energy_prefers_observe(self):
        coordinator = self.coordinator_mod.ChatRuntimeCoordinator()
        now = time.time()

        async def _seed():
            await coordinator.mark_activity("chat-1", now - 1600, "u1", "Alice", "old message")

        asyncio.run(_seed())
        manager = self.heartflow_mod.HeartflowManager(
            runtime_coordinator=coordinator,
            state_engine=_FakeStateEngine(energy=0.1, mood=-0.4),
            memory_engine=_FakeMemory(),
        )

        asyncio.run(manager.tick(now=now))

        state = manager.get_state("chat-1")
        pulse = manager.get_latest_pulse("chat-1")
        self.assertLess(state.talk_willingness, 0.25)
        self.assertEqual(state.recent_impulse, "observe")
        self.assertEqual(pulse.suggested_action_tier, "none")
        decision = manager.get_latest_impulse_decision("chat-1")
        self.assertIsNotNone(decision)
        self.assertTrue(decision.hidden_only)
        self.assertFalse(decision.visible_candidate_allowed)
        self.assertEqual(decision.blocked_reason, "cool_down")
        self.assertIn("low_talk", manager.get_hidden_context("chat-1"))

    def test_heartflow_wait_action_blocks_dispatch(self):
        manager = self.heartflow_mod.HeartflowManager()
        now = time.time()
        session = self.heartflow_mod.HeartflowSessionState(
            chat_id="chat-1",
            started_at=now - 20,
            last_activity_ts=now - 10,
            last_tick_ts=now,
            expires_at=now + 290,
            tick_count=1,
            topic_heat=0.8,
            direct_relevance=0.6,
        )
        manager._sessions["chat-1"] = session
        state = self.heartflow_mod.HeartflowChatState(
            chat_id="chat-1",
            last_tick_ts=now,
            last_activity_ts=now - 10,
            interest=0.9,
            engagement=0.6,
            talk_willingness=0.8,
            silence_pressure=0.9,
            fatigue=0.1,
            mood_bias=0.4,
            current_focus="fresh question?",
            recent_impulse="proactive_hint",
        )
        pulse = self.heartflow_mod.HeartflowPulse(
            chat_id="chat-1",
            timestamp=now,
            pulse_type="proactive_hint",
            reason="high silence pressure",
            guidance="maybe join",
            suggested_social_intent="join",
            suggested_action_tier="chat",
            urgency=0.8,
        )

        action = manager._build_action_decision(
            session,
            state,
            pulse,
            {"latest_activity_ts": now - 10, "wait_targets": ["u1"], "executor_pending": 0},
            now=now,
        )
        decision = manager._build_impulse_decision(
            session,
            state,
            pulse,
            {"latest_activity_ts": now - 10, "wait_targets": [], "executor_pending": 0},
            now=now,
        )
        decision = manager._apply_action_to_impulse_decision(action, decision)

        self.assertEqual(action.action_type, "wait")
        self.assertFalse(decision.visible_candidate_allowed)
        self.assertEqual(decision.blocked_reason, "user_waiting")

    def test_heartflow_proactive_hint_can_become_visible_candidate_but_not_dispatch(self):
        manager = self.heartflow_mod.HeartflowManager()
        now = time.time()
        session = self.heartflow_mod.HeartflowSessionState(
            chat_id="chat-1",
            started_at=now - 180,
            last_activity_ts=now - 1600,
            last_tick_ts=now,
            expires_at=now - 1300,
            tick_count=2,
            topic_heat=0.86,
            direct_relevance=0.62,
            low_cost_retained=True,
        )
        manager._sessions["chat-1"] = session
        state = self.heartflow_mod.HeartflowChatState(
            chat_id="chat-1",
            last_tick_ts=now,
            last_activity_ts=now - 1600,
            interest=0.92,
            engagement=0.2,
            talk_willingness=0.90,
            silence_pressure=0.95,
            fatigue=0.05,
            mood_bias=0.4,
            current_focus="quiet but still relevant",
            recent_impulse="proactive_hint",
        )
        pulse = self.heartflow_mod.HeartflowPulse(
            chat_id="chat-1",
            timestamp=now,
            pulse_type="proactive_hint",
            reason="silence pressure",
            guidance="There may be room to rejoin later.",
            suggested_social_intent="join",
            suggested_action_tier="chat",
            urgency=0.86,
        )

        decision = manager._build_impulse_decision(
            session,
            state,
            pulse,
            {"latest_activity_ts": now - 1600, "wait_targets": [], "executor_pending": 0},
            now=now,
        )

        self.assertTrue(decision.visible_candidate_allowed)
        self.assertTrue(decision.requires_synthetic_event)
        self.assertFalse(decision.hidden_only)
        self.assertFalse(decision.dispatch_enabled)
        self.assertFalse(decision.synthetic_event_queued)
        self.assertGreaterEqual(decision.safety_checks["visible_candidate_score"], 0.72)
        self.assertIn("Heartflow proactive_hint", decision.synthetic_event_preview)

    def test_heartflow_quiet_hours_blocks_visible_candidate(self):
        manager = self.heartflow_mod.HeartflowManager(
            config=SimpleNamespace(
                life=SimpleNamespace(proactive_quiet_hours=["23:30-07:30"]),
                reply=SimpleNamespace(base_frequency=0.7),
            )
        )
        now = time.mktime((2026, 5, 11, 23, 45, 0, 0, 0, -1))
        manager._sessions["chat-1"] = self.heartflow_mod.HeartflowSessionState(
            chat_id="chat-1",
            started_at=now - 180,
            last_activity_ts=now - 1500,
            last_tick_ts=now,
            expires_at=now - 1200,
            tick_count=2,
            topic_heat=0.92,
            direct_relevance=0.7,
        )
        state = self.heartflow_mod.HeartflowChatState(
            chat_id="chat-1",
            last_tick_ts=now,
            last_activity_ts=now - 1500,
            interest=0.92,
            engagement=0.2,
            talk_willingness=0.90,
            silence_pressure=0.90,
            fatigue=0.05,
            mood_bias=0.4,
            current_focus="quiet but still relevant",
            recent_impulse="proactive_hint",
        )
        pulse = self.heartflow_mod.HeartflowPulse(
            chat_id="chat-1",
            timestamp=now,
            pulse_type="proactive_hint",
            reason="silence pressure",
            guidance="There may be room to rejoin later.",
            suggested_social_intent="join",
            suggested_action_tier="chat",
            urgency=0.86,
        )

        decision = manager._build_impulse_decision(
            manager._sessions["chat-1"],
            state,
            pulse,
            {"latest_activity_ts": now - 1500, "wait_targets": [], "executor_pending": 0},
            now=now,
        )

        self.assertFalse(decision.visible_candidate_allowed)
        self.assertEqual(decision.blocked_reason, "quiet_hours")
        self.assertTrue(decision.safety_checks["quiet_hours"])

    def test_heartflow_base_frequency_adjusts_candidate_threshold(self):
        now = time.mktime((2026, 5, 11, 12, 0, 0, 0, 0, -1))
        manager = self.heartflow_mod.HeartflowManager(
            config=SimpleNamespace(
                life=SimpleNamespace(proactive_quiet_hours=["23:30-07:30"]),
                reply=SimpleNamespace(base_frequency=0.3),
            )
        )
        manager._sessions["chat-1"] = self.heartflow_mod.HeartflowSessionState(
            chat_id="chat-1",
            started_at=now - 180,
            last_activity_ts=now - 1500,
            last_tick_ts=now,
            expires_at=now - 1200,
            tick_count=2,
            topic_heat=0.86,
            direct_relevance=0.62,
        )
        state = self.heartflow_mod.HeartflowChatState(
            chat_id="chat-1",
            last_tick_ts=now,
            last_activity_ts=now - 1500,
            interest=0.92,
            engagement=0.2,
            talk_willingness=0.90,
            silence_pressure=0.95,
            fatigue=0.05,
            mood_bias=0.4,
            current_focus="quiet but still relevant",
            recent_impulse="proactive_hint",
        )
        pulse = self.heartflow_mod.HeartflowPulse(
            chat_id="chat-1",
            timestamp=now,
            pulse_type="proactive_hint",
            reason="silence pressure",
            guidance="There may be room to rejoin later.",
            suggested_social_intent="join",
            suggested_action_tier="chat",
            urgency=0.86,
        )

        decision = manager._build_impulse_decision(
            manager._sessions["chat-1"],
            state,
            pulse,
            {"latest_activity_ts": now - 1500, "wait_targets": [], "executor_pending": 0},
            now=now,
        )

        self.assertGreater(decision.safety_checks["base_frequency_factor"], 1.0)
        self.assertGreater(decision.safety_checks["visible_candidate_threshold"], 0.72)
        self.assertEqual(
            decision.safety_checks["topic_source_priority"],
            ["conversation_continuity", "recent_memory", "fresh_small_talk"],
        )

    def test_heartflow_visible_candidate_dispatches_through_dispatcher(self):
        class _Dispatcher:
            def __init__(self):
                self.intents = []

            async def dispatch(self, intent):
                self.intents.append(intent)
                return SimpleNamespace(
                    allowed=True,
                    synthetic_event_queued=True,
                    intent_id="intent-1",
                    blocked_reason="",
                    safety_checks={"ok": True},
                )

        dispatcher = _Dispatcher()
        manager = self.heartflow_mod.HeartflowManager(dispatcher=dispatcher)
        now = time.time()
        manager._sessions["chat-1"] = self.heartflow_mod.HeartflowSessionState(
            chat_id="chat-1",
            started_at=now - 180,
            last_activity_ts=now - 1600,
            last_tick_ts=now,
            expires_at=now - 1300,
            tick_count=2,
            topic_heat=0.86,
            direct_relevance=0.62,
        )
        state = self.heartflow_mod.HeartflowChatState(
            chat_id="chat-1",
            last_tick_ts=now,
            last_activity_ts=now - 1600,
            interest=0.92,
            engagement=0.2,
            talk_willingness=0.90,
            silence_pressure=0.95,
            fatigue=0.05,
            mood_bias=0.4,
            current_focus="quiet but still relevant",
            recent_impulse="proactive_hint",
        )
        pulse = self.heartflow_mod.HeartflowPulse(
            chat_id="chat-1",
            timestamp=now,
            pulse_type="proactive_hint",
            reason="silence pressure",
            guidance="There may be room to rejoin later.",
            suggested_social_intent="join",
            suggested_action_tier="chat",
            urgency=0.86,
        )
        decision = manager._build_impulse_decision(
            manager._sessions["chat-1"],
            state,
            pulse,
            {"latest_activity_ts": now - 1600, "wait_targets": [], "executor_pending": 0},
            now=now,
        )

        decision = asyncio.run(manager._maybe_dispatch_visible_candidate(state, pulse, decision))

        self.assertEqual(len(dispatcher.intents), 1)
        self.assertEqual(dispatcher.intents[0].source, "heartflow")
        self.assertTrue(decision.dispatch_enabled)
        self.assertTrue(decision.synthetic_event_queued)
        self.assertFalse(decision.hidden_only)
        self.assertEqual(decision.safety_checks["dispatch_intent_id"], "intent-1")

    def test_heartflow_visible_candidate_guidance_uses_topic_then_memory(self):
        class _Retrieval:
            async def retrieve(self, query):
                return [SimpleNamespace(summary="memory: Alice liked calmer evening check-ins", content="")]

            def render_recall(self, query, candidates):
                return "memory: Alice liked calmer evening check-ins"

        class _Memory:
            retrieval_service = _Retrieval()

        manager = self.heartflow_mod.HeartflowManager(memory_engine=_Memory())
        now = time.time()
        state = self.heartflow_mod.HeartflowChatState(
            chat_id="chat-1",
            last_tick_ts=now,
            last_activity_ts=now - 900,
            interest=0.8,
            engagement=0.2,
            talk_willingness=0.7,
            silence_pressure=0.8,
            fatigue=0.1,
            mood_bias=0.0,
            current_focus="talking about exam plans",
            recent_impulse="proactive_hint",
        )
        pulse = self.heartflow_mod.HeartflowPulse(
            chat_id="chat-1",
            timestamp=now,
            pulse_type="proactive_hint",
            reason="silence pressure",
            guidance="There may be room to rejoin later.",
            suggested_social_intent="join",
            suggested_action_tier="chat",
            urgency=0.8,
        )

        guidance = asyncio.run(manager._build_visible_candidate_guidance(state, pulse))

        self.assertIn("Topic source: conversation_continuity", guidance)
        self.assertIn("Current chat clue: talking about exam plans", guidance)
        self.assertIn("Optional private memory hint", guidance)
        self.assertNotIn("threshold", guidance.lower())

    def test_heartflow_impulse_blocks_when_user_is_waiting_or_cooldown_hits(self):
        manager = self.heartflow_mod.HeartflowManager()
        now = time.time()
        session = self.heartflow_mod.HeartflowSessionState(
            chat_id="chat-1",
            started_at=now - 180,
            last_activity_ts=now - 1600,
            last_tick_ts=now,
            expires_at=now - 1300,
            tick_count=2,
            topic_heat=0.55,
            direct_relevance=0.4,
        )
        manager._sessions["chat-1"] = session
        state = self.heartflow_mod.HeartflowChatState(
            chat_id="chat-1",
            last_tick_ts=now,
            last_activity_ts=now - 1600,
            interest=0.55,
            engagement=0.2,
            talk_willingness=0.50,
            silence_pressure=0.88,
            fatigue=0.1,
            mood_bias=0.0,
            current_focus="quiet but still relevant",
            recent_impulse="proactive_hint",
        )
        pulse = self.heartflow_mod.HeartflowPulse(
            chat_id="chat-1",
            timestamp=now,
            pulse_type="proactive_hint",
            reason="silence pressure",
            guidance="There may be room to rejoin later.",
            suggested_social_intent="join",
            suggested_action_tier="chat",
            urgency=0.60,
        )

        waiting = manager._build_impulse_decision(
            session,
            state,
            pulse,
            {"latest_activity_ts": now - 1600, "wait_targets": ["user-1"], "executor_pending": 0},
            now=now,
        )
        self.assertFalse(waiting.visible_candidate_allowed)
        self.assertEqual(waiting.blocked_reason, "user_waiting")

        manager._remember_impulse_decision(
            self.heartflow_mod.HeartflowImpulseDecision(
                chat_id="chat-1",
                timestamp=now - 120,
                pulse_type="proactive_hint",
                visible_candidate_allowed=True,
            )
        )
        cooldown = manager._build_impulse_decision(
            session,
            state,
            pulse,
            {"latest_activity_ts": now - 1600, "wait_targets": [], "executor_pending": 0},
            now=now,
        )
        self.assertFalse(cooldown.visible_candidate_allowed)
        self.assertEqual(cooldown.blocked_reason, "cooldown")

    def test_heartflow_feedback_bridge_flushes_after_six_pulses(self):
        memory = _FakeMemory()
        bridge = self.heartflow_mod.HeartflowFeedbackBridge(memory)
        pulses = []
        now = time.time()
        for index in range(6):
            pulses.append(
                self.heartflow_mod.HeartflowPulse(
                    chat_id="chat-1",
                    timestamp=now + index,
                    pulse_type="prepare_reply",
                    reason="high interest",
                    guidance="join carefully",
                    suggested_social_intent="join",
                    suggested_action_tier="chat",
                    urgency=0.7,
                    tags=["high_interest"],
                )
            )

        flushed = asyncio.run(bridge.maybe_flush({"chat-1": pulses}, "chat-1"))

        self.assertTrue(flushed)
        self.assertEqual(len(memory.feedback_calls), 1)
        self.assertEqual(memory.feedback_calls[0]["source"], "heartflow")
        self.assertIn("main_impulse=join", memory.feedback_calls[0]["summary"])
        self.assertIn("Join only", memory.feedback_calls[0]["guidance"])

    def test_heartflow_timeline_merges_pulse_action_and_impulse(self):
        manager = self.heartflow_mod.HeartflowManager()
        now = time.time()
        pulse = self.heartflow_mod.HeartflowPulse(
            chat_id="chat-1",
            timestamp=now,
            pulse_type="prepare_reply",
            reason="high interest",
            guidance="join carefully",
            suggested_social_intent="join",
            suggested_action_tier="chat",
            urgency=0.7,
        )
        action = self.heartflow_mod.HeartflowActionDecision(
            chat_id="chat-1",
            timestamp=now + 1,
            action_type="prepare_reply",
            reason="prepare",
            guidance="wait for fresh cue",
        )
        impulse = self.heartflow_mod.HeartflowImpulseDecision(
            chat_id="chat-1",
            timestamp=now + 2,
            pulse_type="prepare_reply",
            blocked_reason="hidden_impulse",
        )

        manager._remember_pulse(pulse)
        manager._remember_action_decision(action)
        manager._remember_impulse_decision(impulse)

        timeline = manager.list_timeline(chat_id="chat-1", limit=10)

        self.assertEqual([item["kind"] for item in timeline[:3]], ["impulse_decision", "action", "pulse"])
        self.assertEqual(timeline[0]["summary"], "hidden_impulse")

    def test_heartflow_topic_digest_service_writes_and_cools_down(self):
        memory = _FakeMemory()
        service = self.heartflow_mod.HeartflowTopicDigestService(memory)
        manager = self.heartflow_mod.HeartflowManager()
        now = time.time()
        manager._sessions["chat-1"] = self.heartflow_mod.HeartflowSessionState(
            chat_id="chat-1",
            started_at=now - 900,
            last_activity_ts=now - 700,
            last_tick_ts=now,
            expires_at=now - 400,
            tick_count=4,
            recent_bot_reply_count=1,
            topic_heat=0.82,
            direct_relevance=0.6,
            low_cost_retained=True,
        )
        manager._states["chat-1"] = self.heartflow_mod.HeartflowChatState(
            chat_id="chat-1",
            last_tick_ts=now,
            last_activity_ts=now - 700,
            interest=0.8,
            engagement=0.3,
            talk_willingness=0.6,
            silence_pressure=0.5,
            fatigue=0.2,
            mood_bias=0.1,
            current_focus="talking about exam plans",
            recent_impulse="prepare_reply",
        )

        asyncio.run(service.run_once(manager, now=now))
        asyncio.run(service.run_once(manager, now=now + 10))

        self.assertEqual(memory.feedback_calls[0]["source"], "heartflow_topic_digest")
        self.assertIn("talking about exam plans", memory.feedback_calls[0]["summary"])
        self.assertIn("do not quote", memory.feedback_calls[0]["guidance"].lower())
        history = service.list_digests(limit=5)
        self.assertEqual(history[0].status, "skipped")
        self.assertEqual(history[0].skip_reason, "cooldown")

    def test_heartflow_tick_chat_cleans_stale_history_buckets(self):
        manager = self.heartflow_mod.HeartflowManager(
            state_engine=_FakeStateEngine(energy=0.8, mood=0.1),
            memory_engine=_FakeMemory(),
        )
        now = time.time()
        stale_ts = now - (self.heartflow_mod.HeartflowManager.ACTIVE_CHAT_TTL_SECONDS * 2) - 30

        manager._states["stale-chat"] = self.heartflow_mod.HeartflowChatState(
            chat_id="stale-chat",
            last_tick_ts=stale_ts,
            last_activity_ts=stale_ts,
            interest=0.2,
            engagement=0.2,
            talk_willingness=0.2,
            silence_pressure=0.2,
            fatigue=0.8,
            mood_bias=0.0,
            current_focus="old context",
            recent_impulse="observe",
        )
        manager._pulses_by_chat["stale-chat"] = [
            self.heartflow_mod.HeartflowPulse(
                chat_id="stale-chat",
                timestamp=stale_ts,
                pulse_type="observe",
                reason="stale pulse",
                guidance="ignore",
                suggested_social_intent="observe",
                suggested_action_tier="none",
                urgency=0.1,
            )
        ]
        manager._action_decisions_by_chat["stale-chat"] = [
            self.heartflow_mod.HeartflowActionDecision(
                chat_id="stale-chat",
                timestamp=stale_ts,
                action_type="observe",
                reason="stale action",
                guidance="ignore",
            )
        ]
        manager._impulse_decisions_by_chat["stale-chat"] = [
            self.heartflow_mod.HeartflowImpulseDecision(
                chat_id="stale-chat",
                timestamp=stale_ts,
                pulse_type="observe",
                blocked_reason="stale",
            )
        ]

        snapshot = {
            "latest_activity_ts": now - 20,
            "recent_activity_count": 3,
            "recent_activity_count_60s": 2,
            "recent_direct_count": 1,
            "recent_bot_reply_count": 0,
            "latest_activity_preview": "what should we do now?",
            "wait_targets": [],
            "executor_pending": 0,
        }

        payload = asyncio.run(manager.tick_chat("chat-1", snapshot=snapshot, now=now))

        self.assertTrue(payload["performed"])
        self.assertNotIn("stale-chat", manager._states)
        self.assertNotIn("stale-chat", manager._pulses_by_chat)
        self.assertNotIn("stale-chat", manager._action_decisions_by_chat)
        self.assertNotIn("stale-chat", manager._impulse_decisions_by_chat)
        self.assertIn("chat-1", manager._states)
        status = manager.describe_status()
        self.assertEqual(status["active_chats"], 1)
        self.assertEqual(status["pending_pulses"], 1)

    def test_heartflow_tick_batch_cleans_history_only_once_per_cycle(self):
        now = time.time()

        class _Coordinator:
            async def list_active_chats(self, ttl_seconds):
                return ["chat-1", "chat-2"]

            async def get_activity_snapshot(self, chat_id):
                return {
                    "latest_activity_ts": now - 20,
                    "recent_activity_count": 2,
                    "recent_activity_count_60s": 1,
                    "recent_direct_count": 0,
                    "recent_bot_reply_count": 0,
                    "latest_activity_preview": f"{chat_id} hello?",
                    "wait_targets": [],
                    "executor_pending": 0,
                }

        manager = self.heartflow_mod.HeartflowManager(
            runtime_coordinator=_Coordinator(),
            state_engine=_FakeStateEngine(energy=0.8, mood=0.1),
            memory_engine=_FakeMemory(),
        )
        cleanup_calls = {"sessions": 0, "history": 0}
        original_cleanup_sessions = manager._cleanup_sessions
        original_cleanup_history = manager._cleanup_stale_chat_history

        def _count_cleanup_sessions(*, now):
            cleanup_calls["sessions"] += 1
            return original_cleanup_sessions(now=now)

        def _count_cleanup_history(*, now):
            cleanup_calls["history"] += 1
            return original_cleanup_history(now=now)

        manager._cleanup_sessions = _count_cleanup_sessions
        manager._cleanup_stale_chat_history = _count_cleanup_history

        asyncio.run(manager.tick(now=now))

        self.assertEqual(cleanup_calls["sessions"], 1)
        self.assertEqual(cleanup_calls["history"], 1)
        self.assertIn("chat-1", manager._states)
        self.assertIn("chat-2", manager._states)

    def test_cognitive_loop_reads_heartflow_hidden_context(self):
        gateway = _FakeGateway()
        loop = self.loop_mod.CognitiveLoop(gateway)
        event = _FakeEvent("please explain this situation")
        event.set_extra("astrmai_heartflow_context", "interest=0.82; talk_willingness=0.61; guidance=join carefully")

        decision = asyncio.run(loop.decide(event=event))

        self.assertIsNotNone(decision)
        self.assertIn("Heartflow state", gateway.calls[0]["prompt"])
        self.assertIn("join carefully", gateway.calls[0]["prompt"])


if __name__ == "__main__":
    unittest.main()
