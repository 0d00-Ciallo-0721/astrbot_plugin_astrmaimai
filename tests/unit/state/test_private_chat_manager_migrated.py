import asyncio
import unittest

from astrmai.state.private_chat.private_chat_manager import PrivateChatManager


class PrivateChatManagerMigratedTests(unittest.TestCase):
    def test_message_without_active_wait_is_not_buffered(self):
        manager = PrivateChatManager()

        async def _run():
            signaled = await manager.signal_new_message(
                "user-1",
                "hello",
                chat_id="default:FriendMessage:user-1",
            )
            pending = manager.get_pending_messages("user-1")
            return signaled, pending

        signaled, pending = asyncio.run(_run())

        self.assertFalse(signaled)
        self.assertEqual(pending, [])

    def test_message_interrupting_active_wait_is_buffered_and_signaled(self):
        manager = PrivateChatManager()

        async def _run():
            waiter = asyncio.create_task(
                manager.wait_for_new_message(
                    "user-1",
                    timeout=0.2,
                    chat_id="default:FriendMessage:user-1",
                )
            )
            await asyncio.sleep(0)
            signaled = await manager.signal_new_message(
                "user-1",
                "hello",
                chat_id="default:FriendMessage:user-1",
            )
            has_reply = await waiter
            pending = manager.get_pending_messages("user-1")
            return signaled, has_reply, pending

        signaled, has_reply, pending = asyncio.run(_run())

        self.assertTrue(signaled)
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
