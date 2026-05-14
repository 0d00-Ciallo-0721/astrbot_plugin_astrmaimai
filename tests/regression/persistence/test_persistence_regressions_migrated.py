import asyncio
import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.helpers import install_astrbot_stubs


class PersistenceRegressionsMigratedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.managers = []
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.persistence.orm_models", None)
        sys.modules.pop("astrmai.infrastructure.persistence.database_service", None)
        sys.modules.pop("astrmai.memory.dream.dream_agent", None)
        sys.modules.pop("astrmai.memory.services.memory_engine", None)
        sys.modules.pop("astrmai.memory.retrieval.react_retriever", None)
        sys.modules.pop("astrmai.infrastructure.persistence.persistence_manager", None)
        self.persistence_mod = importlib.import_module("astrmai.infrastructure.persistence.persistence_manager")
        self.persistence_mod = importlib.reload(self.persistence_mod)

    def tearDown(self):
        for manager in self.managers:
            manager.engine.dispose()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_sync_init_without_running_loop_creates_session_id_column(self):
        manager = self.persistence_mod.PersistenceManager()
        self.managers.append(manager)

        self.assertIsNone(manager._init_task)
        self.assertTrue(Path(manager.db_path).exists())

        with sqlite3.connect(manager.db_path) as conn:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(memoryevent)").fetchall()]

        self.assertIn("session_id", cols)

    def test_async_init_with_running_loop_schedules_task(self):
        async def _build():
            manager = self.persistence_mod.PersistenceManager()
            self.managers.append(manager)
            self.assertIsNotNone(manager._init_task)
            await manager._init_task
            return manager.db_path

        db_path = asyncio.run(_build())

        with sqlite3.connect(db_path) as conn:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(memoryevent)").fetchall()]

        self.assertIn("session_id", cols)

    def test_reload_datamodels_does_not_duplicate_indexes_on_create_all(self):
        datamodels_mod = importlib.import_module("astrmai.infrastructure.persistence.orm_models")
        datamodels_mod = importlib.reload(datamodels_mod)

        table = datamodels_mod.SQLModel.metadata.tables["memoryretrievaltrace"]
        before_names = sorted(index.name for index in table.indexes)
        self.assertGreater(before_names.count("ix_memoryretrievaltrace_trace_id"), 1)

        manager = self.persistence_mod.PersistenceManager()
        self.managers.append(manager)

        table = datamodels_mod.SQLModel.metadata.tables["memoryretrievaltrace"]
        after_names = sorted(index.name for index in table.indexes)
        self.assertEqual(after_names.count("ix_memoryretrievaltrace_trace_id"), 1)
        self.assertEqual(after_names.count("ix_memoryretrievaltrace_chat_id"), 1)
        self.assertEqual(after_names.count("ix_memoryretrievaltrace_created_at"), 1)

    def test_load_all_user_profiles_returns_structured_profile_fields(self):
        manager = self.persistence_mod.PersistenceManager()
        self.managers.append(manager)

        with sqlite3.connect(manager.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO user_profiles
                (user_id, name, social_score, last_seen, persona_analysis, group_footprints,
                 profile_metadata, identity, tags, nickname, nickname_reason, know_times, is_known,
                 memory_points, message_count_for_profiling, last_persona_gen_time, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "user-1",
                    "Alice",
                    42.5,
                    123.0,
                    "喜欢测试",
                    json.dumps({"group-a": 3}, ensure_ascii=False),
                    json.dumps({"manual_locked_fields": ["nickname"]}, ensure_ascii=False),
                    "tester",
                    json.dumps(["friend", "qa"], ensure_ascii=False),
                    "阿测",
                    "常来验证",
                    7,
                    1,
                    json.dumps(["会写测试"], ensure_ascii=False),
                    9,
                    321.0,
                    999.0,
                ),
            )
            conn.commit()

        profiles = manager.load_all_user_profiles()

        self.assertIn("user-1", profiles)
        self.assertEqual(profiles["user-1"]["name"], "Alice")
        self.assertEqual(profiles["user-1"]["nickname"], "阿测")
        self.assertEqual(profiles["user-1"]["tags"], ["friend", "qa"])
        self.assertEqual(profiles["user-1"]["memory_points"], ["会写测试"])
        self.assertEqual(profiles["user-1"]["message_count_for_profiling"], 9)
        self.assertEqual(profiles["user-1"]["profile_metadata"]["manual_locked_fields"], ["nickname"])

    def test_memory_engine_recall_accepts_and_forwards_top_k(self):
        engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")
        engine_mod = importlib.reload(engine_mod)

        calls = {}

        class _FakeRetriever:
            async def search(self, query, k, session_id=None, persona_id=None):
                calls["query"] = query
                calls["k"] = k
                calls["session_id"] = session_id
                calls["persona_id"] = persona_id
                return [SimpleNamespace(content="old memory", score=0.5)]

        config = SimpleNamespace(
            provider=SimpleNamespace(embedding_models=[]),
            memory=SimpleNamespace(recall_top_k=5),
        )
        gateway = SimpleNamespace(config=config)
        engine = engine_mod.MemoryEngine(context=SimpleNamespace(), gateway=gateway, config=config)
        engine.retriever = _FakeRetriever()

        async def _ready():
            return True

        engine._ensure_faiss_initialized = _ready

        result = asyncio.run(engine.recall("remember this", session_id="chat-1", top_k=3))

        self.assertEqual(calls["k"], 3)
        self.assertEqual(calls["session_id"], "chat-1")
        self.assertIn("remember this", result)

    def test_react_retriever_query_person_uses_profile_loader_and_nickname(self):
        react_mod = importlib.import_module("astrmai.memory.retrieval.react_retriever")
        react_mod = importlib.reload(react_mod)

        persistence = SimpleNamespace(
            load_all_user_profiles=lambda: {
                "user-1": {
                    "name": "Alice",
                    "nickname": "阿测",
                    "persona_analysis": "测试伙伴",
                    "tags": ["friend", "qa"],
                    "social_score": 88,
                }
            }
        )
        db_service = SimpleNamespace(persistence=persistence)
        retriever = react_mod.ReActRetriever(
            memory_engine=None,
            db_service=db_service,
            gateway=SimpleNamespace(),
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=True)),
        )

        by_name = asyncio.run(retriever._tool_query_person(chat_id="chat-1", name="Alice"))
        by_nickname = asyncio.run(retriever._tool_query_person(chat_id="chat-1", name="阿测"))

        self.assertIn("Alice", by_name)
        self.assertIn("阿测", by_nickname)
        self.assertIn("88", by_nickname)


__all__ = ["PersistenceRegressionsMigratedTests"]
