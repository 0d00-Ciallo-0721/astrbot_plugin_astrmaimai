import asyncio
import json

from astrmai.infrastructure.runtime.turn_trace_store import TurnTraceSampleStore


def test_v1_payload_remains_readable_and_migrates_on_append(tmp_path):
    path = tmp_path / "turn_trace_samples.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "by_chat": {
                    "chat-a": [
                        {"chat_id": "chat-a", "created_at": 1.0, "status": "executed"},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    store = TurnTraceSampleStore(tmp_path, max_per_chat=5, max_global=10)

    async def run():
        old_items = await store.recent(chat_id="chat-a", limit=5)
        await store.append({"chat_id": "chat-b", "created_at": 2.0, "status": "skipped_wait"})
        return old_items

    old_items = asyncio.run(run())
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert old_items[0]["status"] == "executed"
    assert payload["version"] == 2
    assert payload["by_chat"]["chat-a"][0]["status"] == "executed"
    assert payload["recent"][0]["chat_id"] == "chat-b"


def test_global_recent_is_chronological_and_bounded(tmp_path):
    store = TurnTraceSampleStore(tmp_path, max_per_chat=2, max_global=3)

    async def run():
        await store.append({"chat_id": "chat-a", "created_at": 1.0, "turn_id": "a1"})
        await store.append({"chat_id": "chat-b", "created_at": 4.0, "turn_id": "b4"})
        await store.append({"chat_id": "chat-a", "created_at": 3.0, "turn_id": "a3"})
        await store.append({"chat_id": "chat-c", "created_at": 2.0, "turn_id": "c2"})
        return await store.recent(limit=10)

    items = asyncio.run(run())
    assert [item["turn_id"] for item in items] == ["b4", "a3", "c2"]
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert len(payload["recent"]) == 3
    assert len(payload["by_chat"]["chat-a"]) == 2
