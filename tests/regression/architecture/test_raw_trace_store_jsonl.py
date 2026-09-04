import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class RawTraceStoreJsonlTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_append_does_not_read_or_rewrite_legacy_json(self):
        from astrmai.infrastructure.runtime.raw_trace_store import RawTraceEventStore

        root = Path(self.temp_dir.name)
        legacy = root / "raw_trace_events.json"
        legacy.write_text(json.dumps({"version": 1, "by_chat": {"old": [{"stage": "old"}]}}), encoding="utf-8")
        store = RawTraceEventStore(root)

        async def run():
            with patch.object(store, "_read_sync", side_effect=AssertionError("legacy read")), patch.object(
                store, "_write_sync", side_effect=AssertionError("legacy rewrite")
            ):
                await store.append({"chat_id": "chat-1", "event_id": "e-1", "stage": "new"})
                await store.flush()

        asyncio.run(run())
        self.assertTrue(store.jsonl_path.exists())
        self.assertEqual(json.loads(store.jsonl_path.read_text(encoding="utf-8").splitlines()[0])["stage"], "new")
        self.assertEqual(json.loads(legacy.read_text(encoding="utf-8"))["by_chat"]["old"][0]["stage"], "old")

    def test_append_many_uses_one_physical_batch_write(self):
        from astrmai.infrastructure.runtime.raw_trace_store import RawTraceEventStore

        store = RawTraceEventStore(Path(self.temp_dir.name))

        async def run():
            with patch.object(store, "_append_lines_sync", wraps=store._append_lines_sync) as write:
                await store.append_many(
                    "chat-1",
                    [{"event_id": "e-1", "stage": "a"}, {"event_id": "e-2", "stage": "b"}],
                )
                await store.flush()
                return write.call_count

        self.assertEqual(asyncio.run(run()), 1)

    def test_recent_falls_back_to_legacy_when_jsonl_is_absent(self):
        from astrmai.infrastructure.runtime.raw_trace_store import RawTraceEventStore

        root = Path(self.temp_dir.name)
        (root / "raw_trace_events.json").write_text(
            json.dumps({"version": 1, "by_chat": {"chat-1": [{"stage": "legacy", "created_at": 1}]}}),
            encoding="utf-8",
        )
        store = RawTraceEventStore(root)
        items = asyncio.run(store.recent(chat_id="chat-1"))
        self.assertEqual(items[0]["stage"], "legacy")

    def test_recent_skips_a_truncated_tail_line(self):
        from astrmai.infrastructure.runtime.raw_trace_store import RawTraceEventStore

        store = RawTraceEventStore(Path(self.temp_dir.name))
        store.jsonl_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "event_id": "e-1",
                    "chat_id": "chat-1",
                    "stage": "complete",
                    "created_at": 1,
                }
            )
            + "\n"
            + '{"event_id":"truncated"',
            encoding="utf-8",
        )

        items = asyncio.run(store.recent(chat_id="chat-1"))
        self.assertEqual([item["stage"] for item in items], ["complete"])

    def test_shutdown_stops_writer_and_rejects_new_events(self):
        from astrmai.infrastructure.runtime.raw_trace_store import RawTraceEventStore

        store = RawTraceEventStore(Path(self.temp_dir.name))

        async def run():
            await store.append({"chat_id": "chat-1", "event_id": "e-1", "stage": "a"})
            await store.close()
            await store.append({"chat_id": "chat-1", "event_id": "e-2", "stage": "b"})
            return store.describe_status()

        status = asyncio.run(run())
        self.assertFalse(status["accepting"])
        self.assertFalse(status["writer_running"])
        self.assertEqual(status["queue_depth"], 0)

    def test_queue_is_bounded_and_drops_oldest_without_blocking(self):
        from astrmai.infrastructure.runtime.raw_trace_store import RawTraceEventStore

        store = RawTraceEventStore(Path(self.temp_dir.name), queue_capacity=2)

        async def run():
            with patch.object(store, "_ensure_writer"):
                await store.append_many(
                    "chat-1",
                    [
                        {"event_id": "e-1", "stage": "a"},
                        {"event_id": "e-2", "stage": "b"},
                        {"event_id": "e-3", "stage": "c"},
                    ],
                )
            status = store.describe_status()
            store._ensure_writer()
            await store.flush()
            await store.close()
            return status

        status = asyncio.run(run())
        self.assertEqual(status["queue_depth"], 2)
        self.assertEqual(status["queue_dropped_total"], 1)

    def test_concurrent_producers_share_one_writer(self):
        from astrmai.infrastructure.runtime.raw_trace_store import RawTraceEventStore

        store = RawTraceEventStore(Path(self.temp_dir.name), queue_capacity=200)

        async def run():
            await asyncio.gather(
                *(
                    store.append(
                        {"chat_id": f"chat-{index % 4}", "event_id": f"e-{index}", "stage": "concurrent"}
                    )
                    for index in range(100)
                )
            )
            writer = store._writer_task
            await store.flush()
            lines = store.jsonl_path.read_text(encoding="utf-8").splitlines()
            await store.close()
            return writer, lines

        writer, lines = asyncio.run(run())
        self.assertIsNotNone(writer)
        self.assertEqual(len(lines), 100)
        self.assertEqual(len({json.loads(line)["event_id"] for line in lines}), 100)

    def test_disk_failure_is_contained_inside_trace_writer(self):
        from astrmai.infrastructure.runtime.raw_trace_store import RawTraceEventStore

        store = RawTraceEventStore(Path(self.temp_dir.name))

        async def run():
            with patch.object(store, "_append_lines_sync", side_effect=OSError("disk unavailable")):
                await store.append({"chat_id": "chat-1", "event_id": "e-1", "stage": "a"})
                await store.flush()
            await store.close()
            return store.describe_status()

        status = asyncio.run(run())
        self.assertEqual(status["write_failure_total"], 1)
        self.assertIn("disk unavailable", status["last_error"])

    def test_payload_is_bounded_and_sensitive_values_are_redacted(self):
        from astrmai.infrastructure.runtime.raw_trace_store import RawTraceEventStore

        store = RawTraceEventStore(Path(self.temp_dir.name), max_event_bytes=1024)

        async def run():
            await store.append(
                {
                    "chat_id": "chat-1",
                    "event_id": "e-1",
                    "stage": "large",
                    "authorization": "Bearer secret-value",
                    "payload": {"image": "data:image/png;base64," + "A" * 5000},
                }
            )
            await store.flush()
            await store.close()

        asyncio.run(run())
        text = store.jsonl_path.read_text(encoding="utf-8")
        self.assertNotIn("secret-value", text)
        self.assertNotIn("A" * 100, text)
        self.assertIn("[REDACTED]", text)

    def test_compaction_enforces_per_chat_and_global_limits(self):
        from astrmai.infrastructure.runtime.raw_trace_store import RawTraceEventStore

        store = RawTraceEventStore(
            Path(self.temp_dir.name),
            max_per_chat=2,
            max_global=3,
            compact_size_bytes=1024 * 1024,
        )

        async def run():
            await store.append_many(
                "chat-1",
                [{"event_id": f"e-{index}", "stage": str(index), "created_at": index + 1} for index in range(7)],
            )
            await store.flush()
            items = await store.recent(chat_id="chat-1", limit=20)
            await store.close()
            return items, store.describe_status()

        items, status = asyncio.run(run())
        self.assertEqual([item["stage"] for item in items], ["6", "5"])
        self.assertEqual(status["compaction_total"], 1)

    def test_planner_caps_raw_trace_events_with_summary(self):
        from astrmai.conversation.planning.planner import Planner

        class _Event:
            def get_extra(self, name, default=None):
                if name == "astrmai_trace_id":
                    return "trace-1"
                if name == "astrmai_trace_log":
                    return [{"stage": f"stage-{index}"} for index in range(200)]
                return default

        planner = object.__new__(Planner)
        events = planner._build_raw_trace_events("chat-1", _Event())
        self.assertEqual(len(events), Planner.MAX_RAW_TRACE_EVENTS_PER_TURN)
        summary = [item for item in events if item["stage"] == "trace.events_truncated"]
        self.assertEqual(summary[0]["payload"]["dropped_event_count"], 73)
        self.assertEqual(len({item["event_id"] for item in events}), len(events))


class MemoryObserverRawTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_memory_observation_uses_single_hub_path(self):
        from astrmai.memory.services.memory_observer import MemoryObserver

        hub = AsyncMock()
        store = AsyncMock()
        observer = MemoryObserver(raw_trace_store=store, observability_hub=hub)

        async def run():
            await observer.record(chat_id="chat-1", component="memory_pipeline", stage="observe")

        asyncio.run(run())
        hub.record.assert_awaited_once()
        store.append.assert_not_awaited()

    def test_memory_observation_falls_back_to_store_without_hub(self):
        from astrmai.memory.services.memory_observer import MemoryObserver

        store = AsyncMock()
        observer = MemoryObserver(raw_trace_store=store)

        async def run():
            await observer.record(chat_id="chat-1", component="memory_pipeline", stage="observe")

        asyncio.run(run())
        store.append.assert_awaited_once()
