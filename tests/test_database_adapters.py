import asyncio
import importlib
import sqlite3
import sys
import tempfile
import unittest

from tests.test_persistence_regressions import _install_astrbot_stubs


class DatabaseAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infra.persistence", None)
        sys.modules.pop("astrmai.infra.database", None)
        self.persistence_mod = importlib.import_module("astrmai.infra.persistence")
        self.database_mod = importlib.import_module("astrmai.infra.database")
        self.datamodels_mod = importlib.import_module("astrmai.infra.datamodels")
        self.manager = self.persistence_mod.PersistenceManager()
        self.db = self.database_mod.DatabaseService(self.manager)

    def tearDown(self):
        self.manager.engine.dispose()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_jargon_adapter_methods(self):
        with self.db.get_session() as session:
            session.add(
                self.datamodels_mod.Jargon(
                    group_id="group-1",
                    content="梗1",
                    meaning="第一条含义",
                    is_jargon=True,
                )
            )
            session.add(
                self.datamodels_mod.Jargon(
                    group_id="group-2",
                    content="梗2",
                    meaning="第二条含义",
                    is_jargon=True,
                )
            )
            session.commit()

        self.assertEqual(self.db.get_jargon("group-1", "梗1"), "第一条含义")

        search_results = self.db.search_jargons("第二条", limit=2)
        self.assertEqual(len(search_results), 1)
        self.assertEqual(search_results[0].content, "梗2")

        jargon_list = asyncio.run(self.db.load_jargon_list("group-1", limit=5))
        self.assertEqual(jargon_list, [{"text": "梗1", "meaning": "第一条含义", "situation": ""}])

    def test_profile_lookup_by_name_and_nickname(self):
        with sqlite3.connect(self.manager.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO user_profiles
                (user_id, name, social_score, last_seen, persona_analysis, group_footprints,
                 identity, tags, nickname, nickname_reason, know_times, is_known,
                 memory_points, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "user-1",
                    "Alice",
                    42.5,
                    123.0,
                    "helpful tester",
                    "{}",
                    "tester",
                    "[]",
                    "ally",
                    "friendly nickname",
                    3,
                    1,
                    "[]",
                    999.0,
                ),
            )
            conn.commit()

        by_name = self.db.get_profile_by_name("Alice")
        by_nickname = self.db.get_profile_by_name("ally")

        self.assertIsNotNone(by_name)
        self.assertEqual(by_name.user_id, "user-1")
        self.assertIsNotNone(by_nickname)
        self.assertEqual(by_nickname.name, "Alice")

    def test_pattern_adapter_methods(self):
        self.db.save_pattern(
            self.datamodels_mod.ExpressionPattern(
                group_id="group-1",
                situation="chat",
                expression="hello",
                weight=1.0,
            )
        )

        self.db.adjust_pattern_weight("group-1", "chat", "hello", -0.4)
        pattern = next(p for p in self.db.get_patterns("group-1", limit=5) if p.expression == "hello")
        self.assertAlmostEqual(pattern.weight, 0.6)

        asyncio.run(self.db.adjust_pattern_weight_async("group-1", "chat", "hello", 2.0))
        pattern = next(p for p in self.db.get_patterns("group-1", limit=5) if p.expression == "hello")
        self.assertAlmostEqual(pattern.weight, 2.0)

        self.db.delete_pattern(pattern.id)
        remaining = [p for p in self.db.get_patterns("group-1", limit=5) if p.expression == "hello"]
        self.assertEqual(remaining, [])


if __name__ == "__main__":
    unittest.main()
