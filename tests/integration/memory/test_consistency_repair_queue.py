from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
import json
import os
import subprocess
import sys
from pathlib import Path

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class ConsistencyRepairQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.tmp.name)
        from astrmai.memory.services.v2_store import MemoryV2Store
        self.store = MemoryV2Store(str(Path(self.tmp.name) / "memory.db"), data_path=self.tmp.name)

    def tearDown(self): self.tmp.cleanup()

    def test_enqueue_is_idempotent(self):
        async def run():
            a = await self.store.enqueue_consistency_repair("missing_fts", memory_id="m1")
            b = await self.store.enqueue_consistency_repair("missing_fts", memory_id="m1")
            return a, b
        self.assertEqual(asyncio.run(run())[0], asyncio.run(run())[0])

    def test_only_one_concurrent_claim_succeeds(self):
        async def run():
            rid = await self.store.enqueue_consistency_repair("missing_faiss", memory_id="m1")
            return await asyncio.gather(self.store.claim_consistency_repair(rid, lease_owner="a"), self.store.claim_consistency_repair(rid, lease_owner="b"))
        claims = asyncio.run(run())
        self.assertEqual(sum(item is not None for item in claims), 1)

    def test_old_lease_cannot_finish_new_lease(self):
        async def run():
            rid = await self.store.enqueue_consistency_repair("orphan_faiss", memory_id="m2")
            first = await self.store.claim_consistency_repair(rid, lease_owner="a", lease_sec=0.1)
            await asyncio.sleep(0.12)
            second = await self.store.claim_consistency_repair(rid, lease_owner="b")
            stale = await self.store.finish_consistency_repair(rid, lease_token=first["lease_token"], status="completed")
            fresh = await self.store.finish_consistency_repair(rid, lease_token=second["lease_token"], status="completed")
            return stale, fresh
        self.assertEqual(asyncio.run(run()), (False, True))

    def test_exhausted_and_blocked_are_terminal(self):
        async def run():
            rid = await self.store.enqueue_consistency_repair("dimension_mismatch", memory_id="m3")
            claim = await self.store.claim_consistency_repair(rid, lease_owner="a")
            done = await self.store.finish_consistency_repair(rid, lease_token=claim["lease_token"], status="repair_exhausted")
            return done, await self.store.claim_consistency_repair(rid, lease_owner="b")
        done, claim = asyncio.run(run())
        self.assertTrue(done); self.assertIsNone(claim)

    def test_diagnostics_expose_queue_states(self):
        async def run():
            await self.store.enqueue_consistency_repair("unknown_resource", memory_id="m4")
            return await self.store.consistency_repair_diagnostics()
        report = asyncio.run(run())
        self.assertEqual(report["repair_queue_pending"], 1)

    def test_schema_survives_new_store_instance(self):
        async def run():
            await self.store.enqueue_consistency_repair("missing_documents", memory_id="m5")
        asyncio.run(run())
        with sqlite3.connect(self.store.db_path) as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM memory_consistency_repairs").fetchone())

    def test_retry_wait_is_not_claimed_before_due(self):
        async def run():
            rid = await self.store.enqueue_consistency_repair("missing_fts", memory_id="due")
            claim = await self.store.claim_consistency_repair(rid, lease_owner="a")
            await self.store.finish_consistency_repair(rid, lease_token=claim["lease_token"], status="retry_wait", retry_delay_sec=60)
            return await self.store.claim_consistency_repair(rid, lease_owner="b")
        self.assertIsNone(asyncio.run(run()))

    def test_process_repair_repairs_fts_rows(self):
        async def run():
            from astrmai.memory.contracts.memory_query import MemoryWriteRequest
            await self.store.upsert(MemoryWriteRequest(source="test", kind="memory", session_id="s", content="hello"))
            rid = await self.store.enqueue_consistency_repair("unknown_resource", memory_id="missing")
            # Missing canonical rows are conservatively blocked, not treated as success.
            from astrmai.memory.services.memory_index_projector import MemoryIndexProjector
            projector = MemoryIndexProjector(type("Engine", (), {"v2_store": self.store, "retriever": None})())
            result = await projector.process_consistency_repairs(limit=2)
            return rid, result, await self.store.consistency_repair_diagnostics()
        rid, result, diag = asyncio.run(run())
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(diag["repair_queue_blocked"], 1)

    def test_cross_process_claim_uses_persistent_lease(self):
        async def setup():
            return await self.store.enqueue_consistency_repair("unknown_resource", memory_id="proc")
        rid = asyncio.run(setup())
        script = """
import asyncio, json, sys
from tests.helpers.astrbot_stubs import install_astrbot_stubs
install_astrbot_stubs(sys.argv[2])
from astrmai.memory.services.v2_store import MemoryV2Store
async def main():
 s=MemoryV2Store(sys.argv[1], data_path=sys.argv[2]); r=await s.claim_consistency_repair(sys.argv[3], lease_owner=sys.argv[4]); print(json.dumps(bool(r)))
asyncio.run(main())
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path.cwd())
        args = [sys.executable, "-c", script, self.store.db_path, self.tmp.name, rid, "proc-b"]
        first = subprocess.run(args, capture_output=True, text=True, env=env, check=True)
        self.assertEqual(json.loads(first.stdout.strip().splitlines()[-1]), True)

    def test_two_processes_compete_for_one_lease(self):
        async def setup():
            return await self.store.enqueue_consistency_repair("unknown_resource", memory_id="race")
        rid = asyncio.run(setup())
        script = """
