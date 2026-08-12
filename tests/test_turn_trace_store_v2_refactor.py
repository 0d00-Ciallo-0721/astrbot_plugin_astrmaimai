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
    # G8/WU-06: 落盘格式由整文件 JSON 改为 append-only JSONL；legacy 历史在首次
    # 写入时自动迁移，读取端语义（v1 可读、追加后新旧共存）保持不变
    lines = [line for line in store.jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    migrated = [json.loads(line) for line in lines]
    assert old_items[0]["status"] == "executed"
    assert any(item["chat_id"] == "chat-a" and item["status"] == "executed" for item in migrated)
    assert migrated[-1]["chat_id"] == "chat-b"


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
    # G8/WU-06: max_global 由读取端兜底 + 周期性压实共同保证（append-only 下
    # 两次压实之间文件行数可短暂超出，上限由 max_global*COMPACTION_FACTOR 护栏兜住）
    asyncio.run(asyncio.to_thread(store._compact_sync))
    compacted = [
        json.loads(line)
        for line in store.jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(compacted) <= 3
    assert len([item for item in compacted if item["chat_id"] == "chat-a"]) <= 2


def test_append_replaces_existing_turn_id_instead_of_duplicating(tmp_path):
    store = TurnTraceSampleStore(tmp_path, max_per_chat=5, max_global=10)

    async def run():
        await store.append(
            {
                "chat_id": "chat-a",
                "created_at": 1.0,
                "turn_id": "turn-1",
                "status": "skipped_wait",
            }
        )
        await store.append(
            {
                "chat_id": "chat-a",
                "created_at": 2.0,
                "turn_id": "turn-1",
                "status": "executed",
            }
        )
        return await store.recent(chat_id="chat-a", limit=5)

    items = asyncio.run(run())

    assert len(items) == 1
    assert items[0]["status"] == "executed"
    # G8/WU-06: append-only 下旧行不再被物理删除，由「后写覆盖先写」在读取端去重；
    # 压实后物理合并为单行
    asyncio.run(asyncio.to_thread(store._compact_sync))
    compacted = [
        json.loads(line)
        for line in store.jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(compacted) == 1
    assert compacted[0]["status"] == "executed"


def test_late_duplicate_snapshot_does_not_erase_sent_reply(tmp_path):
    store = TurnTraceSampleStore(tmp_path, max_per_chat=5, max_global=10)

    async def run():
        await store.append(
            {
                "chat_id": "chat-a",
                "created_at": 1.0,
                "turn_id": "turn-sent",
                "status": "executed",
                "reply_stats": {"actual_send_count": 1},
            }
        )
        await store.append(
            {
                "chat_id": "chat-a",
                "created_at": 2.0,
                "turn_id": "turn-sent",
                "status": "duplicate_blocked",
                "reply_stats": {},
            }
        )
        return await store.recent(chat_id="chat-a", limit=5)

    items = asyncio.run(run())

    assert len(items) == 1
    assert items[0]["status"] == "executed"
    assert items[0]["turn_final_status"] == "executed"
    assert items[0]["latest_snapshot_status"] == "duplicate_blocked"
    assert items[0]["reply_stats"]["actual_send_count"] == 1
    assert items[0]["snapshot_seq"] == 2


def test_late_duplicate_snapshot_does_not_erase_vision_observation(tmp_path):
    store = TurnTraceSampleStore(tmp_path, max_per_chat=5, max_global=10)

    async def run():
        await store.append(
            {
                "chat_id": "chat-a",
                "created_at": 1.0,
                "turn_id": "turn-vision",
                "status": "executed",
                "vision_observation": {
                    "vision_path": "direct",
                    "vision_call_status": "success",
                    "image_count": 1,
                },
            }
        )
        await store.append(
            {
                "chat_id": "chat-a",
                "created_at": 2.0,
                "turn_id": "turn-vision",
                "status": "duplicate_blocked",
                "vision_observation": {},
            }
        )
        return await store.recent(chat_id="chat-a", limit=5)

    items = asyncio.run(run())

    assert len(items) == 1
    assert items[0]["vision_observation"]["vision_path"] == "direct"
    assert items[0]["vision_observation"]["vision_call_status"] == "success"
