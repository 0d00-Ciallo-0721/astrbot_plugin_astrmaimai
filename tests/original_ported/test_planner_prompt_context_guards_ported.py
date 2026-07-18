import importlib
import sys
import tempfile
import unittest

from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeEvent:
    def __init__(self, message="hello", extras=None, sender_id="user-1", sender_name="Alice", group_id="group-1"):
        self.message_str = message
        self.unified_msg_origin = f"default:GroupMessage:{group_id}" if group_id else f"default:FriendMessage:{sender_id}"
        self._extras = dict(extras or {})
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._group_id = group_id

    def get_sender_name(self):
        return self._sender_name

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._group_id

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)


class PlannerPromptContextGuardsPortedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.conversation.planning.planner_prompt_context", None)
        self.prompt_context_mod = importlib.import_module("astrmai.conversation.planning.planner_prompt_context")
        self.prompt_context_mod = importlib.reload(self.prompt_context_mod)
        self.focus_mod = importlib.import_module("astrmai.conversation.contracts.focus_context")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_event_line_uses_speaker_content_layout(self):
        line = self.prompt_context_mod.PlannerPromptContextMixin._render_event_line(
            _FakeEvent("<hello>", extras={"astrmai_rich_text": "ATRI: hi <3"})
        )

        self.assertEqual(line, "Alice: ATRI: hi <3")
        self.assertNotIn("<message speaker=", line)

    def test_poke_event_is_lightweight(self):
        context = self.focus_mod.FocusThreadContext(focus_event=None)
        event = _FakeEvent("", extras={"is_virtual_poke": True, "astrmai_interaction_kind": "poke"})

        self.assertTrue(self.prompt_context_mod.PlannerPromptContextMixin._is_lightweight_event(event, context))

    def test_current_speaker_block_marks_group_weak_input_boundary(self):
        context = self.focus_mod.FocusThreadContext(
            focus_event=None,
            focus_sender_id="2639044966",
            focus_sender_name="哥哥",
        )
        event = _FakeEvent(
            "",
            extras={"extracted_image_refs": ["image-a"]},
            sender_id="2639044966",
            sender_name="哥哥",
            group_id="1048285592",
        )
        context.vision_bundle.image_urls.append("image-a")

        block = self.prompt_context_mod.PlannerPromptContextMixin._build_current_speaker_block(
            event,
            context,
            is_lightweight_event=True,
        )

        self.assertIn("QQ: 2639044966", block)
        self.assertIn("昵称: 哥哥", block)
        self.assertIn("这是群聊", block)
        self.assertIn("弱文本/图片/互动输入", block)
        self.assertIn("不要从旧对话里补出其他人的名字", block)


if __name__ == "__main__":
    unittest.main()
