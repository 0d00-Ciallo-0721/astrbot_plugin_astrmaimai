import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs
from tests.helpers.attention_stubs import install_attention_stubs


class Reply:
    def __init__(self, sender_id="", sender_nickname=""):
        self.sender_id = sender_id
        self.sender_nickname = sender_nickname


class _FakeSensors:
    def is_wakeup_signal(self, event, self_id):
        return event.get_extra("wakeup", False)

    async def is_command(self, msg_str):
        return False

    async def should_process_message(self, event):
        return True


class _FakeEvent:
    def __init__(self, sender_id, sender_name, text, extras=None, components=None):
        self.message_str = text
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self.message_obj = SimpleNamespace(message=components or [], message_id=f"{sender_id}:{text}")
        self.timestamp = 123.0
        self._extra = dict(extras or {})
        self._sender_id = sender_id
        self._sender_name = sender_name

    def get_group_id(self):
        return "group-1"

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_self_id(self):
        return "bot-1"

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)


class _FakePrivateEvent(_FakeEvent):
    def __init__(self, sender_id, sender_name, text, extras=None, components=None):
        super().__init__(sender_id, sender_name, text, extras=extras, components=components)
        self.unified_msg_origin = f"default:FriendMessage:{sender_id}"

    def get_group_id(self):
        return None


class _FakePrivateChatManager:
    def __init__(self):
        self.calls = []

    async def signal_new_message(self, user_id, message_str, chat_id=""):
        self.calls.append((user_id, message_str, chat_id))