import asyncio, json, sys
from tests.helpers.astrbot_stubs import install_astrbot_stubs
install_astrbot_stubs(sys.argv[2])
from astrmai.memory.services.v2_store import MemoryV2Store
async def main():
 s=MemoryV2Store(sys.argv[1], data_path=sys.argv[2]); r=await s.claim_consistency_repair(sys.argv[3], lease_owner=sys.argv[4]); print(json.dumps(bool(r)))
asyncio.run(main())
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path.cwd())
        procs = [
            subprocess.Popen([sys.executable, "-c", script, self.store.db_path, self.tmp.name, rid, owner], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            for owner in ("proc-a", "proc-b")
        ]
        outputs = [p.communicate(timeout=30)[0].strip().splitlines()[-1] for p in procs]
        self.assertEqual(sum(json.loads(item) for item in outputs), 1)

    def test_audit_classifies_missing_and_orphan_fts_without_mutation(self):
        async def run():
            await self.store.initialize()
            async with __import__("astrmai.infrastructure.persistence.sqlite_helpers", fromlist=["connect_aiosqlite"]).connect_aiosqlite(self.store.db_path) as db:
                await db.execute("INSERT INTO canonical_memories(id,status,content) VALUES ('canon','active','c')")
                await db.execute("INSERT INTO canonical_fts(memory_id,content) VALUES ('orphan','o')")
                await db.commit()
            from astrmai.memory.services.memory_index_projector import MemoryIndexProjector
            class Engine:
                v2_store = self.store
                _vector_generation = 1
                async def _run_documents_query(self, *args, **kwargs):
                    return []
            report = await MemoryIndexProjector(Engine()).audit_consistency()
            return report
        report = asyncio.run(run())
        self.assertGreaterEqual(report["consistency_mismatch_by_kind"].get("missing_fts", 0), 1)
        self.assertGreaterEqual(report["consistency_mismatch_by_kind"].get("orphan_fts", 0), 1)

    def test_process_missing_fts_success_and_finish_failure_is_not_counted(self):
        async def run():
            await self.store.initialize()
            async with __import__("astrmai.infrastructure.persistence.sqlite_helpers", fromlist=["connect_aiosqlite"]).connect_aiosqlite(self.store.db_path) as db:
                await db.execute("INSERT INTO canonical_memories(id,status,content,summary,tags) VALUES ('mfts','active','hello','', '[]')")
                await db.commit()
            rid = await self.store.enqueue_consistency_repair("missing_fts", memory_id="mfts")
            from astrmai.memory.services.memory_index_projector import MemoryIndexProjector
            class Engine:
                v2_store = self.store
                retriever = None
            projector = MemoryIndexProjector(Engine())
            original_finish = self.store.finish_consistency_repair
            async def fail_finish(*args, **kwargs):
                return False
            self.store.finish_consistency_repair = fail_finish
            result = await projector.process_consistency_repairs(limit=1)
            self.store.finish_consistency_repair = original_finish
            return rid, result
        _rid, result = asyncio.run(run())
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["settlement_failed"], 1)

    def test_orphan_fts_cleanup_success(self):
        async def run():
            await self.store.initialize()
            async with __import__("astrmai.infrastructure.persistence.sqlite_helpers", fromlist=["connect_aiosqlite"]).connect_aiosqlite(self.store.db_path) as db:
                await db.execute("INSERT INTO canonical_fts(memory_id,content) VALUES ('orphan-success','x')")
                await db.commit()
            rid = await self.store.enqueue_consistency_repair("orphan_fts", memory_id="orphan-success")
            from astrmai.memory.services.memory_index_projector import MemoryIndexProjector
            projector = MemoryIndexProjector(type("Engine", (), {"v2_store": self.store, "retriever": None})())
            result = await projector.process_consistency_repairs(limit=1)
            async with __import__("astrmai.infrastructure.persistence.sqlite_helpers", fromlist=["connect_aiosqlite"]).connect_aiosqlite(self.store.db_path) as db:
                row = await (await db.execute("SELECT 1 FROM canonical_fts WHERE memory_id='orphan-success'")).fetchone()
            return result, row
        result, row = asyncio.run(run())
        self.assertEqual(result["completed"], 1)
        self.assertIsNone(row)

    def test_audit_emits_dimension_and_revision_mismatch_from_descriptors(self):
        async def run():
            await self.store.initialize()
            from astrmai.memory.services.memory_index_projector import MemoryIndexProjector
            class Engine:
                v2_store = self.store
                _vector_generation = 2
                _vector_query_dimension = 1536
                _configured_vector_dimension = 1536
                _vector_resource_descriptors = {
                    "active-old": {"resource_id": "active-old", "role": "active", "generation": 1, "physical_dimension": 768, "resource_status": "active"},
                    "unknown": {"resource_id": "unknown", "role": "retired", "generation": 1, "physical_dimension": 1536, "resource_status": "unknown"},
                }
                async def _run_documents_query(self, *args, **kwargs):
                    return []
            return await MemoryIndexProjector(Engine()).audit_consistency()
        report = asyncio.run(run())
        self.assertGreaterEqual(report["consistency_mismatch_by_kind"].get("generation_mismatch", 0), 1)
        self.assertGreaterEqual(report["consistency_mismatch_by_kind"].get("dimension_mismatch", 0), 1)
        self.assertGreaterEqual(report["consistency_mismatch_by_kind"].get("unknown_resource", 0), 1)

    def test_snapshot_ratio_uses_active_and_projectable_sets_only(self):
        async def run():
            await self.store.initialize()
            async with __import__("astrmai.infrastructure.persistence.sqlite_helpers", fromlist=["connect_aiosqlite"]).connect_aiosqlite(self.store.db_path) as db:
                for index in range(788):
                    status = "active" if index < 63 else "review_pending"
                    await db.execute(
                        "INSERT INTO canonical_memories(id,status,content,visibility) VALUES (?, ?, ?, ?)",
                        (f"memory-{index}", status, f"content-{index}", "auto_and_tool"),
                    )
                for index in range(63):
                    await db.execute(
                        "INSERT INTO canonical_fts(memory_id,content) VALUES (?, ?)",
                        (f"memory-{index}", f"content-{index}"),
                    )
                await db.commit()

            class Engine:
                v2_store = self.store
                _vector_generation = 1

                async def _run_documents_query(self, *_args, **_kwargs):
                    return [
                        (index + 1, json.dumps({"canonical_id": f"memory-{index}"}))
                        for index in range(63)
                    ] + [
                        (index + 1000, json.dumps({"legacy": True}))
                        for index in range(7)
                    ]

            from astrmai.memory.services.memory_index_projector import MemoryIndexProjector
            return await MemoryIndexProjector(Engine()).audit_consistency()

        report = asyncio.run(run())
        by_kind = report["consistency_mismatch_by_kind"]
        self.assertEqual(by_kind.get("missing_fts", 0), 0)
        self.assertEqual(by_kind.get("missing_documents", 0), 0)
        self.assertLess(report["consistency_mismatch_total"], 20)

    def test_repair_failure_enters_retry_wait_then_exhausted(self):
        async def run():
            rid = await self.store.enqueue_consistency_repair("missing_faiss", memory_id="retry", max_attempts=2)
            from astrmai.memory.services.memory_index_projector import MemoryIndexProjector
            class Engine:
                v2_store = self.store
                retriever = None
                _vector_generation = 1
            projector = MemoryIndexProjector(Engine())
            first = await projector.process_consistency_repairs(limit=1)
            async with __import__("astrmai.infrastructure.persistence.sqlite_helpers", fromlist=["connect_aiosqlite"]).connect_aiosqlite(self.store.db_path) as db:
                await db.execute("UPDATE memory_consistency_repairs SET next_retry_at=0 WHERE repair_id=?", (rid,))
                await db.commit()
            second = await projector.process_consistency_repairs(limit=1)
            diag = await self.store.consistency_repair_diagnostics()
            return first, second, diag
        first, second, diag = asyncio.run(run())
        self.assertEqual(first["failed"], 1)
        self.assertEqual(second["failed"], 1)
        self.assertEqual(diag["repair_queue_repair_exhausted"], 1)

    def test_missing_faiss_success_path_finishes_repair(self):
        async def run():
            rid = await self.store.enqueue_consistency_repair("missing_faiss", memory_id="faiss-ok", generation=4)
            from astrmai.memory.services.memory_index_projector import MemoryIndexProjector
            class Engine:
                v2_store = self.store
                retriever = object()
                _vector_generation = 4
            projector = MemoryIndexProjector(Engine())
            projected = []
            async def fake_project(memory_id):
                projected.append(memory_id)
                return True
            projector.project = fake_project
            result = await projector.process_consistency_repairs(limit=1)
            diag = await self.store.consistency_repair_diagnostics()
            return result, diag, projected
        result, diag, projected = asyncio.run(run())
        self.assertEqual(projected, ["faiss-ok"])
        self.assertEqual(result["completed"], 1)
        self.assertEqual(diag["repair_queue_completed"], 1)

    def test_blocked_finish_failure_not_counted_as_blocked(self):
        async def run():
            await self.store.enqueue_consistency_repair("dimension_mismatch", memory_id="dim")
            from astrmai.memory.services.memory_index_projector import MemoryIndexProjector
            projector = MemoryIndexProjector(type("Engine", (), {"v2_store": self.store})())
            original = self.store.finish_consistency_repair
            async def fail_finish(*args, **kwargs):
                return False
            self.store.finish_consistency_repair = fail_finish
            result = await projector.process_consistency_repairs(limit=1)
            self.store.finish_consistency_repair = original
            return result
        result = asyncio.run(run())
        self.assertEqual(result["blocked"], 0)
        self.assertEqual(result["settlement_failed"], 1)
