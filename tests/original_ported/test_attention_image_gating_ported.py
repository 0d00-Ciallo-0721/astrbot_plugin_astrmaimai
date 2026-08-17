import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers import install_attention_stubs as _install_attention_stubs
from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeSensors:
    def __init__(self, wakeup=False):
        self._wakeup = wakeup

    def is_wakeup_signal(self, event, self_id):
        return self._wakeup

    async def is_command(self, msg_str):
        return False

    async def should_process_message(self, event):
        return True


class _FakeStateEngine:
    def __init__(self, config):
        self.config = config
        self.persistence = SimpleNamespace(add_last_message_meta=self._noop)

    async def get_state(self, chat_id):
        return SimpleNamespace(energy=1.0, mood=0.0)

    async def _noop(self, *args, **kwargs):
        return None


class _FakeEvent:
    def __init__(self, text="", extras=None, group_id="group-1"):
        self.message_str = text
        self.unified_msg_origin = f"default:{'GroupMessage' if group_id else 'FriendMessage'}:{group_id or 'user-1'}"
        self.message_obj = None
        self.timestamp = 123.0
        self._group_id = group_id
        self._extra = dict(extras or {})

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return "user-1"

    def get_sender_name(self):
        return "Alice"

    def get_self_id(self):
        return "bot-1"

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)


class AttentionImageGatingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        _install_attention_stubs()
        sys.modules.pop("astrmai.conversation.attention.gate", None)
        self.attention_mod = importlib.import_module("astrmai.conversation.attention.gate")
        self.attention_mod = importlib.reload(self.attention_mod)
        self.config = SimpleNamespace(
            attention=SimpleNamespace(
                max_message_length=100,
                throttle_min_entropy=2,
                throttle_probability=1.0,
                repeater_threshold=3,
                debounce_window=0.1,
                focus_thread_enabled=True,
                focus_thread_core_max_messages=4,
                focus_thread_related_max_messages=3,
                ambient_background_max_messages=2,
                thread_same_speaker_followup_sec=8,
                thread_reply_priority_enabled=True,
            ),
            system1=SimpleNamespace(wakeup_words=[], nicknames=[]),
            vision=SimpleNamespace(enable_vision=True, at_image_pair_window_sec=3.0),
            global_settings=SimpleNamespace(debug_mode=False),
        )

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_passive_group_image_share_is_ignored(self):
        gate = self.attention_mod.AttentionGate(
            state_engine=_FakeStateEngine(self.config),
            judge=SimpleNamespace(),
            sensors=_FakeSensors(wakeup=False),
            system2_callback=None,
        )
        event = _FakeEvent(
            text="",
            extras={"extracted_image_urls": ["passive.jpg"]},
            group_id="group-1",
        )

        result = asyncio.run(gate.process_event(event))
        self.assertEqual(result, "IGNORED_IMAGE")

    def test_prefilter_selected_group_image_reaches_attention_without_forcing_wakeup(self):
        gate = self.attention_mod.AttentionGate(
            state_engine=_FakeStateEngine(self.config),
            judge=SimpleNamespace(),
            sensors=_FakeSensors(wakeup=False),
            system2_callback=None,
        )
        event = _FakeEvent(
            text="",
            extras={
                "extracted_image_urls": ["passive.jpg"],
                "vision_prefilter_selected": True,
            },
            group_id="group-1",
        )

        result = asyncio.run(gate.process_event(event))

        self.assertEqual(result, "BUFFERED")
        self.assertFalse(event.get_extra("astrmai_group_direct_wakeup", False))

    def test_group_perception_is_refreshed_after_sensor_extracts_image(self):
        class _SelectingSensors(_FakeSensors):
            async def should_process_message(self, event):
                event.set_extra("extracted_image_urls", ["selected.jpg"])
                event.set_extra("extracted_image_refs", ["selected.jpg"])
                event.set_extra("vision_prefilter_selected", True)
                return True

        gate = self.attention_mod.AttentionGate(
            state_engine=_FakeStateEngine(self.config),
            judge=SimpleNamespace(),
            sensors=_SelectingSensors(wakeup=False),
            system2_callback=None,
        )
        event = _FakeEvent(text="", group_id="group-1")

        result = asyncio.run(gate.process_event(event))

        self.assertEqual(result, "BUFFERED")
        self.assertEqual(
            self.attention_mod.ensure_turn_context(event).perception.image_urls,
            ["selected.jpg"],
        )

    def test_direct_vision_request_is_not_treated_as_passive_share(self):
        gate = self.attention_mod.AttentionGate(
            state_engine=_FakeStateEngine(self.config),
            judge=SimpleNamespace(),
            sensors=_FakeSensors(wakeup=True),
            system2_callback=None,
        )
        event = _FakeEvent(
            text="@亚托莉 看这个",
            extras={
                "extracted_image_urls": ["direct.jpg"],
                "direct_vision_urls": ["direct.jpg"],
            },
            group_id="group-1",
        )

        result = asyncio.run(gate.process_event(event))
        self.assertEqual(result, "ENGAGED")

    def test_gate_upgrades_probability_missed_image_when_followed_by_pure_at(self):
        gate = self.attention_mod.AttentionGate(
            state_engine=_FakeStateEngine(self.config),
            judge=SimpleNamespace(),
            sensors=_FakeSensors(wakeup=False),
            system2_callback=None,
        )
        gate._spawn_session_worker = lambda *args, **kwargs: None
        image = _FakeEvent(
            text="",
            extras={
                "extracted_image_refs": ["missed.jpg"],
                "extracted_image_urls": ["missed.jpg"],
                "vision_prefilter_selected": False,
                "astrmai_vision_candidates": [
                    {
                        "message_id": "image-first",
                        "group_id": "group-1",
                        "sender_id": "user-1",
                        "timestamp": 10.0,
                        "image_index": 0,
                        "candidate_refs": ["missed.jpg"],
                        "source_kind": "inline",
                        "prefilter_selected": False,
                    }
                ],
            },
        )
        mention = _FakeEvent(
            text="@bot",
            extras={
                "astrmai_at_bot_wakeup": True,
                "astrmai_pure_at_bot": True,
                "astrmai_vision_candidates": [],
            },
        )

        first_result = asyncio.run(gate.process_event(image))
        second_result = asyncio.run(gate.process_event(mention))

        self.assertEqual(first_result, "IGNORED_IMAGE")
        self.assertEqual(second_result, "BUFFERED")
        self.assertTrue(mention.get_extra("vision_prefilter_selected"))
        self.assertEqual(mention.get_extra("extracted_image_refs"), ["missed.jpg"])
        self.assertEqual(
            mention.get_extra("astrmai_vision_candidates")[0]["pairing_mode"],
            "image_then_at",
        )

    def test_gate_merges_pure_at_then_image_without_passive_image_drop(self):
        gate = self.attention_mod.AttentionGate(
            state_engine=_FakeStateEngine(self.config),
            judge=SimpleNamespace(),
            sensors=_FakeSensors(wakeup=False),
            system2_callback=None,
        )
        gate._spawn_session_worker = lambda *args, **kwargs: None
        mention = _FakeEvent(
            text="",
            extras={
                "astrmai_at_bot_wakeup": True,
                "astrmai_pure_at_bot": True,
                "astrmai_vision_candidates": [],
            },
        )
        image = _FakeEvent(
            text="",
            extras={
                "extracted_image_refs": ["after-at.jpg"],
                "extracted_image_urls": ["after-at.jpg"],
                "vision_prefilter_selected": False,
                "astrmai_vision_candidates": [
                    {
                        "message_id": "image-second",
                        "group_id": "group-1",
                        "sender_id": "user-1",
                        "timestamp": 11.0,
                        "image_index": 0,
                        "candidate_refs": ["after-at.jpg"],
                        "source_kind": "inline",
                        "prefilter_selected": False,
                    }
                ],
            },
        )

        first_result = asyncio.run(gate.process_event(mention))
        second_result = asyncio.run(gate.process_event(image))

        self.assertEqual(first_result, "BUFFERED")
        self.assertEqual(second_result, "BUFFERED")
        self.assertTrue(image.get_extra("vision_prefilter_selected"))
        self.assertEqual(mention.get_extra("extracted_image_refs"), ["after-at.jpg"])
        self.assertEqual(
            mention.get_extra("astrmai_vision_candidates")[0]["pairing_mode"],
            "at_then_image",
        )
        session = gate.focus_pools[mention.unified_msg_origin]
        self.assertEqual(session.accumulation_pool, [mention, image])
        self.assertTrue(session.vision_pair_signal.is_set())
        self.assertFalse(image.get_extra("astrmai_release_vision_pair_waiter"))

    def test_gate_holds_selected_image_until_following_pure_at_joins_batch(self):
        gate = self.attention_mod.AttentionGate(
            state_engine=_FakeStateEngine(self.config),
            judge=SimpleNamespace(),
            sensors=_FakeSensors(wakeup=False),
            system2_callback=None,
        )
        gate._spawn_session_worker = lambda *args, **kwargs: None
        image = _FakeEvent(
            text="",
            extras={
                "extracted_image_refs": ["selected-first.jpg"],
                "extracted_image_urls": ["selected-first.jpg"],
                "vision_prefilter_selected": True,
                "astrmai_vision_candidates": [
                    {
                        "message_id": "selected-image-first",
                        "group_id": "group-1",
                        "sender_id": "user-1",
                        "timestamp": 12.0,
                        "image_index": 0,
                        "candidate_refs": ["selected-first.jpg"],
                        "source_kind": "inline",
                        "prefilter_selected": True,
                    }
                ],
            },
        )
        mention = _FakeEvent(
            text="@bot",
            extras={
                "astrmai_at_bot_wakeup": True,
                "astrmai_pure_at_bot": True,
                "astrmai_vision_candidates": [],
            },
        )

        first_result = asyncio.run(gate.process_event(image))
        second_result = asyncio.run(gate.process_event(mention))

        self.assertEqual(first_result, "BUFFERED")
        self.assertEqual(second_result, "BUFFERED")
        session = gate.focus_pools[image.unified_msg_origin]
        self.assertEqual(session.accumulation_pool, [image, mention])
        self.assertTrue(session.vision_pair_signal.is_set())
        self.assertEqual(
            mention.get_extra("astrmai_vision_candidates")[0]["pairing_mode"],
            "image_then_at",
        )

    def test_gate_never_pairs_same_sender_across_groups(self):
        gate = self.attention_mod.AttentionGate(
            state_engine=_FakeStateEngine(self.config),
            judge=SimpleNamespace(),
            sensors=_FakeSensors(wakeup=False),
            system2_callback=None,
        )
        gate._spawn_session_worker = lambda *args, **kwargs: None
        image = _FakeEvent(
            text="",
            group_id="group-1",
            extras={
                "extracted_image_refs": ["group-one.jpg"],
                "extracted_image_urls": ["group-one.jpg"],
                "vision_prefilter_selected": False,
                "astrmai_vision_candidates": [
                    {
                        "message_id": "group-one-image",
                        "group_id": "group-1",
                        "sender_id": "user-1",
                        "timestamp": 13.0,
                        "image_index": 0,
                        "candidate_refs": ["group-one.jpg"],
                        "source_kind": "inline",
                        "prefilter_selected": False,
                    }
                ],
            },
        )
        mention = _FakeEvent(
            text="@bot",
            group_id="group-2",
            extras={
                "astrmai_at_bot_wakeup": True,
                "astrmai_pure_at_bot": True,
                "astrmai_vision_candidates": [],
            },
        )

        first_result = asyncio.run(gate.process_event(image))
        second_result = asyncio.run(gate.process_event(mention))

        self.assertEqual(first_result, "IGNORED_IMAGE")
        self.assertEqual(second_result, "BUFFERED")
        self.assertEqual(mention.get_extra("astrmai_vision_candidates"), [])
        self.assertIn("default:GroupMessage:group-1", gate.focus_pools)
        self.assertIn("default:GroupMessage:group-2", gate.focus_pools)


if __name__ == "__main__":
    unittest.main()
