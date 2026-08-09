import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _FakeEvent:
    def __init__(self, *, group_id, components, text=""):
        self.message_str = text
        self.unified_msg_origin = f"default:{'GroupMessage' if group_id else 'FriendMessage'}:{group_id or 'user-1'}"
        self.message_obj = SimpleNamespace(message=components, self_id="bot-1")
        self._group_id = group_id
        self._extra = {}
        self.bot = SimpleNamespace(api=SimpleNamespace(calls=[]))

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return "12345"

    def get_sender_name(self):
        return "Alice"

    def get_self_id(self):
        return "bot-1"

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)


class RefactoredSensorsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.conversation.ingress.sensors", None)
        self.sensors_mod = importlib.import_module("astrmai.conversation.ingress.sensors")
        self.sensors_mod = importlib.reload(self.sensors_mod)

        async def _list_commands():
            return []

        self.sensors_mod.list_commands = _list_commands
        from astrbot.api import message_components as Comp

        self.Comp = Comp
        for name in ("Reply", "Video", "Record", "File"):
            if not hasattr(self.Comp, name):
                setattr(self.Comp, name, type(name, (), {}))
        if not hasattr(self.Comp, "Poke"):
            setattr(self.Comp, "Poke", type("Poke", (), {}))

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _config(self, *, enable_vision=True, probability=1.0):
        return SimpleNamespace(
            system1=SimpleNamespace(nicknames=[], extra_command_list=[]),
            global_settings=SimpleNamespace(command_prefixes=["/"]),
            vision=SimpleNamespace(
                enable_vision=enable_vision,
                image_recognition_probability=probability,
            ),
        )

    def _image_component(self, *, file_path=None, url=None):
        image = self.Comp.Image()
        if file_path is not None:
            image.file = file_path
        if url is not None:
            image.url = url
        return image

    def _plain_component(self, text):
        plain = self.Comp.Plain()
        plain.text = text
        return plain

    def _at_component(self, qq):
        at = self.Comp.At()
        at.qq = qq
        return at

    def _reply_component(self, chain):
        reply = self.Comp.Reply()
        reply.chain = chain
        return reply

    def _poke_component(self, target_id="bot-1"):
        poke = self.Comp.Poke()
        poke.target_id = target_id
        return poke

    def test_private_image_bypasses_probability_gate(self):
        filters = self.sensors_mod.PreFilters(self._config(probability=0.0))
        filters._commands_loaded = True

        event = _FakeEvent(
            group_id=None,
            components=[self._image_component(file_path="private.jpg")],
        )

        result = asyncio.run(filters.should_process_message(event))

        self.assertTrue(result)
        self.assertEqual(event.get_extra("extracted_image_urls"), ["private.jpg"])
        self.assertTrue(event.get_extra("vision_direct_selected"))
        self.assertEqual(event.get_extra("vision_direct_skip_reason"), "")
        self.assertTrue(event.get_extra("astrmai_is_direct_vision_request"))
        self.assertEqual(event.get_extra("direct_vision_urls"), ["private.jpg"])

    def test_private_image_respects_vision_disable_switch(self):
        filters = self.sensors_mod.PreFilters(self._config(enable_vision=False, probability=1.0))
        filters._commands_loaded = True

        event = _FakeEvent(
            group_id=None,
            components=[self._image_component(file_path="private.jpg")],
        )

        result = asyncio.run(filters.should_process_message(event))

        self.assertTrue(result)
        self.assertFalse(event.get_extra("vision_direct_selected"))
        self.assertEqual(event.get_extra("vision_direct_skip_reason"), "disabled")
        self.assertFalse(event.get_extra("astrmai_is_direct_vision_request"))

    def test_private_image_selects_direct_vision_when_probability_hits(self):
        filters = self.sensors_mod.PreFilters(self._config(enable_vision=True, probability=1.0))
        filters._commands_loaded = True

        event = _FakeEvent(
            group_id=None,
            components=[self._image_component(file_path="private.jpg")],
        )

        result = asyncio.run(filters.should_process_message(event))

        self.assertTrue(result)
        self.assertTrue(event.get_extra("vision_direct_selected"))
        self.assertEqual(event.get_extra("vision_direct_skip_reason"), "")
        self.assertTrue(event.get_extra("astrmai_is_direct_vision_request"))
        self.assertEqual(event.get_extra("direct_vision_urls"), ["private.jpg"])

    def test_group_at_reply_image_bypasses_probability_gate(self):
        filters = self.sensors_mod.PreFilters(self._config(enable_vision=True, probability=0.0))
        filters._commands_loaded = True

        event = _FakeEvent(
            group_id="group-1",
            text="看这个",
            components=[
                self._at_component("bot-1"),
                self._plain_component("看这个"),
                self._reply_component([self._image_component(file_path="reply.jpg")]),
            ],
        )

        result = asyncio.run(filters.should_process_message(event))

        self.assertTrue(result)
        self.assertTrue(event.get_extra("vision_direct_selected"))
        self.assertEqual(event.get_extra("vision_direct_skip_reason"), "")
        self.assertEqual(event.get_extra("direct_vision_urls"), ["reply.jpg"])
        self.assertEqual(event.get_extra("extracted_image_urls"), ["reply.jpg"])

    def test_group_at_inline_image_bypasses_probability_gate(self):
        filters = self.sensors_mod.PreFilters(self._config(enable_vision=True, probability=0.0))
        filters._commands_loaded = True

        event = _FakeEvent(
            group_id="group-1",
            text="你看",
            components=[
                self._at_component("bot-1"),
                self._plain_component("你看"),
                self._image_component(file_path="inline.jpg"),
            ],
        )

        result = asyncio.run(filters.should_process_message(event))

        self.assertTrue(result)
        self.assertTrue(event.get_extra("vision_direct_selected"))
        self.assertEqual(event.get_extra("vision_direct_skip_reason"), "")
        self.assertTrue(event.get_extra("astrmai_is_direct_vision_request"))
        self.assertEqual(event.get_extra("direct_vision_urls"), ["inline.jpg"])
        self.assertEqual(event.get_extra("extracted_image_urls"), ["inline.jpg"])

    def test_pure_at_bot_message_is_not_filtered_as_empty(self):
        filters = self.sensors_mod.PreFilters(self._config())
        filters._commands_loaded = True
        event = _FakeEvent(
            group_id="group-1",
            components=[self._at_component("bot-1")],
        )

        result = asyncio.run(filters.should_process_message(event))

        self.assertTrue(result)
        self.assertTrue(filters.is_wakeup_signal(event, "bot-1"))
        self.assertTrue(event.get_extra("astrmai_at_bot_wakeup"))
        self.assertTrue(event.get_extra("astrmai_group_direct_wakeup"))

    def test_at_target_id_adapter_shape_is_recognized(self):
        filters = self.sensors_mod.PreFilters(self._config())
        filters._commands_loaded = True
        event = _FakeEvent(
            group_id="group-1",
            components=[SimpleNamespace(type="at", target_id="bot-1")],
        )

        result = asyncio.run(filters.should_process_message(event))

        self.assertTrue(result)
        self.assertTrue(filters.is_wakeup_signal(event, "bot-1"))

    def test_pure_at_all_is_not_treated_as_bot_wakeup(self):
        filters = self.sensors_mod.PreFilters(self._config())
        filters._commands_loaded = True
        event = _FakeEvent(
            group_id="group-1",
            components=[SimpleNamespace(type="at", target_id="all")],
        )

        result = asyncio.run(filters.should_process_message(event))

        self.assertFalse(result)
        self.assertFalse(filters.is_wakeup_signal(event, "bot-1"))
        self.assertFalse(event.get_extra("astrmai_at_bot_wakeup", False))

    def test_passive_group_image_keeps_original_non_direct_behavior(self):
        filters = self.sensors_mod.PreFilters(self._config(enable_vision=True, probability=1.0))
        filters._commands_loaded = True

        event = _FakeEvent(
            group_id="group-1",
            components=[self._image_component(file_path="passive.jpg")],
        )

        result = asyncio.run(filters.should_process_message(event))

        self.assertTrue(result)
        self.assertFalse(event.get_extra("vision_direct_selected"))
        self.assertEqual(event.get_extra("vision_direct_skip_reason"), "not_direct_path")
        self.assertFalse(event.get_extra("astrmai_is_direct_vision_request"))
        self.assertTrue(event.get_extra("astrmai_is_passive_image_share"))
        self.assertFalse(event.get_extra("direct_vision_urls"))
        self.assertEqual(event.get_extra("extracted_image_urls"), ["passive.jpg"])

    def test_group_reply_image_disable_switch_still_keeps_extracted_urls(self):
        filters = self.sensors_mod.PreFilters(self._config(enable_vision=False, probability=1.0))
        filters._commands_loaded = True

        event = _FakeEvent(
            group_id="group-1",
            text="看这个",
            components=[
                self._at_component("bot-1"),
                self._plain_component("看这个"),
                self._reply_component([self._image_component(file_path="reply.jpg")]),
            ],
        )

        result = asyncio.run(filters.should_process_message(event))

        self.assertTrue(result)
        self.assertFalse(event.get_extra("vision_direct_selected"))
        self.assertEqual(event.get_extra("vision_direct_skip_reason"), "disabled")
        self.assertEqual(event.get_extra("extracted_image_urls"), ["reply.jpg"])

    def test_group_pure_reply_image_is_not_dropped_as_empty_message(self):
        filters = self.sensors_mod.PreFilters(self._config(enable_vision=True, probability=0.0))
        filters._commands_loaded = True

        event = _FakeEvent(
            group_id="group-1",
            components=[
                self._at_component("bot-1"),
                self._reply_component([self._image_component(file_path="reply.jpg")]),
            ],
        )

        result = asyncio.run(filters.should_process_message(event))

        self.assertTrue(result)
        self.assertEqual(event.get_extra("extracted_image_urls"), ["reply.jpg"])

    def test_remote_url_only_image_is_not_selected_for_direct_vision(self):
        filters = self.sensors_mod.PreFilters(self._config(enable_vision=True, probability=1.0))
        filters._commands_loaded = True

        event = _FakeEvent(
            group_id=None,
            components=[self._image_component(url="https://example.com/private.jpg")],
        )

        result = asyncio.run(filters.should_process_message(event))

        self.assertTrue(result)
        self.assertEqual(event.get_extra("extracted_image_urls"), [])
        self.assertFalse(event.get_extra("direct_vision_urls"))
        self.assertEqual(event.get_extra("vision_direct_skip_reason"), "not_direct_path")

    def test_poke_event_writes_lightweight_play_context(self):
        filters = self.sensors_mod.PreFilters(self._config())
        event = _FakeEvent(group_id="456", components=[self._poke_component("bot-1")])

        class _Api:
            def __init__(self):
                self.calls = []

            async def call_action(self, action, **kwargs):
                self.calls.append((action, kwargs))
                return {}

        class _State:
            def __init__(self):
                self.affection_calls = []
                self.relationship_engine = SimpleNamespace(get_social_score=lambda _uid: 42.0)

            async def get_user_profile(self, uid):
                return SimpleNamespace(name="")

            async def apply_profile_name(self, uid, name, source=""):
                return None

            async def calculate_and_update_affection(self, **kwargs):
                self.affection_calls.append(kwargs)

        class _Gate:
            def __init__(self):
                self.state_engine = _State()
                self.processed = []

            async def process_event(self, event):
                self.processed.append(event)

        api = _Api()
        event.bot = SimpleNamespace(api=api)
        gate = _Gate()

        asyncio.run(filters.process_poke_event(event, SimpleNamespace(), gate))

        self.assertTrue(event.get_extra("is_virtual_poke"))
        self.assertTrue(event.get_extra("astrmai_lightweight_event"))
        self.assertEqual(event.get_extra("astrmai_interaction_kind"), "poke")
        self.assertEqual(event.get_extra("astrmai_reply_mode"), "playful_interaction")
        self.assertEqual(event.get_extra("astrmai_poke_intent"), "affectionate_ping")
        self.assertIn("亲近", event.get_extra("astrmai_rich_text"))
        self.assertIn("一两句", event.get_extra("astrmai_poke_reply_hint"))
        self.assertEqual(api.calls, [("send_poke", {"user_id": 12345, "group_id": 456})])
        self.assertEqual(len(gate.processed), 1)
        self.assertEqual(gate.state_engine.affection_calls[0]["event_type"], "normal_chat")

    def test_peer_poke_enters_observer_context_without_self_poke_semantics(self):
        filters = self.sensors_mod.PreFilters(self._config())
        event = _FakeEvent(group_id="456", components=[self._poke_component("67890")])

        class _Api:
            def __init__(self):
                self.calls = []

            async def call_action(self, action, **kwargs):
                self.calls.append((action, kwargs))
                if action == "get_group_member_info" and str(kwargs.get("user_id")) == "67890":
                    return {"card": "Bob"}
                return {}

        class _State:
            def __init__(self):
                self.affection_calls = []
                self.relationship_engine = SimpleNamespace(get_social_score=lambda _uid: 42.0)

            async def get_user_profile(self, uid):
                return SimpleNamespace(name="")

            async def apply_profile_name(self, uid, name, source=""):
                return None

            async def calculate_and_update_affection(self, **kwargs):
                self.affection_calls.append(kwargs)

        class _Gate:
            def __init__(self):
                self.state_engine = _State()
                self.processed = []

            async def process_event(self, event):
                self.processed.append(event)

        api = _Api()
        event.bot = SimpleNamespace(api=api)
        gate = _Gate()

        asyncio.run(filters.process_poke_event(event, SimpleNamespace(), gate))

        self.assertFalse(event.get_extra("is_virtual_poke"))
        self.assertEqual(event.get_extra("astrmai_interaction_kind"), "peer_poke")
        self.assertEqual(event.get_extra("astrmai_reply_mode"), "peer_interaction_observer")
        self.assertFalse(event.get_extra("astrmai_interaction_target_is_bot"))
        self.assertIn("Alice", event.get_extra("astrmai_rich_text"))
        self.assertIn("Bob", event.get_extra("astrmai_rich_text"))
        self.assertIn("不是你被戳", event.get_extra("astrmai_poke_reply_hint"))
        self.assertEqual(event.get_extra("astrmai_peer_poke_allowed_target_ids"), ["12345", "67890"])
        self.assertEqual(event.get_extra("astrmai_bonus_score"), 0.2)
        self.assertFalse(event.get_extra("astrmai_force_meme", False))
        self.assertEqual(gate.state_engine.affection_calls, [])
        self.assertEqual([call for call in api.calls if call[0] == "send_poke"], [])
        self.assertEqual(len(gate.processed), 1)

    def test_poke_event_uses_deep_raw_user_id_instead_of_event_sender(self):
        filters = self.sensors_mod.PreFilters(self._config())
        event = _FakeEvent(group_id="456", components=[])
        event.raw_event = {
            "outer": {
                "middle": [
                    {
                        "inner": {
                            "post_type": "notice",
                            "notice_type": "notify",
                            "sub_type": "poke",
                            "user_id": "67890",
                            "target_id": "bot-1",
                            "group_id": "456",
                        }
                    }
                ]
            }
        }

        class _Api:
            def __init__(self):
                self.calls = []

            async def call_action(self, action, **kwargs):
                self.calls.append((action, kwargs))
                if action == "get_group_member_info":
                    return {"card": "Bob"}
                return {}

        class _State:
            def __init__(self):
                self.affection_calls = []
                self.relationship_engine = SimpleNamespace(get_social_score=lambda _uid: 42.0)

            async def get_user_profile(self, uid):
                return SimpleNamespace(name="")

            async def apply_profile_name(self, uid, name, source=""):
                return None

            async def calculate_and_update_affection(self, **kwargs):
                self.affection_calls.append(kwargs)

        class _Gate:
            def __init__(self):
                self.state_engine = _State()
                self.processed = []

            async def process_event(self, event):
                self.processed.append(event)

        api = _Api()
        event.bot = SimpleNamespace(api=api)
        gate = _Gate()

        asyncio.run(filters.process_poke_event(event, SimpleNamespace(), gate))

        self.assertEqual(event.get_extra("astrmai_interaction_actor_id"), "67890")
        self.assertEqual(event.get_extra("astrmai_interaction_actor_name"), "Bob")
        self.assertTrue(event.get_extra("astrmai_interaction_actor_confident"))
        self.assertNotIn("Alice", event.get_extra("astrmai_rich_text"))
        self.assertEqual(gate.state_engine.affection_calls[0]["user_id"], "67890")

    def test_poke_event_without_raw_user_id_does_not_misattribute_to_event_sender(self):
        filters = self.sensors_mod.PreFilters(self._config())
        event = _FakeEvent(group_id="456", components=[])
        event.raw_event = {
            "notice": {
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "target_id": "bot-1",
                "group_id": "456",
            }
        }

        class _Api:
            def __init__(self):
                self.calls = []

            async def call_action(self, action, **kwargs):
                self.calls.append((action, kwargs))
                return {}

        class _State:
            def __init__(self):
                self.affection_calls = []
                self.relationship_engine = SimpleNamespace(get_social_score=lambda _uid: 42.0)

            async def get_user_profile(self, uid):
                return SimpleNamespace(name="")

            async def calculate_and_update_affection(self, **kwargs):
                self.affection_calls.append(kwargs)

        class _Gate:
            def __init__(self):
                self.state_engine = _State()
                self.processed = []

            async def process_event(self, event):
                self.processed.append(event)

        api = _Api()
        event.bot = SimpleNamespace(api=api)
        gate = _Gate()

        asyncio.run(filters.process_poke_event(event, SimpleNamespace(), gate))

        self.assertEqual(event.get_extra("astrmai_interaction_actor_id"), "")
        self.assertEqual(event.get_extra("astrmai_interaction_actor_name"), "有人")
        self.assertEqual(event.get_extra("astrmai_interaction_actor_display_name"), "有人")
        self.assertFalse(event.get_extra("astrmai_interaction_actor_confident"))
        self.assertIn("有人", event.get_extra("astrmai_rich_text"))
        self.assertNotIn("Alice", event.get_extra("astrmai_rich_text"))
        self.assertEqual(api.calls, [])
        self.assertEqual(gate.state_engine.affection_calls, [])

    def test_repeated_poke_switches_to_cooldown_hint_and_meme_flag(self):
        filters = self.sensors_mod.PreFilters(self._config())

        class _State:
            relationship_engine = SimpleNamespace(get_social_score=lambda _uid: 0.0)

            async def get_user_profile(self, uid):
                return SimpleNamespace(name="")

            async def calculate_and_update_affection(self, **kwargs):
                return None

        class _Gate:
            state_engine = _State()

            async def process_event(self, event):
                return None

        async def _run_three():
            events = []
            async def _call_action(*args, **kwargs):
                return {}
            for _ in range(3):
                event = _FakeEvent(group_id="456", components=[self._poke_component("bot-1")])
                event.bot = SimpleNamespace(api=SimpleNamespace(call_action=_call_action))
                await filters.process_poke_event(event, SimpleNamespace(), _Gate())
                events.append(event)
            return events

        events = asyncio.run(_run_three())

        self.assertEqual(events[0].get_extra("astrmai_poke_streak_count"), 1)
        self.assertEqual(events[1].get_extra("astrmai_poke_streak_count"), 2)
        self.assertTrue(events[1].get_extra("astrmai_force_meme"))
        self.assertEqual(events[2].get_extra("astrmai_poke_intent"), "attention_spam")
        self.assertGreater(events[2].get_extra("astrmai_poke_cooldown_seconds"), 0)
        self.assertIn("别一直戳", events[2].get_extra("astrmai_poke_reply_hint"))

    def test_extract_social_relations_includes_poke_edge(self):
        filters = self.sensors_mod.PreFilters(self._config())
        event = _FakeEvent(group_id="456", components=[self._poke_component("67890")])

        relations = filters.extract_social_relations(event, "456")

        self.assertEqual(relations, [("12345", "67890", "poke", 0.2)])


if __name__ == "__main__":
    unittest.main()
