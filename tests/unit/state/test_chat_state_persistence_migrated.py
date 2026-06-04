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
        return dict(self.payload)

    async def save_chat_state(self, chat_id, state):
        self.saved_states.append(
            {
                "chat_id": chat_id,
                "energy": state.energy,
                "mood": state.mood,
                "last_reset_date": state.last_reset_date,
                "last_reply_time": state.last_reply_time,
                "last_passive_decay_time": state.last_passive_decay_time,
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
            )
            await persistence.save_chat_state("chat-1", state)
            return await persistence.load_chat_state("chat-1")

        try:
            loaded = asyncio.run(_run())
        finally:
            temp_dir.cleanup()

        self.assertEqual(loaded["last_reply_time"], 111.0)
        self.assertEqual(loaded["last_passive_decay_time"], 222.0)
        self.assertEqual(loaded["total_replies"], 3)

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


if __name__ == "__main__":
    unittest.main()
