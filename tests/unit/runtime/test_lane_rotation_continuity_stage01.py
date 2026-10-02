import asyncio
import importlib
import sys
import tempfile
import unittest

from tests.original_ported.helpers import _FakeConversationManager, _install_astrbot_stubs


class _FailOnUpdateConversationManager(_FakeConversationManager):
    def __init__(self):
        super().__init__()
        self.fail_on_update = False

    async def update_conversation(self, *args, **kwargs):
        if self.fail_on_update:
            raise RuntimeError("rotation_seed_write_failed")
        return await super().update_conversation(*args, **kwargs)


class _FailOnNewConversationManager(_FakeConversationManager):
    def __init__(self):
        super().__init__()
        self.fail_on_new = False

    async def new_conversation(self, *args, **kwargs):
        if self.fail_on_new:
            raise RuntimeError("rotation_conversation_create_failed")
        return await super().new_conversation(*args, **kwargs)


class LaneRotationContinuityStage01Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.runtime.lane_manager", None)
        self.lane_mod = importlib.import_module("astrmai.infrastructure.runtime.lane_manager")
        self.lane_mod = importlib.reload(self.lane_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    @staticmethod
    def _lane_key(lane_mod):
        return lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")

    @staticmethod
    def _history(pair_count=8):
        history = []
        for index in range(pair_count):
            history.extend(
                [
                    {"role": "user", "content": f"user-{index}", "timestamp": float(index * 2 + 1)},
                    {"role": "assistant", "content": f"assistant-{index}", "timestamp": float(index * 2 + 2)},
                ]
            )
        return history

    def test_prefix_rotation_preserves_recent_pairs_and_tail_summary(self):
        manager = self.lane_mod.LaneManager(_FakeConversationManager())
        lane_key = self._lane_key(self.lane_mod)

        async def _run():
            lane_umo, conversation_id, _, _ = await manager.ensure_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prefix_hash="prefix-a",
            )
            await manager.conversation_manager.update_conversation(
                unified_msg_origin=lane_umo,
                conversation_id=conversation_id,
                history=self._history(),
            )
            old_id = conversation_id
            _, new_id, history, _ = await manager.ensure_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prefix_hash="prefix-b",
            )
            return old_id, new_id, history

        old_id, new_id, history = asyncio.run(_run())

        self.assertNotEqual(old_id, new_id)
        self.assertEqual(
            [item["role"] for item in history],
            ["assistant", "user", "assistant", "user", "assistant", "user", "assistant"],
        )
        self.assertTrue(history[0]["content"].startswith("历史上下文摘要（轮换桥接，非真实回复）："))
        self.assertIn("user-4", history[0]["content"])
        self.assertNotIn("user-0", history[0]["content"])
        self.assertEqual(
            [item["content"] for item in history[-6:]],
            ["user-5", "assistant-5", "user-6", "assistant-6", "user-7", "assistant-7"],
        )

    def test_repeated_rotation_does_not_duplicate_seed_turns(self):
        manager = self.lane_mod.LaneManager(_FakeConversationManager())
        lane_key = self._lane_key(self.lane_mod)

        async def _run():
            lane_umo, conversation_id, _, _ = await manager.ensure_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prefix_hash="prefix-a",
            )
            await manager.conversation_manager.update_conversation(
                unified_msg_origin=lane_umo,
                conversation_id=conversation_id,
                history=self._history(),
            )
            _, first_id, first_history, _ = await manager.ensure_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prefix_hash="prefix-b",
            )
            _, second_id, second_history, _ = await manager.ensure_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prefix_hash="prefix-c",
            )
            return first_id, first_history, second_id, second_history

        first_id, first_history, second_id, second_history = asyncio.run(_run())

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(len(first_history), 7)
        self.assertEqual(len(second_history), 6)
        self.assertEqual(
            [item["content"] for item in second_history],
            ["user-5", "assistant-5", "user-6", "assistant-6", "user-7", "assistant-7"],
        )
        self.assertEqual(len({item["content"] for item in second_history}), len(second_history))
        self.assertNotIn("历史上下文摘要（轮换桥接，非真实回复）：", second_history[0]["content"])

    def test_same_prefix_reuses_lane_without_rotation_seed(self):
        manager = self.lane_mod.LaneManager(_FakeConversationManager())
        lane_key = self._lane_key(self.lane_mod)

        async def _run():
            lane_umo, conversation_id, _, _ = await manager.ensure_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prefix_hash="prefix-a",
            )
            await manager.conversation_manager.update_conversation(
                unified_msg_origin=lane_umo,
                conversation_id=conversation_id,
                history=[{"role": "user", "content": "existing-user"}],
            )
            _, reused_id, history, _ = await manager.ensure_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prefix_hash="prefix-a",
            )
            return conversation_id, reused_id, history

        old_id, reused_id, history = asyncio.run(_run())
        self.assertEqual(old_id, reused_id)
        self.assertEqual([item["content"] for item in history], ["existing-user"])

    def test_rotation_never_bridges_across_chat_keys(self):
        manager = self.lane_mod.LaneManager(_FakeConversationManager())
        lane_key = self._lane_key(self.lane_mod)

        async def _run():
            lane_umo, conversation_id, _, _ = await manager.ensure_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prefix_hash="prefix-a",
            )
            await manager.conversation_manager.update_conversation(
                unified_msg_origin=lane_umo,
                conversation_id=conversation_id,
                history=[
                    {"role": "user", "content": "group-1-user"},
                    {"role": "assistant", "content": "group-1-assistant"},
                ],
            )
            other_umo, other_id, other_history, _ = await manager.ensure_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-2",
                prefix_hash="prefix-b",
            )
            return lane_umo, other_umo, other_id, other_history

        lane_umo, other_umo, other_id, other_history = asyncio.run(_run())
        self.assertNotEqual(lane_umo, other_umo)
        self.assertIsNotNone(other_id)
        self.assertEqual(other_history, [])

    def test_rotation_discards_unpaired_tail_user_conservatively(self):
        manager = self.lane_mod.LaneManager(_FakeConversationManager())
        lane_key = self._lane_key(self.lane_mod)

        async def _run():
            lane_umo, conversation_id, _, _ = await manager.ensure_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prefix_hash="prefix-a",
            )
            await manager.conversation_manager.update_conversation(
                unified_msg_origin=lane_umo,
                conversation_id=conversation_id,
                history=[
                    {"role": "user", "content": "complete-user"},
                    {"role": "assistant", "content": "complete-assistant"},
                    {"role": "user", "content": "orphan-user"},
                ],
            )
            _, _, history, _ = await manager.ensure_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prefix_hash="prefix-b",
            )
            return history

        history = asyncio.run(_run())
        self.assertEqual([item["role"] for item in history], ["user", "assistant"])
        self.assertEqual(
            [item["content"] for item in history],
            ["complete-user", "complete-assistant"],
        )
        self.assertNotIn("orphan-user", str(history))

    def test_rotation_seed_is_created_atomically_without_post_switch_update(self):
        conversation_manager = _FailOnUpdateConversationManager()
        manager = self.lane_mod.LaneManager(conversation_manager)
        lane_key = self._lane_key(self.lane_mod)

        async def _run():
            lane_umo, conversation_id, _, _ = await manager.ensure_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prefix_hash="prefix-a",
            )
            await conversation_manager.update_conversation(
                unified_msg_origin=lane_umo,
                conversation_id=conversation_id,
                history=self._history(4),
            )
            conversation_manager.fail_on_update = True
            _, rotated_id, history, _ = await manager.ensure_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prefix_hash="prefix-b",
            )
            current_id = await conversation_manager.get_curr_conversation_id(lane_umo)
            return conversation_id, rotated_id, current_id, history

        old_id, rotated_id, current_id, history = asyncio.run(_run())
        self.assertNotEqual(old_id, rotated_id)
        self.assertEqual(current_id, rotated_id)
        self.assertTrue(history)

    def test_rotation_conversation_creation_failure_keeps_original_lane_current(self):
        conversation_manager = _FailOnNewConversationManager()
        manager = self.lane_mod.LaneManager(conversation_manager)
        lane_key = self._lane_key(self.lane_mod)

        async def _run():
            lane_umo, conversation_id, _, _ = await manager.ensure_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                prefix_hash="prefix-a",
            )
            conversation_manager.fail_on_new = True
            with self.assertRaisesRegex(RuntimeError, "rotation_conversation_create_failed"):
                await manager.ensure_lane(
                    lane_key=lane_key,
                    base_origin="default:GroupMessage:group-1",
                    prefix_hash="prefix-b",
                )
            current_id = await conversation_manager.get_curr_conversation_id(lane_umo)
            return conversation_id, current_id

        old_id, current_id = asyncio.run(_run())
        self.assertEqual(old_id, current_id)


if __name__ == "__main__":
    unittest.main()
