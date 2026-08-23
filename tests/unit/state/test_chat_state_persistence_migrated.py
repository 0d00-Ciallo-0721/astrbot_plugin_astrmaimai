import asyncio
import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrmai.infrastructure.persistence.database_service import DatabaseService
from astrmai.infrastructure.persistence.orm_models import ChatState
from astrmai.infrastructure.persistence.persistence_schema import PersistenceSchemaMixin
from astrmai.infrastructure.persistence.state_profile_persistence import StateProfilePersistenceMixin
from astrmai.state.chat_state_service import ChatStateService


class _SqliteStatePersistence(PersistenceSchemaMixin, StateProfilePersistenceMixin):
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db_sync()


class _ResetPersistence:
    def __init__(self, payload):
        self.payload = payload
        self.saved_states = []

    async def load_chat_state(self, chat_id):
        return ChatState(**self.payload)

    async def save_chat_state(self, chat_id, state):
        self.saved_states.append(
            {
                "chat_id": chat_id,
                "energy": state.energy,
                "mood": state.mood,
                "last_reset_date": state.last_reset_date,
                "last_reply_time": state.last_reply_time,
                "last_passive_decay_time": state.last_passive_decay_time,
                "last_energy_recovery_time": state.last_energy_recovery_time,
            }
        )


