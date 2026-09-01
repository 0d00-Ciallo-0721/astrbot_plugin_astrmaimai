import asyncio
import inspect
import json
import sqlite3
import time
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from config import AstrMaiConfig
from astrmai.learning.evolution_manager import EvolutionManager
from astrmai.app.plugin_facade import PluginFacade
from astrmai.learning.mining.jargon_candidate_extractor import JargonCandidateExtractor
from astrmai.learning.review.reflector import ExpressionReflector
from astrmai.learning.review.expression_auto_check_task import ExpressionAutoCheckTask
from astrmai.learning.review.jargon_auto_check_task import JargonAutoCheckTask
from astrmai.memory.services.memory_turn_pipeline import MemoryTurnPipeline
from astrmai.infrastructure.gateway.json_utils import parse_json_payload
from astrmai.learning.contracts.learning_envelope import LearningMessageEnvelope
from astrmai.infrastructure.persistence.learning_ingest_outbox import LearningIngressOutboxStore
from astrmai.infrastructure.persistence.reflection_outbox import ReflectionOutboxStore
from astrmai.infrastructure.runtime.background_task_ledger import (
    BackgroundTaskLedger,
    TaskLease,
)
from astrmai.infrastructure.runtime.background_task_budget import (
    BackgroundTaskBudget,
    BackgroundTaskQueueFull,
    BackgroundTaskQueueTimeout,
)
from astrmai.infrastructure.runtime.background_task_owner_registry import (
    BackgroundTaskOwnerRegistry,
)
from astrmai.infrastructure.persistence.memory_turn_ledger import MemoryTurnLedgerStore
from astrmai.learning.review.expression_governance_runner import ExpressionGovernanceRunner
from astrmai.proactive.proactive_task import ProactiveTask
from astrmai.webui.backend.services.admin_ui_service import AdminUiService


def _create_background_ledger_schema(db_path: Path) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.executescript(
            """
            CREATE TABLE background_task_ledger (
                task_id TEXT PRIMARY KEY, task_family TEXT NOT NULL,
                scope_id TEXT NOT NULL DEFAULT '', scheduled_at REAL NOT NULL DEFAULT 0,
                started_at REAL NOT NULL DEFAULT 0, finished_at REAL NOT NULL DEFAULT 0,
                lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
                input_fingerprint TEXT NOT NULL DEFAULT '', checkpoint_before TEXT NOT NULL DEFAULT '{}',
                checkpoint_after TEXT NOT NULL DEFAULT '{}', llm_call_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued', retry_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0,
                UNIQUE(task_family, scope_id, input_fingerprint)
            );
            """
        )
        db.commit()
    finally:
        db.close()


def _create_learning_ingest_schema(db_path: Path) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            """CREATE TABLE learning_ingest_outbox (
                event_id TEXT PRIMARY KEY, envelope_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                lease_token TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0)"""
        )
        db.commit()
    finally:
        db.close()


def _create_reflection_outbox_schema(db_path: Path) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            """CREATE TABLE reflection_outbox (
                reflection_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL DEFAULT '',
                pattern_id TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                lease_token TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0)"""
        )
        db.commit()
    finally:
        db.close()


