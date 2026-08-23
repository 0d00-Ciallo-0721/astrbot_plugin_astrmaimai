import asyncio
import importlib
import unittest
from collections import defaultdict


class _FakePersistence:
    def __init__(self, seed=None):
        self.seed = dict(seed or {})
        self.saved = {}

    async def load_user_profile(self, user_id):
        return self.seed.get(user_id)

    async def save_user_profile(self, profile):
        self.saved[profile.user_id] = profile


class _FlakyPersistence(_FakePersistence):
    def __init__(self):
        super().__init__()
        self.calls = defaultdict(int)

    async def save_user_profile(self, profile):
        self.calls[profile.user_id] += 1
        if self.calls[profile.user_id] == 1:
            raise RuntimeError("save failed")
        await super().save_user_profile(profile)


class UserProfileServiceMigratedTests(unittest.TestCase):
    def setUp(self):
        self.mod = importlib.import_module("astrmai.state.user_profile_service")
        self.mod = importlib.reload(self.mod)

    def test_record_profile_learning_touch_updates_count_and_know_times(self):
        service = self.mod.UserProfileService(_FakePersistence())

        async def _run():
            await service.record_profile_learning_touch(
                "user-1",
                chat_id="FriendMessage:user-1",
                source="private_reply",
                weight=1,
                sender_name="Alice",
                increment_know_times=True,
            )
            profile = await service.get_user_profile("user-1")
            return profile

        profile = asyncio.run(_run())
        self.assertEqual(profile.name, "Alice")
        self.assertEqual(profile.message_count_for_profiling, 1)
        self.assertEqual(profile.know_times, 1)
        footprint = profile.group_footprints["FriendMessage:user-1"]
        self.assertEqual(footprint["private_touch_count"], 1)

    def test_observe_activity_collects_recent_messages_without_incrementing_learning_counter(self):
        service = self.mod.UserProfileService(_FakePersistence())

        async def _run():
            await service.observe_user_activity(
                "user-1",
                chat_id="group-1",
                sender_name="Alice",
                content="今天去看电影了",
                source="learning_message",
            )
            profile = await service.get_user_profile("user-1")
            return profile

        profile = asyncio.run(_run())
        self.assertEqual(profile.message_count_for_profiling, 0)
        self.assertEqual(profile.group_footprints["group-1"]["message_count"], 1)
        self.assertEqual(profile.group_footprints["group-1"]["recent_messages"][-1]["text"], "今天去看电影了")

    def test_observe_real_qq_event_persists_verified_identity_and_alias_without_group_leak(self):
        service = self.mod.UserProfileService(_FakePersistence())

        async def _run():
            await service.observe_user_activity(
                "3650815443",
                chat_id="ff:GroupMessage:111",
                sender_name="萤",
                content="在吗",
                source="learning_message",
            )
            return await service.get_user_profile("3650815443")

        profile = asyncio.run(_run())
        identity = profile.profile_metadata["verified_identity"]
        self.assertEqual(identity["user_id"], "3650815443")
        self.assertTrue(identity["verified"])
        self.assertNotIn("group_id", identity)
        self.assertEqual(profile.profile_metadata["verified_aliases"][-1]["value"], "萤")

    def test_refresh_profile_from_generation_merges_points_and_respects_manual_locks(self):
        service = self.mod.UserProfileService(_FakePersistence())
        profile = self.mod.UserProfile(
            user_id="user-1",
            name="Alice",
            persona_analysis="旧画像保持不动",
            tags=["老朋友"],
            memory_points=["爱好:咖啡:0.90", "关系:熟人:0.40"],
            identity_points=["身份:手工设定"],
        )
        service.set_manual_lock(profile, "persona_analysis")
        service.set_manual_lock(profile, "identity_points")

        refreshed = service.refresh_profile_from_generation(
            profile,
            analysis="新的画像总结会被锁挡住",
            tags=["夜猫子", "老朋友"],
            memory_points=["爱好:电影:0.80", "身份:大学生:0.70"],
            source="test",
        )

        self.assertEqual(refreshed.persona_analysis, "旧画像保持不动")
        self.assertIn("夜猫子", refreshed.tags)
        self.assertIn("老朋友", refreshed.tags)
        self.assertIn("爱好:电影:0.80", refreshed.memory_points)
        self.assertEqual(refreshed.identity_points, ["身份:手工设定"])
        self.assertEqual(refreshed.profile_metadata["last_refresh_source"], "test")

    def test_apply_profile_name_rejects_placeholder_and_respects_manual_lock(self):
        service = self.mod.UserProfileService(_FakePersistence())

        async def _run():
            profile = await service.get_user_profile("user-1")
            profile.name = "Alice"
            service.set_manual_lock(profile, "name")
            locked_changed = await service.apply_profile_name("user-1", "Bob")
            unlocked_placeholder = await service.apply_profile_name("user-1", "群友1234")
            return profile, locked_changed, unlocked_placeholder

        profile, locked_changed, unlocked_placeholder = asyncio.run(_run())
        self.assertFalse(locked_changed)
        self.assertFalse(unlocked_placeholder)
        self.assertEqual(profile.name, "Alice")

    def test_flush_message_counters_keeps_dirty_when_save_fails_then_clears_on_success(self):
        persistence = _FlakyPersistence()
        service = self.mod.UserProfileService(persistence)

        async def _run():
            profile = await service.get_user_profile("user-1")
            profile.name = "Alice"
            profile.is_dirty = True
            with self.assertRaisesRegex(RuntimeError, "save failed"):
                await service.flush_message_counters()
            dirty_after_failure = profile.is_dirty
            await service.flush_message_counters()
            return profile, dirty_after_failure

        profile, dirty_after_failure = asyncio.run(_run())

        self.assertTrue(dirty_after_failure)
        self.assertFalse(profile.is_dirty)
        self.assertIn("user-1", persistence.saved)

    def test_profile_generation_failure_backoff_is_exponential_and_clearable(self):
        service = self.mod.UserProfileService(_FakePersistence())
        profile = self.mod.UserProfile(user_id="user-1", name="Alice")

        first = service.record_profile_generation_failure(profile, "nickname", now=100.0)
        second = service.record_profile_generation_failure(profile, "nickname", now=100.0)

        self.assertEqual(first["failure_count"], 1)
        self.assertEqual(second["failure_count"], 2)
        self.assertTrue(service.profile_generation_backoff_active(profile, "nickname", now=100.0))
        service.clear_profile_generation_failure(profile, "nickname")
        self.assertFalse(service.profile_generation_backoff_active(profile, "nickname", now=100.0))


if __name__ == "__main__":
    unittest.main()
