from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class MemoryProcessRestartRecoveryTests(unittest.TestCase):
    def _run_worker(self, script: str, db_path: str, data_path: str) -> dict:
        project_root = Path(__file__).resolve().parents[3]
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(project_root), existing_pythonpath) if part
        )
        completed = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(script), db_path, data_path],
            cwd=project_root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_projection_outbox_recovers_across_real_process_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "process-restart.db")
            worker_a = r'''
                import asyncio
                import json
                import sys

                from tests.helpers.astrbot_stubs import install_astrbot_stubs

                db_path, data_path = sys.argv[1:3]
                install_astrbot_stubs(data_path)
                from astrmai.memory.services.v2_store import MemoryV2Store

                async def main():
                    store = MemoryV2Store(db_path, data_path=data_path)
                    pending_ok = await store.schedule_projection_retry(
                        "process-pending",
                        "projection_error:TimeoutError",
                        base_delay_sec=1,
                        max_delay_sec=1,
                    )
                    exhausted_first = await store.schedule_projection_retry(
                        "process-exhausted",
                        "vector_delete_unavailable",
                        base_delay_sec=1,
                        max_delay_sec=1,
                        max_attempts=1,
                    )
                    exhausted_second = await store.schedule_projection_retry(
                        "process-exhausted",
                        "vector_delete_unavailable",
                        base_delay_sec=1,
                        max_delay_sec=1,
                        max_attempts=1,
                    )
                    print(json.dumps({
                        "pending_ok": pending_ok,
                        "exhausted_first": exhausted_first,
                        "exhausted_second": exhausted_second,
                    }))

                asyncio.run(main())
            '''
            first = self._run_worker(worker_a, db_path, temp_dir)
            self.assertEqual(
                first,
                {
                    "pending_ok": True,
                    "exhausted_first": True,
                    "exhausted_second": False,
                },
            )

            worker_b = r'''
                import asyncio
                import json
                import sys

                import aiosqlite
                from tests.helpers.astrbot_stubs import install_astrbot_stubs

                db_path, data_path = sys.argv[1:3]
                install_astrbot_stubs(data_path)
                from astrmai.memory.services.v2_store import MemoryV2Store

                async def main():
                    store = MemoryV2Store(db_path, data_path=data_path)
                    async with aiosqlite.connect(db_path) as db:
                        await db.execute(
                            "UPDATE memory_projection_outbox SET next_retry_at = 0 WHERE memory_id = ?",
                            ("process-pending",),
                        )
                        await db.commit()
                    snapshot = await store.projection_retry_snapshot()
                    status = await store.projection_retry_status("process-exhausted")
                    due = await store.list_due_projection_retries(limit=10)
                    revision = await store.projection_retry_revision("process-pending")
                    removed = await store.complete_projection_retry_if_unchanged(
                        "process-pending", revision
                    )
                    exhausted_retry = await store.schedule_projection_retry(
                        "process-exhausted", "later-retry", max_attempts=5
                    )
                    print(json.dumps({
                        "snapshot": snapshot,
                        "status": status,
                        "due_ids": [item["memory_id"] for item in due],
                        "removed": removed,
                        "exhausted_retry": exhausted_retry,
                        "remaining": await store.projection_retry_snapshot(),
                    }))

                asyncio.run(main())
            '''
            second = self._run_worker(worker_b, db_path, temp_dir)
            self.assertEqual(
                second["snapshot"],
                {"process-pending": "projection_error:TimeoutError"},
            )
            self.assertEqual(second["status"], "repair_exhausted")
            self.assertEqual(second["due_ids"], ["process-pending"])
            self.assertTrue(second["removed"])
            self.assertFalse(second["exhausted_retry"])
            self.assertEqual(second["remaining"], {})


if __name__ == "__main__":
    unittest.main()
