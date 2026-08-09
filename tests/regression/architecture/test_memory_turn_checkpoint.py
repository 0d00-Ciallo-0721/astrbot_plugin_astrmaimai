import asyncio
import sqlite3


def test_memory_turn_checkpoint_round_trip_survives_reload(tmp_path):
    from astrmai.infrastructure.persistence.memory_turn_checkpoint import (
        MemoryTurnCheckpointStore,
    )
    from astrmai.infrastructure.persistence.persistence_schema import _run_migrations

    db_path = tmp_path / "checkpoint.db"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version = 78")
        _run_migrations(db)
        db.commit()

    async def _run():
        store = MemoryTurnCheckpointStore(db_path)
        await store.upsert(
            "ff:FriendMessage:1",
            {
                "buffer": [{"content": "未完成的记忆", "sender_name": "用户"}],
                "last_activity": 123.0,
            },
        )
        await store.save_many(
            {
                "ff:GroupMessage:2": {
                    "buffer": [{"content": "群聊片段"}],
                    "last_activity": 456.0,
                }
            }
        )

        restored = await MemoryTurnCheckpointStore(db_path).load_all()
        assert restored["ff:FriendMessage:1"]["buffer"][0]["content"] == "未完成的记忆"
        assert restored["ff:GroupMessage:2"]["buffer"][0]["content"] == "群聊片段"

        await store.delete("ff:FriendMessage:1")
        restored = await store.load_all()
        assert "ff:FriendMessage:1" not in restored
        assert "ff:GroupMessage:2" in restored

    asyncio.run(_run())
