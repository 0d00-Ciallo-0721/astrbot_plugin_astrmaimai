import asyncio
import unittest

from astrmai.state.private_chat.private_chat_manager import PrivateChatManager


class PrivateChatManagerMigratedTests(unittest.TestCase):
    def test_wait_for_new_message_uses_buffered_message_arrived_before_wait(self):
        manager = PrivateChatManager()

        async def _run():
            await manager.signal_new_message("user-1", "hello", chat_id="default:FriendMessage:user-1")
            has_reply = await manager.wait_for_new_message(
                "user-1",
                timeout=0.01,
                chat_id="default:FriendMessage:user-1",
            )
            pending = manager.get_pending_messages("user-1")
            return has_reply, pending

        has_reply, pending = asyncio.run(_run())

        self.assertTrue(has_reply)
        self.assertEqual(pending, ["hello"])

    def test_group_chat_id_does_not_alias_friend_session_with_same_numeric_tail(self):
        manager = PrivateChatManager()

        async def _run():
            await manager.wait_for_new_message(
                "12345",
                timeout=0.01,
                chat_id="default:FriendMessage:12345",
            )

        try:
            asyncio.run(_run())
        except Exception:
            pass

        friend_info = manager.get_session_info_by_chat_id("default:FriendMessage:12345")
        group_info = manager.get_session_info_by_chat_id("default:GroupMessage:12345")

        self.assertIsNotNone(friend_info)
        self.assertIsNone(group_info)


if __name__ == "__main__":
    unittest.main()
