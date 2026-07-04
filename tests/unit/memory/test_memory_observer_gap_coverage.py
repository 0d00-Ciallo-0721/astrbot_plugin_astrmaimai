import asyncio
import tempfile
import unittest

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _TraceStore:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.items = []

    async def append(self, item):
        if self.fail:
            raise RuntimeError("trace unavailable")
        self.items.append(item)


class _ObservabilityHub:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.records = []

    async def record(self, **payload):
        if self.fail:
            raise RuntimeError("hub unavailable")
        self.records.append(payload)
        return payload


class MemoryObserverGapCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_record_forwards_to_trace_and_observability_and_updates_counters(self):
        async def _run():
            from astrmai.memory.services.memory_observer import MemoryObserver

            trace_store = _TraceStore()
            hub = _ObservabilityHub()
            observer = MemoryObserver(
                trace_store,
                max_recent_events=3,
                max_events_per_chat=2,
                observability_hub=hub,
            )

            first = await observer.record(
                chat_id="chat-1",
                component="instant_gate",
                stage="gate_hit",
                level="warning",
                turn_id="turn-1",
                memory_id="mem-1",
                reason="matched",
                summary="hit summary",
                payload={"score": 0.9},
            )
            await observer.record(
                chat_id="chat-1",
                component="instant_gate",
                stage="backfill_success",
                level="info",
            )
            await observer.record(
                chat_id="chat-1",
                component="session_summarizer",
                stage="canonical_write_success",
                level="error",
                reason="write failed",
            )

            self.assertEqual(first["chat_id"], "chat-1")
            self.assertEqual(len(trace_store.items), 3)
            self.assertEqual(trace_store.items[0]["stage"], "memory.instant_gate.gate_hit")
            self.assertEqual(trace_store.items[0]["memory_event"]["payload"], {"score": 0.9})
            self.assertEqual(len(hub.records), 3)
            self.assertEqual(hub.records[0]["domain"], "memory")
            self.assertEqual(hub.records[0]["kind"], "action")
            self.assertEqual(hub.records[0]["facets"]["memory_id"], "mem-1")

            runtime = await observer.runtime_snapshot(
                instant_gate_ready=True,
                memory_pipeline_ready=True,
                session_summarizer_ready=False,
                pipeline_status={
                    "running": True,
                    "sweep_task_running": True,
                    "buffered_chats": 2,
                    "tracked_chats": 4,
                    "active_worker_count": 1,
                    "active_worker_chats": ["chat-1"],
                },
            )
            self.assertTrue(runtime["pipeline_running"])
            self.assertEqual(runtime["recent_warning_count"], 1)
            self.assertEqual(runtime["recent_error_count"], 1)
            self.assertGreater(runtime["last_gate_hit_at"], 0)
            self.assertGreater(runtime["last_backfill_success_at"], 0)
            self.assertGreater(runtime["last_summarize_success_at"], 0)
            self.assertGreater(runtime["last_summarize_failure_at"], 0)

        asyncio.run(_run())

    def test_chat_snapshot_recent_events_filters_and_reset(self):
        async def _run():
            from astrmai.memory.services.memory_observer import MemoryObserver

            observer = MemoryObserver(max_recent_events=10, max_events_per_chat=2)
            await observer.record(chat_id="chat-1", component="instant_gate", stage="gate_entered")
            await observer.record(chat_id="chat-1", component="memory_pipeline", stage="maintenance_started")
            await observer.record(chat_id="chat-1", component="session_summarizer", stage="summarize_started")
            await observer.record(chat_id="chat-2", component="instant_gate", stage="gate_miss", level="warning")

            chat = await observer.chat_snapshot(
                chat_id="chat-1",
                pipeline_buffer={
                    "pending_messages": 5,
                    "cooldown_until": 12.5,
                    "failures": 2,
                    "last_update": 10,
                    "last_memory_run_at": 11,
                },
                worker_active=True,
                limit=5,
            )
            self.assertEqual(chat["pending_messages"], 5)
            self.assertTrue(chat["worker_active"])
            self.assertEqual(chat["last_gate_stage"], "gate_entered")
            self.assertEqual(chat["last_backfill_stage"], "maintenance_started")
            self.assertEqual(chat["last_summarize_stage"], "summarize_started")
            self.assertEqual([item["stage"] for item in chat["recent_events"]], ["summarize_started", "maintenance_started"])

            filtered = await observer.recent_events(component="instant_gate", level="warning", limit=10)
            self.assertEqual(len(filtered), 1)
            self.assertEqual(filtered[0]["chat_id"], "chat-2")

            errors = await observer.recent_errors(limit=10)
            self.assertEqual([item["chat_id"] for item in errors], ["chat-2"])

            await observer.reset()
            self.assertEqual(await observer.recent_events(limit=10), [])
            reset_runtime = await observer.runtime_snapshot(
                instant_gate_ready=False,
                memory_pipeline_ready=False,
                session_summarizer_ready=False,
            )
            self.assertEqual(reset_runtime["recent_warning_count"], 0)
            self.assertEqual(reset_runtime["last_gate_hit_at"], 0.0)

        asyncio.run(_run())

    def test_record_degrades_when_trace_or_hub_fails(self):
        async def _run():
            from astrmai.memory.services.memory_observer import MemoryObserver

            observer = MemoryObserver(
                _TraceStore(fail=True),
                observability_hub=_ObservabilityHub(fail=True),
            )
            event = await observer.record(
                chat_id="chat-degraded",
                component="memory_pipeline",
                stage="worker_consumed",
                level="info",
            )

            self.assertEqual(event["chat_id"], "chat-degraded")
            recent = await observer.recent_events(chat_id="chat-degraded")
            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0]["stage"], "worker_consumed")

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
