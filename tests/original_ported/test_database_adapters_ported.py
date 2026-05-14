import asyncio
import importlib
import sqlite3
import sys
import tempfile
import unittest

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class DatabaseAdaptersPortedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        for module_name in list(sys.modules):
            if module_name.startswith("astrmai.infrastructure.persistence"):
                sys.modules.pop(module_name, None)
        persistence_mod = importlib.import_module("astrmai.infrastructure.persistence.persistence_manager")
        database_mod = importlib.import_module("astrmai.infrastructure.persistence.database_service")
        orm_mod = importlib.import_module("astrmai.infrastructure.persistence.orm_models")
        self.ExpressionPattern = orm_mod.ExpressionPattern
        self.Jargon = orm_mod.Jargon
        self.manager = persistence_mod.PersistenceManager()
        self.db = database_mod.DatabaseService(self.manager)

    def tearDown(self):
        self.manager.engine.dispose()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_jargon_adapter_methods(self):
        with self.db.get_session() as session:
            session.add(self.Jargon(group_id="group-1", content="spark", meaning="first meaning", is_jargon=True))
            session.add(self.Jargon(group_id="group-2", content="spark", meaning="second meaning", is_jargon=True))
            session.commit()

        self.assertEqual(self.db.get_jargon("group-1", "spark"), "first meaning")

        search_results = self.db.search_jargons("second", limit=2)
        self.assertEqual(len(search_results), 1)
        self.assertEqual(search_results[0].content, "spark")

        jargon_list = asyncio.run(self.db.load_jargon_list("group-1", limit=5))
        self.assertEqual(jargon_list, [{"text": "spark", "meaning": "first meaning", "situation": ""}])

    def test_legacy_save_jargon_redirects_to_canonical_memory(self):
        saved = self.db.save_jargon(
            self.Jargon(
                group_id="group-1",
                content="phoenix",
                raw_content="phoenix",
                meaning="reborn strategy",
                is_jargon=True,
                is_complete=True,
                count=1,
            )
        )

        with sqlite3.connect(self.manager.db_path) as conn:
            canonical = conn.execute(
                """
                SELECT kind, session_id, content, summary, status, visibility, metadata
                FROM canonical_memories
                WHERE kind = 'jargon' AND session_id = ? AND content = ?
                """,
                ("group-1", "phoenix"),
            ).fetchone()
            legacy_count = conn.execute(
                "SELECT COUNT(*) FROM Jargon WHERE group_id = ? AND content = ?",
                ("group-1", "phoenix"),
            ).fetchone()[0]

        self.assertIsNotNone(canonical)
        self.assertEqual(canonical[0], "jargon")
        self.assertEqual(canonical[3], "reborn strategy")
        self.assertEqual(canonical[4], "active")
        self.assertEqual(canonical[5], "auto_and_tool")
        self.assertEqual(legacy_count, 0)
        self.assertEqual(saved.content, "phoenix")
        self.assertEqual(saved.meaning, "reborn strategy")

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
            self.ExpressionPattern(group_id="group-1", situation="chat", expression="hello", weight=1.0)
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
