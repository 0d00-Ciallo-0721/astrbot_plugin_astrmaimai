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
    def __init__(self, sender_id, sender_name, text, extras=None, components=None, message_id=None):
        self.message_str = text
        self.unified_msg_origin = "default:GroupMessage:group-1"
        resolved_message_id = f"{sender_id}:{text}" if message_id is None else message_id
        self.message_obj = SimpleNamespace(message=components or [], message_id=resolved_message_id)
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
    def __init__(self, sender_id, sender_name, text, extras=None, components=None, message_id=None):
        super().__init__(
            sender_id,
            sender_name,
            text,
            extras=extras,
            components=components,
            message_id=message_id,
        )
        self.unified_msg_origin = f"default:FriendMessage:{sender_id}"

    def get_group_id(self):
        return None


class _FakePrivateChatManager:
    def __init__(self, signaled=True):
        self.calls = []
        self.signaled = signaled

    async def signal_new_message(self, user_id, message_str, chat_id=""):
        self.calls.append((user_id, message_str, chat_id))
        return self.signaled


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

    def test_message_dedup_prefers_platform_message_id_and_expires_fallback(self):
        first = _FakeEvent("user-1", "Alice", "same", message_id="message-1")
        second = _FakeEvent("user-1", "Alice", "same", message_id="message-2")
        replay = _FakeEvent("user-1", "Alice", "changed", message_id="message-1")

        self.assertTrue(self.gate._claim_message(first, now=10.0))
        self.assertTrue(self.gate._claim_message(second, now=10.1))
        self.assertFalse(self.gate._claim_message(replay, now=10.2))

        fallback = _FakeEvent("user-1", "Alice", "same", message_id="")
        fallback_replay = _FakeEvent("user-1", "Alice", "same", message_id="")
        fallback_later = _FakeEvent("user-1", "Alice", "same", message_id="")
        self.assertTrue(self.gate._claim_message(fallback, now=20.0))
        self.assertFalse(self.gate._claim_message(fallback_replay, now=20.5))
        self.assertTrue(
            self.gate._claim_message(
                fallback_later,
                now=20.0 + self.gate.MESSAGE_DEDUP_FALLBACK_TTL_SECONDS + 0.1,
            )
        )

    def test_clear_chat_state_cancels_owned_session_worker(self):
        async def _run():
            session = self.gate_mod.SessionContext()
            session.is_evaluating = True
            self.gate.focus_pools["group-1"] = session
            started = asyncio.Event()

            async def pending_worker():
                started.set()
                await asyncio.Event().wait()

            task = asyncio.create_task(pending_worker())
            task._worker_context = SimpleNamespace(chat_id="group-1", session=session, self_id="bot-1")
            self.gate._session_tasks.add(task)
            task.add_done_callback(self.gate._handle_session_worker_result)
            await started.wait()

            removed = await self.gate.clear_chat_state("group-1")
            return removed, task, session

        removed, task, session = asyncio.run(_run())

        self.assertTrue(removed)
        self.assertTrue(task.cancelled())
        self.assertFalse(session.is_evaluating)
        self.assertNotIn("group-1", self.gate.focus_pools)

    def test_session_worker_cancel_does_not_cancel_replacement_session(self):
        async def _run():
            old_session = self.gate_mod.SessionContext()
            new_session = self.gate_mod.SessionContext()

            async def pending_worker():
                await asyncio.Event().wait()

            old_task = asyncio.create_task(pending_worker())
            new_task = asyncio.create_task(pending_worker())
            old_task._worker_context = SimpleNamespace(chat_id="group-1", session=old_session, self_id="bot-1")
            new_task._worker_context = SimpleNamespace(chat_id="group-1", session=new_session, self_id="bot-1")
            self.gate._session_tasks.update({old_task, new_task})
            old_task.add_done_callback(self.gate._handle_session_worker_result)
            new_task.add_done_callback(self.gate._handle_session_worker_result)
            await asyncio.sleep(0)

            await self.gate._cancel_session_workers("group-1", session=old_session)
            new_still_running = not new_task.done()
            new_task.cancel()
            await asyncio.gather(new_task, return_exceptions=True)
            return old_task, new_still_running

        old_task, new_still_running = asyncio.run(_run())

        self.assertTrue(old_task.cancelled())
        self.assertTrue(new_still_running)

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

    def test_focus_thread_exposes_recent_same_sender_image_as_safe_tool_candidate(self):
        image_event = _FakeEvent(
            "user-1",
            "Alice",
            "",
            extras={"extracted_image_urls": ["https://private.example/image.jpg"]},
            message_id="image-message-1",
        )
        image_event.timestamp = 100.0
        focus_event = _FakeEvent(
            "user-1",
            "Alice",
            "中文区是什么猎奇区吗？",
            message_id="text-message-1",
        )
        focus_event.timestamp = 120.0

        normalized = self.gate._build_normalized_events(
            [image_event, focus_event],
            self_id="bot-1",
        )
        root_candidate, _ = self.gate._resolve_thread_root(normalized[1], normalized)
        focus_thread = self.gate._build_focus_thread(
            normalized[1],
            root_candidate,
            normalized,
        )

        self.assertEqual(len(focus_thread.recent_media_candidates), 1)
        candidate = focus_thread.recent_media_candidates[0]
        self.assertEqual(candidate.message_id, "image-message-1")
        self.assertEqual(candidate.relation, "same_sender_recent")
        safe_candidates = focus_event.get_extra("astrmai_recent_media_candidates")
        self.assertEqual(safe_candidates[0]["message_id"], "image-message-1")
        self.assertNotIn("url", safe_candidates[0])
        self.assertNotIn("https://private.example/image.jpg", str(safe_candidates))
        self.assertIn(
            "image-message-1",
            focus_event.get_extra("astrmai_bound_message_ids"),
        )

    def test_focus_thread_does_not_attach_other_sender_image_without_reference(self):
        image_event = _FakeEvent(
            "user-2",
            "Bob",
            "",
            extras={"extracted_image_urls": ["opaque-image-reference"]},
            message_id="image-message-2",
        )
        image_event.timestamp = 100.0
        focus_event = _FakeEvent(
            "user-1",
            "Alice",
            "今天要吃什么？",
            message_id="text-message-2",
        )
        focus_event.timestamp = 110.0

        normalized = self.gate._build_normalized_events(
            [image_event, focus_event],
            self_id="bot-1",
        )
        root_candidate, _ = self.gate._resolve_thread_root(normalized[1], normalized)
        focus_thread = self.gate._build_focus_thread(
            normalized[1],
            root_candidate,
            normalized,
        )

        self.assertEqual(focus_thread.recent_media_candidates, [])
        self.assertEqual(
            focus_event.get_extra("astrmai_recent_media_candidates", []),
            [],
        )

    def test_focus_thread_attaches_other_sender_image_on_explicit_reference(self):
        image_event = _FakeEvent(
            "user-2",
            "Bob",
            "",
            extras={"extracted_image_urls": ["opaque-image-reference"]},
            message_id="image-message-3",
        )
        image_event.timestamp = 100.0
        focus_event = _FakeEvent(
            "user-1",
            "Alice",
            "刚才那张图是什么？",
            message_id="text-message-3",
        )
        focus_event.timestamp = 110.0

        normalized = self.gate._build_normalized_events(
            [image_event, focus_event],
            self_id="bot-1",
        )
        root_candidate, _ = self.gate._resolve_thread_root(normalized[1], normalized)
        focus_thread = self.gate._build_focus_thread(
            normalized[1],
            root_candidate,
            normalized,
        )

        self.assertEqual(len(focus_thread.recent_media_candidates), 1)
        self.assertEqual(
            focus_thread.recent_media_candidates[0].relation,
            "explicit_recent_reference",
        )


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

    def test_at_target_id_adapter_shape_is_detected_and_retained_when_text_is_empty(self):
        component = SimpleNamespace(type="at", target_id="bot-1")
        event = _FakeEvent("user-1", "Alice", "", components=[component])

        self.assertTrue(self.gate._is_at_bot_event(event, "bot-1"))
        retained = asyncio.run(self.gate._format_and_filter_messages([event]))
        self.assertEqual(retained, [event])

    def test_at_all_is_not_detected_or_retained_as_bot_wakeup(self):
        component = SimpleNamespace(type="at", target_id="all")
        event = _FakeEvent("user-1", "Alice", "", components=[component])

        self.assertFalse(self.gate._is_at_bot_event(event, "bot-1"))
        retained = asyncio.run(self.gate._format_and_filter_messages([event]))
        self.assertEqual(retained, [])



    def test_process_event_fast_mode_engages_on_direct_wakeup(self):
        captured = []
        store_mod = importlib.import_module(
            "astrmai.conversation.attention.group_dialogue_store"
        )
        self.gate.dialogue_store = store_mod.GroupDialogueStore()

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
        self.assertEqual(turn_context.perception.chat_id, "default:GroupMessage:group-1")
        self.assertTrue(turn_context.perception.is_strong_wakeup)
        self.assertEqual(turn_context.attention.retrieve_keys, ["CORE_ONLY"])
        self.assertTrue(turn_context.attention.is_fast_mode)
        self.assertEqual(len(captured), 1)
        actor_tail = asyncio.run(
            self.gate.dialogue_store.get_actor_tail(
                "default:GroupMessage:group-1",
                current_sender_id="user-1",
                now=123.1,
            )
        )
        pending = asyncio.run(
            self.gate.dialogue_store.get_pending_direct_items(
                "default:GroupMessage:group-1",
                current_sender_id="user-1",
                now=123.1,
            )
        )
        self.assertEqual([segment.content for segment in actor_tail], ["AstrMai"])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].event_id, actor_tail[0].event_id)

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
        traced = []
        self.gate.judge = _SequenceJudge(["WAIT", "PASS"])
        self.gate._compute_debounce_delay = lambda *args, **kwargs: 0.0

        async def fake_sys2(event, events):
            captured.append((event, events))

        async def trace_turn(chat_id, event, *, status, reply_text=None):
            traced.append((chat_id, status, reply_text))

        self.gate.sys2_process = fake_sys2
        self.gate.bind_turn_trace_callback(trace_turn)

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
        self.assertEqual(
            traced,
            [("default:GroupMessage:group-1", "skipped_wait", None)],
        )

    def test_judge_ignore_keeps_focus_as_window_only(self):
        captured = []
        traced = []
        self.gate.judge = _SequenceJudge(["IGNORE"])
        self.gate._compute_debounce_delay = lambda *args, **kwargs: 0.0
        self.gate.sys2_process = lambda event, events: captured.append((event, events))

        async def trace_turn(chat_id, event, *, status, reply_text=None):
            traced.append((chat_id, status, reply_text))

        self.gate.bind_turn_trace_callback(trace_turn)

        async def _run():
            session = self.gate_mod.SessionContext()
            session.accumulation_pool = [_FakeEvent("user-1", "Alice", "skip me", extras={"astrmai_timestamp": time.time()})]
            session.is_evaluating = True
            await self.gate._debounce_and_judge("default:GroupMessage:group-1", session, "bot-1")
            return [event.message_str for event in session.attention_window]

        retained = asyncio.run(_run())

        self.assertEqual(captured, [])
        self.assertEqual(retained, ["skip me"])
        self.assertEqual(
            traced,
            [("default:GroupMessage:group-1", "skipped_ignore", None)],
        )

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

        # OPT-08/RT-03: 本测试锁定的是"前置 mood"旧路径的去重语义，显式关闭后置开关
        self.gate._mood_post_judge_enabled = lambda: False
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

    def test_router_skips_separate_mood_call_for_micro_utterance(self):
        mood_calls = []

        async def _update_mood(chat_id, text):
            mood_calls.append((chat_id, text))
            return "neutral", 0.0

        # OPT-08/RT-03: micro-utterance 跳过是"前置 mood"路径的子行为，显式关闭后置开关
        self.gate._mood_post_judge_enabled = lambda: False
        self.gate.state_engine.update_mood = _update_mood
        judge = _SequenceJudge(["PASS"])
        self.gate.judge = judge
        router_mod = importlib.import_module("astrmai.conversation.attention.decision_router")
        router_mod = importlib.reload(router_mod)
        router = router_mod.AttentionDecisionRouter(self.gate)
        focus = _FakeEvent("user-1", "Alice", "行")

        decision = asyncio.run(
            router.evaluate(
                "default:GroupMessage:group-1",
                focus,
                {"core_events": [focus]},
                [focus],
                is_strong_wakeup=False,
            )
        )

        self.assertEqual(decision.action, "PASS")
        self.assertEqual(mood_calls, [])
        self.assertEqual(len(judge.calls), 1)
        self.assertEqual(
            focus.get_extra("astrmai_primary_mood_skipped_reason"),
            "micro_utterance",
        )

    def test_router_force_passes_short_continuation_immediately_after_bot(self):
        judge = _SequenceJudge(["IGNORE"])
        self.gate.judge = judge
        self.gate.state_engine.bot_id = "bot-1"
        router_mod = importlib.import_module("astrmai.conversation.attention.decision_router")
        router_mod = importlib.reload(router_mod)
        router = router_mod.AttentionDecisionRouter(self.gate)
        bot_event = _FakeEvent(
            "bot-1",
            "AstrMai",
            "还想继续聊这个吗？",
            extras={"astrmai_timestamp": 100.0},
        )
        focus = _FakeEvent(
            "user-1",
            "Alice",
            "继续",
            extras={"astrmai_timestamp": 120.0},
        )

        decision = asyncio.run(
            router.evaluate(
                "default:GroupMessage:group-1",
                focus,
                SimpleNamespace(root_reason=""),
                [bot_event, focus],
                is_strong_wakeup=False,
            )
        )

        self.assertEqual(decision.action, "PASS")
        self.assertEqual(decision.reason, "prefilter:active_bot_continuation")
        self.assertEqual(judge.calls, [])
        self.assertEqual(
            focus.get_extra("astrmai_attention_prefilter_action"),
            "force_pass",
        )

    def test_router_force_passes_committed_target_short_continuation(self):
        judge = _SequenceJudge(["IGNORE"])
        self.gate.judge = judge

        class _Store:
            async def get_recent_bot_turns(self, chat_id, **kwargs):
                return [
                    SimpleNamespace(
                        turn_id="turn-1",
                        timestamp=100.0,
                        target_sender_id="user-1",
                        source_event_ids=["source-1"],
                        topic_epoch=1,
                    )
                ]

        self.gate.dialogue_store = _Store()
        router_mod = importlib.import_module("astrmai.conversation.attention.decision_router")
        router_mod = importlib.reload(router_mod)
        router = router_mod.AttentionDecisionRouter(self.gate)
        focus = _FakeEvent(
            "user-1",
            "Alice",
            "继续",
            extras={
                "astrmai_timestamp": 120.0,
                "astrmai_dialog_history_policy": {"topic_epoch": 1},
            },
        )

        decision = asyncio.run(
            router.evaluate(
                "default:GroupMessage:group-1",
                focus,
                SimpleNamespace(root_reason=""),
                [focus],
                is_strong_wakeup=False,
            )
        )

        self.assertEqual(decision.action, "PASS")
        self.assertEqual(judge.calls, [])
        self.assertTrue(focus.get_extra("astrmai_judge_avoided"))
        self.assertEqual(focus.get_extra("astrmai_participation_score"), 75)

    def test_router_does_not_transfer_committed_target_to_other_actor(self):
        judge = _SequenceJudge(["IGNORE"])
        self.gate.judge = judge

        class _Store:
            async def get_recent_bot_turns(self, chat_id, **kwargs):
                self.requested_sender = kwargs["target_sender_id"]
                return []

        store = _Store()
        self.gate.dialogue_store = store
        router_mod = importlib.import_module("astrmai.conversation.attention.decision_router")
        router_mod = importlib.reload(router_mod)
        router = router_mod.AttentionDecisionRouter(self.gate)
        focus = _FakeEvent("user-2", "Bob", "继续")

        decision = asyncio.run(
            router.evaluate(
                "default:GroupMessage:group-1",
                focus,
                SimpleNamespace(root_reason=""),
                [focus],
                is_strong_wakeup=False,
            )
        )

        self.assertEqual(decision.action, "IGNORE")
        self.assertEqual(store.requested_sender, "user-2")
        self.assertEqual(len(judge.calls), 1)
        self.assertFalse(focus.get_extra("astrmai_judge_avoided"))

    def test_router_keeps_ambiguous_short_group_message_for_judge(self):
        judge = _SequenceJudge(["IGNORE"])
        self.gate.judge = judge
        self.gate.state_engine.bot_id = "bot-1"
        router_mod = importlib.import_module("astrmai.conversation.attention.decision_router")
        router_mod = importlib.reload(router_mod)
        router = router_mod.AttentionDecisionRouter(self.gate)
        other_user = _FakeEvent("user-2", "Bob", "你觉得呢")
        focus = _FakeEvent("user-1", "Alice", "继续")

        decision = asyncio.run(
            router.evaluate(
                "default:GroupMessage:group-1",
                focus,
                SimpleNamespace(root_reason=""),
                [other_user, focus],
                is_strong_wakeup=False,
            )
        )

        self.assertEqual(decision.action, "IGNORE")
        self.assertEqual(len(judge.calls), 1)
        self.assertEqual(
            focus.get_extra("astrmai_attention_prefilter_action"),
            "need_judge",
        )

    def test_router_drops_empty_unmentioned_group_event_without_judge(self):
        judge = _SequenceJudge(["PASS"])
        self.gate.judge = judge
        router_mod = importlib.import_module("astrmai.conversation.attention.decision_router")
        router_mod = importlib.reload(router_mod)
        router = router_mod.AttentionDecisionRouter(self.gate)
        focus = _FakeEvent("user-1", "Alice", "")

        decision = asyncio.run(
            router.evaluate(
                "default:GroupMessage:group-1",
                focus,
                SimpleNamespace(root_reason=""),
                [focus],
                is_strong_wakeup=False,
            )
        )

        self.assertEqual(decision.action, "IGNORE")
        self.assertEqual(decision.reason, "prefilter:empty_group_event")
        self.assertEqual(judge.calls, [])

    def test_router_keeps_empty_private_event_on_judge_path(self):
        judge = _SequenceJudge(["PASS"])
        self.gate.judge = judge
        router_mod = importlib.import_module("astrmai.conversation.attention.decision_router")
        router_mod = importlib.reload(router_mod)
        router = router_mod.AttentionDecisionRouter(self.gate)
        focus = _FakePrivateEvent("user-1", "Alice", "")

        decision = asyncio.run(
            router.evaluate(
                "default:FriendMessage:user-1",
                focus,
                SimpleNamespace(root_reason=""),
                [focus],
                is_strong_wakeup=False,
            )
        )

        self.assertEqual(decision.action, "PASS")
        self.assertEqual(len(judge.calls), 1)
        self.assertEqual(
            focus.get_extra("astrmai_attention_prefilter_action"),
            "need_judge",
        )

    def test_router_ignores_peer_poke_when_judge_times_out(self):
        async def _slow_evaluate(*args, **kwargs):
            await asyncio.sleep(0.05)
            return SimpleNamespace(action="PASS")

        self.gate.config.attention.judge_timeout = 0.001
        self.gate.judge = SimpleNamespace(evaluate=_slow_evaluate)
        router_mod = importlib.import_module("astrmai.conversation.attention.decision_router")
        router_mod = importlib.reload(router_mod)
        router = router_mod.AttentionDecisionRouter(self.gate)
        focus = _FakeEvent(
            "user-1",
            "Alice",
            "Alice 戳了 Bob 一下，这是群友之间的轻互动。",
            extras={"astrmai_interaction_kind": "peer_poke"},
        )

        decision = asyncio.run(
            router.evaluate(
                "default:GroupMessage:group-1",
                focus,
                {"core_events": [focus]},
                [focus],
                is_strong_wakeup=False,
            )
        )

        self.assertEqual(decision.action, "IGNORE")
        self.assertEqual(decision.reason, "peer_poke_judge_timeout")
        self.assertEqual(focus.get_extra("astrmai_judge_outcome"), "timeout")
        self.assertTrue(focus.get_extra("astrmai_judge_timeout"))

    def test_router_keeps_private_chat_responsive_when_judge_times_out(self):
        async def _slow_evaluate(*args, **kwargs):
            await asyncio.sleep(0.05)
            return SimpleNamespace(action="PASS")

        self.gate.config.attention.judge_timeout = 0.001
        self.gate.judge = SimpleNamespace(evaluate=_slow_evaluate)
        router_mod = importlib.import_module("astrmai.conversation.attention.decision_router")
        router_mod = importlib.reload(router_mod)
        router = router_mod.AttentionDecisionRouter(self.gate)
        focus = _FakePrivateEvent("user-1", "Alice", "继续刚才的话题")

        decision = asyncio.run(
            router.evaluate(
                "default:FriendMessage:user-1",
                focus,
                {"core_events": [focus]},
                [focus],
                is_strong_wakeup=False,
            )
        )

        self.assertEqual(decision.action, "PASS")
        self.assertEqual(focus.get_extra("astrmai_judge_fallback_action"), "pass")
        self.assertEqual(focus.get_extra("astrmai_judge_failure_type"), "timeout")

    def test_router_does_not_promote_unmentioned_group_timeout_to_reply(self):
        async def _slow_evaluate(*args, **kwargs):
            await asyncio.sleep(0.05)
            return SimpleNamespace(action="PASS")

        self.gate.config.attention.judge_timeout = 0.001
        self.gate.judge = SimpleNamespace(evaluate=_slow_evaluate)
        router_mod = importlib.import_module("astrmai.conversation.attention.decision_router")
        router_mod = importlib.reload(router_mod)
        router = router_mod.AttentionDecisionRouter(self.gate)
        focus = _FakeEvent("user-1", "Alice", "群友之间的普通闲聊")

        decision = asyncio.run(
            router.evaluate(
                "default:GroupMessage:group-1",
                focus,
                {"core_events": [focus]},
                [focus],
                is_strong_wakeup=False,
            )
        )

        self.assertEqual(decision.action, "WAIT")
        self.assertEqual(focus.get_extra("astrmai_judge_fallback_action"), "wait")
        self.assertEqual(focus.get_extra("astrmai_judge_fallback_reason"), "group_unmentioned")

    def test_router_treats_empty_judge_result_like_a_failure(self):
        async def _empty_evaluate(*args, **kwargs):
            return SimpleNamespace(action="")

        self.gate.judge = SimpleNamespace(evaluate=_empty_evaluate)
        router_mod = importlib.import_module("astrmai.conversation.attention.decision_router")
        router_mod = importlib.reload(router_mod)
        router = router_mod.AttentionDecisionRouter(self.gate)
        focus = _FakePrivateEvent("user-1", "Alice", "你还记得吗")

        decision = asyncio.run(
            router.evaluate(
                "default:FriendMessage:user-1",
                focus,
                {"core_events": [focus]},
                [focus],
                is_strong_wakeup=False,
            )
        )

        self.assertEqual(decision.action, "PASS")
        self.assertEqual(focus.get_extra("astrmai_judge_outcome"), "empty_response")
        self.assertEqual(focus.get_extra("astrmai_judge_failure_type"), "empty_response")

    def test_process_event_resumes_private_wait_without_consuming_message(self):
        calls = []

        async def _update_mood(chat_id, text):
            calls.append((chat_id, text))
            return "sad", -0.35

        manager = _FakePrivateChatManager()
        self.gate.private_chat_manager = manager
        # OPT-08/RT-03: 本测试断言即时 mood 应用与私聊 wait 复活的组合，走前置路径
        self.gate._mood_post_judge_enabled = lambda: False
        self.gate.state_engine.update_mood = _update_mood
        event = _FakePrivateEvent("user-1", "Alice", "今天有点难受")

        status = asyncio.run(self.gate.process_event(event))

        self.assertEqual(status, "BUFFERED")
        self.assertEqual(calls, [("default:FriendMessage:user-1", "今天有点难受")])
        self.assertEqual(manager.calls, [("user-1", "今天有点难受", "default:FriendMessage:user-1")])
        self.assertTrue(event.get_extra("astrmai_primary_mood_applied"))
        self.assertEqual(event.get_extra("astrmai_primary_mood_tag"), "sad")
        self.assertAlmostEqual(event.get_extra("astrmai_primary_mood_value"), -0.35)
        self.assertEqual(event.get_extra("astrmai_primary_mood_source"), "attention_ingress")

    def test_private_message_without_active_wait_continues_normal_attention(self):
        manager = _FakePrivateChatManager(signaled=False)
        self.gate.private_chat_manager = manager
        event = _FakePrivateEvent("user-1", "Alice", "hello")

        async def _run():
            status = await self.gate.process_event(event)
            await asyncio.sleep(0)
            for task in list(self.gate._session_tasks):
                task.cancel()
            await asyncio.gather(*list(self.gate._session_tasks), return_exceptions=True)
            return status

        status = asyncio.run(_run())

        self.assertEqual(status, "BUFFERED")
        self.assertEqual(manager.calls, [("user-1", "hello", "default:FriendMessage:user-1")])

    def test_private_messages_wait_for_quiet_window_and_dispatch_once_in_order(self):
        coordinator_mod = importlib.import_module(
            "astrmai.conversation.attention.private_turn_coordinator"
        )
        self.gate.config.private_chat = SimpleNamespace(input_settle_sec=0.03)
        self.gate.private_turn_coordinator = coordinator_mod.PrivateTurnCoordinator(
            config=self.gate.config,
            image_resolver=None,
            visual_cortex=None,
        )
        captured = []

        async def fake_sys2(event, events):
            captured.append([item.message_str for item in events])

        self.gate.sys2_process = fake_sys2

        async def _run():
            first = _FakePrivateEvent("user-1", "Alice", "第一句")
            second = _FakePrivateEvent("user-1", "Alice", "第二句")
            first_status = await self.gate.process_event(first)
            await asyncio.sleep(0.015)
            second_status = await self.gate.process_event(second)
            await asyncio.sleep(0.08)
            return first_status, second_status

        first_status, second_status = asyncio.run(_run())

        self.assertEqual((first_status, second_status), ("BUFFERED", "BUFFERED"))
        self.assertEqual(captured, [["第一句", "第二句"]])

    def test_private_batch_updates_mood_once_for_fragmented_input(self):
        coordinator_mod = importlib.import_module(
            "astrmai.conversation.attention.private_turn_coordinator"
        )
        self.gate.config.private_chat = SimpleNamespace(input_settle_sec=0.01)
        self.gate.private_turn_coordinator = coordinator_mod.PrivateTurnCoordinator(
            config=self.gate.config,
            image_resolver=None,
            visual_cortex=None,
        )
        mood_inputs = []

        async def update_mood(chat_id, text):
            mood_inputs.append((chat_id, text))
            return "neutral", 0.0

        self.gate.state_engine.update_mood = update_mood
        self.gate.sys2_process = lambda event, events: asyncio.sleep(0)

        async def _run():
            await self.gate.process_event(_FakePrivateEvent("user-1", "Alice", "第一句"))
            await asyncio.sleep(0.005)
            await self.gate.process_event(_FakePrivateEvent("user-1", "Alice", "第二句"))
            await asyncio.sleep(0.05)

        asyncio.run(_run())

        self.assertEqual(
            mood_inputs,
            [("default:FriendMessage:user-1", "第一句\n第二句")],
        )

    def test_private_message_during_slow_reply_is_carried_into_next_batch(self):
        coordinator_mod = importlib.import_module(
            "astrmai.conversation.attention.private_turn_coordinator"
        )
        self.gate.config.private_chat = SimpleNamespace(input_settle_sec=0.01)
        self.gate.private_turn_coordinator = coordinator_mod.PrivateTurnCoordinator(
            config=self.gate.config,
            image_resolver=None,
            visual_cortex=None,
        )
        captured = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def fake_sys2(event, events):
            captured.append([item.message_str for item in events])
            if len(captured) == 1:
                first_started.set()
                await release_first.wait()
            return True

        self.gate.sys2_process = fake_sys2

        async def _run():
            first = _FakePrivateEvent(
                "user-1",
                "Alice",
                "第一句还没答完",
                message_id="slow-first",
            )
            second = _FakePrivateEvent(
                "user-1",
                "Alice",
                "第二句继续补充",
                message_id="slow-second",
            )
            first_status = await self.gate.process_event(first)
            await first_started.wait()
            second_status = await self.gate.process_event(second)
            release_first.set()
            await asyncio.sleep(0.08)
            return first_status, second_status

        first_status, second_status = asyncio.run(_run())

        self.assertEqual((first_status, second_status), ("BUFFERED", "BUFFERED"))
        self.assertEqual(
            captured,
            [
                ["第一句还没答完"],
                ["第一句还没答完", "第二句继续补充"],
            ],
        )

    def test_private_direct_wakeup_still_uses_buffered_serial_path(self):
        coordinator_mod = importlib.import_module(
            "astrmai.conversation.attention.private_turn_coordinator"
        )
        self.gate.config.private_chat = SimpleNamespace(input_settle_sec=0.01)
        self.gate.private_turn_coordinator = coordinator_mod.PrivateTurnCoordinator(
            config=self.gate.config,
            image_resolver=None,
            visual_cortex=None,
        )
        event = _FakePrivateEvent("user-1", "Alice", "AstrMai", extras={"wakeup": True})

        async def _run():
            status = await self.gate.process_event(event)
            await asyncio.sleep(0.03)
            return status

        status = asyncio.run(_run())

        self.assertEqual(status, "BUFFERED")

    def test_private_message_arriving_during_vision_barrier_joins_same_reply_batch(self):
        class _BlockingBarrier:
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.calls = 0

            async def wait_for_input_stability(self, session):
                return None

            async def prepare_batch(self, events, chat_id):
                self.calls += 1
                if self.calls == 1:
                    self.started.set()
                    await self.release.wait()

        barrier = _BlockingBarrier()
        self.gate.private_turn_coordinator = barrier
        captured = []

        async def fake_sys2(event, events):
            captured.append([item.message_str for item in events])

        self.gate.sys2_process = fake_sys2

        async def _run():
            first = _FakePrivateEvent("user-1", "Alice", "先看图片")
            second = _FakePrivateEvent("user-1", "Alice", "补充说明")
            await self.gate.process_event(first)
            await barrier.started.wait()
            await self.gate.process_event(second)
            barrier.release.set()
            await asyncio.sleep(0.05)

        asyncio.run(_run())

        self.assertGreaterEqual(barrier.calls, 2)
        self.assertEqual(captured, [["先看图片", "补充说明"]])

    def test_private_image_then_text_uses_latest_text_with_merged_vision_and_generation(self):
        coordinator_mod = importlib.import_module(
            "astrmai.conversation.attention.private_turn_coordinator"
        )
        turn_mod = importlib.import_module("astrmai.conversation.contracts.turn_identity")

        class _BlockingBarrier(coordinator_mod.PrivateTurnCoordinator):
            def __init__(self, config):
                super().__init__(config=config, image_resolver=None, visual_cortex=None)
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.calls = 0

            async def wait_for_input_stability(self, session):
                return None

            async def prepare_batch(self, events, chat_id):
                self.calls += 1
                if self.calls == 1:
                    self.started.set()
                    await self.release.wait()
                for event in events:
                    if event.get_extra("test_image", False):
                        event.set_extra("direct_image_refs", ["resolved.jpg"])
                        event.set_extra("extracted_image_refs", ["resolved.jpg"])
                        event.set_extra(
                            "astrmai_vision_records",
                            [{
                                "picid": "pic-1",
                                "source_ref": "resolved.jpg",
                                "type": "image",
                                "description": "两个人正在泡温泉",
                                "emotion_tags": ["放松"],
                            }],
                        )
                        event.set_extra("astrmai_vision_barrier_complete", True)
                        event.set_extra("astrmai_vision_barrier_failed", False)

        self.gate.config.private_chat = SimpleNamespace(input_settle_sec=0.0)
        barrier = _BlockingBarrier(self.gate.config)
        self.gate.private_turn_coordinator = barrier
        captured = []

        async def fake_sys2(event, events):
            captured.append((event, list(events), event.get_extra("astrmai_focus_thread_context")))

        self.gate.sys2_process = fake_sys2
        first = _FakePrivateEvent(
            "user-1",
            "Alice",
            "[图片]",
            extras={"test_image": True, "direct_image_refs": ["pending.jpg"]},
            message_id="image-message",
        )
        second = _FakePrivateEvent(
            "user-1",
            "Alice",
            "还记得我们一起去泡温泉吗？",
            message_id="text-message",
        )
        first.timestamp = 100.0
        second.timestamp = 101.0
        first.set_extra(
            "astrmai_turn_identity",
            turn_mod.TurnIdentity(
                mode="private",
                chat_id=first.unified_msg_origin,
                thread_id=f"private:{first.unified_msg_origin}",
                generation=1,
                sender_id="user-1",
                input_message_ids=("image-message",),
                created_at=100.0,
            ),
        )
        second.set_extra(
            "astrmai_turn_identity",
            turn_mod.TurnIdentity(
                mode="private",
                chat_id=second.unified_msg_origin,
                thread_id=f"private:{second.unified_msg_origin}",
                generation=2,
                sender_id="user-1",
                input_message_ids=("text-message",),
                created_at=101.0,
            ),
        )

        async def _run():
            await self.gate.process_event(first)
            await barrier.started.wait()
            await self.gate.process_event(second)
            barrier.release.set()
            await asyncio.sleep(0.08)

        asyncio.run(_run())

        self.assertEqual(len(captured), 1)
        focus_event, thread_events, focus_context = captured[0]
        self.assertIs(focus_event, second)
        self.assertIn(first, thread_events)
        self.assertIn("两个人正在泡温泉", focus_event.get_extra("astrmai_rich_text"))
        self.assertEqual(focus_event.get_extra("direct_image_refs"), ["resolved.jpg"])
        self.assertTrue(focus_event.get_extra("is_private_chat"))
        turn = focus_event.get_extra("astrmai_turn_identity")
        self.assertEqual(turn.generation, 2)
        self.assertEqual(turn.input_message_ids, ("image-message", "text-message"))
        self.assertEqual(
            focus_context.freshness_budget.created_at,
            focus_event.get_extra("astrmai_timestamp"),
        )
        self.assertEqual(focus_context.vision_bundle.image_urls, ["resolved.jpg"])

    def test_private_focus_prefers_latest_actionable_text_but_group_policy_is_unchanged(self):
        image = _FakePrivateEvent(
            "user-1",
            "Alice",
            "[图片]",
            extras={"direct_image_refs": ["image.jpg"]},
        )
        text_event = _FakePrivateEvent("user-1", "Alice", "这张图好看吗？")
        normalized = self.gate._build_normalized_events([image, text_event], "bot-1")

        private_focus, _, private_reason = self.gate._select_focus_event(
            [image, text_event],
            "bot-1",
            normalized_events=normalized,
            is_private=True,
        )
        group_focus, _, group_reason = self.gate._select_focus_event(
            [image, text_event],
            "bot-1",
            normalized_events=normalized,
            is_private=False,
        )

        self.assertIs(private_focus, text_event)
        self.assertEqual(private_reason, "private_latest_actionable_text")
        self.assertIs(group_focus, image)
        self.assertEqual(group_reason, "direct_vision_request")

    def test_private_system2_runs_are_serial_for_same_chat(self):
        class _ImmediateCoordinator:
            async def wait_for_input_stability(self, session):
                return None

            async def prepare_batch(self, events, chat_id):
                return None

        self.gate.private_turn_coordinator = _ImmediateCoordinator()
        first_started = asyncio.Event()
        first_release = asyncio.Event()
        calls = []
        active = 0
        max_active = 0

        async def fake_sys2(event, events):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            calls.append(event.message_str)
            try:
                if len(calls) == 1:
                    first_started.set()
                    await first_release.wait()
            finally:
                active -= 1

        self.gate.sys2_process = fake_sys2

        async def _run():
            await self.gate.process_event(_FakePrivateEvent("user-1", "Alice", "第一轮"))
            await first_started.wait()
            await self.gate.process_event(_FakePrivateEvent("user-1", "Alice", "第二轮"))
            await asyncio.sleep(0.01)
            calls_before_release = list(calls)
            first_release.set()
            await asyncio.sleep(0.05)
            return calls_before_release

        calls_before_release = asyncio.run(_run())

        self.assertEqual(calls_before_release, ["第一轮"])
        self.assertEqual(calls, ["第一轮", "第二轮"])
        self.assertEqual(max_active, 1)

    def test_private_mood_update_runs_after_vision_context_is_ready(self):
        class _VisionCoordinator:
            async def wait_for_input_stability(self, session):
                return None

            async def prepare_batch(self, events, chat_id):
                for event in events:
                    event.set_extra("astrmai_rich_text", "你看\n[图片转述：一只白猫坐在窗边]")
                    event.set_extra("astrmai_vision_barrier_complete", True)

        mood_inputs = []

        async def update_mood(chat_id, text):
            mood_inputs.append(text)
            return "calm", 0.1

        self.gate.private_turn_coordinator = _VisionCoordinator()
        self.gate.state_engine.update_mood = update_mood

        async def _run():
            await self.gate.process_event(_FakePrivateEvent("user-1", "Alice", "你看"))
            await asyncio.sleep(0.03)

        asyncio.run(_run())

        self.assertEqual(mood_inputs, ["你看\n[图片转述：一只白猫坐在窗边]"])

    def test_private_stale_topic_requests_confirmation_without_judge_or_system2(self):
        from astrmai.conversation.planning.conversation_continuity import ConversationContinuityStore

        store = ConversationContinuityStore()
        store.record(
            chat_id="default:FriendMessage:user-1",
            focus_preview="一起去泡温泉的安排",
            goal_summary="确认温泉行程",
            social_intent="answer",
            action_taken="reply",
            now=1000.0,
        )
        self.gate.config.private_chat = SimpleNamespace(input_settle_sec=0.0)
        gate = self.gate_mod.AttentionGate(
            state_engine=self.gate.state_engine,
            judge=self.gate.judge,
            sensors=self.gate.sensors,
            system2_callback=None,
            private_turn_coordinator=SimpleNamespace(
                wait_for_input_stability=lambda session: asyncio.sleep(0),
                prepare_batch=lambda events, chat_id: asyncio.sleep(0),
            ),
            conversation_continuity=store,
        )
        gate._schedule_compaction_task = lambda *args, **kwargs: None
        gate._compute_debounce_delay = lambda *args, **kwargs: 0.0
        judge_calls = []
        system2_calls = []
        sent = []
        traced = []

        async def unexpected_judge(*args, **kwargs):
            judge_calls.append((args, kwargs))
            raise AssertionError("stale topic confirmation must not call Judge")

        async def unexpected_system2(*args, **kwargs):
            system2_calls.append((args, kwargs))
            raise AssertionError("stale topic confirmation must not call System2")

        async def send(result):
            sent.append(result)

        async def trace_turn(chat_id, traced_event, *, status, reply_text=None):
            traced.append((chat_id, traced_event, status, reply_text))

        gate._evaluate_judge_gate = unexpected_judge
        gate.sys2_process = unexpected_system2
        gate.bind_turn_trace_callback(trace_turn)
        event = _FakePrivateEvent("user-1", "Alice", "温泉后来怎么样了")
        event.send = send
        event.plain_result = lambda text: text

        async def _run():
            session = self.gate_mod.SessionContext()
            session.accumulation_pool = [event]
            session.is_evaluating = True
            await gate._debounce_and_judge(
                "default:FriendMessage:user-1",
                session,
                "bot-1",
                is_private=True,
            )

        asyncio.run(_run())

        self.assertEqual(judge_calls, [])
        self.assertEqual(system2_calls, [])
        self.assertEqual(len(sent), 1)
        self.assertIn("还要接着聊吗", sent[0])
        self.assertEqual(event.get_extra("judge_action"), "TOPIC_CONFIRM")
        self.assertTrue(event.get_extra("astrmai_topic_confirmation_sent"))
        self.assertEqual(len(traced), 1)
        self.assertEqual(traced[0][0], "default:FriendMessage:user-1")
        self.assertIs(traced[0][1], event)
        self.assertEqual(traced[0][2], "executed_topic_confirmation")
        self.assertEqual(traced[0][3], sent[0])

    def test_private_topic_confirmation_direct_send_blocks_internal_event_envelope(self):
        event = _FakePrivateEvent("user-1", "Alice", ".")
        sent = []

        async def send(result):
            sent.append(result)

        event.send = send
        event.plain_result = lambda text: text
        leaked = (
            "我们之前在聊“[事件=1727617753 | 发言人=恸（ID:516779421） | "
            "角色=成员 | 类型=image | 来源=original | 媒体=图片:1]”，还要继续吗？"
        )

        sent_ok = asyncio.run(self.gate._send_private_topic_confirmation(event, leaked))

        self.assertTrue(sent_ok)
        self.assertEqual(len(sent), 1)
        self.assertNotIn("事件=", sent[0])
        self.assertNotIn("516779421", sent[0])
        self.assertTrue(event.get_extra("astrmai_topic_confirmation_safe_fallback"))
        self.assertTrue(event.get_extra("astrmai_internal_context_leak_blocked"))
        self.assertEqual(
            event.get_extra("astrmai_topic_confirmation_guard_reason"),
            "internal_event_envelope",
        )

    def test_group_direct_image_waits_for_visual_context_before_fast_dispatch(self):
        class _GroupVisionBarrier:
            def __init__(self):
                self.calls = []

            def refresh_config(self, config):
                return None

            async def prepare_direct_event(self, event, chat_id):
                self.calls.append((event.message_str, chat_id))
                event.set_extra("astrmai_rich_text", "你看\n[图片转述：一只白猫坐在窗边]")
                event.set_extra("astrmai_vision_barrier_complete", True)

        barrier = _GroupVisionBarrier()
        self.gate.private_turn_coordinator = barrier
        captured = []

        async def fake_sys2(event, events):
            captured.append(
                (
                    event.get_extra("astrmai_rich_text", ""),
                    event.get_extra("astrmai_vision_barrier_complete", False),
                )
            )

        self.gate.sys2_process = fake_sys2
        event = _FakeEvent("user-1", "Alice", "你看", extras={"wakeup": True})

        async def _run():
            status = await self.gate.process_event(event)
            await asyncio.sleep(0.02)
            return status

        status = asyncio.run(_run())

        self.assertEqual(status, "ENGAGED")
        self.assertEqual(
            barrier.calls,
            [("你看", "default:GroupMessage:group-1")],
        )
        self.assertEqual(captured, [("你看\n[图片转述：一只白猫坐在窗边]", True)])

    def test_group_required_vision_failure_stops_before_dispatch(self):
        class _RequiredVisionBarrier:
            async def prepare_direct_event(self, event, chat_id):
                event.set_extra("astrmai_vision_required_failed", True)
                return SimpleNamespace(should_abort=True)

        self.gate.private_turn_coordinator = _RequiredVisionBarrier()
        system2_calls = []
        self.gate.sys2_process = lambda event, events: system2_calls.append((event, events))
        event = _FakeEvent("user-1", "Alice", "你看", extras={"wakeup": True})
        sent = []

        async def send(result):
            sent.append(result)

        event.send = send
        event.plain_result = lambda text: text

        status = asyncio.run(self.gate.process_event(event))

        self.assertEqual(status, "VISION_REQUIRED_FAILED")
        self.assertEqual(system2_calls, [])
        self.assertEqual(sent, ["这张图片暂时没有识别成功，请稍后再发一次。"])
        self.assertTrue(event.get_extra("astrmai_vision_failure_notice_sent"))

    def test_private_required_vision_failure_stops_before_mood_judge_and_system2(self):
        class _RequiredVisionBarrier:
            async def wait_for_input_stability(self, session):
                return None

            async def prepare_batch(self, events, chat_id):
                events[-1].set_extra("astrmai_vision_required_failed", True)
                return SimpleNamespace(should_abort=True)

        mood_calls = []
        judge_calls = []
        system2_calls = []
        self.gate.private_turn_coordinator = _RequiredVisionBarrier()
        self.gate._apply_primary_mood_update = lambda *args: mood_calls.append(args)
        self.gate._evaluate_judge_gate = lambda *args, **kwargs: judge_calls.append((args, kwargs))
        self.gate.sys2_process = lambda *args: system2_calls.append(args)
        event = _FakePrivateEvent("user-1", "Alice", "这张图是什么")
        sent = []

        async def send(result):
            sent.append(result)

        event.send = send
        event.plain_result = lambda text: text

        async def run():
            session = self.gate_mod.SessionContext()
            session.accumulation_pool = [event]
            session.is_evaluating = True
            await self.gate._debounce_and_judge(
                event.unified_msg_origin,
                session,
                "bot-1",
                is_private=True,
            )

        asyncio.run(run())

        self.assertEqual(mood_calls, [])
        self.assertEqual(judge_calls, [])
        self.assertEqual(system2_calls, [])
        self.assertEqual(sent, ["这张图片暂时没有识别成功，请稍后再发一次。"])

    def test_group_perception_uses_unified_origin_as_chat_id(self):
        event = _FakeEvent("user-1", "Alice", "hello")
        event.unified_msg_origin = "napcat-a:GroupMessage:group-1"

        perception = self.gate.perception_builder.build(event)

        self.assertEqual(perception.chat_id, "napcat-a:GroupMessage:group-1")

    def test_immediate_engage_preserves_existing_accumulation_pool(self):
        pending = _FakeEvent("user-2", "Bob", "pending")
        current = _FakeEvent("user-1", "Alice", "AstrMai", extras={"wakeup": True})

        async def _run():
            session = await self.gate._get_or_create_session(current.unified_msg_origin)
            session.accumulation_pool = [pending]
            result = await self.gate._engage_immediately(current, current.unified_msg_origin, ["ALL"], fast_mode=True)
            return result, session.accumulation_pool

        result, pool = asyncio.run(_run())

        self.assertEqual(result, "ENGAGED")
        self.assertEqual(pool, [pending])

    def test_historical_direct_wakeup_does_not_steal_focus_from_current_message(self):
        historical = _FakeEvent("user-1", "Alice", "AstrMai", extras={"wakeup": True})
        current = _FakeEvent("user-2", "Bob", "current message")

        async def _run():
            session = await self.gate._get_or_create_session(current.unified_msg_origin)
            self.gate._append_attention_window(session, [historical], timestamp=time.time())
            merged = self.gate._merge_attention_window(session, [current])
            return self.gate._select_focus_event(merged, "bot-1")[0]

        focus = asyncio.run(_run())

        self.assertIs(focus, current)

    def test_force_engage_event_bypassing_facade_gets_turn_identity(self):
        coordinator_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.chat_runtime_coordinator"
        )
        self.gate.runtime_coordinator = coordinator_mod.ChatRuntimeCoordinator()
        self.gate.config.conversation = SimpleNamespace(
            conversation_generation_enabled=True,
            group_thread_wait_enabled=True,
        )
        event = _FakeEvent(
            "user-1",
            "Alice",
            "proactive",
            extras={"astrmai_force_engage": True},
        )

        status = asyncio.run(self.gate.process_event(event))
        turn = event.get_extra("astrmai_turn_identity")

        self.assertEqual(status, "ENGAGED")
        self.assertIsNotNone(turn)
        self.assertEqual(turn.chat_id, "default:GroupMessage:group-1")
        self.assertEqual(turn.thread_id, "sender:user-1")
        self.assertEqual(turn.generation, 1)

    def test_background_system2_failure_sends_one_fallback_and_completes_proactive(self):
        event = _FakeEvent("user-1", "Alice", "hello")
        sent = []
        completed = []

        async def _send(result):
            sent.append(result)

        async def _completion(reply_sent, preview):
            completed.append((reply_sent, preview))

        async def _fail():
            raise RuntimeError("planner failed")

        event.send = _send
        event.plain_result = lambda text: text
        event.set_extra("astrmai_proactive_completion_callback", _completion)
        self.gate.config.reply = SimpleNamespace(fallback_text="fallback")

        async def _run():
            await self.gate._run_managed_system2_task(_fail(), event)
            await self.gate._handle_system2_failure(event, RuntimeError("duplicate"))

        asyncio.run(_run())

        self.assertEqual(sent, ["fallback"])
        self.assertEqual(completed, [(True, "")])
        self.assertTrue(event.get_extra("astrmai_reply_sent", False))

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
        self.assertEqual(calls, [("default:GroupMessage:group-1", "AstrMai")])
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
        expected_event_type = self.gate_mod.AstrMessageEvent

        class _Kernel:
            async def tick(self, *, chat_id, trigger, event=None):
                assert isinstance(event, expected_event_type)
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

    def test_proactive_candidate_wait_completes_without_sys2_dispatch(self):
        completed = []
        dispatched = []

        async def fake_sys2(event, events):
            dispatched.append((event, events))

        async def completion(reply_sent, reply_preview):
            completed.append((reply_sent, reply_preview))

        self.gate.judge = _SequenceJudge(["WAIT"])
        self.gate.sys2_process = fake_sys2
        self.gate._compute_debounce_delay = lambda *args, **kwargs: 0.0

        async def _run():
            session = self.gate_mod.SessionContext()
            event = _FakeEvent(
                "astrmai_proactive_candidate",
                "主动开口候选",
                "[主动开口候选]\n候选指引：说一句轻松的话",
                extras={
                    "astrmai_is_proactive_event": True,
                    "astrmai_proactive_candidate": True,
                    "astrmai_proactive_completion_callback": completion,
                },
            )
            session.accumulation_pool = [event]
            session.is_evaluating = True
            await self.gate._debounce_and_judge("default:GroupMessage:group-1", session, "bot-1")
            return event

        event = asyncio.run(_run())

        self.assertEqual(dispatched, [])
        self.assertEqual(completed, [(False, "")])
        self.assertTrue(event.get_extra("astrmai_proactive_completed"))
        self.assertEqual(event.get_extra("judge_action"), "WAIT")

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

    def test_worker_failure_recovery_only_resets_failed_session(self):
        async def _run():
            failed_session = self.gate_mod.SessionContext()
            failed_session.is_evaluating = True
            failed_session.accumulation_pool = [_FakeEvent("user-1", "Alice", "retry me")]
            healthy_session = self.gate_mod.SessionContext()
            healthy_session.is_evaluating = True
            self.gate.focus_pools["failed-chat"] = failed_session
            self.gate.focus_pools["healthy-chat"] = healthy_session

            async def broken_worker():
                raise RuntimeError("boom")

            task = asyncio.create_task(broken_worker())
            task._worker_context = SimpleNamespace(
                chat_id="failed-chat",
                session=failed_session,
                self_id="bot-1",
            )
            self.gate._session_tasks.add(task)
            task.add_done_callback(self.gate._handle_session_worker_result)
            await asyncio.sleep(0.05)

            pending = list(getattr(self.gate, "_background_tasks", set()) or [])
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            spawned = list(getattr(self.gate, "_session_tasks", set()) or [])
            for spawned_task in spawned:
                spawned_task.cancel()
            if spawned:
                await asyncio.gather(*spawned, return_exceptions=True)
            return failed_session, healthy_session, spawned

        failed_session, healthy_session, spawned = asyncio.run(_run())

        self.assertFalse(failed_session.is_evaluating)
        self.assertTrue(healthy_session.is_evaluating)
        self.assertTrue(spawned)

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
