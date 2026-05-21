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
        self.message_obj = SimpleNamespace(message=components)
        self._group_id = group_id
        self._extra = {}

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return "user-1"

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

    def _image_component(self, url):
        image = self.Comp.Image()
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

    def test_private_image_probability_gate_skips_direct_vision_but_keeps_extracted_urls(self):
        filters = self.sensors_mod.PreFilters(self._config(probability=0.0))
        filters._commands_loaded = True

        event = _FakeEvent(
            group_id=None,
            components=[self._image_component("https://example.com/private.jpg")],
        )

        result = asyncio.run(filters.should_process_message(event))

        self.assertTrue(result)
        self.assertEqual(event.get_extra("extracted_image_urls"), ["https://example.com/private.jpg"])
        self.assertFalse(event.get_extra("vision_direct_selected"))
        self.assertEqual(event.get_extra("vision_direct_skip_reason"), "probability_gate")
        self.assertFalse(event.get_extra("astrmai_is_direct_vision_request"))
        self.assertFalse(event.get_extra("direct_vision_urls"))

    def test_private_image_respects_vision_disable_switch(self):
        filters = self.sensors_mod.PreFilters(self._config(enable_vision=False, probability=1.0))
        filters._commands_loaded = True

        event = _FakeEvent(
            group_id=None,
            components=[self._image_component("https://example.com/private.jpg")],
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
            components=[self._image_component("https://example.com/private.jpg")],
        )

        result = asyncio.run(filters.should_process_message(event))

        self.assertTrue(result)
        self.assertTrue(event.get_extra("vision_direct_selected"))
        self.assertEqual(event.get_extra("vision_direct_skip_reason"), "")
        self.assertTrue(event.get_extra("astrmai_is_direct_vision_request"))
        self.assertEqual(event.get_extra("direct_vision_urls"), ["https://example.com/private.jpg"])

    def test_group_reply_image_probability_gate_keeps_extracted_urls(self):
        filters = self.sensors_mod.PreFilters(self._config(enable_vision=True, probability=0.0))
        filters._commands_loaded = True

        event = _FakeEvent(
            group_id="group-1",
            text="看这个",
            components=[
                self._at_component("bot-1"),
                self._plain_component("看这个"),
                self._reply_component([self._image_component("https://example.com/reply.jpg")]),
            ],
        )

        result = asyncio.run(filters.should_process_message(event))

        self.assertTrue(result)
        self.assertFalse(event.get_extra("vision_direct_selected"))
        self.assertEqual(event.get_extra("vision_direct_skip_reason"), "probability_gate")
        self.assertFalse(event.get_extra("direct_vision_urls"))
        self.assertEqual(event.get_extra("extracted_image_urls"), ["https://example.com/reply.jpg"])

    def test_group_reply_image_disable_switch_still_keeps_extracted_urls(self):
        filters = self.sensors_mod.PreFilters(self._config(enable_vision=False, probability=1.0))
        filters._commands_loaded = True

        event = _FakeEvent(
            group_id="group-1",
            text="看这个",
            components=[
                self._at_component("bot-1"),
                self._plain_component("看这个"),
                self._reply_component([self._image_component("https://example.com/reply.jpg")]),
            ],
        )

        result = asyncio.run(filters.should_process_message(event))

        self.assertTrue(result)
        self.assertFalse(event.get_extra("vision_direct_selected"))
        self.assertEqual(event.get_extra("vision_direct_skip_reason"), "disabled")
        self.assertEqual(event.get_extra("extracted_image_urls"), ["https://example.com/reply.jpg"])

    def test_group_pure_reply_image_is_not_dropped_as_empty_message(self):
        filters = self.sensors_mod.PreFilters(self._config(enable_vision=True, probability=0.0))
        filters._commands_loaded = True

        event = _FakeEvent(
            group_id="group-1",
            components=[
                self._at_component("bot-1"),
                self._reply_component([self._image_component("https://example.com/reply.jpg")]),
            ],
        )

        result = asyncio.run(filters.should_process_message(event))

        self.assertTrue(result)
        self.assertEqual(event.get_extra("extracted_image_urls"), ["https://example.com/reply.jpg"])
        self.assertEqual(event.get_extra("vision_direct_skip_reason"), "probability_gate")


if __name__ == "__main__":
    unittest.main()