class _Gateway:
    def __init__(self):
        self.prompts = []
        self.config = SimpleNamespace()

    async def call_data_process_task(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return [{"index": 1, "score": 5}]


class _MetaStore:
    def __init__(self):
        self.values = {}

    async def get_meta(self, key, default=""):
        return self.values.get(key, default)

    async def set_meta(self, key, value):
        self.values[key] = value


class _LearningDb:
    def __init__(self, store):
        self.memory_engine = SimpleNamespace(v2_store=store)

    async def add_message_log_async(self, **_kwargs):
        return None


class BackgroundScheduleContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_learning_envelope_is_immutable_and_has_stable_fallback_id(self):
        class _Event:
            unified_msg_origin = "group-1"
            message_str = "hello"
            message_obj = SimpleNamespace(message_id="", timestamp=123.0)
            def get_sender_id(self): return "u1"
            def get_sender_name(self): return "Alice"
            def get_extra(self, key, default=None): return default

        first = LearningMessageEnvelope.from_event(_Event())
        second = LearningMessageEnvelope.from_event(_Event())
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(first.as_dict()["chat_id"], "group-1")
        with self.assertRaises(Exception):
            first.content = "mutated"

    async def test_learning_ingest_outbox_deduplicates_event_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            import sqlite3
            db = sqlite3.connect(db_path)
            try:
                db.execute("""CREATE TABLE learning_ingest_outbox (
                    event_id TEXT PRIMARY KEY, envelope_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0)""")
                db.commit()
            finally:
                db.close()
            store = LearningIngressOutboxStore(db_path)
            envelope = LearningMessageEnvelope("evt-1", "g", "u", "n", "text")
            self.assertTrue(await store.enqueue(envelope))
            self.assertFalse(await store.enqueue(envelope))
            entries = await store.list_due()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].event_id, "evt-1")

    async def test_learning_ingest_outbox_stops_after_max_attempts(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            db = sqlite3.connect(db_path)
            try:
                db.execute("""CREATE TABLE learning_ingest_outbox (
                    event_id TEXT PRIMARY KEY, envelope_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                    lease_token TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0)""")
                db.commit()
            finally:
                db.close()
            store = LearningIngressOutboxStore(
                db_path, retry_base_seconds=1, max_attempts=2
            )
            envelope = LearningMessageEnvelope("evt-1", "g", "u", "n", "text")
            self.assertTrue(await store.enqueue(envelope))
            first = (await store.claim_due())[0]
            self.assertEqual(
                await store.mark_retry(
                    first.event_id, 1, "temporary", lease_token=first.lease_token
                ),
                "retry_wait",
            )
            db = sqlite3.connect(db_path)
            try:
                db.execute(
                    "UPDATE learning_ingest_outbox SET next_retry_at=0 WHERE event_id='evt-1'"
                )
                db.commit()
            finally:
                db.close()
            second = (await store.claim_due())[0]
            self.assertEqual(
                await store.mark_retry(
                    second.event_id, 2, "permanent", lease_token=second.lease_token
                ),
                "exhausted",
            )
            self.assertEqual(await store.claim_due(), [])
            db = sqlite3.connect(db_path)
            try:
                status, attempts = db.execute(
                    "SELECT status, attempts FROM learning_ingest_outbox WHERE event_id='evt-1'"
                ).fetchone()
            finally:
                db.close()
            self.assertEqual((status, attempts), ("exhausted", 2))

    async def test_learning_ingest_cancel_releases_lease_for_immediate_reload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_learning_ingest_schema(db_path)
            first_store = LearningIngressOutboxStore(db_path)
            second_store = LearningIngressOutboxStore(db_path)
            await first_store.enqueue(
                LearningMessageEnvelope("evt-cancel", "g", "u", "n", "text")
            )
            manager = EvolutionManager.__new__(EvolutionManager)
            manager._ingest_outbox = first_store
            manager._ingest_processing = set()
            started = asyncio.Event()

            async def record_user_message(_envelope):
                started.set()
                await asyncio.Event().wait()

            manager.record_user_message = record_user_message
            task = asyncio.create_task(manager._drain_ingest_outbox())
            await asyncio.wait_for(started.wait(), timeout=1.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            claimed = await second_store.claim_due(lease_seconds=60.0)
            self.assertEqual([entry.event_id for entry in claimed], ["evt-cancel"])

    async def test_learning_ingest_primary_failure_falls_back_to_spool(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            spool = LearningIngressOutboxStore(Path(temp_dir) / "spool.db")

            class _FailingPrimary:
                async def enqueue(self, _envelope):
                    raise OSError("primary unavailable")

                async def contains(self, _event_id):
                    return False

            manager = EvolutionManager.__new__(EvolutionManager)
            manager._ingest_outbox = _FailingPrimary()
            manager._ingest_fallback_outbox = spool
            manager._ingest_worker = None
            manager._background_tasks = set()
            manager._ingest_processing = set()
            manager._handle_task_result = lambda _task: None

            async def record_user_message(_envelope):
                return {"recorded": True}

            manager.record_user_message = record_user_message
            envelope = LearningMessageEnvelope("evt-spool", "g", "u", "n", "text")
            self.assertTrue(await manager.enqueue_user_message(envelope))
            worker = manager._ingest_worker
            if worker is not None:
                await worker
            self.assertEqual(await spool.list_due(limit=10), [])

    async def test_learning_ingest_fallback_only_spool_recovers_on_worker_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            spool = LearningIngressOutboxStore(Path(temp_dir) / "spool.db")
            manager = EvolutionManager.__new__(EvolutionManager)
            manager._ingest_outbox = None
            manager._ingest_fallback_outbox = spool
            manager._ingest_worker = None
            manager._background_tasks = set()
            manager._ingest_processing = set()
            manager._handle_task_result = lambda _task: None

            processed = []

            async def record_user_message(envelope):
                processed.append(envelope.event_id)

            manager.record_user_message = record_user_message
            envelope = LearningMessageEnvelope("evt-fallback-only", "g", "u", "n", "text")

            self.assertTrue(await manager.enqueue_user_message(envelope))
            worker = manager._ingest_worker
            self.assertIsNotNone(worker)
            await worker

            self.assertEqual(processed, ["evt-fallback-only"])
            self.assertEqual(await spool.list_due(limit=10), [])

    async def test_learning_ingest_uses_cache_spool_without_primary_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = AstrMaiConfig()
            db = _LearningDb(_MetaStore())
            db.persistence = SimpleNamespace(
                db_path=None,
                cache_dir=Path(temp_dir),
            )
            manager = EvolutionManager(db, _Gateway(), config=config)

            self.assertIsNone(manager._ingest_outbox)
            self.assertIsNotNone(manager._ingest_fallback_outbox)
            self.assertEqual(
                Path(manager._ingest_fallback_outbox.db_path),
                Path(temp_dir) / "learning_ingest_spool.db",
            )

            envelope = LearningMessageEnvelope("evt-cache-spool", "g", "u", "n", "text")
            self.assertTrue(await manager.enqueue_user_message(envelope))
            worker = manager._ingest_worker
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            self.assertTrue(await manager._ingest_fallback_outbox.contains(envelope.event_id))

    async def test_kernel_signal_always_uses_background_budget(self):
        calls = []

        class _Budget:
            async def run(self, awaitable_factory, **kwargs):
                calls.append((kwargs.get("task_name"), kwargs.get("scope_id")))
                return await awaitable_factory()

        task = ProactiveTask.__new__(ProactiveTask)
        task.background_task_budget = _Budget()
        task._task_ledger = None
        task._background_tasks = set()
        task._kernel_signal_tasks = {}
        task._background_task_stats = {}

        async def handler(chat_id, _snapshot, _decision):
            calls.append(("handler", chat_id))
            return {"performed": True}

        queued = await task._enqueue_kernel_signal(
            "DREAM_MAINTENANCE",
            handler,
            "chat-budget",
            {},
            {},
        )
        self.assertTrue(queued["queued"])
        await task._kernel_signal_tasks[("DREAM_MAINTENANCE", "chat-budget")]
        self.assertEqual(
            calls,
            [
                ("proactive.dream_maintenance", "chat-budget"),
                ("handler", "chat-budget"),
            ],
        )

    async def test_kernel_signal_immediate_cancel_settles_lease(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            task = ProactiveTask.__new__(ProactiveTask)
            task._task_ledger = BackgroundTaskLedger(db_path)
            task._background_tasks = set()
            task._lease_settlement_tasks = set()
            task._kernel_signal_tasks = {}
            task._background_task_stats = {}
            task.background_task_budget = BackgroundTaskBudget(limit=1)

            queued = await task._enqueue_kernel_signal(
                "DREAM_MAINTENANCE",
                lambda *_args: asyncio.sleep(0),
                "chat-cancel",
                {},
                {},
            )
            signal_task = task._kernel_signal_tasks[("DREAM_MAINTENANCE", "chat-cancel")]
            signal_task.cancel()
            await asyncio.gather(signal_task, return_exceptions=True)
            settlements = list(task._lease_settlement_tasks)
            if settlements:
                await asyncio.gather(*settlements, return_exceptions=True)
            rows = await task._task_ledger.list_recent(task_family="proactive.dream_maintenance", limit=1)
            self.assertTrue(queued["queued"])
            self.assertEqual(rows[0]["status"], "cancelled")

    async def test_lease_settlement_retries_then_succeeds(self):
        class _Lease:
            task_id = "task-retry"

        class _Ledger:
            def __init__(self):
                self.calls = 0

            async def finish(self, _lease, **_kwargs):
                self.calls += 1
                if self.calls < 3:
                    raise OSError("temporary sqlite failure")
                return True

        class _Budget:
            async def run(self, awaitable_factory, **_kwargs):
                return await awaitable_factory()

        ledger = _Ledger()
        task = ProactiveTask.__new__(ProactiveTask)
        task._task_ledger = ledger
        task._background_tasks = set()
        task._lease_settlement_tasks = set()
        task._background_task_stats = {}
        task.background_task_budget = _Budget()
        task._handle_task_result = lambda completed: task._background_tasks.discard(completed)

        running = task._fire_background_task(
            lambda: asyncio.sleep(0, result={}),
            task_name="decay",
            scope_id="global",
            task_lease=_Lease(),
        )
        await running
        self.assertEqual(ledger.calls, 3)

    async def test_lease_settlement_failure_is_bounded(self):
        class _Lease:
            task_id = "task-fail"

        class _Ledger:
            def __init__(self):
                self.calls = 0

            async def finish(self, _lease, **_kwargs):
                self.calls += 1
                return False

        class _Budget:
            async def run(self, awaitable_factory, **_kwargs):
                return await awaitable_factory()

        ledger = _Ledger()
        task = ProactiveTask.__new__(ProactiveTask)
        task._task_ledger = ledger
        task._background_tasks = set()
        task._lease_settlement_tasks = set()
        task._background_task_stats = {}
        task.background_task_budget = _Budget()
        task._handle_task_result = lambda completed: task._background_tasks.discard(completed)

        running = task._fire_background_task(
            lambda: asyncio.sleep(0, result={}),
            task_name="diary",
            scope_id="global",
            task_lease=_Lease(),
        )
        await running
        self.assertEqual(ledger.calls, 3)

    async def test_pending_lease_settlement_persists_and_replays(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            ledger = BackgroundTaskLedger(db_path)
            lease = await ledger.claim(
                task_family="decay",
                scope_id="global",
                input_fingerprint="pending",
            )
            self.assertIsNotNone(lease)
            self.assertTrue(
                await ledger.enqueue_pending_settlement(
                    lease,
                    run_id="decay-run-1",
                    status="succeeded",
                    checkpoint_after={"run_id": "decay-run-1"},
                    retry_after_seconds=0,
                )
            )
            pending = await ledger.describe_pending_settlements()
            self.assertEqual(pending["pending_total"], 1)
            replay = await ledger.replay_pending_settlements()
            self.assertEqual(replay["replayed"], 1)
            self.assertEqual(
                (await ledger.describe_pending_settlements())["pending_total"], 0
            )
            row = (await ledger.list_recent(task_family="decay", limit=1))[0]
            self.assertEqual(row["status"], "succeeded")

    async def test_finish_failures_enqueue_pending_settlement_for_recovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            ledger = BackgroundTaskLedger(db_path)
            lease = await ledger.claim(
                task_family="diary", scope_id="global", input_fingerprint="recover"
            )
            original_finish = ledger.finish

            async def _fail_finish(*_args, **_kwargs):
                raise OSError("sqlite unavailable")

            ledger.finish = _fail_finish
            task = ProactiveTask.__new__(ProactiveTask)
            task._task_ledger = ledger
            task._background_tasks = set()
            task._lease_settlement_tasks = set()
            task._background_task_stats = {}
            task.background_task_budget = BackgroundTaskBudget(limit=1)
            task._handle_task_result = lambda completed: task._background_tasks.discard(completed)
            running = task._fire_background_task(
                lambda: asyncio.sleep(0, result={}),
                task_name="diary",
                scope_id="global",
                task_lease=lease,
                run_id="diary-recover-1",
                cancel_status="cancelled",
            )
            await running
            ledger.finish = original_finish
            pending = await ledger.describe_pending_settlements()
            self.assertEqual(pending["pending_total"], 1)
            replay = await ledger.replay_pending_settlements()
            self.assertEqual(replay["replayed"], 1)
            self.assertEqual(
                (await ledger.describe_pending_settlements())["pending_total"], 0
            )

    async def test_finish_with_recovery_persists_after_all_immediate_attempts_fail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            ledger = BackgroundTaskLedger(db_path)
            lease = await ledger.claim(
                task_family="decay", scope_id="global", input_fingerprint="finish-recovery"
            )
            self.assertIsNotNone(lease)
            original_finish = ledger.finish

            async def _fail_finish(*_args, **_kwargs):
                raise OSError("sqlite unavailable")

            ledger.finish = _fail_finish
            self.assertFalse(
                await ledger.finish_with_recovery(
                    lease,
                    run_id="decay-run-recovery",
                    status="retry_wait",
                    error="provider busy",
                    retry_after_seconds=0,
                )
            )
            ledger.finish = original_finish
            pending = await ledger.describe_pending_settlements()
            self.assertEqual(pending["pending_total"], 1)
            replay = await ledger.replay_pending_settlements()
            self.assertEqual(replay["replayed"], 1)
            self.assertEqual(
                (await ledger.describe_pending_settlements())["pending_total"], 0
            )

    async def test_pending_settlement_old_lease_is_marked_superseded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            ledger = BackgroundTaskLedger(db_path)
            lease = await ledger.claim(
                task_family="profile_scan", scope_id="global", input_fingerprint="aba"
            )
            self.assertIsNotNone(lease)
            self.assertTrue(
                await ledger.enqueue_pending_settlement(
                    lease,
                    run_id="profile-old",
                    status="succeeded",
                )
            )
            import sqlite3

            db = sqlite3.connect(db_path)
            try:
                db.execute(
                    "UPDATE background_task_ledger SET lease_token=? WHERE task_id=?",
                    ("new-lease-token", lease.task_id),
                )
                db.commit()
            finally:
                db.close()
            replay = await ledger.replay_pending_settlements()
            self.assertEqual(replay["superseded"], 1)
            self.assertEqual(
                (await ledger.describe_pending_settlements())["pending_total"], 0
            )

    async def test_pending_settlement_is_immediately_replayable_with_business_retry_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            ledger = BackgroundTaskLedger(db_path)
            lease = await ledger.claim(
                task_family="long_term_memory",
                scope_id="chat-retry",
                input_fingerprint="immediate-replay",
            )
            self.assertIsNotNone(lease)
            original_finish = ledger.finish

            async def _fail_finish(*_args, **_kwargs):
                raise OSError("sqlite unavailable")

            ledger.finish = _fail_finish
            self.assertFalse(
                await ledger.finish_with_recovery(
                    lease,
                    run_id="memory-run-immediate",
                    status="retry_wait",
                    error="provider busy",
                    retry_after_seconds=600.0,
                )
            )
            ledger.finish = original_finish
            db = sqlite3.connect(db_path)
            try:
                run_id, settlement_raw, next_retry_at = db.execute(
                    "SELECT run_id, settlement_json, next_retry_at "
                    "FROM background_task_pending_settlements"
                ).fetchone()
            finally:
                db.close()
            self.assertEqual(run_id, "memory-run-immediate")
            self.assertEqual(
                json.loads(settlement_raw)["retry_after_seconds"], 600.0
            )
            self.assertLessEqual(next_retry_at, time.time())

            replay = await ledger.replay_pending_settlements()
            self.assertEqual(replay["replayed"], 1)
            row = (await ledger.list_recent(task_family="long_term_memory", limit=1))[0]
            self.assertEqual(row["status"], "retry_wait")
            self.assertGreater(row["lease_until"], time.time() + 540.0)

    async def test_pending_settlement_ttl_is_enforced_by_recovery_worker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            ledger = BackgroundTaskLedger(db_path)
            lease = await ledger.claim(
                task_family="dream",
                scope_id="__global__",
                input_fingerprint="expired-settlement",
            )
            self.assertIsNotNone(lease)
            await ledger.enqueue_pending_settlement(
                lease,
                run_id="dream-expired",
                status="succeeded",
            )
            db = sqlite3.connect(db_path)
            try:
                db.execute(
                    "UPDATE background_task_pending_settlements SET created_at=?",
                    (time.time() - ledger.PENDING_SETTLEMENT_TTL_SECONDS - 1.0,),
                )
                db.commit()
            finally:
                db.close()

            replay = await ledger.replay_pending_settlements()
            self.assertEqual(replay["expired"], 1)
            self.assertEqual(
                (await ledger.describe_pending_settlements())["pending_total"], 0
            )

    async def test_proactive_stop_performs_final_pending_settlement_replay(self):
        calls = []

        class _Ledger:
            async def replay_pending_settlements(self, **_kwargs):
                calls.append("replay")
                return {"replayed": 1, "failed": 0, "superseded": 0, "expired": 0}

        task = ProactiveTask.__new__(ProactiveTask)
        task.scheduled_scenario_service = None
        task._is_running = True
        task._task = None
        task._settlement_recovery_task = None
        task._background_tasks = set()
        task._maintenance_tasks = {}
        task._lease_settlement_tasks = set()
        task._kernel_signal_tasks = {}
        task._task_ledger = _Ledger()
        task.proactive_dispatcher = SimpleNamespace(shutdown=lambda: asyncio.sleep(0))
        task._persist_profile_scheduler_state = lambda: asyncio.sleep(0)

        await task.stop()

        self.assertEqual(calls, ["replay"])

    async def test_memory_and_dream_settlements_provide_nonempty_run_ids(self):
        memory_settlements = []

        class _MemoryLedger:
            async def claim(self, **_kwargs):
                return TaskLease(
                    "memory-task",
                    "long_term_memory",
                    "chat-run-id",
                    "memory-token",
                    time.time() + 300.0,
                    0,
                )

            async def finish_with_recovery(self, _lease, *, run_id="", **_kwargs):
                memory_settlements.append(run_id)
                return True

        class _Summarizer:
            async def summarize_session(self, **_kwargs):
                return {"summary": "ok"}

        pipeline = MemoryTurnPipeline(
            context=None,
            gateway=SimpleNamespace(),
            engine=SimpleNamespace(),
            session_summarizer=_Summarizer(),
            instant_gate=SimpleNamespace(),
            config=SimpleNamespace(
                memory=SimpleNamespace(
                    summary_threshold=1,
                    maintenance_concurrency=1,
                    long_term_memory_cooldown_sec=0,
                )
            ),
            task_ledger=_MemoryLedger(),
        )
        pipeline._session_history_buffer["chat-run-id"] = {
            "buffer": ["user: one", "assistant: two"],
            "buffered_turn_ids": ["turn-1"],
            "last_update": time.time(),
            "cooldown_until": 0.0,
            "failures": 0,
            "last_run_at": 0.0,
        }
        result = await pipeline.run_maintenance_for_session("chat-run-id", force=True)
        self.assertTrue(result["performed"])
        self.assertTrue(memory_settlements[0].startswith("long_term_memory_"))

        from astrmai.proactive.dream_scheduler import DreamScheduler

        dream_settlements = []

        class _DreamLedger:
            async def finish_with_recovery(self, _lease, *, run_id="", **_kwargs):
                dream_settlements.append(run_id)
                return True

        scheduler = DreamScheduler(
            context=SimpleNamespace(),
            memory_engine=None,
            config=SimpleNamespace(life=SimpleNamespace(dream_interval_min=1)),
            semaphore=asyncio.Semaphore(1),
        )
        scheduler._task_ledger = _DreamLedger()
        dream_lease = TaskLease(
            "dream-task",
            "dream",
            "__global__",
            "dream-token",
            time.time() + 300.0,
            0,
        )
        self.assertTrue(
            await scheduler._finish_task_lease(
                "chat-dream",
                None,
                dream_lease,
                status="succeeded",
            )
        )
        self.assertEqual(dream_settlements, ["dream_chat-dream_dream-task"])

    async def test_proactive_metadata_llm_call_uses_shared_background_budget(self):
        calls = []

        class _Budget:
            async def run(self, awaitable_factory, **kwargs):
                calls.append((kwargs.get("task_name"), kwargs.get("scope_id")))
                return await awaitable_factory()

        class _Gateway:
            async def call_proactive_task_result(self, **_kwargs):
                return SimpleNamespace(text='{"analysis":"ok"}', model_id="model-1")

        task = ProactiveTask.__new__(ProactiveTask)
        task.gateway = _Gateway()
        task.background_task_budget = _Budget()
        task.config = SimpleNamespace(persona=SimpleNamespace(persona_id="mai"))
        task._profiling_stats = {}

        text, model_id = await task._call_background_lane_with_metadata(
            "profile", "user-1", "profile prompt"
        )

        self.assertEqual((text, model_id), ('{"analysis":"ok"}', "model-1"))
        self.assertEqual(calls, [("proactive.profile", "user-1")])

    def test_proactive_background_launcher_has_no_budget_escape_hatch(self):
        parameters = inspect.signature(ProactiveTask._fire_background_task).parameters
        self.assertNotIn("use_budget", parameters)

    async def test_memory_instant_backfill_uses_shared_budget_and_skips_on_rejection(self):
        calls = []

        class _Gate:
            def should_run_llm_backfill(self, *_args, **_kwargs):
                return True

            async def run_llm_backfill(self, _turn):
                calls.append("provider")

        class _Budget:
            async def run(self, awaitable_factory, **kwargs):
                calls.append((kwargs.get("task_name"), kwargs.get("scope_id")))
                raise BackgroundTaskQueueFull("budget full")

        config = SimpleNamespace(memory=SimpleNamespace(maintenance_concurrency=1))
        pipeline = MemoryTurnPipeline(
            context=None,
            gateway=SimpleNamespace(),
            engine=SimpleNamespace(),
            session_summarizer=SimpleNamespace(),
            instant_gate=_Gate(),
            config=config,
            background_task_budget=_Budget(),
        )
        turn = pipeline.build_turn(
            chat_id="chat-budget",
            user_text="值得记住的一段话",
            assistant_text="收到",
            sender_id="user-1",
            source="test",
            think_level=2,
        )

        await pipeline._maybe_run_llm_backfill(turn)

        self.assertEqual(calls, [("memory_instant_backfill", "chat-budget")])

    async def test_learning_message_append_is_atomic_across_runtime_instances(self):
        from sqlmodel import SQLModel, create_engine

        from astrmai.infrastructure.persistence.database_service import DatabaseService

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            engine = create_engine(f"sqlite:///{db_path}")
            SQLModel.metadata.create_all(engine)
            first = DatabaseService(SimpleNamespace(db_path=str(db_path)))
            second = DatabaseService(SimpleNamespace(db_path=str(db_path)))
            kwargs = {
                "group_id": "group-1",
                "sender_id": "user-1",
                "sender_name": "Alice",
                "content": "hello",
                "conversation_event": {"event_id": "event-atomic", "role": "user"},
            }

            results = await asyncio.gather(
                first.add_message_log_if_absent_async(**kwargs),
                second.add_message_log_if_absent_async(**kwargs),
            )

            self.assertEqual(sorted(results), [False, True])
            db = sqlite3.connect(db_path)
            try:
                count = db.execute(
                    "SELECT COUNT(*) FROM messagelog WHERE event_id='event-atomic'"
                ).fetchone()[0]
            finally:
                db.close()
            self.assertEqual(count, 1)
            engine.dispose()

    async def test_background_ledger_retry_wait_releases_execution_lease(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            db = sqlite3.connect(db_path)
            try:
                db.executescript(
                    """
                    CREATE TABLE background_task_ledger (
                        task_id TEXT PRIMARY KEY, task_family TEXT NOT NULL,
                        scope_id TEXT NOT NULL DEFAULT '', scheduled_at REAL NOT NULL DEFAULT 0,
                        started_at REAL NOT NULL DEFAULT 0, finished_at REAL NOT NULL DEFAULT 0,
                        lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
                        input_fingerprint TEXT NOT NULL DEFAULT '', checkpoint_before TEXT NOT NULL DEFAULT '{}',
                        checkpoint_after TEXT NOT NULL DEFAULT '{}', llm_call_count INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'queued', retry_count INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0,
                        UNIQUE(task_family, scope_id, input_fingerprint)
                    );
                    """
                )
                db.commit()
            finally:
                db.close()
            ledger = BackgroundTaskLedger(db_path)
            lease = await ledger.claim(
                task_family="test", scope_id="g", input_fingerprint="x", lease_seconds=60
            )
            self.assertIsNotNone(lease)
            await ledger.finish(lease, status="retry_wait", retry_after_seconds=0, error="busy")
            replacement = await ledger.claim(
                task_family="test", scope_id="g", input_fingerprint="x", lease_seconds=60
            )
            self.assertIsNotNone(replacement)
            self.assertEqual(replacement.retry_count, 1)

    async def test_background_ledger_blocks_live_lease_and_recent_success(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            db = sqlite3.connect(db_path)
            try:
                db.executescript(
                    """
                    CREATE TABLE background_task_ledger (
                        task_id TEXT PRIMARY KEY, task_family TEXT NOT NULL,
                        scope_id TEXT NOT NULL DEFAULT '', scheduled_at REAL NOT NULL DEFAULT 0,
                        started_at REAL NOT NULL DEFAULT 0, finished_at REAL NOT NULL DEFAULT 0,
                        lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
                        input_fingerprint TEXT NOT NULL DEFAULT '', checkpoint_before TEXT NOT NULL DEFAULT '{}',
                        checkpoint_after TEXT NOT NULL DEFAULT '{}', llm_call_count INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'queued', retry_count INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0,
                        UNIQUE(task_family, scope_id, input_fingerprint)
                    );
                    """
                )
                db.commit()
            finally:
                db.close()
            ledger = BackgroundTaskLedger(db_path)
            lease = await ledger.claim(
                task_family="test", scope_id="g", input_fingerprint="first", lease_seconds=60
            )
            self.assertIsNotNone(lease)
            self.assertIsNone(
                await ledger.claim(
                    task_family="test", scope_id="g", input_fingerprint="second", lease_seconds=60
                )
            )
            self.assertTrue(await ledger.finish(lease, status="succeeded"))
            self.assertIsNone(
                await ledger.claim(
                    task_family="test",
                    scope_id="g",
                    input_fingerprint="second",
                    min_interval_seconds=60,
                )
            )

    async def test_background_ledger_exposes_checkpoints_and_llm_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            ledger = BackgroundTaskLedger(db_path)
            lease = await ledger.claim(
                task_family="profile_scan",
                scope_id="__global__",
                checkpoint_before={"cursor": "before"},
            )
            self.assertIsNotNone(lease)
            await ledger.finish(
                lease,
                status="succeeded",
                checkpoint_after={"cursor": "after"},
                llm_call_count=2,
            )

            rows = await ledger.list_recent(status="succeeded")

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["checkpoint_before"], {"cursor": "before"})
            self.assertEqual(rows[0]["checkpoint_after"], {"cursor": "after"})

    async def test_background_ledger_exposes_run_priority_timing_and_parse_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            ledger = BackgroundTaskLedger(db_path)
            lease = await ledger.claim(
                task_family="profile",
                scope_id="user-1",
                payload={"run_id": "profile-run-1", "priority": 2, "parse_status": "parsed"},
            )
            self.assertIsNotNone(lease)
            await ledger.finish(lease, status="succeeded", llm_call_count=1)
            rows = await ledger.list_recent(task_family="profile", scope_id="user-1")
            self.assertEqual(rows[0]["run_id"], "profile-run-1")
            self.assertEqual(rows[0]["priority"], 2)
            self.assertEqual(rows[0]["parse_status"], "parsed")
            self.assertGreaterEqual(rows[0]["queue_wait_ms"], 0.0)
            self.assertGreaterEqual(rows[0]["execution_ms"], 0.0)

    async def test_profile_and_proactive_global_leases_are_cross_instance_exclusive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            first = BackgroundTaskLedger(db_path)
            second = BackgroundTaskLedger(db_path)

            for family in ("profile_scan", "proactive_scan"):
                claims = await asyncio.gather(
                    first.claim(task_family=family, scope_id="__global__", lease_seconds=60),
                    second.claim(task_family=family, scope_id="__global__", lease_seconds=60),
                )
                winners = [lease for lease in claims if lease is not None]
                self.assertEqual(len(winners), 1)
                await first.finish(winners[0], status="succeeded")

    async def test_memory_maintenance_lease_prevents_cross_instance_duplicate_summary(self):
        class _Summarizer:
            def __init__(self):
                self.calls = 0
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def summarize_session(self, **_kwargs):
                self.calls += 1
                self.started.set()
                await self.release.wait()

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            config = SimpleNamespace(
                memory=SimpleNamespace(summary_threshold=1, long_term_memory_cooldown_sec=7200),
            )
            summarizer = _Summarizer()

            def build_pipeline():
                pipeline = MemoryTurnPipeline(
                    context=None,
                    gateway=SimpleNamespace(config=config),
                    engine=SimpleNamespace(),
                    session_summarizer=summarizer,
                    instant_gate=SimpleNamespace(),
                    config=config,
                    task_ledger=BackgroundTaskLedger(db_path),
                )
                pipeline._session_history_buffer["chat-1"] = {
                    "buffer": ["user", "assistant"],
                    "last_update": time.time(),
                    "cooldown_until": 0.0,
                    "failures": 0,
                    "last_run_at": 0.0,
                }
                return pipeline

            first = build_pipeline()
            second = build_pipeline()
            first_task = asyncio.create_task(first.run_maintenance_for_session("chat-1", force=True))
            await asyncio.wait_for(summarizer.started.wait(), timeout=1.0)

            second_result = await second.run_maintenance_for_session("chat-1", force=True)
            summarizer.release.set()
            first_result = await asyncio.wait_for(first_task, timeout=1.0)

            self.assertTrue(first_result["performed"])
            self.assertEqual(second_result["reason"], "lease_busy_or_cooldown")
            self.assertEqual(summarizer.calls, 1)

    async def test_memory_failure_uses_short_retry_ledger_state(self):
        class _FailingSummarizer:
            async def summarize_session(self, **_kwargs):
                raise RuntimeError("provider unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            ledger = BackgroundTaskLedger(db_path)
            config = SimpleNamespace(
                memory=SimpleNamespace(summary_threshold=1, long_term_memory_cooldown_sec=7200),
            )
            pipeline = MemoryTurnPipeline(
                context=None,
                gateway=SimpleNamespace(config=config),
                engine=SimpleNamespace(),
                session_summarizer=_FailingSummarizer(),
                instant_gate=SimpleNamespace(),
                config=config,
                task_ledger=ledger,
            )
            pipeline._session_history_buffer["chat-1"] = {
                "buffer": ["user", "assistant"],
                "last_update": time.time(),
                "cooldown_until": 0.0,
                "failures": 0,
                "last_run_at": 0.0,
            }

            result = await pipeline.run_maintenance_for_session("chat-1", force=True)
            rows = await ledger.list_recent(task_family="long_term_memory", scope_id="chat-1")

            self.assertEqual(result["reason"], "summary_failed")
            self.assertEqual(rows[0]["status"], "retry_wait")
            self.assertEqual(rows[0]["llm_call_count"], 1)
            self.assertEqual(rows[0]["checkpoint_before"]["pending_messages"], 2)
            self.assertEqual(rows[0]["checkpoint_after"]["pending_messages_restored"], 2)
            self.assertLessEqual(rows[0]["lease_until"] - time.time(), 301.0)

    async def test_profile_background_task_settles_success_timeout_and_cancel(self):
        class _TimeoutBudget:
            async def run(self, *_args, **_kwargs):
                raise BackgroundTaskQueueTimeout("busy")

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            ledger = BackgroundTaskLedger(db_path)
            task = ProactiveTask.__new__(ProactiveTask)
            task._task_ledger = ledger
            task._background_tasks = set()
            task._background_task_stats = {}
            task.background_task_budget = None

            success_lease = await ledger.claim(
                task_family="profile_scan", scope_id="success", input_fingerprint="success"
            )
            success_task = task._fire_background_task(
                lambda: asyncio.sleep(0, result={"profile_count": 1, "llm_call_count": 2}),
                task_name="proactive.profile",
                scope_id="success",
                task_lease=success_lease,
                checkpoint_after=lambda result: result,
                llm_call_count=lambda result: result["llm_call_count"],
            )
            await success_task

            task.background_task_budget = _TimeoutBudget()
            timeout_lease = await ledger.claim(
                task_family="profile_scan", scope_id="timeout", input_fingerprint="timeout"
            )
            timeout_task = task._fire_background_task(
                lambda: asyncio.sleep(0),
                task_name="proactive.profile",
                scope_id="timeout",
                task_lease=timeout_lease,
            )
            with self.assertRaises(BackgroundTaskQueueTimeout):
                await timeout_task

            task.background_task_budget = None
            started = asyncio.Event()

            async def wait_forever():
                started.set()
                await asyncio.Event().wait()

            cancel_lease = await ledger.claim(
                task_family="profile_scan", scope_id="cancel", input_fingerprint="cancel"
            )
            cancel_task = task._fire_background_task(
                wait_forever,
                task_name="proactive.profile",
                scope_id="cancel",
                task_lease=cancel_lease,
            )
            await started.wait()
            cancel_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancel_task

            rows = await ledger.list_recent(task_family="profile_scan", limit=10)
            by_scope = {row["scope_id"]: row for row in rows}
            self.assertEqual(by_scope["success"]["status"], "succeeded")
            self.assertEqual(by_scope["success"]["llm_call_count"], 2)
            self.assertEqual(by_scope["timeout"]["status"], "retry_wait")
            self.assertEqual(by_scope["cancel"]["status"], "retry_wait")

    async def test_maintenance_cycle_routes_all_four_jobs_through_budget_and_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            calls = []

            class _Budget:
                async def run(self, awaitable_factory, **kwargs):
                    calls.append((kwargs.get("task_name"), kwargs.get("scope_id")))
                    return await awaitable_factory()

            async def _service(name):
                calls.append(("service", name))
                return {"processed": 1}

            task = ProactiveTask.__new__(ProactiveTask)
            task._is_running = True
            task._task_ledger = BackgroundTaskLedger(db_path)
            task._background_tasks = set()
            task._maintenance_tasks = {}
            task._background_task_stats = {}
            task.background_task_budget = _Budget()
            task.decay_service = SimpleNamespace(run_once=lambda: _service("decay"))
            task.group_signin_service = SimpleNamespace(run_once=lambda: _service("signin"))
            task.heartflow_topic_digest_service = SimpleNamespace(
                run_once=lambda _manager: _service("heartflow")
            )
            task.heartflow_manager = object()
            task._run_memory_store_maintenance = lambda: _service("memory")

            await task._run_maintenance_cycle()
            await asyncio.sleep(0)
            await asyncio.gather(*list(task._background_tasks), return_exceptions=True)

            self.assertEqual(
                {name for name, _scope in calls if name in {
                    "decay", "memory_maintenance", "group_signin", "heartflow_digest"
                }},
                {"decay", "memory_maintenance", "group_signin", "heartflow_digest"},
            )
            rows = await task._task_ledger.list_recent(limit=10)
            self.assertEqual(len(rows), 4)
            for row in rows:
                self.assertEqual(row["scope_id"], "global")
                self.assertTrue(row["run_id"])
                self.assertEqual(row["status"], "succeeded")

    async def test_managed_maintenance_queue_failure_and_cancel_settle_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            ledger = BackgroundTaskLedger(db_path)

            class _Budget:
                def __init__(self):
                    self.mode = "full"

                async def run(self, awaitable_factory, **_kwargs):
                    if self.mode == "full":
                        raise BackgroundTaskQueueFull("full")
                    if self.mode == "timeout":
                        raise BackgroundTaskQueueTimeout("timeout")
                    return await awaitable_factory()

            task = ProactiveTask.__new__(ProactiveTask)
            task._is_running = True
            task._task_ledger = ledger
            task._background_tasks = set()
            task._maintenance_tasks = {}
            task._background_task_stats = {}
            task.background_task_budget = _Budget()

            queued = await task._enqueue_managed_maintenance(
                task_family="decay", scope_id="global", awaitable_factory=lambda: asyncio.sleep(0)
            )
            self.assertTrue(queued["queued"])
            await asyncio.sleep(0)
            await asyncio.gather(*list(task._background_tasks), return_exceptions=True)
            row = (await ledger.list_recent(task_family="decay", limit=1))[0]
            self.assertEqual(row["status"], "retry_wait")

            task.background_task_budget.mode = "run"
            await task._enqueue_managed_maintenance(
                task_family="group_signin", scope_id="global", awaitable_factory=lambda: asyncio.sleep(0)
            )
            await asyncio.gather(*list(task._background_tasks), return_exceptions=True)
            row = (await ledger.list_recent(task_family="group_signin", limit=1))[0]
            self.assertEqual(row["status"], "succeeded")

            async def _fail():
                raise RuntimeError("failed")

            await task._enqueue_managed_maintenance(
                task_family="heartflow_digest", scope_id="global", awaitable_factory=_fail
            )
            await asyncio.gather(*list(task._background_tasks), return_exceptions=True)
            row = (await ledger.list_recent(task_family="heartflow_digest", limit=1))[0]
            self.assertEqual(row["status"], "retry_wait")

            task.background_task_budget.mode = "timeout"
            await task._enqueue_managed_maintenance(
                task_family="group_signin", scope_id="timeout", awaitable_factory=lambda: asyncio.sleep(0)
            )
            await asyncio.gather(*list(task._background_tasks), return_exceptions=True)
            row = (await ledger.list_recent(task_family="group_signin", scope_id="timeout", limit=1))[0]
            self.assertEqual(row["status"], "retry_wait")

            task.background_task_budget.mode = "run"
            started = asyncio.Event()

            async def _wait_forever():
                started.set()
                await asyncio.Event().wait()

            await task._enqueue_managed_maintenance(
                task_family="memory_maintenance",
                scope_id="global",
                awaitable_factory=_wait_forever,
            )
            active = next(iter(task._background_tasks))
            await started.wait()
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
            settlement_tasks = list(getattr(task, "_lease_settlement_tasks", set()))
            if settlement_tasks:
                await asyncio.gather(*settlement_tasks, return_exceptions=True)
            row = (await ledger.list_recent(task_family="memory_maintenance", limit=1))[0]
            self.assertEqual(row["status"], "cancelled")

    async def test_managed_maintenance_rejects_shutdown_and_deduplicates_scope(self):
        calls = []

        class _Budget:
            async def run(self, awaitable_factory, **kwargs):
                calls.append((kwargs.get("task_name"), kwargs.get("scope_id")))
                return await awaitable_factory()

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            task = ProactiveTask.__new__(ProactiveTask)
            task._is_running = True
            task._task_ledger = BackgroundTaskLedger(db_path)
            task._background_tasks = set()
            task._maintenance_tasks = {}
            task._background_task_stats = {}
            task.background_task_budget = _Budget()
            started = asyncio.Event()

            async def _wait_forever():
                started.set()
                await asyncio.Event().wait()

            first = await task._enqueue_managed_maintenance(
                task_family="group_signin", scope_id="global", awaitable_factory=_wait_forever
            )
            second = await task._enqueue_managed_maintenance(
                task_family="group_signin", scope_id="global", awaitable_factory=_wait_forever
            )
            self.assertTrue(first["queued"])
            self.assertEqual(second["reason"], "already_queued")
            await started.wait()
            task._is_running = False
            rejected = await task._enqueue_managed_maintenance(
                task_family="heartflow_digest", scope_id="global", awaitable_factory=lambda: asyncio.sleep(0)
            )
            self.assertEqual(rejected["reason"], "shutdown_rejected")
            rows = await task._task_ledger.list_recent(task_family="heartflow_digest", limit=1)
            self.assertEqual(rows[0]["status"], "rejected")
            self.assertEqual(rows[0]["run_id"], rejected["run_id"])
            self.assertEqual(rows[0]["execution_ms"], 0.0)
            for pending in list(task._background_tasks):
                pending.cancel()
            await asyncio.gather(*list(task._background_tasks), return_exceptions=True)
            settlement_tasks = list(getattr(task, "_lease_settlement_tasks", set()))
            if settlement_tasks:
                await asyncio.gather(*settlement_tasks, return_exceptions=True)

    async def test_managed_maintenance_immediate_cancel_settles_lease(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            task = ProactiveTask.__new__(ProactiveTask)
            task._is_running = True
            task._task_ledger = BackgroundTaskLedger(db_path)
            task._background_tasks = set()
            task._maintenance_tasks = {}
            task._lease_settlement_tasks = set()
            task._background_task_stats = {}
            task.background_task_budget = BackgroundTaskBudget(limit=1, max_queue=1)
            task.owner_registry = BackgroundTaskOwnerRegistry(generation=15)

            queued = await task._enqueue_managed_maintenance(
                task_family="decay", scope_id="immediate", awaitable_factory=lambda: asyncio.sleep(0)
            )
            pending = next(iter(task._background_tasks))
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            settlement_tasks = list(task._lease_settlement_tasks)
            if settlement_tasks:
                await asyncio.gather(*settlement_tasks, return_exceptions=True)
            row = (await task._task_ledger.list_recent(task_family="decay", scope_id="immediate", limit=1))[0]
            self.assertTrue(queued["run_id"])
            self.assertEqual(row["status"], "cancelled")
            settlement_record = next(
                item
                for item in task.owner_registry.describe()["tasks"]
                if item["task_family"] == "background.lease_settlement"
            )
            self.assertEqual(settlement_record["scope_id"], "immediate")
            self.assertEqual(settlement_record["generation"], 15)
            self.assertTrue(settlement_record["run_id"])
            self.assertEqual(settlement_record["status"], "succeeded")

    async def test_managed_maintenance_uses_real_budget_queue_full(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            budget = BackgroundTaskBudget(limit=1, max_queue=0, wait_timeout_sec=0.1)
            task = ProactiveTask.__new__(ProactiveTask)
            task._is_running = True
            task._task_ledger = BackgroundTaskLedger(db_path)
            task._background_tasks = set()
            task._maintenance_tasks = {}
            task._lease_settlement_tasks = set()
            task._background_task_stats = {}
            task.background_task_budget = budget
            started = asyncio.Event()

            async def _hold():
                started.set()
                await asyncio.Event().wait()

            holder = asyncio.create_task(
                budget.run(_hold, task_name="maintenance.holder", scope_id="global")
            )
            await started.wait()
            await task._enqueue_managed_maintenance(
                task_family="heartflow_digest",
                scope_id="global",
                awaitable_factory=lambda: asyncio.sleep(0),
            )
            await asyncio.gather(*list(task._background_tasks), return_exceptions=True)
            row = (await task._task_ledger.list_recent(task_family="heartflow_digest", limit=1))[0]
            self.assertEqual(row["status"], "retry_wait")
            holder.cancel()
            await asyncio.gather(holder, return_exceptions=True)

    async def test_managed_maintenance_uses_real_budget_queue_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            budget = BackgroundTaskBudget(limit=1, max_queue=1, wait_timeout_sec=0.01)
            task = ProactiveTask.__new__(ProactiveTask)
            task._is_running = True
            task._task_ledger = BackgroundTaskLedger(db_path)
            task._background_tasks = set()
            task._maintenance_tasks = {}
            task._lease_settlement_tasks = set()
            task._background_task_stats = {}
            task.background_task_budget = budget
            started = asyncio.Event()

            async def _hold():
                started.set()
                await asyncio.Event().wait()

            holder = asyncio.create_task(
                budget.run(_hold, task_name="maintenance.holder", scope_id="global")
            )
            await started.wait()
            await task._enqueue_managed_maintenance(
                task_family="diary",
                scope_id="timeout",
                awaitable_factory=lambda: asyncio.sleep(0),
            )
            await asyncio.gather(*list(task._background_tasks), return_exceptions=True)
            row = (await task._task_ledger.list_recent(task_family="diary", scope_id="timeout", limit=1))[0]
            self.assertEqual(row["status"], "retry_wait")
            holder.cancel()
            await asyncio.gather(holder, return_exceptions=True)

    async def test_managed_maintenance_rechecks_shutdown_after_claim(self):
        class _Lease:
            task_id = "task-race"

        class _Ledger:
            def __init__(self):
                self.claim_started = asyncio.Event()
                self.release_claim = asyncio.Event()
                self.finished = []

            async def claim(self, **_kwargs):
                self.claim_started.set()
                await self.release_claim.wait()
                return _Lease()

            async def finish(self, _lease, **kwargs):
                self.finished.append(kwargs)
                return True

        ledger = _Ledger()
        task = ProactiveTask.__new__(ProactiveTask)
        task._is_running = True
        task._task_ledger = ledger
        task._background_tasks = set()
        task._maintenance_tasks = {}
        task._background_task_stats = {}
        task.background_task_budget = None

        async def _work():
            raise AssertionError("shutdown race must not execute work")

        pending = asyncio.create_task(
            task._enqueue_managed_maintenance(
                task_family="diary", scope_id="global", awaitable_factory=_work
            )
        )
        await ledger.claim_started.wait()
        task._is_running = False
        ledger.release_claim.set()
        result = await pending
        self.assertEqual(result["status"], "shutdown")
        self.assertEqual(ledger.finished[0]["status"], "shutdown")

    def test_diary_scheduler_branch_uses_managed_maintenance_entry(self):
        source = Path(__file__).parents[3] / "astrmai" / "proactive" / "proactive_task.py"
        content = source.read_text(encoding="utf-8")
        self.assertIn('task_family="diary"', content)
        self.assertNotIn('task_name="proactive.diary"', content)

    async def test_background_task_diagnostics_exposes_filtered_ledger_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            ledger = BackgroundTaskLedger(db_path)
            lease = await ledger.claim(
                task_family="long_term_memory",
                scope_id="chat-1",
                checkpoint_before={"pending_messages": 2},
            )
            await ledger.finish(
                lease,
                status="succeeded",
                checkpoint_after={"pending_messages_processed": 2},
                llm_call_count=1,
            )
            plugin_api = SimpleNamespace(
                get_persistence=lambda: SimpleNamespace(db_path=db_path),
            )
            service = AdminUiService(plugin_api)

            response = await service.background_task_diagnostics(
                task_family="long_term_memory",
                scope_id="chat-1",
                status="succeeded",
                limit=10,
            )

            self.assertEqual(response["status"], "ok")
            self.assertEqual(response["summary"], {"succeeded": 1})
            self.assertEqual(response["diagnostics"]["status_counts"]["succeeded"], 1)
            self.assertIn("long_term_memory", response["diagnostics"]["by_task_family"])
            self.assertEqual(response["total"], 1)
            self.assertEqual(response["items"][0]["llm_call_count"], 1)

    async def test_governance_retry_wait_uses_short_poll_without_changing_six_hour_cooldown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            runner = ExpressionGovernanceRunner(
                state_engine=SimpleNamespace(persistence=SimpleNamespace(db_path=db_path)),
                interval_seconds=21600,
            )

            async def timeout_work():
                raise BackgroundTaskQueueTimeout("busy")

            await runner._run_scoped(
                timeout_work,
                task_name="governance.audit",
                scope_id="chat-1",
            )
            rows = await runner._task_ledger.list_recent(task_family="governance.audit")

            self.assertEqual(runner.interval_seconds, 21600)
            self.assertEqual(runner._poll_interval_seconds(), 300.0)
            self.assertEqual(rows[0]["status"], "retry_wait")
            self.assertLessEqual(rows[0]["lease_until"] - time.time(), 301.0)

    async def test_governance_cancel_releases_scope_lease_immediately(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            runner = ExpressionGovernanceRunner(
                state_engine=SimpleNamespace(
                    persistence=SimpleNamespace(db_path=db_path)
                ),
                interval_seconds=21600,
            )
            started = asyncio.Event()

            async def wait_forever():
                started.set()
                await asyncio.Event().wait()

            task = asyncio.create_task(
                runner._run_scoped(
                    wait_forever,
                    task_name="governance.audit",
                    scope_id="chat-1",
                )
            )
            await asyncio.wait_for(started.wait(), timeout=1.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            replacement = await runner._task_ledger.claim(
                task_family="governance.audit",
                scope_id="chat-1",
                lease_seconds=60.0,
            )
            self.assertIsNotNone(replacement)

    async def test_realtime_mining_cancel_restores_timestamp_and_releases_lease(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            manager = EvolutionManager.__new__(EvolutionManager)
            manager._task_ledger = BackgroundTaskLedger(db_path)
            manager._last_mining_at = {"chat-1": 123.0}
            manager._mining_timestamp_loaded = {"chat-1"}
            manager.recorder = SimpleNamespace(min_messages=1)
            manager._load_mining_timestamp = lambda _group_id: asyncio.sleep(0)
            manager._mining_cooldown_active = lambda _group_id: False
            manager._backlog_batch_size = lambda: 10
            manager._pipeline_enabled = lambda pipeline: pipeline == "expression"
            manager._pipeline_threshold = lambda _pipeline: 1
            manager._mining_batch_id = lambda *_args, **_kwargs: "batch-1"
            manager._mining_interval = lambda: 21600.0
            manager._backlog_failure_cooldown = lambda: 300.0
            manager._evolution_config = lambda: SimpleNamespace(
                learning_pipeline_timeout_sec=60.0
            )
            logs = [SimpleNamespace(id=1)]

            async def load_logs(*_args, **_kwargs):
                return logs

            persisted: list[float] = []

            async def persist_timestamp(_group_id, value):
                persisted.append(float(value))

            started = asyncio.Event()

            async def process_snapshot(_group_id, _logs, **_kwargs):
                started.set()
                await asyncio.Event().wait()

            manager._load_pipeline_logs = load_logs
            manager._persist_mining_timestamp = persist_timestamp
            manager._process_mining_snapshot = process_snapshot

            task = asyncio.create_task(manager._try_trigger_mining("chat-1"))
            await asyncio.wait_for(started.wait(), timeout=1.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            self.assertEqual(manager._last_mining_at["chat-1"], 123.0)
            self.assertEqual(persisted[-1], 123.0)
            replacement = await manager._task_ledger.claim(
                task_family="learning_mining",
                scope_id="chat-1",
                input_fingerprint="batch-2",
                lease_seconds=60.0,
            )
            self.assertIsNotNone(replacement)

    async def test_memory_turn_failed_claim_can_be_retried_but_commit_is_idempotent(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            db = sqlite3.connect(db_path)
            try:
                db.execute(
                    """CREATE TABLE memory_turn_ledger (
                        turn_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'recording', first_seen_at REAL NOT NULL DEFAULT 0,
                        committed_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                        updated_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                        lease_token TEXT NOT NULL DEFAULT '')"""
                )
                db.commit()
            finally:
                db.close()
            store = MemoryTurnLedgerStore(db_path)
            first_token = await store.claim("turn-1", "chat-1")
            self.assertTrue(first_token)
            await store.release_failed(
                "turn-1", "temporary", lease_token=first_token
            )
            second_token = await store.claim("turn-1", "chat-1")
            self.assertTrue(second_token)
            await store.mark_committed("turn-1", lease_token=second_token)
            self.assertFalse(await store.claim("turn-1", "chat-1"))

    async def test_memory_turn_expired_lease_is_reclaimed_and_fences_stale_worker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            db = sqlite3.connect(db_path)
            try:
                db.execute(
                    """CREATE TABLE memory_turn_ledger (
                        turn_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'recording', first_seen_at REAL NOT NULL DEFAULT 0,
                        committed_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                        updated_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                        lease_token TEXT NOT NULL DEFAULT '')"""
                )
                db.commit()
            finally:
                db.close()
            store = MemoryTurnLedgerStore(db_path)
            stale_token = await store.claim("turn-stale", "chat-1")
            self.assertTrue(stale_token)
            self.assertFalse(await store.claim("turn-stale", "chat-1"))
            db = sqlite3.connect(db_path)
            try:
                db.execute(
                    "UPDATE memory_turn_ledger SET lease_until=0 WHERE turn_id='turn-stale'"
                )
                db.commit()
            finally:
                db.close()

            current_token = await store.claim("turn-stale", "chat-1")

            self.assertTrue(current_token)
            self.assertNotEqual(current_token, stale_token)
            self.assertFalse(
                await store.mark_committed(
                    "turn-stale", lease_token=stale_token
                )
            )
            self.assertTrue(
                await store.mark_committed(
                    "turn-stale", lease_token=current_token
                )
            )

    async def test_memory_turn_checkpoint_retry_does_not_duplicate_buffer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            db = sqlite3.connect(db_path)
            try:
                db.execute(
                    """CREATE TABLE memory_turn_ledger (
                        turn_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'recording', first_seen_at REAL NOT NULL DEFAULT 0,
                        committed_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                        updated_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                        lease_token TEXT NOT NULL DEFAULT '')"""
                )
                db.commit()
            finally:
                db.close()

            class _CheckpointStore:
                def __init__(self):
                    self.attempts = 0
                    self.saved = {}

                async def upsert(self, chat_id, snapshot):
                    self.attempts += 1
                    if self.attempts == 1:
                        raise RuntimeError("checkpoint unavailable")
                    self.saved[chat_id] = snapshot

                async def delete(self, chat_id):
                    self.saved.pop(chat_id, None)

            checkpoint = _CheckpointStore()
            config = AstrMaiConfig()
            pipeline = MemoryTurnPipeline(
                context=None,
                gateway=SimpleNamespace(config=config),
                engine=SimpleNamespace(),
                session_summarizer=SimpleNamespace(),
                instant_gate=SimpleNamespace(),
                config=config,
                checkpoint_store=checkpoint,
                turn_ledger=MemoryTurnLedgerStore(db_path),
            )
            turn = pipeline.build_turn(
                chat_id="chat-1",
                user_text="hello",
                assistant_text="world",
                sender_id="user-1",
                source="reply_commit",
            )

            with self.assertRaisesRegex(RuntimeError, "checkpoint persist failed"):
                await pipeline.record_turn(turn)
            result = await pipeline.record_turn(turn)
            duplicate = await pipeline.record_turn(turn)

            buffer = pipeline._session_history_buffer["chat-1"]["buffer"]
            self.assertEqual(result["pending_messages"], 2)
            self.assertEqual(duplicate["reason"], "duplicate_turn")
            self.assertEqual([item["text"] for item in buffer], ["hello", "world"])
            self.assertEqual(
                pipeline._session_history_buffer["chat-1"]["buffered_turn_ids"],
                [turn.turn_id],
            )
            self.assertEqual(checkpoint.saved["chat-1"]["buffer"], buffer)

    async def test_existing_jargon_is_filtered_before_enrichment(self):
        extractor = JargonCandidateExtractor(min_count=1)
        messages = [
            SimpleNamespace(id="1", content="星轨 星轨", sender_id="u1", sender_name="甲"),
        ]
        candidates = await extractor.extract(
            "group-1",
            messages,
            existing_terms={"星轨": "星轨"},
        )
        self.assertEqual(candidates, [])
        self.assertGreaterEqual(extractor.last_report.get("route_reasons", {}).get("existing_term", 0), 1)

    async def test_reflector_does_not_mix_scoped_pending_items(self):
        gateway = _Gateway()
        reflector = ExpressionReflector(SimpleNamespace(memory_engine=None), gateway)
        await reflector.record_usage(pattern_id="a1", pattern_expression="A1", chat_id="group-a")
        await reflector.record_usage(pattern_id="a2", pattern_expression="A2", chat_id="group-a")
        await reflector.record_usage(pattern_id="a3", pattern_expression="A3", chat_id="group-a")
        await reflector.record_usage(pattern_id="b1", pattern_expression="B1", chat_id="group-b")
        await reflector.record_usage(pattern_id="b2", pattern_expression="B2", chat_id="group-b")
        await reflector.record_usage(pattern_id="b3", pattern_expression="B3", chat_id="group-b")
        await reflector.reflect_batch("group-a")
        self.assertEqual(len(gateway.prompts), 1)
        self.assertIn("A1", gateway.prompts[0])
        self.assertNotIn("B1", gateway.prompts[0])

    async def test_reflector_pending_items_survive_reload(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            db = sqlite3.connect(db_path)
            try:
                db.executescript(
                    """
                    CREATE TABLE reflection_outbox (
                        reflection_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL DEFAULT '',
                        pattern_id TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                        next_retry_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                        lease_token TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0
                    );
                    """
                )
                db.commit()
            finally:
                db.close()
            gateway = _Gateway()
            db_service = SimpleNamespace(db_path=str(db_path), memory_engine=None)
            first = ExpressionReflector(db_service, gateway)
            await first.record_usage(pattern_id="p1", pattern_expression="表达", chat_id="group-a")
            second = ExpressionReflector(db_service, gateway)
            self.assertEqual(await second.pending_scope_ids(), ["group-a"])
            await second._load_pending_from_store("group-a")
            self.assertEqual(len(second._pending_reflections), 1)
            self.assertEqual(second._pending_reflections[0]["pattern_id"], "p1")

    async def test_reflection_outbox_claim_and_ack_are_token_scoped(self):
        import sqlite3

        from astrmai.infrastructure.persistence.reflection_outbox import (
            ReflectionOutboxStore,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            db = sqlite3.connect(db_path)
            try:
                db.executescript(
                    """
                    CREATE TABLE reflection_outbox (
                        reflection_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL DEFAULT '',
                        pattern_id TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                        next_retry_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                        lease_token TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0
                    );
                    """
                )
                db.commit()
            finally:
                db.close()
            first = ReflectionOutboxStore(db_path)
            second = ReflectionOutboxStore(db_path)
            reflection_id = await first.enqueue(
                {"chat_id": "group-a", "pattern_id": "p1", "expression": "表达"}
            )
            claims = await asyncio.gather(
                first.claim_due(chat_id="group-a"),
                second.claim_due(chat_id="group-a"),
            )
            claimed = next(items[0] for items in claims if items)
            self.assertEqual(sorted(len(items) for items in claims), [0, 1])
            self.assertFalse(
                await first.mark_done(reflection_id, lease_token="wrong-token")
            )
            self.assertTrue(
                await first.mark_done(
                    reflection_id, lease_token=claimed.lease_token
                )
            )

    async def test_reflector_cancel_releases_claimed_batch_for_immediate_reload(self):
        class _BlockingGateway:
            def __init__(self):
                self.config = SimpleNamespace()
                self.started = asyncio.Event()

            async def call_data_process_task(self, *_args, **_kwargs):
                self.started.set()
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_reflection_outbox_schema(db_path)
            gateway = _BlockingGateway()
            reflector = ExpressionReflector(
                SimpleNamespace(db_path=str(db_path), memory_engine=None),
                gateway,
            )
            for index in range(3):
                await reflector.record_usage(
                    pattern_id=f"p{index}",
                    pattern_expression=f"expression-{index}",
                    chat_id="group-a",
                )

            task = asyncio.create_task(reflector.reflect_batch("group-a"))
            await asyncio.wait_for(gateway.started.wait(), timeout=1.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            replacement = ReflectionOutboxStore(db_path)
            claimed = await replacement.claim_due(
                chat_id="group-a",
                lease_seconds=60.0,
            )
            self.assertEqual(len(claimed), 3)

    async def test_reflector_empty_score_schedules_retry_instead_of_leaking_lease(self):
        class _EmptyGateway:
            config = SimpleNamespace()

            async def call_data_process_task(self, *_args, **_kwargs):
                return []

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_reflection_outbox_schema(db_path)
            reflector = ExpressionReflector(
                SimpleNamespace(db_path=str(db_path), memory_engine=None),
                _EmptyGateway(),
            )
            for index in range(3):
                await reflector.record_usage(
                    pattern_id=f"p{index}",
                    pattern_expression=f"expression-{index}",
                    chat_id="group-a",
                )

            await reflector.reflect_batch("group-a")

            db = sqlite3.connect(db_path)
            try:
                rows = db.execute(
                    "SELECT status, attempts, lease_token FROM reflection_outbox "
                    "ORDER BY reflection_id"
                ).fetchall()
            finally:
                db.close()
            self.assertEqual(rows, [("retry_wait", 1, "")] * 3)

    async def test_reflector_auto_audit_cooldown_is_per_scope(self):
        calls: list[str] = []

        class _PatternService:
            async def list_patterns(self, group_id, **_kwargs):
                calls.append(str(group_id))
                return []

        db_service = SimpleNamespace(
            memory_engine=SimpleNamespace(
                expression_pattern_service=_PatternService()
            )
        )
        reflector = ExpressionReflector(db_service, _Gateway())

        await reflector.auto_audit("group-a")
        await reflector.auto_audit("group-b")
        await reflector.auto_audit("group-a")

        self.assertEqual(calls, ["group-a", "group-b"])

    async def test_governance_force_does_not_bypass_hard_scope_cooldown(self):
        config = SimpleNamespace(
            evolution=SimpleNamespace(
                review_runner_min_interval_sec=21600,
                review_batch_size=2,
                review_min_count=1,
            )
        )

        class _PatternService:
            async def list_reviewable_patterns(self, **_kwargs):
                return []

            async def list_patterns(self, *_args, **_kwargs):
                return []

        db = SimpleNamespace(memory_engine=SimpleNamespace(expression_pattern_service=_PatternService()))
        gateway = _Gateway()
        expression = ExpressionAutoCheckTask(db, gateway, config=config)
        jargon = JargonAutoCheckTask(db, gateway, config=config)
        reflector = ExpressionReflector(db, gateway, config=config)

        self.assertEqual(await expression.run_once("chat-1"), 0)
        self.assertEqual(await expression.run_once("chat-1", force=True), 0)
        self.assertEqual(await jargon.run_once("chat-1"), 0)
        self.assertEqual(await jargon.run_once("chat-1", force=True), 0)
        await reflector.auto_audit("chat-1")
        await reflector.auto_audit("chat-1", force=True)

        # Every component queried its store only once; manual force did not
        # create a second run inside the six-hour scope cooldown.
        self.assertEqual(len(gateway.prompts), 0)

    async def test_dream_completion_outbox_claim_is_exclusive(self):
        import sqlite3

        from astrmai.infrastructure.persistence.dream_completion_outbox import (
            DreamCompletionOutboxStore,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            db = sqlite3.connect(db_path)
            try:
                db.executescript(
                    """
                    CREATE TABLE dream_completion_outbox (
                        request_key TEXT PRIMARY KEY, run_id TEXT NOT NULL DEFAULT '',
                        session_id TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL DEFAULT 0,
                        updated_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                        lease_token TEXT NOT NULL DEFAULT ''
                    );
                    """
                )
                db.commit()
            finally:
                db.close()
            first = DreamCompletionOutboxStore(db_path)
            second = DreamCompletionOutboxStore(db_path)
            await first.save(
                "session-a",
                {"run_id": "dream-1", "session_id": "session-a"},
            )
            claims = await asyncio.gather(
                first.claim_pending(),
                second.claim_pending(),
            )
            claimed = next(items[0] for items in claims if items)
            self.assertEqual(sorted(len(items) for items in claims), [0, 1])
            self.assertFalse(
                await first.delete("session-a", lease_token="wrong-token")
            )
            self.assertTrue(
                await first.delete("session-a", lease_token=claimed[2])
            )

    async def test_memory_pipeline_exposes_two_hour_cooldown(self):
        config = AstrMaiConfig()
        pipeline = MemoryTurnPipeline(
            context=None,
            gateway=SimpleNamespace(config=config),
            engine=SimpleNamespace(),
            session_summarizer=SimpleNamespace(),
            instant_gate=SimpleNamespace(),
            config=config,
        )
        self.assertEqual(pipeline._long_term_memory_cooldown(), 7200.0)

    async def test_memory_force_does_not_bypass_hard_cooldown(self):
        config = SimpleNamespace(
            memory=SimpleNamespace(summary_threshold=1, long_term_memory_cooldown_sec=7200),
        )
        pipeline = MemoryTurnPipeline(
            context=None,
            gateway=SimpleNamespace(config=config),
            engine=SimpleNamespace(),
            session_summarizer=SimpleNamespace(),
            instant_gate=SimpleNamespace(),
            config=config,
        )
        pipeline._session_history_buffer["chat-1"] = {
            "buffer": ["user", "assistant"],
            "last_update": time.time(),
            "cooldown_until": 0.0,
            "failures": 0,
            "last_run_at": time.time() - 60.0,
        }

        result = await pipeline.run_maintenance_for_session("chat-1", force=True)

        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], "cooldown")
        self.assertEqual(pipeline._session_history_buffer["chat-1"]["buffer"], ["user", "assistant"])

    async def test_mining_cooldown_survives_manager_restart(self):
        config = AstrMaiConfig()
        store = _MetaStore()
        first = EvolutionManager(_LearningDb(store), SimpleNamespace(config=config), config=config)
        first._last_mining_at["group-1"] = time.time()
        await first._persist_mining_timestamp("group-1", first._last_mining_at["group-1"])

        second = EvolutionManager(_LearningDb(store), SimpleNamespace(config=config), config=config)
        await second._load_mining_timestamp("group-1")

        self.assertGreater(second._last_mining_at["group-1"], 0.0)
        self.assertTrue(second._mining_cooldown_active("group-1"))

    async def test_realtime_mining_persists_cooldown_before_provider_call(self):
        config = AstrMaiConfig()
        store = _MetaStore()
        manager = EvolutionManager(_LearningDb(store), SimpleNamespace(config=config), config=config)
        manager._pipeline_enabled = lambda _pipeline: True
        manager._pipeline_threshold = lambda _pipeline: 1
        manager._backlog_batch_size = lambda: 1
        manager.recorder.min_messages = 1
        manager._load_pipeline_logs = lambda *_args, **_kwargs: asyncio.sleep(
            0, result=[SimpleNamespace(id=1)]
        )
        calls = []

        async def process_logs(group_id, logs):
            calls.append((group_id, logs))
            return {}

        manager.process_logs_and_mine = process_logs

        await manager._try_trigger_mining("group-1")

        self.assertEqual(len(calls), 1)
        self.assertIn(manager._mining_timestamp_key("group-1"), store.values)
        self.assertTrue(manager._mining_cooldown_active("group-1"))

    async def test_failed_realtime_mining_uses_short_retry_instead_of_six_hour_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            config = AstrMaiConfig()
            store = _MetaStore()
            db = _LearningDb(store)
            db.persistence = SimpleNamespace(db_path=db_path)
            manager = EvolutionManager(db, SimpleNamespace(config=config), config=config)
            manager._pipeline_enabled = lambda _pipeline: True
            manager._pipeline_threshold = lambda _pipeline: 1
            manager._backlog_batch_size = lambda: 1
            manager.recorder.min_messages = 1
            manager._load_pipeline_logs = lambda *_args, **_kwargs: asyncio.sleep(
                0, result=[SimpleNamespace(id=1, timestamp=1.0)]
            )
            manager.process_logs_and_mine = lambda *_args, **_kwargs: asyncio.sleep(
                0,
                result={
                    "expression": {"status": "failed"},
                    "jargon": {"status": "quarantined"},
                },
            )

            await manager._try_trigger_mining("group-1")
            rows = await manager._task_ledger.list_recent(
                task_family="learning_mining",
                scope_id="group-1",
            )

            self.assertEqual(rows[0]["status"], "retry_wait")
            self.assertEqual(rows[0]["last_error"], "all_pipelines_failed")
            self.assertFalse(manager._mining_cooldown_active("group-1"))
            self.assertLess(rows[0]["lease_until"] - time.time(), 3600.0)

    async def test_mining_ledger_uses_canonical_interval_with_legacy_fallback(self):
        async def claimed_interval(config):
            intervals = []

            class _Ledger:
                async def claim(self, **kwargs):
                    intervals.append(kwargs["min_interval_seconds"])
                    return SimpleNamespace()

                async def finish(self, _lease, **_kwargs):
                    return True

            manager = EvolutionManager(
                _LearningDb(_MetaStore()),
                SimpleNamespace(config=config),
                config=config,
            )
            manager._task_ledger = _Ledger()
            manager._pipeline_enabled = lambda _pipeline: True
            manager._pipeline_threshold = lambda _pipeline: 1
            manager._backlog_batch_size = lambda: 1
            manager.recorder.min_messages = 1
            manager._load_pipeline_logs = lambda *_args, **_kwargs: asyncio.sleep(
                0, result=[SimpleNamespace(id=1, timestamp=1.0)]
            )
            manager.process_logs_and_mine = lambda *_args, **_kwargs: asyncio.sleep(
                0, result={}
            )

            await manager._try_trigger_mining("group-1")
            return intervals

        legacy = AstrMaiConfig()
        legacy.evolution.learning_mining_interval_sec = None
        legacy.evolution.mining_interval_sec = 1800
        canonical = AstrMaiConfig()
        canonical.evolution.mining_interval_sec = 1800
        canonical.evolution.learning_mining_interval_sec = 2400

        self.assertEqual(await claimed_interval(legacy), [1800.0])
        self.assertEqual(await claimed_interval(canonical), [2400.0])

    async def test_mining_pipelines_share_one_stable_run_id(self):
        config = AstrMaiConfig()
        store = _MetaStore()
        manager = EvolutionManager(
            _LearningDb(store), SimpleNamespace(config=config), config=config
        )
        logs = [SimpleNamespace(id=11), SimpleNamespace(id=12)]
        recorded = []

        async def record_pipeline(payload):
            recorded.append(dict(payload))

        manager._record_pipeline_run = record_pipeline
        run_id = manager._mining_run_id("group-1", logs)
        async def record_run(payload):
            recorded.append(dict(payload))
        manager._record_pipeline_run = record_run

        for pipeline in ("expression", "jargon"):
            await manager._record_pipeline_state(
                run_id=run_id,
                pipeline=pipeline,
                group_id="group-1",
                logs=logs,
                batch_id=f"{pipeline}:batch",
                status="completed",
                reason="completed",
                cursor_before=0,
                cursor_after=12,
                retained_count=2,
                report={},
            )

        self.assertEqual(len(recorded), 2)
        self.assertEqual({item["mining_run_id"] for item in recorded}, {run_id})
        self.assertEqual(
            {item["run_id"] for item in recorded},
            {f"{run_id}:expression", f"{run_id}:jargon"},
        )

    async def test_learning_ingress_duplicate_event_is_idempotent(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_learning_ingest_schema(db_path)
            store = LearningIngressOutboxStore(db_path)
            envelope = LearningMessageEnvelope(
                event_id="evt-duplicate",
                chat_id="group-1",
                sender_id="u1",
                sender_name="Alice",
                content="hello",
            )
            self.assertTrue(await store.enqueue(envelope))
            self.assertFalse(await store.enqueue(envelope))
            entries = await store.claim_due(limit=10)
            self.assertEqual([item.event_id for item in entries], ["evt-duplicate"])

    async def test_background_ledger_recovery_marks_expired_rows_stale(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            _create_background_ledger_schema(db_path)
            ledger = BackgroundTaskLedger(db_path)
            lease = await ledger.claim(
                task_family="learning.backlog",
                scope_id="GLOBAL",
                input_fingerprint="batch-a",
                lease_seconds=60.0,
            )
            self.assertIsNotNone(lease)
            with sqlite3.connect(db_path) as db:
                db.execute(
                    "UPDATE background_task_ledger SET lease_until=? WHERE task_id=?",
                    (time.time() - 1.0, lease.task_id),
                )
                db.commit()
            self.assertEqual(await ledger.recover_expired_leases(), 1)
            rows = await ledger.list_recent(task_family="learning.backlog")
            self.assertEqual(rows[0]["status"], "stale")
            self.assertEqual(rows[0]["last_error"], "lease_expired")
            await asyncio.sleep(0.05)

    async def test_learning_and_profile_json_contracts_accept_naked_members(self):
        profile = parse_json_payload(
            '"tags": ["夜猫子"], "summary": "喜欢深夜聊天", "memory_points": [],',
            allowed_keys=("tags", "summary", "memory_points"),
            allow_naked_members=True,
        )
        self.assertEqual(profile.value["tags"], ["夜猫子"])
        review = parse_json_payload(
            '"decision": "approved", "reason": "证据充分",',
            allowed_keys=("decision", "reason"),
            allow_naked_members=True,
        )
        self.assertEqual(review.value["decision"], "approved")

    async def test_main_attention_does_not_wait_for_learning_recorder(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def record_user_message(_event):
            started.set()
            await release.wait()

        class _Manager:
            def __init__(self):
                self.tasks = set()

            def track_task(self, awaitable):
                task = asyncio.create_task(awaitable)
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
                return task

        class _Event:
            def __init__(self):
                self.extras = {}

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

            def set_extra(self, key, value):
                self.extras[key] = value

        runtime = SimpleNamespace(
            evolution=SimpleNamespace(record_user_message=record_user_message),
            lifecycle=SimpleNamespace(manager=_Manager()),
            chat_loop_kernel=SimpleNamespace(
                tick=lambda **_kwargs: asyncio.sleep(
                    0, result=SimpleNamespace(dispatch_result="BUFFERED")
                )
            ),
        )
        facade = object.__new__(PluginFacade)
        facade.runtime = runtime
        scope = SimpleNamespace(chat_id="group-1", is_anonymous_sender=False)

        result = await facade.record_and_dispatch_attention(_Event(), scope)

        self.assertEqual(result, "BUFFERED")
        await asyncio.wait_for(started.wait(), timeout=1.0)
        self.assertTrue(runtime.lifecycle.manager.tasks)
        release.set()
        await asyncio.gather(*list(runtime.lifecycle.manager.tasks), return_exceptions=True)

    async def test_main_attention_without_lifecycle_manager_still_detaches_learning(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def record_user_message(_envelope):
            started.set()
            await release.wait()

        class _Event:
            def __init__(self):
                self.extras = {}
                self.message_str = "hello"
                self.unified_msg_origin = "group-1"
                self.message_obj = SimpleNamespace(message_id="event-1")

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

            def set_extra(self, key, value):
                self.extras[key] = value

            def get_sender_id(self):
                return "user-1"

            def get_sender_name(self):
                return "Alice"

        runtime = SimpleNamespace(
            evolution=SimpleNamespace(record_user_message=record_user_message),
            lifecycle=SimpleNamespace(manager=None),
            chat_loop_kernel=SimpleNamespace(
                tick=lambda **_kwargs: asyncio.sleep(
                    0, result=SimpleNamespace(dispatch_result="BUFFERED")
                )
            ),
        )
        facade = object.__new__(PluginFacade)
        facade.runtime = runtime
        event = _Event()

        result = await asyncio.wait_for(
            facade.record_and_dispatch_attention(
                event,
                SimpleNamespace(chat_id="group-1", is_anonymous_sender=False),
            ),
            timeout=0.5,
        )

        self.assertEqual(result, "BUFFERED")
        await asyncio.wait_for(started.wait(), timeout=1.0)
        self.assertTrue(event.get_extra("astrmai_evolution_record_pending"))
        release.set()
        for _ in range(10):
            if not event.get_extra("astrmai_evolution_record_pending"):
                break
            await asyncio.sleep(0)
        self.assertTrue(event.get_extra("astrmai_evolution_recorded"))

    async def test_learning_enqueue_failure_is_explicitly_degraded_without_inline_fallback(self):
        class _Manager:
            def __init__(self):
                self.tasks = []

            def track_task(self, awaitable):
                task = asyncio.create_task(awaitable)
                self.tasks.append(task)
                return task

        class _Event:
            def __init__(self):
                self.extras = {}
                self.message_str = "hello"
                self.unified_msg_origin = "group-1"
                self.message_obj = SimpleNamespace(message_id="event-fail")

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

            def set_extra(self, key, value):
                self.extras[key] = value

            def get_sender_id(self):
                return "user-1"

            def get_sender_name(self):
                return "Alice"

        inline_called = []

        class _Evolution:
            async def enqueue_user_message(self, _envelope):
                inline_called.append("enqueue")
                return False

            async def record_user_message(self, _envelope):
                inline_called.append("inline")
                return {"recorded": True}

        manager = _Manager()
        runtime = SimpleNamespace(
            evolution=_Evolution(),
            lifecycle=SimpleNamespace(manager=manager),
            chat_loop_kernel=SimpleNamespace(
                tick=lambda **_kwargs: asyncio.sleep(
                    0, result=SimpleNamespace(dispatch_result="BUFFERED")
                )
            ),
        )
        facade = object.__new__(PluginFacade)
        facade.runtime = runtime
        event = _Event()

        self.assertEqual(
            await facade.record_and_dispatch_attention(
                event,
                SimpleNamespace(chat_id="group-1", is_anonymous_sender=False),
            ),
            "BUFFERED",
        )
        await asyncio.gather(*manager.tasks, return_exceptions=True)
        self.assertEqual(inline_called, ["enqueue"])
        self.assertEqual(event.get_extra("astrmai_evolution_record_failed"), "outbox_unavailable")
        self.assertEqual(event.get_extra("astrmai_evolution_record_source"), "degraded")
        self.assertFalse(event.get_extra("astrmai_evolution_recorded", False))

    async def test_learning_ingress_survives_lifecycle_scheduler_failure(self):
        class _Manager:
            def track_task(self, _awaitable):
                raise RuntimeError("manager is draining")

        class _Event:
            def __init__(self):
                self.extras = {}
                self.message_str = "hello"
                self.unified_msg_origin = "group-1"
                self.message_obj = SimpleNamespace(message_id="event-fallback")

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

            def set_extra(self, key, value):
                self.extras[key] = value

            def get_sender_id(self):
                return "user-1"

            def get_sender_name(self):
                return "Alice"

        calls = []

        class _Evolution:
            async def enqueue_user_message(self, envelope):
                calls.append(envelope.event_id)
                return True

            async def record_user_message(self, _envelope):
                raise AssertionError("inline learning must never be used")

        runtime = SimpleNamespace(
            evolution=_Evolution(),
            lifecycle=SimpleNamespace(manager=_Manager()),
            chat_loop_kernel=SimpleNamespace(
                tick=lambda **_kwargs: asyncio.sleep(
                    0, result=SimpleNamespace(dispatch_result="BUFFERED")
                )
            ),
        )
        facade = object.__new__(PluginFacade)
        facade.runtime = runtime
        event = _Event()

        self.assertEqual(
            await facade.record_and_dispatch_attention(
                event,
                SimpleNamespace(chat_id="group-1", is_anonymous_sender=False),
            ),
            "BUFFERED",
        )
        for _ in range(20):
            if calls:
                break
            await asyncio.sleep(0)
        self.assertEqual(calls, ["event-fallback"])
        self.assertTrue(event.get_extra("astrmai_evolution_recorded"))
        self.assertEqual(event.get_extra("astrmai_evolution_record_source"), "outbox")

    def test_default_background_intervals(self):
        config = AstrMaiConfig()
        self.assertEqual(config.evolution.mining_interval_sec, 21600)
        self.assertEqual(config.evolution.review_runner_interval_sec, 21600)
        self.assertEqual(config.evolution.review_runner_min_interval_sec, 21600)
        self.assertEqual(config.memory.long_term_memory_cooldown_sec, 7200)
        self.assertEqual(config.life.dream_interval_min, 720)
        self.assertEqual(config.life.profile_scan_interval_sec, 7200)
        self.assertEqual(config.life.proactive_scan_interval_sec, 3600)


def test_background_ledger_reclaims_expired_scope_as_stale(tmp_path):
    db_path = tmp_path / "background-ledger.db"
    _create_background_ledger_schema(db_path)

    async def run():
        ledger = BackgroundTaskLedger(db_path)
        lease = await ledger.claim(
            task_family="memory.maintenance",
            scope_id="chat-1",
            input_fingerprint="batch-a",
            lease_seconds=60.0,
        )
        assert lease is not None
        with sqlite3.connect(db_path) as db:
            db.execute(
                "UPDATE background_task_ledger SET lease_until=? WHERE task_id=?",
                (time.time() - 1.0, lease.task_id),
            )
            db.commit()
        replacement = await ledger.claim(
            task_family="memory.maintenance",
            scope_id="chat-1",
            input_fingerprint="batch-b",
            lease_seconds=60.0,
        )
        assert replacement is not None
        with sqlite3.connect(db_path) as db:
            stale = db.execute(
                "SELECT status, last_error FROM background_task_ledger WHERE task_id=?",
                (lease.task_id,),
            ).fetchone()
        return stale

    assert asyncio.run(run()) == ("stale", "lease_expired")


if __name__ == "__main__":
    unittest.main()
