import unittest

from astrmai.conversation.contracts.turn_identity import (
    TurnIdentity,
    build_p0_thread_id,
    build_turn_send_key,
)


class TurnIdentityTests(unittest.TestCase):
    def test_private_thread_id_is_scoped_by_chat_id(self):
        self.assertEqual(
            build_p0_thread_id("private", "default:FriendMessage:user-1"),
            "private:default:FriendMessage:user-1",
        )

    def test_group_thread_id_uses_chat_id(self):
        self.assertEqual(
            build_p0_thread_id("group", "default:GroupMessage:group-1"),
            "default:GroupMessage:group-1",
        )

    def test_empty_chat_id_is_stable(self):
        self.assertEqual(build_p0_thread_id("private", ""), "private:")
        self.assertEqual(build_p0_thread_id("group", ""), "")

    def test_send_key_is_stable(self):
        turn = TurnIdentity(
            mode="group",
            chat_id="chat-1",
            thread_id="thread-1",
            generation=3,
            sender_id="user-1",
        )

        self.assertEqual(
            build_turn_send_key(turn),
            "group:chat-1:thread-1:3:final",
        )

    def test_empty_response_kind_falls_back_to_final(self):
        turn = TurnIdentity(
            mode="private",
            chat_id="chat-1",
            thread_id="private:chat-1",
            generation=1,
        )

        self.assertEqual(
            build_turn_send_key(turn, ""),
            "private:chat-1:private:chat-1:1:final",
        )


if __name__ == "__main__":
    unittest.main()
