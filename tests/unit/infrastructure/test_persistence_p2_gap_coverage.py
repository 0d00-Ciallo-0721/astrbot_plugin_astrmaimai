import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlmodel import SQLModel, Session, create_engine

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _SqlitePersistence:
    def __init__(self, db_path: Path):
        from astrmai.infrastructure.persistence.database_cron import CronPersistenceMixin
        from astrmai.infrastructure.persistence.database_memory import MemoryPersistenceMixin
        from astrmai.infrastructure.persistence.database_profile_relation import ProfileRelationPersistenceMixin
        from astrmai.infrastructure.persistence.persistence_schema import PersistenceSchemaMixin, _dedupe_sqlmodel_metadata_indexes
        from astrmai.infrastructure.persistence.sqlite_helpers import sqlite_connect_args

        class _Host(
            PersistenceSchemaMixin,
            CronPersistenceMixin,
            MemoryPersistenceMixin,
            ProfileRelationPersistenceMixin,
        ):
            def __init__(self, path: Path):
                self.db_path = path
                self.persistence = SimpleNamespace(load_all_user_profiles=lambda: {})
                self._db_lock = asyncio.Lock()
                self._init_db_sync()
                self.engine = create_engine(f"sqlite:///{path}", connect_args=sqlite_connect_args())
                _dedupe_sqlmodel_metadata_indexes()
                SQLModel.metadata.create_all(self.engine)

            def get_session(self):
                return Session(self.engine)

            def dispose(self):
                self.engine.dispose()

        self.host = _Host(db_path)

    def __getattr__(self, name):
        return getattr(self.host, name)


class PersistenceP2GapCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        self.persistence = _SqlitePersistence(Path(self.temp_dir.name) / "p2.db")

    def tearDown(self):
        try:
            self.persistence.dispose()
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_cron_snapshot_roundtrip_update_and_deactivate(self):
        from astrmai.infrastructure.persistence.orm_models import CronSnapshot

        async def _run():
            await self.persistence.save_cron_snapshot(
                CronSnapshot(job_id="job-1", name="old", target_origin="umo", is_active=True)
            )
            await self.persistence.save_cron_snapshot(
                CronSnapshot(job_id="job-1", name="new", target_origin="umo", note="updated", is_active=True)
            )
            active_before = await self.persistence.get_all_active_cron_snapshots()
            await self.persistence.deactivate_cron_snapshot("job-1")
            active_after = await self.persistence.get_all_active_cron_snapshots()
            return active_before, active_after

        active_before, active_after = asyncio.run(_run())

        self.assertEqual(len(active_before), 1)
        self.assertEqual(active_before[0].name, "new")
        self.assertEqual(active_before[0].note, "updated")
        self.assertEqual(active_after, [])

    def test_cron_snapshot_identity_replacement_commits_as_one_state(self):
        from astrmai.infrastructure.persistence.orm_models import CronSnapshot

        async def _run():
            await self.persistence.save_cron_snapshot(
                CronSnapshot(job_id="old-job", name="daily", target_origin="umo", is_active=True)
            )
            await self.persistence.replace_cron_snapshot(
                "old-job",
                CronSnapshot(job_id="new-job", name="daily", target_origin="umo", is_active=True),
            )

        asyncio.run(_run())

        with self.persistence.get_session() as session:
            old_snapshot = session.get(CronSnapshot, "old-job")
            new_snapshot = session.get(CronSnapshot, "new-job")
        self.assertFalse(old_snapshot.is_active)
        self.assertTrue(new_snapshot.is_active)

    def test_memory_nodes_reflection_and_retrieval_trace_roundtrip(self):
        from astrmai.infrastructure.persistence.orm_models import MemoryNode, MemoryRetrievalTrace

        self.persistence.update_nodes(
            [
                MemoryNode(name="literal%node", type="topic", description="percent"),
                MemoryNode(name="ordinary", type="topic", description="plain"),
            ]
        )
        self.persistence.save_reflection("2026-07-04", "daily note")
        self.persistence.save_retrieval_trace(
            MemoryRetrievalTrace(trace_id="trace-1", chat_id="chat-1", query="hello", final_answer="answer")
        )

        nodes = self.persistence.search_nodes("%", limit=5, include_description=False)
        reflection = self.persistence.get_reflection("2026-07-04")
        traces = self.persistence.get_recent_retrieval_traces("chat-1")

        self.assertEqual([item.name for item in nodes], ["literal%node"])
        self.assertEqual(reflection.reflection, "daily note")
        self.assertEqual(traces[0].trace_id, "trace-1")
        self.assertEqual(traces[0].final_answer, "answer")

    def test_social_relation_updates_and_entity_resolution_paths(self):
        from astrmai.infrastructure.persistence.orm_models import MessageLog

        self.persistence.update_social_relation("group-1", "u1", "u2", "friend", 0.4)
        self.persistence.update_social_relation("group-1", "u1", "u2", "friend", 0.8)
        with self.persistence.get_session() as session:
            session.add(MessageLog(group_id="group-1", sender_id="u3", sender_name="Alice", content="hi"))
            session.commit()

        class _Event:
            unified_msg_origin = "default:GroupMessage:group-1"

            def get_group_id(self):
                return "group-1"

            def get_sender_name(self):
                return "Bob"

            def get_sender_id(self):
                return "u4"

            def get_self_id(self):
                return "bot"

            def get_extra(self, key, default=None):
                return default

        relations = self.persistence.get_user_relations("group-1", "u1")
        resolved_numeric = asyncio.run(self.persistence.resolve_entity_spatio_temporal("12345", _Event()))
        resolved_log = asyncio.run(self.persistence.resolve_entity_spatio_temporal("Alice", _Event()))

        self.assertEqual(relations[0].strength, 1.0)
        self.assertEqual(relations[0].frequency, 2)
        self.assertEqual(resolved_numeric, ("12345", "group-1"))
        self.assertEqual(resolved_log, ("u3", "group-1"))

    def test_persona_cache_handles_missing_invalid_and_save_roundtrip(self):
        from astrmai.infrastructure.persistence.persona_cache import PersonaCacheMixin

        class _CacheHost(PersonaCacheMixin):
            def __init__(self, path):
                self.persona_cache_path = path

        path = Path(self.temp_dir.name) / "persona.json"
        host = _CacheHost(path)

        self.assertEqual(host.load_persona_cache(), {})
        path.write_text("{bad-json", encoding="utf-8")
        self.assertEqual(host.load_persona_cache(), {})

        host.save_persona_cache({"persona-1": {"summary": "calm"}})
        self.assertEqual(host.load_persona_cache(), {"persona-1": {"summary": "calm"}})
        self.assertEqual(asyncio.run(host.load_persona_cache_async()), {"persona-1": {"summary": "calm"}})


if __name__ == "__main__":
    unittest.main()
