import asyncio
import importlib
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite


class UserUiServiceMigratedTests(unittest.TestCase):
    def setUp(self):
        self.mod = importlib.import_module("astrmai.webui.backend.services.user_ui_service")
        self.mod = importlib.reload(self.mod)
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "users.db"

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_update_user_and_slice_mutations_mark_manual_locks(self):
        async def _run():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    CREATE TABLE user_profiles (
                        user_id TEXT PRIMARY KEY,
                        name TEXT,
                        social_score REAL,
                        last_seen REAL,
                        persona_analysis TEXT,
                        message_count_for_profiling INTEGER DEFAULT 0,
                        last_persona_gen_time REAL DEFAULT 0,
                        group_footprints TEXT,
                        profile_metadata TEXT DEFAULT '{}',
                        identity TEXT,
                        tags TEXT DEFAULT '[]',
                        nickname TEXT DEFAULT '',
                        nickname_reason TEXT DEFAULT '',
                        know_times INTEGER DEFAULT 0,
                        is_known INTEGER DEFAULT 0,
                        memory_points TEXT DEFAULT '[]',
                        identity_points TEXT DEFAULT '[]',
                        preference_points TEXT DEFAULT '[]',
                        relationship_points TEXT DEFAULT '[]',
                        speech_style_points TEXT DEFAULT '[]',
                        updated_at REAL
                    )
                    """
                )
                await db.execute(
                    """
                    INSERT INTO user_profiles
                    (user_id, name, social_score, last_seen, persona_analysis, group_footprints, profile_metadata,
                     identity, tags, nickname, nickname_reason, know_times, is_known,
                     memory_points, identity_points, preference_points, relationship_points, speech_style_points, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "user-1",
                        "Alice",
                        1.0,
                        0.0,
                        "old analysis",
                        "{}",
                        "{}",
                        "",
                        "[]",
                        "",
                        "",
                        0,
                        0,
                        "[]",
                        "[]",
                        "[]",
                        "[]",
                        "[]",
                        0.0,
                    ),
                )
                await db.commit()

            @asynccontextmanager
            async def _db_factory():
                conn = await aiosqlite.connect(self.db_path)
                conn.row_factory = aiosqlite.Row
                try:
                    yield conn
                finally:
                    await conn.close()

            service = self.mod.UserUiService(_db_factory)
            await service.update_user(
                "user-1",
                {"name": "Alice Manual", "tags": "夜猫子, 熟人", "persona_analysis": "manual analysis"},
            )
            await service.add_slice("user-1", "identity_points", "身份:手工设定")
            record = await service.get_user("user-1")
            return record

        record = asyncio.run(_run())
        self.assertEqual(record["name"], "Alice Manual")
        self.assertEqual(record["tags"], ["夜猫子", "熟人"])
        locks = set(record["profile_metadata"]["manual_locked_fields"])
        self.assertTrue({"name", "tags", "persona_analysis", "identity_points"}.issubset(locks))


if __name__ == "__main__":
    unittest.main()
