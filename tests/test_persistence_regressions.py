import asyncio
import importlib
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


class _DummyLogger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def _install_astrbot_stubs(data_dir: str):
    astrbot_mod = types.ModuleType("astrbot")
    astrbot_mod.__path__ = []
    api_mod = types.ModuleType("astrbot.api")
    api_mod.__path__ = []
    api_mod.logger = _DummyLogger()
    api_star_mod = types.ModuleType("astrbot.api.star")
    api_event_mod = types.ModuleType("astrbot.api.event")
    api_star_mod.Context = type("Context", (), {})
    api_event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})

    core_mod = types.ModuleType("astrbot.core")
    core_mod.__path__ = []
    utils_mod = types.ModuleType("astrbot.core.utils")
    utils_mod.__path__ = []
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    agent_mod = types.ModuleType("astrbot.core.agent")
    agent_mod.__path__ = []
    agent_message_mod = types.ModuleType("astrbot.core.agent.message")
    db_mod = types.ModuleType("astrbot.core.db")
    db_mod.__path__ = []
    vec_db_mod = types.ModuleType("astrbot.core.db.vec_db")
    vec_db_mod.__path__ = []
    faiss_impl_mod = types.ModuleType("astrbot.core.db.vec_db.faiss_impl")
    faiss_impl_mod.__path__ = []
    faiss_vec_db_mod = types.ModuleType("astrbot.core.db.vec_db.faiss_impl.vec_db")

    agent_message_mod.SystemMessageSegment = type("SystemMessageSegment", (), {})
    agent_message_mod.UserMessageSegment = type("UserMessageSegment", (), {})
    agent_message_mod.TextPart = type("TextPart", (), {})
    agent_message_mod.ImagePart = type("ImagePart", (), {})
    faiss_vec_db_mod.FaissVecDB = type("FaissVecDB", (), {})

    def _get_astrbot_data_path():
        return data_dir

    path_mod.get_astrbot_data_path = _get_astrbot_data_path

    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.star"] = api_star_mod
    sys.modules["astrbot.api.event"] = api_event_mod
    sys.modules["astrbot.core"] = core_mod
    sys.modules["astrbot.core.db"] = db_mod
    sys.modules["astrbot.core.db.vec_db"] = vec_db_mod
    sys.modules["astrbot.core.db.vec_db.faiss_impl"] = faiss_impl_mod
    sys.modules["astrbot.core.db.vec_db.faiss_impl.vec_db"] = faiss_vec_db_mod
    sys.modules["astrbot.core.utils"] = utils_mod
    sys.modules["astrbot.core.utils.astrbot_path"] = path_mod
    sys.modules["astrbot.core.agent"] = agent_mod
    sys.modules["astrbot.core.agent.message"] = agent_message_mod


class PersistenceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.managers = []
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infra.persistence", None)
        self.persistence_mod = importlib.import_module("astrmai.infra.persistence")
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

    def test_load_all_user_profiles_returns_structured_profile_fields(self):
        manager = self.persistence_mod.PersistenceManager()
        self.managers.append(manager)

        with sqlite3.connect(manager.db_path) as conn:
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
                    "喜欢测试",
                    json.dumps({"group-a": 3}, ensure_ascii=False),
                    "tester",
                    json.dumps(["friend", "qa"], ensure_ascii=False),
                    "阿测",
                    "常来验证",
                    7,
                    1,
                    json.dumps(["会写测试"], ensure_ascii=False),
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

    def test_dream_agent_seed_events_use_serialization_and_sample_cap(self):
        dream_agent_mod = importlib.import_module("astrmai.memory.dream_agent")
        dream_agent_mod = importlib.reload(dream_agent_mod)

        gateway = SimpleNamespace(config=SimpleNamespace())
        db_service = SimpleNamespace(get_session=lambda: None)
        agent = dream_agent_mod.DreamAgent(gateway=gateway, db_service=db_service, memory_engine=None, config=SimpleNamespace())

        class _FakeEvent:
            def __init__(self, idx):
                self.event_id = f"evt-{idx}"
                self.narrative = f"narrative-{idx}"
                self.emotion = "neutral"
                self.importance = idx

        fake_events = [_FakeEvent(i) for i in range(7)]
        agent._load_session_events = lambda session, session_id: fake_events

        class _Ctx:
            def __enter__(self_inner):
                return object()

            def __exit__(self_inner, exc_type, exc, tb):
                return False

        agent.db = SimpleNamespace(get_session=lambda: _Ctx())

        result = asyncio.run(agent._get_seed_events("session-1"))

        self.assertEqual(len(result), agent.SEED_SAMPLE_SIZE)
        self.assertTrue(all(set(item.keys()) == {"event_id", "narrative", "emotion", "importance"} for item in result))

    def test_dream_agent_seed_events_fall_back_to_date_for_legacy_rows(self):
        manager = self.persistence_mod.PersistenceManager()
        self.managers.append(manager)

        database_mod = importlib.import_module("astrmai.infra.database")
        datamodels_mod = importlib.import_module("astrmai.infra.datamodels")
        dream_agent_mod = importlib.import_module("astrmai.memory.dream_agent")
        dream_agent_mod = importlib.reload(dream_agent_mod)

        db_service = database_mod.DatabaseService(manager)
        with db_service.get_session() as session:
            session.add(
                datamodels_mod.MemoryEvent(
                    event_id="legacy-1",
                    session_id="",
                    date="2026-04-07",
                    narrative="legacy narrative 1",
                    emotion="neutral",
                    importance=1,
                    emotional_intensity=1,
                    reflection="",
                    tags="[]",
                )
            )
            session.add(
                datamodels_mod.MemoryEvent(
                    event_id="legacy-2",
                    session_id="",
                    date="2026-04-07",
                    narrative="legacy narrative 2",
                    emotion="happy",
                    importance=2,
                    emotional_intensity=2,
                    reflection="",
                    tags="[]",
                )
            )
            session.commit()

        agent = dream_agent_mod.DreamAgent(
            gateway=SimpleNamespace(config=SimpleNamespace()),
            db_service=db_service,
            memory_engine=None,
            config=SimpleNamespace(),
        )

        result = asyncio.run(agent._get_seed_events("2026-04-07"))

        self.assertEqual({item["event_id"] for item in result}, {"legacy-1", "legacy-2"})

    def test_memory_engine_recall_accepts_and_forwards_top_k(self):
        engine_mod = importlib.import_module("astrmai.memory.engine")
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
        react_mod = importlib.import_module("astrmai.memory.react_retriever")
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


if __name__ == "__main__":
    unittest.main()
