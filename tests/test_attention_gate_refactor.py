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
            extras={"extracted_image_urls": ["https://example.com/reply.jpg"]},
        )

        context = self.gate._resolve_event_context(event)

        self.assertEqual(context["extracted_images"], ["https://example.com/reply.jpg"])
        self.assertFalse(context["is_private"])



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
if __name__ == "__main__":
    unittest.main()