class ChatStatePersistenceMigratedTests(unittest.TestCase):
    def test_chat_state_roundtrip_preserves_decay_fields(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

        async def _run():
            persistence = _SqliteStatePersistence(Path(temp_dir.name) / "state.db")
            state = ChatState(
                chat_id="chat-1",
                energy=0.4,
                mood=0.6,
                last_reset_date="2026-05-27",
                total_replies=3,
                last_reply_time=111.0,
                last_passive_decay_time=222.0,
                last_energy_recovery_time=333.0,
            )
            await persistence.save_chat_state("chat-1", state)
            return await persistence.load_chat_state("chat-1")

        try:
            loaded = asyncio.run(_run())
        finally:
            temp_dir.cleanup()

        self.assertEqual(loaded.last_reply_time, 111.0)
        self.assertEqual(loaded.last_passive_decay_time, 222.0)
        self.assertEqual(loaded.last_energy_recovery_time, 333.0)
        self.assertEqual(loaded.total_replies, 3)

    def test_database_service_get_chat_state_preserves_decay_fields(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

        async def _prepare():
            persistence = _SqliteStatePersistence(Path(temp_dir.name) / "state.db")
            state = ChatState(
                chat_id="chat-db",
                energy=0.3,
                mood=-0.4,
                last_reset_date="2026-05-27",
                total_replies=4,
                last_reply_time=333.0,
                last_passive_decay_time=444.0,
                last_energy_recovery_time=555.0,
            )
            await persistence.save_chat_state("chat-db", state)
            return persistence

        try:
            persistence = asyncio.run(_prepare())
            db_service = DatabaseService(SimpleNamespace(db_path=persistence.db_path))
            loaded = db_service.get_chat_state("chat-db")
        finally:
            temp_dir.cleanup()

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.last_reply_time, 333.0)
        self.assertEqual(loaded.last_passive_decay_time, 444.0)
        self.assertEqual(loaded.last_energy_recovery_time, 555.0)
        self.assertEqual(loaded.total_replies, 4)

    def test_get_state_persists_daily_reset_on_first_load(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        persistence = _ResetPersistence(
            {
                "chat_id": "chat-reset",
                "energy": 0.4,
                "mood": 0.6,
                "group_config": {},
                "last_reset_date": yesterday,
                "total_replies": 2,
                "last_reply_time": 10.0,
                "last_passive_decay_time": 20.0,
            }
        )
        service = ChatStateService(
            persistence,
            SimpleNamespace(
                energy=SimpleNamespace(daily_recovery=0.2),
                mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
            ),
        )

        state = asyncio.run(service.get_state("chat-reset"))

        self.assertEqual(state.last_reset_date, datetime.date.today().isoformat())
        self.assertAlmostEqual(state.energy, 0.6)
        self.assertAlmostEqual(state.mood, 0.0)
        self.assertEqual(len(persistence.saved_states), 1)
        self.assertEqual(persistence.saved_states[0]["last_reset_date"], datetime.date.today().isoformat())
        self.assertAlmostEqual(persistence.saved_states[0]["mood"], 0.0)

    def test_peek_state_does_not_reset_persist_or_warm_mutable_cache(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        persistence = _ResetPersistence(
            {
                "chat_id": "chat-shadow",
                "energy": 0.4,
                "mood": 0.6,
                "group_config": {},
                "last_reset_date": yesterday,
            }
        )
        service = ChatStateService(
            persistence,
            SimpleNamespace(
                energy=SimpleNamespace(daily_recovery=0.2),
                mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
            ),
        )

        snapshot = asyncio.run(service.peek_state("chat-shadow"))

        self.assertEqual(snapshot.last_reset_date, yesterday)
        self.assertAlmostEqual(snapshot.energy, 0.4)
        self.assertAlmostEqual(snapshot.mood, 0.6)
        self.assertEqual(persistence.saved_states, [])
        self.assertNotIn("chat-shadow", service.chat_states)

    def test_get_chat_state_survives_schema_rebuild_with_column_reorder(self):
        """回归 (w11): 列缓存预热后chat_states被重建且列序变化,
        get_chat_state应按当前结果集真实列序恢复字段，不受旧缓存污染。"""
        import sqlite3
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db_path = Path(temp_dir.name) / "state.db"

        async def _prepare():
            persistence = _SqliteStatePersistence(db_path)
            state = ChatState(chat_id="chat-reorder", energy=0.7, mood=0.3)
            await persistence.save_chat_state("chat-reorder", state)
            return persistence

        try:
            asyncio.run(_prepare())
            db_service = DatabaseService(SimpleNamespace(db_path=str(db_path)))

            # Step 1: warm the column cache
            first = db_service.get_chat_state("chat-reorder")
            self.assertIsNotNone(first)
            self.assertEqual(first.energy, 0.7)

            # Step 2: rebuild table with mood/energy column order swapped,
            # but migrate data by explicit column names so semantics stay correct.
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("ALTER TABLE chat_states RENAME TO chat_states_old")
                conn.execute("""
                    CREATE TABLE chat_states (
                        chat_id TEXT PRIMARY KEY,
                        mood REAL,
                        energy REAL,
                        group_config TEXT,
                        last_reset_date TEXT,
                        total_replies INTEGER,
                        last_reply_time REAL DEFAULT 0,
                        last_passive_decay_time REAL DEFAULT 0,
                        last_energy_recovery_time REAL DEFAULT 0,
                        total_messages INTEGER DEFAULT 0,
                        judgment_mode TEXT DEFAULT 'single',
                        last_msg_info TEXT DEFAULT '{}',
                        last_access_time REAL DEFAULT 0,
                        next_wakeup_timestamp REAL DEFAULT 0,
                        is_dirty INTEGER DEFAULT 0,
                        updated_at REAL
                    )
                """)
                old = conn.execute(
                    "SELECT * FROM chat_states_old WHERE chat_id = ?",
                    ("chat-reorder",),
                ).fetchone()
                conn.execute("""
                    INSERT INTO chat_states (
                        chat_id, mood, energy, group_config, last_reset_date, total_replies,
                        last_reply_time, last_passive_decay_time, last_energy_recovery_time,
                        total_messages, judgment_mode, last_msg_info, last_access_time,
                        next_wakeup_timestamp, is_dirty, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    old[0], old[2], old[1], old[3], old[4], old[5], old[6], old[7],
                    old[8], old[9], old[10], old[11], old[12], old[13], old[14], old[15],
                ))
                conn.execute("DROP TABLE chat_states_old")
                conn.commit()

            # Step 3: column count unchanged and chat_id stays in place, but business
            # columns changed physical order. The read path must honor current result columns.
            second = db_service.get_chat_state("chat-reorder")
            self.assertIsNotNone(second)
            self.assertEqual(second.chat_id, "chat-reorder")
            self.assertEqual(second.energy, 0.7)
            self.assertEqual(second.mood, 0.3)
        finally:
            temp_dir.cleanup()

    def test_load_chat_state_survives_schema_rebuild_with_column_reorder(self):
        """回归 (w11): 同test_get_chat_state_...，覆盖异步load_chat_state路径。"""
        import sqlite3
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db_path = Path(temp_dir.name) / "state.db"

        async def _run():
            persistence = _SqliteStatePersistence(db_path)
            state = ChatState(chat_id="chat-async-reorder", energy=0.8, mood=-0.2)
            await persistence.save_chat_state("chat-async-reorder", state)

            # Step 1: warm the cache
            first = await persistence.load_chat_state("chat-async-reorder")
            self.assertIsNotNone(first)
            self.assertEqual(first.energy, 0.8)

            # Step 2: rebuild with swapped mood/energy column order and named migration
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("ALTER TABLE chat_states RENAME TO chat_states_old")
                conn.execute("""
                    CREATE TABLE chat_states (
                        chat_id TEXT PRIMARY KEY,
                        mood REAL,
                        energy REAL,
                        group_config TEXT,
                        last_reset_date TEXT,
                        total_replies INTEGER,
                        last_reply_time REAL DEFAULT 0,
                        last_passive_decay_time REAL DEFAULT 0,
                        last_energy_recovery_time REAL DEFAULT 0,
                        total_messages INTEGER DEFAULT 0,
                        judgment_mode TEXT DEFAULT 'single',
                        last_msg_info TEXT DEFAULT '{}',
                        last_access_time REAL DEFAULT 0,
                        next_wakeup_timestamp REAL DEFAULT 0,
                        is_dirty INTEGER DEFAULT 0,
                        updated_at REAL
                    )
                """)
                old = conn.execute(
                    "SELECT * FROM chat_states_old WHERE chat_id = ?",
                    ("chat-async-reorder",),
                ).fetchone()
                conn.execute("""
                    INSERT INTO chat_states (
                        chat_id, mood, energy, group_config, last_reset_date, total_replies,
                        last_reply_time, last_passive_decay_time, last_energy_recovery_time,
                        total_messages, judgment_mode, last_msg_info, last_access_time,
                        next_wakeup_timestamp, is_dirty, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    old[0], old[2], old[1], old[3], old[4], old[5], old[6], old[7],
                    old[8], old[9], old[10], old[11], old[12], old[13], old[14], old[15],
                ))
                conn.execute("DROP TABLE chat_states_old")
                conn.commit()

            # Step 3: async path should also honor current result columns.
            second = await persistence.load_chat_state("chat-async-reorder")
            self.assertIsNotNone(second)
            self.assertEqual(second.chat_id, "chat-async-reorder")
            self.assertEqual(second.energy, 0.8)
            self.assertEqual(second.mood, -0.2)

        try:
            asyncio.run(_run())
        finally:
            temp_dir.cleanup()

    def test_profile_generation_claim_is_atomic_and_token_scoped(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

        async def _run():
            persistence = _SqliteStatePersistence(Path(temp_dir.name) / "claims.db")
            first, second = await asyncio.gather(
                persistence.claim_profile_generation("user-1"),
                persistence.claim_profile_generation("user-1"),
            )
            token = first or second
            self.assertTrue(token)
            self.assertNotEqual(bool(first), bool(second))
            self.assertFalse(await persistence.release_profile_generation("user-1", "stale-token"))
            self.assertTrue(await persistence.release_profile_generation("user-1", token))
            self.assertTrue(await persistence.claim_profile_generation("user-1"))

            replacement = await persistence.get_profile_generation_claim("user-1")
            self.assertTrue(replacement)
            self.assertEqual(replacement, await persistence.get_profile_generation_claim("user-1"))

        try:
            asyncio.run(_run())
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
