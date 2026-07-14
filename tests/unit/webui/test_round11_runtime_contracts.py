import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import aiosqlite


class Round11RuntimeContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "round11.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    @asynccontextmanager
    async def _db_factory(self):
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    async def _create_profile_table(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE user_profiles (
                    user_id TEXT PRIMARY KEY, name TEXT, social_score REAL, last_seen REAL,
                    persona_analysis TEXT, message_count_for_profiling INTEGER,
                    last_persona_gen_time REAL, identity TEXT, tags TEXT, nickname TEXT,
                    nickname_reason TEXT, know_times INTEGER, is_known INTEGER,
                    memory_points TEXT, identity_points TEXT, preference_points TEXT,
                    relationship_points TEXT, speech_style_points TEXT, group_footprints TEXT,
                    profile_metadata TEXT, relationship_vector TEXT, updated_at REAL
                )
                """
            )
            await db.execute(
                """
                INSERT INTO user_profiles VALUES (
                    'u1', 'old', 1.0, 0, '', 0, 0, '', '[]', '', '', 0, 0,
                    '[]', '[]', '[]', '[]', '[]', '{}', '{}', '{}', 0
                )
                """
            )
            await db.commit()

    def test_profile_mutations_replace_live_object_and_clear_relationship_cache(self):
        from astrmai.infrastructure.persistence.orm_models import UserProfile
        from astrmai.state.user_profile_service import UserProfileService
        from astrmai.webui.backend.services.user_ui_service import UserUiService

        async def run():
            await self._create_profile_table()
            profile_service = UserProfileService(SimpleNamespace())
            live = UserProfile(user_id="u1", name="stale", is_dirty=True)
            profile_service.user_profiles["u1"] = live

            class Relationship:
                def __init__(self):
                    self._vectors = {"u1": object()}
                    self.aligned = None

                def align_social_score(self, user_id, score):
                    self.aligned = (user_id, score)

            relationship = Relationship()
            state_engine = SimpleNamespace(
                user_profile_service=profile_service,
                relationship_engine=relationship,
            )
            service = UserUiService(self._db_factory, state_engine)
            await service.update_user("u1", {"name": "manual", "social_score": 7.5})
            await service.add_slice("u1", "identity_points", "manual identity")
            updated_snapshot = (
                live.name,
                live.social_score,
                list(live.identity_points),
                dict(live.profile_metadata),
                live.is_dirty,
                relationship.aligned,
            )
            await service.delete_user("u1")
            return updated_snapshot, live.name, live.is_dirty, relationship._vectors

        updated, deleted_name, deleted_dirty, vectors = asyncio.run(run())
        self.assertEqual(updated[0], "manual")
        self.assertEqual(updated[1], 7.5)
        self.assertEqual(updated[2], ["manual identity"])
        self.assertIn("identity_points", updated[3]["manual_locked_fields"])
        self.assertFalse(updated[4])
        self.assertEqual(updated[5], ("u1", 7.5))
        self.assertEqual(deleted_name, "未知用户")
        self.assertFalse(deleted_dirty)
        self.assertNotIn("u1", vectors)

    def test_persona_adapter_prefers_runtime_persistence_and_syncs_summarizer(self):
        from astrmai.webui.backend.adapters.plugin_api import PluginApiAdapter

        class Persistence:
            def __init__(self):
                self.saved = None

            async def load_persona_cache_async(self):
                return {"live": {"summary": "runtime"}}

            async def save_persona_cache_async(self, data):
                self.saved = dict(data)

        persistence = Persistence()
        summarizer = SimpleNamespace(cache={"stale": {}})

        class Facade:
            def get_state_engine(self):
                return SimpleNamespace(persistence=persistence)

            def get_persona_summarizer(self):
                return summarizer

            def get_memory_engine(self):
                return None

        async def run():
            adapter = PluginApiAdapter(facade=Facade())
            loaded = await adapter.read_persona_cache()
            await adapter.write_persona_cache({"live": {"summary": "updated"}})
            return loaded

        loaded = asyncio.run(run())
        self.assertEqual(loaded["live"]["summary"], "runtime")
        self.assertEqual(persistence.saved["live"]["summary"], "updated")
        self.assertEqual(summarizer.cache, persistence.saved)

    def test_dashboard_counts_real_profile_table_and_marks_missing_table_degraded(self):
        from astrmai.webui.backend.services.dashboard_repository import DashboardRepository

        async def run():
            async with aiosqlite.connect(self.db_path) as db:
                await db.executescript(
                    """
                    CREATE TABLE user_profiles (user_id TEXT);
                    CREATE TABLE MemoryEvent (id INTEGER);
                    CREATE TABLE canonical_memories (kind TEXT, status TEXT, metadata TEXT);
                    INSERT INTO user_profiles VALUES ('u1'), ('u2');
                    """
                )
                await db.commit()
            repo = DashboardRepository(self._db_factory)
            healthy = await repo.snapshot_counts()
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DROP TABLE user_profiles")
                await db.commit()
            degraded = await repo.snapshot_counts()
            return healthy, degraded

        healthy, degraded = asyncio.run(run())
        self.assertEqual(healthy["total_users"], 2)
        self.assertEqual(healthy["_degraded"], {})
        self.assertIsNone(degraded["total_users"])
        self.assertIn("total_users", degraded["_degraded"])

    def test_review_filter_counts_records_beyond_old_bounded_prefix(self):
        from astrmai.webui.backend.adapters.plugin_api import PluginApiAdapter
        from astrmai.webui.backend.services.review_ui_service import ReviewUiService

        async def run():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    CREATE TABLE canonical_memories (
                        id TEXT, session_id TEXT, source TEXT, content TEXT, status TEXT,
                        create_time REAL, update_time REAL, last_access_time REAL,
                        metadata TEXT, kind TEXT
                    )
                    """
                )
                for index in range(75):
                    content = "late needle" if index == 70 else f"expression {index}"
                    await db.execute(
                        "INSERT INTO canonical_memories VALUES (?, 'g1', 'test', ?, 'review_pending', ?, ?, ?, ?, 'expression_pattern')",
                        (
                            f"r{index}",
                            content,
                            float(index),
                            float(index),
                            float(index),
                            json.dumps({"review_status": "pending"}),
                        ),
                    )
                await db.commit()
            service = ReviewUiService(PluginApiAdapter(facade=None), self._db_factory)
            return await service.list_reviews(keyword="needle", page=1, page_size=10)

        result = asyncio.run(run())
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["id"], "r70")
        self.assertEqual(result["items"][0]["expression"], "late needle")

    def test_memory_event_create_is_visible_in_paired_list_and_deletable(self):
        from astrmai.webui.backend.services.memory_ui_service import MemoryUiService

        async def run():
            async with aiosqlite.connect(self.db_path) as db:
                await db.executescript(
                    """
                    CREATE TABLE MemoryEvent (id INTEGER PRIMARY KEY, tags TEXT, metadata TEXT);
                    CREATE TABLE canonical_memories (
                        id TEXT PRIMARY KEY, session_id TEXT, persona_id TEXT, source TEXT,
                        kind TEXT, content TEXT, summary TEXT, tags TEXT, importance REAL,
                        confidence REAL, status TEXT, decay_score REAL, create_time REAL,
                        update_time REAL, last_access_time REAL, access_count INTEGER,
                        superseded_by TEXT, deleted_reason TEXT, metadata TEXT, dedup_key TEXT,
                        source_ref TEXT, visibility TEXT
                    );
                    """
                )
                await db.commit()
            service = MemoryUiService(self._db_factory)
            created = await service.create_event({"narrative": "visible event"})
            listed = await service.list_events()
            deleted = await service.delete_event(created["canonical_id"])
            return created, listed, deleted

        created, listed, deleted = asyncio.run(run())
        self.assertTrue(any(item.get("id") == created["canonical_id"] for item in listed))
        self.assertEqual(deleted["mode"], "canonical_soft_delete")
        self.assertTrue(deleted["changed"])

    def test_admin_frontend_uses_single_unwrap_and_round11_fields(self):
        app_js = (Path(__file__).resolve().parents[3] / "pages" / "admin" / "app.js").read_text(encoding="utf-8")
        self.assertIn('Object.prototype.hasOwnProperty.call(result, "data")', app_js)
        self.assertNotIn(".data ||", app_js)
        self.assertNotIn("?.data?.", app_js)
        self.assertIn("item.expression || item.text", app_js)
        self.assertIn("data-review-page", app_js)
        self.assertIn("snapshot.db_size_kb ?? 0", app_js)


if __name__ == "__main__":
    unittest.main()
