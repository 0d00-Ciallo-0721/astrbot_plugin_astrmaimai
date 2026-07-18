import asyncio
import importlib
import sqlite3
import sys
import tempfile
import unittest

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class DatabaseAdapterRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infra.datamodels", None)
        sys.modules.pop("astrmai.infra.persistence", None)
        sys.modules.pop("astrmai.infra.database", None)
        sys.modules.pop("astrmai.infrastructure.persistence", None)
        sys.modules.pop("astrmai.infrastructure.persistence.persistence_manager", None)
        sys.modules.pop("astrmai.infrastructure.persistence.database_service", None)
        sys.modules.pop("astrmai.infrastructure.persistence.orm_models", None)
        self.persistence_mod = importlib.import_module(
            "astrmai.infrastructure.persistence.persistence_manager"
        )
        self.database_mod = importlib.import_module(
            "astrmai.infrastructure.persistence.database_service"
        )
        self.orm_mod = importlib.import_module(
            "astrmai.infrastructure.persistence.orm_models"
        )
        self.manager = self.persistence_mod.PersistenceManager()
        self.db = self.database_mod.DatabaseService(self.manager)

    def tearDown(self):
        self.manager.engine.dispose()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_repositories_are_mounted_on_database_service(self):
        repositories = self.db.repositories
        self.assertEqual(sorted(repositories.keys()), ["chat", "memory", "profile", "review"])
        self.assertIs(repositories["chat"], self.db.chat_repository)
        self.assertIs(repositories["profile"], self.db.profile_repository)

    def test_memory_and_profile_repositories_delegate_to_legacy_behavior(self):
        with self.db.get_session() as session:
            session.add(
                self.orm_mod.Jargon(
                    group_id="group-1",
                    content="梗",
                    meaning="第一条含义",
                    is_jargon=True,
                )
            )
            session.commit()

        self.assertEqual(self.db.memory_repository.get_jargon("group-1", "梗"), "第一条含义")

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

        by_nickname = self.db.profile_repository.get_profile_by_name("ally")
        self.assertIsNotNone(by_nickname)
        self.assertEqual(by_nickname.name, "Alice")

    def test_profile_repository_resolves_verified_historical_alias(self):
        with sqlite3.connect(self.manager.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO user_profiles
                (user_id, name, social_score, last_seen, persona_analysis, group_footprints,
                 profile_metadata, identity, tags, nickname, nickname_reason, know_times,
                 is_known, memory_points, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "3650815443",
                    "NewDisplayName",
                    1.0,
                    123.0,
                    "group member",
                    "{}",
                    '{"verified_aliases":[{"value":"萤","verified":true,"source":"message"}]}',
                    "",
                    "[]",
                    "",
                    "",
                    1,
                    1,
                    "[]",
                    999.0,
                ),
            )
            conn.commit()

        profile = self.db.profile_repository.get_profile_by_name("萤")

        self.assertIsNotNone(profile)
        self.assertEqual(profile.user_id, "3650815443")
        self.assertEqual(profile.name, "NewDisplayName")


if __name__ == "__main__":
    unittest.main()
