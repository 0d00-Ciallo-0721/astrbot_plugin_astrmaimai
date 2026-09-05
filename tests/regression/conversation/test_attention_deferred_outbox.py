from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from astrmai.infrastructure.persistence.attention_deferred_outbox import (
    AttentionDeferredOutboxStore,
)


class AttentionDeferredOutboxTests(unittest.TestCase):
    def test_legacy_outbox_schema_gains_diagnostics_and_revision_columns(self):
        async def run():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
                db_path = Path(temp_dir) / "legacy.db"
                with sqlite3.connect(db_path) as db:
                    db.execute(
                        "CREATE TABLE attention_deferred_outbox ("
                        "work_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, task_name TEXT NOT NULL, "
                        "reason TEXT NOT NULL, event_json TEXT NOT NULL DEFAULT '{}', "
                        "turn_thread_id TEXT NOT NULL DEFAULT '', turn_generation INTEGER NOT NULL DEFAULT 0, "
                        "worker_generation INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, "
                        "max_attempts INTEGER NOT NULL DEFAULT 3, next_retry_at REAL NOT NULL DEFAULT 0, "
                        "expires_at REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'queued', "
                        "lease_token TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0, "
                        "created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0, "
                        "last_error TEXT NOT NULL DEFAULT ''"
                        ")"
                    )
                    db.commit()
                store = AttentionDeferredOutboxStore(db_path)
                now = time.time()
                self.assertTrue(
                    await store.enqueue(
                        {
                            "work_id": "legacy-1",
                            "chat_id": "chat-1",
                            "task_name": "attention.system2",
                            "reason": "legacy",
                            "next_retry_at_wall": now,
                            "expires_at": now + 30.0,
                            "revision": 1,
                            "diagnostics": {"legacy": True},
                        },
                        event_data={"message_str": "legacy"},
                    )
                )
                rows = await store.claim_due()
                self.assertEqual(rows[0]["revision"], 1)
                self.assertTrue(rows[0]["diagnostics"]["legacy"])
                # Allow the aiosqlite worker thread to finish closing before
                # Windows removes the temporary database file.
                await asyncio.sleep(0.05)

        asyncio.run(run())

    def test_revision_prevents_stale_attempt_overwrite(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                store = AttentionDeferredOutboxStore(Path(temp_dir) / "state.db")
                now = time.time()
                self.assertTrue(
                    await store.enqueue(
                        {
                            "work_id": "revision-1",
                            "chat_id": "chat-1",
                            "task_name": "attention.system2",
                            "reason": "new-attempt",
                            "attempts": 2,
                            "next_retry_at_wall": now + 120.0,
                            "expires_at": now + 300.0,
                            "revision": 2,
                            "diagnostics": {"marker": "new"},
                        },
                        event_data={"message_str": "new"},
                    )
                )
                self.assertTrue(
                    await store.enqueue(
                        {
                            "work_id": "revision-1",
                            "chat_id": "chat-1",
                            "task_name": "attention.system2",
                            "reason": "stale-attempt",
                            "attempts": 1,
                            "next_retry_at_wall": now,
                            "expires_at": now + 300.0,
                            "revision": 1,
                            "diagnostics": {"marker": "stale"},
                        },
                        event_data={"message_str": "stale"},
                    )
                )
                rows = await store.claim_due()
                self.assertEqual(len(rows), 0)
                rows = await store.claim_due(include_future=True)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["attempts"], 2)
                self.assertEqual(rows[0]["event_data"]["message_str"], "new")
                self.assertEqual(rows[0]["diagnostics"]["marker"], "new")
                self.assertEqual(rows[0]["revision"], 2)

        asyncio.run(run())

    def test_outbox_preserves_claim_lease_and_retries_then_deletes_terminal(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                store = AttentionDeferredOutboxStore(Path(temp_dir) / "state.db")
                now = time.time()
                item = {
                    "work_id": "attention-deferred-1",
                    "chat_id": "chat-1",
                    "task_name": "attention.system2",
                    "reason": "queue_timeout",
                    "turn_thread_id": "thread-1",
                    "turn_generation": 2,
                    "worker_generation": 3,
                    "attempts": 0,
                    "max_attempts": 3,
                    "next_retry_at_wall": now + 60.0,
                    "expires_at": now + 300.0,
                    "diagnostics": {
                        "last_failure_stage": "local_slot_wait",
                        "last_failure_kind": "local_slot_wait_timeout",
                        "original_turn_budget_remaining_ms": 0.0,
                    },
                }
                self.assertTrue(await store.enqueue(item, event_data={"message_str": "hello"}))

                rows = await store.claim_due(include_future=True)
                self.assertEqual(len(rows), 1)
                token = rows[0]["lease_token"]
                self.assertEqual(rows[0]["event_data"]["message_str"], "hello")
                self.assertEqual(rows[0]["diagnostics"]["last_failure_stage"], "local_slot_wait")

                item["attempts"] = 1
                item["next_retry_at_wall"] = now + 120.0
                self.assertTrue(await store.enqueue(item, event_data={"message_str": "updated"}))
                self.assertEqual(await store.claim_due(include_future=True), [])

                self.assertTrue(
                    await store.finish(
                        item["work_id"],
                        lease_token=token,
                        status="retry_wait",
                        attempts=1,
                        next_retry_at=now + 120.0,
                        error="still busy",
                        diagnostics={"last_failure_kind": "background_queue_wait"},
                    )
                )
                description = await store.describe()
                self.assertEqual(description["queued"], 1)

                retry_rows = await store.claim_due(include_future=True)
                self.assertEqual(len(retry_rows), 1)
                self.assertNotEqual(retry_rows[0]["lease_token"], token)
                self.assertEqual(retry_rows[0]["attempts"], 1)
                self.assertEqual(retry_rows[0]["event_data"]["message_str"], "updated")
                self.assertEqual(
                    retry_rows[0]["diagnostics"]["last_failure_kind"],
                    "background_queue_wait",
                )

                self.assertTrue(
                    await store.finish(
                        item["work_id"],
                        lease_token=retry_rows[0]["lease_token"],
                        status="replayed",
                        attempts=2,
                    )
                )
                final = await store.describe()
                self.assertEqual(final["total"], 0)

        asyncio.run(run())

    def test_expired_inflight_lease_is_requeued_for_recovery(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                store = AttentionDeferredOutboxStore(Path(temp_dir) / "state.db")
                item = {
                    "work_id": "attention-deferred-expired",
                    "chat_id": "chat-1",
                    "task_name": "attention.system2",
                    "reason": "queue_full",
                    "next_retry_at_wall": time.time(),
                    "expires_at": time.time() + 300.0,
                }
                await store.enqueue(item, event_data={"message_str": "recover"})
                claimed = await store.claim_due(lease_seconds=1.0)
                self.assertEqual(len(claimed), 1)
                await asyncio.sleep(1.05)
                recovered = await store.claim_due()
                self.assertEqual(len(recovered), 1)
                self.assertEqual(recovered[0]["work_id"], item["work_id"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
