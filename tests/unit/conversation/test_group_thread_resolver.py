import unittest
from types import SimpleNamespace

from astrmai.conversation.threading.group_thread_resolver import resolve_group_thread


class _Event:
    def __init__(
        self,
        *,
        chat_id="default:GroupMessage:group-1",
        extras=None,
        components=None,
        sender_id="user-1",
        self_id="bot-1",
    ):
        self.unified_msg_origin = chat_id
        self._extra = dict(extras or {})
        self.message_obj = SimpleNamespace(message=list(components or []))
        self._sender_id = sender_id
        self._self_id = self_id

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id


class GroupThreadResolverTests(unittest.TestCase):
    def test_thread_signature_wins(self):
        result = resolve_group_thread(_Event(extras={"astrmai_thread_signature": "thread-a"}))

        self.assertEqual(result.thread_id, "thread-a")
        self.assertEqual(result.source, "thread_signature")
        self.assertEqual(result.confidence, 1.0)

    def test_reply_component_builds_reply_thread(self):
        component = SimpleNamespace(type="Reply", message_id="msg-42")

        result = resolve_group_thread(_Event(components=[component]))

        self.assertEqual(result.thread_id, "reply:msg-42")
        self.assertEqual(result.source, "reply_component")

    def test_reply_component_can_fallback_to_sender(self):
        component = SimpleNamespace(type="Reply", sender_id="bot-1")

        result = resolve_group_thread(_Event(components=[component]))

        self.assertEqual(result.thread_id, "reply:reply_sender:bot-1")

    def test_plain_message_falls_back_to_sender_thread(self):
        result = resolve_group_thread(_Event(chat_id="default:GroupMessage:group-2"))

        self.assertEqual(result.chat_id, "default:GroupMessage:group-2")
        self.assertEqual(result.thread_id, "sender:user-1")
        self.assertEqual(result.source, "sender")

    def test_anonymous_plain_message_falls_back_to_chat_id(self):
        result = resolve_group_thread(_Event(chat_id="default:GroupMessage:group-2", sender_id=""))

        self.assertEqual(result.thread_id, "default:GroupMessage:group-2")
        self.assertEqual(result.source, "chat")


if __name__ == "__main__":
    unittest.main()