class _SequenceJudge:
    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = []

    async def evaluate(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        action = self.actions.pop(0) if self.actions else "PASS"
        return SimpleNamespace(action=action)


class _FakeCompactionScheduler:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def schedule_compaction_evaluation(self, chat_id, focus_context=None, message_source=None):
        self.calls.append((chat_id, focus_context, message_source))
        self.started.set()
        await self.release.wait()
        return SimpleNamespace(chat_id=chat_id, state="DONE")


class RefactoredAttentionGateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        install_attention_stubs()
        sys.modules.pop("astrmai.Heart.attention", None)
        sys.modules.pop("astrmai.conversation.attention.gate", None)
        gate_mod = importlib.import_module("astrmai.conversation.attention.gate")
        self.gate_mod = importlib.reload(gate_mod)
        config = SimpleNamespace(
            attention=SimpleNamespace(
                max_message_length=100,
                focus_thread_enabled=True,
                focus_thread_core_max_messages=4,
                focus_thread_related_max_messages=3,
                ambient_background_max_messages=2,
                thread_same_speaker_followup_sec=8,
                thread_reply_priority_enabled=True,
            ),
            system1=SimpleNamespace(wakeup_words=[], nicknames=["AstrMai"]),
            global_settings=SimpleNamespace(debug_mode=False),
        )
        self.gate = self.gate_mod.AttentionGate(
            state_engine=SimpleNamespace(config=config),
            judge=SimpleNamespace(),
            sensors=_FakeSensors(),
            system2_callback=None,
        )

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_focus_thread_selection_matches_legacy_behavior(self):
        bot_event = _FakeEvent("bot-1", "AstrMai", "legacy-bot-message")
        focus_event = _FakeEvent(
            "user-1",
            "Alice",
            "涓轰粈涔堜笉鍙互",
            components=[Reply(sender_id="bot-1", sender_nickname="AstrMai")],
        )
        unrelated = _FakeEvent("user-2", "Bob", "鎴戝幓鍚冮キ")

        normalized = self.gate._build_normalized_events([bot_event, focus_event, unrelated], self_id="bot-1")
        focus_candidate = normalized[1]
        root_candidate, _ = self.gate._resolve_thread_root(focus_candidate, normalized)
        focus_thread = self.gate._build_focus_thread(focus_candidate, root_candidate, normalized)

        self.assertEqual(focus_thread["core_events"], [bot_event, focus_event])
        self.assertEqual(focus_thread["ambient_events"], [unrelated])

    def test_resolve_event_context_keeps_reply_image_footprints_without_direct_vision(self):
        event = _FakeEvent(
            "user-1",
            "Alice",
            "",
            extras={"extracted_image_urls": ["reply.jpg"]},
        )

        context = self.gate._resolve_event_context(event)

        self.assertEqual(context["extracted_images"], ["reply.jpg"])
        self.assertFalse(context["is_private"])


    def test_is_direct_wakeup_event_handles_missing_sensors_without_losing_fast_paths(self):
        gate = self.gate_mod.AttentionGate(
            state_engine=self.gate.state_engine,
            judge=self.gate.judge,
            sensors=None,
            system2_callback=None,
        )

        ordinary = _FakeEvent("user-1", "Alice", "hello")
        direct = _FakeEvent("user-2", "Bob", "ping", extras={"astrmai_group_direct_wakeup": True})
        bonus = _FakeEvent("user-3", "Carol", "ping", extras={"astrmai_bonus_score": 1.0})

        self.assertFalse(gate._is_direct_wakeup_event(ordinary, "bot-1"))
        self.assertTrue(gate._is_direct_wakeup_event(direct, "bot-1"))
        self.assertTrue(gate._is_direct_wakeup_event(bonus, "bot-1"))



    def test_process_event_fast_mode_engages_on_direct_wakeup(self):
        captured = []

        async def fake_sys2(event, events):
            captured.append((event, events))

        def capture_task(coro):
            captured.append(coro)
            return coro

        self.gate.sys2_process = fake_sys2
        self.gate._fire_background_task = capture_task

        event = _FakeEvent(
            "user-1",
            "Alice",
            "AstrMai",
            extras={"wakeup": True},
        )

        status = asyncio.run(self.gate.process_event(event))

        self.assertEqual(status, "ENGAGED")
        self.assertEqual(event.get_extra("retrieve_keys"), ["CORE_ONLY"])
        self.assertTrue(event.get_extra("is_fast_mode"))
        self.assertEqual(event.get_extra("astrmai_group_direct_wakeup"), True)
        turn_context = event.get_extra("astrmai_turn_context")
        self.assertIsNotNone(turn_context)
        self.assertEqual(turn_context.perception.chat_id, "group-1")
        self.assertTrue(turn_context.perception.is_strong_wakeup)
        self.assertEqual(turn_context.attention.retrieve_keys, ["CORE_ONLY"])
        self.assertTrue(turn_context.attention.is_fast_mode)
        self.assertEqual(len(captured), 1)

        for item in captured:
            if asyncio.iscoroutine(item):
                item.close()

    def test_process_event_coalesces_messages_while_debounce_is_open(self):
        captured = []

        async def fake_sys2(event, events):
            captured.append((event, events))

        self.gate.sys2_process = fake_sys2
        self.gate._compute_debounce_delay = lambda *args, **kwargs: 0.05

        async def _run():
            first = _FakeEvent("user-1", "Alice", "first")
            second = _FakeEvent("user-1", "Alice", "second")
            first_status = await self.gate.process_event(first)
            second_status = await self.gate.process_event(second)
            await asyncio.sleep(0.12)
            return first_status, second_status

        first_status, second_status = asyncio.run(_run())

        self.assertEqual(first_status, "BUFFERED")
        self.assertEqual(second_status, "BUFFERED")
        self.assertEqual(len(captured), 1)
        self.assertEqual([event.message_str for event in captured[0][1]], ["first", "second"])

    def test_judge_wait_keeps_batch_for_next_pass_without_sys2(self):
        captured = []
        self.gate.judge = _SequenceJudge(["WAIT", "PASS"])
        self.gate._compute_debounce_delay = lambda *args, **kwargs: 0.0

        async def fake_sys2(event, events):
            captured.append((event, events))

        self.gate.sys2_process = fake_sys2

        async def _run():
            session = self.gate_mod.SessionContext()
            session.accumulation_pool = [_FakeEvent("user-1", "Alice", "not done", extras={"astrmai_timestamp": time.time()})]
            session.is_evaluating = True
            await self.gate._debounce_and_judge("default:GroupMessage:group-1", session, "bot-1")
            after_wait = [event.message_str for event in session.attention_window]
            session.accumulation_pool = [_FakeEvent("user-1", "Alice", "now done", extras={"astrmai_timestamp": time.time()})]
            session.is_evaluating = True
            await self.gate._debounce_and_judge("default:GroupMessage:group-1", session, "bot-1")
            return after_wait

        after_wait = asyncio.run(_run())

        self.assertEqual(after_wait, ["not done"])
        self.assertEqual(len(captured), 1)
        self.assertEqual([event.message_str for event in captured[0][1]], ["not done", "now done"])

    def test_judge_ignore_keeps_focus_as_window_only(self):
        captured = []
        self.gate.judge = _SequenceJudge(["IGNORE"])
        self.gate._compute_debounce_delay = lambda *args, **kwargs: 0.0
        self.gate.sys2_process = lambda event, events: captured.append((event, events))

        async def _run():
            session = self.gate_mod.SessionContext()
            session.accumulation_pool = [_FakeEvent("user-1", "Alice", "skip me", extras={"astrmai_timestamp": time.time()})]
            session.is_evaluating = True
            await self.gate._debounce_and_judge("default:GroupMessage:group-1", session, "bot-1")
            return [event.message_str for event in session.attention_window]

        retained = asyncio.run(_run())

        self.assertEqual(captured, [])
        self.assertEqual(retained, ["skip me"])

    def test_strong_wakeup_skips_judge_gate(self):
        captured = []
        judge = _SequenceJudge(["IGNORE"])
        self.gate.judge = judge
        self.gate._compute_debounce_delay = lambda *args, **kwargs: 0.0

        async def fake_sys2(event, events):
            captured.append((event, events))

        self.gate.sys2_process = fake_sys2

        async def _run():
            session = self.gate_mod.SessionContext()
            session.accumulation_pool = [_FakeEvent("user-1", "Alice", "AstrMai", extras={"wakeup": True, "astrmai_timestamp": time.time()})]
            session.is_evaluating = True
            await self.gate._debounce_and_judge(
                "default:GroupMessage:group-1",
                session,
                "bot-1",
                is_strong_wakeup=True,
            )

        asyncio.run(_run())

        self.assertEqual(judge.calls, [])
        self.assertEqual(len(captured), 1)

    def test_judge_reply_action_maps_to_pass_through(self):
        captured = []
        self.gate.judge = _SequenceJudge(["REPLY"])
        self.gate._compute_debounce_delay = lambda *args, **kwargs: 0.0

        async def fake_sys2(event, events):
            captured.append((event, events))

        self.gate.sys2_process = fake_sys2
        focus = _FakeEvent("user-1", "Alice", "please answer", extras={"astrmai_timestamp": time.time()})

        async def _run():
            session = self.gate_mod.SessionContext()
            session.accumulation_pool = [focus]
            session.is_evaluating = True
            await self.gate._debounce_and_judge("default:GroupMessage:group-1", session, "bot-1")

        asyncio.run(_run())

        self.assertEqual(focus.get_extra("judge_action"), "PASS")
        turn_context = focus.get_extra("astrmai_turn_context")
        self.assertIsNotNone(turn_context)
        self.assertEqual(turn_context.attention.judge_action, "PASS")
        self.assertEqual(turn_context.attention.retrieve_keys, ["ALL"])
        self.assertEqual([event.message_str for event in turn_context.attention.window_events], ["please answer"])
        self.assertEqual(len(captured), 1)

    def test_router_applies_primary_mood_before_judge_once(self):
        calls = []

        async def _update_mood(chat_id, text):
            calls.append((chat_id, text))
            return "happy", 0.45

        self.gate.state_engine.update_mood = _update_mood
        self.gate.judge = _SequenceJudge(["PASS", "PASS"])
        router_mod = importlib.import_module("astrmai.conversation.attention.decision_router")
        router_mod = importlib.reload(router_mod)
        router = router_mod.AttentionDecisionRouter(self.gate)
        focus = _FakeEvent("user-1", "Alice", "need help")

        async def _run():
            first = await router.evaluate(
                "default:GroupMessage:group-1",
                focus,
                {"core_events": [focus]},
                [focus],
                is_strong_wakeup=False,
            )
            second = await router.evaluate(
                "default:GroupMessage:group-1",
                focus,
                {"core_events": [focus]},
                [focus],
                is_strong_wakeup=False,
            )
            return first, second

        first, second = asyncio.run(_run())

        self.assertEqual(first.action, "PASS")
        self.assertEqual(second.action, "PASS")
        self.assertEqual(calls, [("default:GroupMessage:group-1", "need help")])
        self.assertTrue(focus.get_extra("astrmai_primary_mood_applied"))
        self.assertEqual(focus.get_extra("astrmai_primary_mood_tag"), "happy")
        self.assertAlmostEqual(focus.get_extra("astrmai_primary_mood_value"), 0.45)
        self.assertEqual(focus.get_extra("astrmai_primary_mood_source"), "attention_pre_judge")

    def test_process_event_applies_primary_mood_before_private_wait(self):
        calls = []

        async def _update_mood(chat_id, text):
            calls.append((chat_id, text))
            return "sad", -0.35

        manager = _FakePrivateChatManager()
        self.gate.private_chat_manager = manager
        self.gate.state_engine.update_mood = _update_mood
        event = _FakePrivateEvent("user-1", "Alice", "今天有点难受")

        status = asyncio.run(self.gate.process_event(event))

        self.assertEqual(status, "PRIVATE_WAIT")
        self.assertEqual(calls, [("default:FriendMessage:user-1", "今天有点难受")])
        self.assertEqual(manager.calls, [("user-1", "今天有点难受", "default:FriendMessage:user-1")])
        self.assertTrue(event.get_extra("astrmai_primary_mood_applied"))
        self.assertEqual(event.get_extra("astrmai_primary_mood_tag"), "sad")
        self.assertAlmostEqual(event.get_extra("astrmai_primary_mood_value"), -0.35)
        self.assertEqual(event.get_extra("astrmai_primary_mood_source"), "attention_ingress")

    def test_process_event_applies_primary_mood_before_fast_wakeup_engage(self):
        calls = []

        async def _update_mood(chat_id, text):
            calls.append((chat_id, text))
            return "happy", 0.5

        self.gate.state_engine.update_mood = _update_mood
        event = _FakeEvent(
            "user-1",
            "Alice",
            "AstrMai",
            extras={"wakeup": True},
        )

        status = asyncio.run(self.gate.process_event(event))

        self.assertEqual(status, "ENGAGED")
        self.assertEqual(calls, [("group-1", "AstrMai")])
        self.assertTrue(event.get_extra("astrmai_primary_mood_applied"))
        self.assertEqual(event.get_extra("astrmai_primary_mood_source"), "attention_ingress")

    def test_debounce_normalizes_merged_events_once(self):
        captured = []
        call_count = 0
        original = self.gate._build_normalized_events

        def counted_normalize(events, self_id):
            nonlocal call_count
            call_count += 1
            return original(events, self_id)

        async def fake_sys2(event, events):
            captured.append((event, events))

        self.gate._build_normalized_events = counted_normalize
        self.gate.sys2_process = fake_sys2
        self.gate._compute_debounce_delay = lambda *args, **kwargs: 0.0

        async def _run():
            session = self.gate_mod.SessionContext()
            session.accumulation_pool = [_FakeEvent("user-1", "Alice", "one", extras={"astrmai_timestamp": time.time()})]
            session.is_evaluating = True
            await self.gate._debounce_and_judge("default:GroupMessage:group-1", session, "bot-1")

        asyncio.run(_run())

        self.assertEqual(call_count, 1)
        self.assertEqual(len(captured), 1)

    def test_inject_external_event_routes_through_kernel_when_bound(self):
        calls = []

        class _Kernel:
            async def tick(self, *, chat_id, trigger, event=None):
                calls.append(
                    (
                        chat_id,
                        trigger,
                        getattr(event, "message_str", ""),
                        event.get_extra("astrmai_loop_source") if hasattr(event, "get_extra") else None,
                    )
                )
                return SimpleNamespace(dispatch_result="BUFFERED")

        self.gate.bind_chat_loop_kernel(_Kernel())

        result = asyncio.run(
            self.gate.inject_external_event(
                "default:GroupMessage:group-1",
                {
                    "message_str": "synthetic proactive nudge",
                    "extra": {
                        "astrmai_is_proactive_event": True,
                        "astrmai_loop_source": "proactive_dispatcher",
                    },
                },
            )
        )

        self.assertEqual(result, "BUFFERED")
        self.assertEqual(
            calls,
            [("default:GroupMessage:group-1", "external", "synthetic proactive nudge", "proactive_dispatcher")],
        )

    def test_inject_external_event_falls_back_to_process_event_without_kernel(self):
        calls = []

        async def _fake_process(event):
            calls.append((event.unified_msg_origin, event.message_str, event.get_extra("astrmai_loop_source")))
            return "ENGAGED"

        self.gate.process_event = _fake_process

        result = asyncio.run(
            self.gate.inject_external_event(
                "default:GroupMessage:group-1",
                {
                    "content": "external plugin reply",
                    "is_external_bot_reply": True,
                    "extra": {"astrmai_loop_source": "external_result_bridge"},
                },
            )
        )

        self.assertEqual(result, "ENGAGED")
        self.assertEqual(calls, [("default:GroupMessage:group-1", "external plugin reply", "external_result_bridge")])

    def test_debounce_worker_drain_loop_keeps_late_arrivals(self):
        captured = []
        first_batch_entered = asyncio.Event()
        release_first_batch = asyncio.Event()

        async def fake_sys2(event, events):
            captured.append([item.message_str for item in events])
            if len(captured) == 1:
                first_batch_entered.set()
                await release_first_batch.wait()

        self.gate.sys2_process = fake_sys2
        self.gate._compute_debounce_delay = lambda *args, **kwargs: 0.0

        async def _run():
            first = _FakeEvent("user-1", "Alice", "first")
            second = _FakeEvent("user-1", "Alice", "second")
            first_status = await self.gate.process_event(first)
            await asyncio.wait_for(first_batch_entered.wait(), timeout=1.0)
            second_status = await self.gate.process_event(second)
            release_first_batch.set()
            await asyncio.sleep(0.05)
            session = await self.gate._get_or_create_session("group-1")
            return first_status, second_status, session

        first_status, second_status, session = asyncio.run(_run())

        self.assertEqual(first_status, "BUFFERED")
        self.assertEqual(second_status, "BUFFERED")
        self.assertGreaterEqual(len(captured), 2)
        self.assertIn(["first"], captured)
        self.assertIn(["first", "second"], captured)
        self.assertFalse(session.is_evaluating)
        self.assertEqual(session.accumulation_pool, [])

    def test_background_task_semaphore_limits_parallelism(self):
        max_running = 0
        running = 0

        async def background_job():
            nonlocal max_running, running
            running += 1
            max_running = max(max_running, running)
            await asyncio.sleep(0.01)
            running -= 1

        async def _run():
            tasks = [self.gate._fire_background_task(background_job()) for _ in range(20)]
            await asyncio.gather(*tasks)

        asyncio.run(_run())

        self.assertLessEqual(max_running, self.gate.BACKGROUND_TASK_MAX_CONCURRENCY)

    def test_context_compaction_engine_coalesces_execution_but_keeps_message_accounting(self):
        compaction_mod = importlib.import_module("astrmai.conversation.attention.context_compaction")
        compaction_mod = importlib.reload(compaction_mod)
        engine = compaction_mod.ContextCompactionEngine(dialogue_store=None)
        started = asyncio.Event()
        release = asyncio.Event()
        maybe_compact_calls = 0

        async def fake_maybe_compact(chat_id, focus_context=None):
            nonlocal maybe_compact_calls
            maybe_compact_calls += 1
            started.set()
            await release.wait()
            return compaction_mod.CompactionResult(chat_id=chat_id, state="DONE")

        engine.maybe_compact = fake_maybe_compact

        async def _run():
            first_task = asyncio.create_task(
                engine.schedule_compaction_evaluation(
                    "default:GroupMessage:group-1",
                    focus_context={"focus": 1},
                    message_source="user",
                )
            )
            await asyncio.wait_for(started.wait(), timeout=1.0)
            second_result = await engine.schedule_compaction_evaluation(
                "default:GroupMessage:group-1",
                focus_context={"focus": 2},
                message_source="user",
            )
            state = engine._state_for_chat("default:GroupMessage:group-1")
            release.set()
            first_result = await first_task
            return first_result, second_result, state

        first_result, second_result, state = asyncio.run(_run())

        self.assertEqual(first_result.state, "DONE")
        self.assertEqual(second_result.skipped_reason, "evaluation_already_scheduled")
        self.assertEqual(maybe_compact_calls, 1)
        self.assertEqual(state["message_count_since_last_compaction"], 2)

    def test_fast_wakeup_bypasses_background_semaphore(self):
        running_background = 0
        background_saturated = asyncio.Event()
        release_background = asyncio.Event()
        sys2_started = asyncio.Event()

        async def background_job():
            nonlocal running_background
            running_background += 1
            if running_background >= self.gate.BACKGROUND_TASK_MAX_CONCURRENCY:
                background_saturated.set()
            await release_background.wait()
            running_background -= 1

        async def fake_sys2(event, events):
            sys2_started.set()

        self.gate.sys2_process = fake_sys2

        async def _run():
            tasks = [
                self.gate._fire_background_task(background_job())
                for _ in range(self.gate.BACKGROUND_TASK_MAX_CONCURRENCY)
            ]
            await asyncio.wait_for(background_saturated.wait(), timeout=1.0)
            event = _FakeEvent(
                "user-1",
                "Alice",
                "AstrMai",
                extras={"wakeup": True},
            )
            status = await self.gate.process_event(event)
            await asyncio.wait_for(sys2_started.wait(), timeout=1.0)
            release_background.set()
            await asyncio.gather(*tasks)
            await asyncio.sleep(0)
            return status

        status = asyncio.run(_run())

        self.assertEqual(status, "ENGAGED")

    def test_attention_window_ttl_keeps_recent_events_for_180_seconds(self):
        session = self.gate_mod.SessionContext()
        recent = _FakeEvent("user-1", "Alice", "recent")
        expired = _FakeEvent("user-2", "Bob", "expired")
        now = 500.0
        session.attention_window = [recent, expired]
        session.attention_window_ts = [now - 179.0, now - 181.0]

        retained = self.gate._prune_attention_window(session, now=now)

        self.assertEqual(retained, [recent])
        self.assertEqual(session.attention_window, [recent])

    def test_focus_thread_sorts_output_by_original_event_order(self):
        thread_builder = importlib.import_module("astrmai.conversation.attention.thread_builder")
        original_score = thread_builder._score_thread_relation
        first = _FakeEvent("user-1", "Alice", "first")
        focus = _FakeEvent("user-1", "Alice", "focus")
        related = _FakeEvent("user-2", "Bob", "related")
        ambient = _FakeEvent("user-3", "Cara", "ambient")
        normalized = self.gate._build_normalized_events([first, focus, related, ambient], self_id="bot-1")
        focus_candidate = normalized[1]

        def fake_score(_gate, candidate, _focus_candidate, _root_candidate):
            mapping = {"first": 70, "related": 50, "ambient": 10}
            return mapping.get(candidate.event.message_str, -1)

        thread_builder._score_thread_relation = fake_score
        try:
            focus_thread = self.gate._build_focus_thread(focus_candidate, focus_candidate, normalized)
        finally:
            thread_builder._score_thread_relation = original_score

        self.assertEqual([event.message_str for event in focus_thread.core_events], ["first", "focus"])
        self.assertEqual([event.message_str for event in focus_thread.related_events], ["related"])
        self.assertEqual([event.message_str for event in focus_thread.ambient_events], ["ambient"])

    def test_throttle_gracefully_handles_missing_should_drop(self):
        result_with_none = self.gate._should_skip_by_throttle(
            "hello",
            [],
            None,
            "default:GroupMessage:group-1",
            False,
            False,
        )
        result_with_plain_object = self.gate._should_skip_by_throttle(
            "hello",
            [],
            object(),
            "default:GroupMessage:group-1",
            False,
            False,
        )

        self.assertIsNone(result_with_none)
        self.assertIsNone(result_with_plain_object)

    def test_repeater_echo_signature_cleanup_keeps_behavior(self):
        session = self.gate_mod.SessionContext()

        first = self.gate._handle_repeater_echo(session, False, [], "echo")
        second = self.gate._handle_repeater_echo(session, False, [], "echo")
        third = self.gate._handle_repeater_echo(session, False, [], "echo")

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(third, "repeater_echo")
if __name__ == "__main__":
    unittest.main()
