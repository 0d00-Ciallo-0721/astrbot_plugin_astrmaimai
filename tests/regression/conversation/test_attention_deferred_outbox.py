from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from astrmai.infrastructure.persistence.attention_deferred_outbox import (
    AttentionDeferredOutboxStore,
)


class AttentionDeferredOutboxTests(unittest.TestCase):
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
                }
                self.assertTrue(await store.enqueue(item, event_data={"message_str": "hello"}))

                rows = await store.claim_due(include_future=True)
                self.assertEqual(len(rows), 1)
                token = rows[0]["lease_token"]
                self.assertEqual(rows[0]["event_data"]["message_str"], "hello")

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
                    )
                )
                description = await store.describe()
                self.assertEqual(description["queued"], 1)

                retry_rows = await store.claim_due(include_future=True)
                self.assertEqual(len(retry_rows), 1)
                self.assertNotEqual(retry_rows[0]["lease_token"], token)
                self.assertEqual(retry_rows[0]["attempts"], 1)
                self.assertEqual(retry_rows[0]["event_data"]["message_str"], "updated")

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
