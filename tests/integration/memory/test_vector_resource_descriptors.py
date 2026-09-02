from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class VectorResourceDescriptorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.tmp.name)
        import importlib
        for name in list(sys.modules):
            if name.startswith("astrmai.memory."):
                sys.modules.pop(name, None)
        self.mod = importlib.import_module("astrmai.memory.services.memory_engine")

    def tearDown(self): self.tmp.cleanup()

    def engine(self):
        cfg = SimpleNamespace(provider=SimpleNamespace(embedding_models=["embedding-v2"]), memory=SimpleNamespace(recall_top_k=5))
        e = self.mod.MemoryEngine(SimpleNamespace(), SimpleNamespace(config=cfg), config=cfg)
        e.data_path = Path(self.tmp.name); e.v2_db_path = str(Path(self.tmp.name) / "memory_v2.db")
        return e

    def test_round_trip_restart(self):
        e = self.engine(); p = Path(self.tmp.name) / "a.index"; d = e._record_vector_resource_descriptor(p, role="active", generation=1, resource_status="active"); e._persist_vector_resource_descriptor_sync(d); e2 = self.engine(); asyncio.run(e2._load_vector_resource_descriptors()); self.assertEqual(next(iter(e2._vector_resource_descriptors.values()))["role"], "active")

    def test_active_protected(self):
        e = self.engine(); p = Path(self.tmp.name) / "a.index"; p.write_bytes(b"x"); e._record_vector_resource_descriptor(p, role="active", generation=1, resource_status="active"); e._scan_vector_resources_sync(); self.assertEqual(e._vector_orphan_indexes[str(p.resolve())]["status"], "protected")

    def test_unknown_not_deleted(self):
        e = self.engine(); p = Path(self.tmp.name) / "u.index"; p.write_bytes(b"x"); e._scan_vector_resources_sync(); self.assertTrue(p.exists()); self.assertEqual(e._vector_orphan_indexes[str(p.resolve())]["status"], "unknown")

    def test_retired_cleanup_allowed(self):
        e = self.engine(); p = Path(self.tmp.name) / "r.index"; p.write_bytes(b"x"); e._vector_generation = 2; e._record_vector_resource_descriptor(p, role="retired", generation=1, resource_status="retired"); e._scan_vector_resources_sync(); self.assertEqual(e._vector_orphan_indexes[str(p.resolve())]["status"], "cleanup_allowed")

    def test_candidate_generation_mismatch_blocked(self):
        e = self.engine(); p = Path(self.tmp.name) / "c.index"; p.write_bytes(b"x"); e._vector_generation = 3; e._record_vector_resource_descriptor(p, role="candidate", generation=1, resource_status="candidate_ready"); e._scan_vector_resources_sync(); self.assertEqual(e._vector_orphan_indexes[str(p.resolve())]["status"], "cleanup_blocked")

    def test_revision_fence(self):
        e = self.engine(); d = e._record_vector_resource_descriptor(Path(self.tmp.name) / "x.index", role="active", generation=4, resource_status="active"); self.assertFalse(e._persist_vector_resource_descriptor_sync(dict(d, generation=3, revision=0)))

    def test_idempotent_upsert(self):
        e = self.engine(); d = e._record_vector_resource_descriptor(Path(self.tmp.name) / "x.index", role="retired", generation=1, resource_status="retired"); e._persist_vector_resource_descriptor_sync(d); e._persist_vector_resource_descriptor_sync(d); self.assertEqual(sqlite3.connect(e.v2_db_path).execute("select count(*) from vector_resource_descriptors").fetchone()[0], 1)

    def test_exhausted_persists(self):
        e = self.engine(); d = e._record_vector_resource_descriptor(Path(self.tmp.name) / "x.index", role="retired", generation=1, resource_status="repair_exhausted"); e._persist_vector_resource_descriptor_sync(d); self.assertEqual(e._load_vector_resource_descriptors_sync()[0]["resource_status"], "repair_exhausted")

    def test_subprocess_reads_table(self):
        e = self.engine(); d = e._record_vector_resource_descriptor(Path(self.tmp.name) / "x.index", role="active", generation=1, resource_status="active"); e._persist_vector_resource_descriptor_sync(d); out = subprocess.run([sys.executable, "-c", "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute('select role from vector_resource_descriptors').fetchone())", e.v2_db_path], capture_output=True, text=True, check=True); self.assertIn("active", out.stdout)

    def test_missing_repair_deleted(self):
        e = self.engine(); rid = "m:index-delete"; e._vector_index_delete_repairs[rid] = {"repair_id": rid, "stack_id": "m", "generation": 1, "index_path": str(Path(self.tmp.name) / "gone.index"), "attempts": 0, "max_attempts": 2, "next_retry_at": 0, "status": "pending", "last_error": "", "revision": 0, "created_at": 0, "updated_at": 0, "task": None, "sync_future": None}; self.assertTrue(e._run_vector_index_delete_repair_attempt(rid))

    def test_diagnostics(self):
        status = self.engine().describe_vector_status(); self.assertIn("resource_descriptor_count", status); self.assertIn("startup_scan_status", status)

    def test_manifest_fallback_protects_active(self):
        e = self.engine(); p = Path(self.tmp.name) / "vectors.index"; p.write_bytes(b"x"); (e.data_path / "vector_index_manifest.json").write_text(json.dumps({"file_name": p.name, "generation": 2}), encoding="utf-8"); e._scan_vector_resources_sync(); self.assertEqual(e._vector_orphan_indexes[str(p.resolve())]["status"], "protected")

    def test_repeated_scan_is_idempotent(self):
        e = self.engine(); p = Path(self.tmp.name) / "r.index"; p.write_bytes(b"x"); e._vector_generation = 2; e._record_vector_resource_descriptor(p, role="retired", generation=1, resource_status="retired"); e._scan_vector_resources_sync(); before = set(e._vector_index_delete_repairs); e._scan_vector_resources_sync(); self.assertEqual(before, set(e._vector_index_delete_repairs))

    def test_exhausted_descriptor_is_not_reactivated_by_scan(self):
        e = self.engine(); p = Path(self.tmp.name) / "dead.index"; p.write_bytes(b"x"); e._vector_generation = 2; e._record_vector_resource_descriptor(p, role="retired", generation=1, resource_status="retired", last_repair_status="repair_exhausted"); e._scan_vector_resources_sync(); self.assertEqual(e._vector_orphan_indexes[str(p.resolve())]["status"], "repair_exhausted"); self.assertFalse(e._vector_index_delete_repairs)

    def test_real_process_a_to_b_engine_scan_and_recovery(self):
        root = Path(__file__).resolve().parents[3]
        data_path = Path(self.tmp.name)
        db_path = data_path / "memory_v2.db"
        worker = r'''
import asyncio, json, sys
from pathlib import Path
from types import SimpleNamespace
from tests.helpers.astrbot_stubs import install_astrbot_stubs
install_astrbot_stubs(sys.argv[1])
from astrmai.memory.services.memory_engine import MemoryEngine
cfg = SimpleNamespace(provider=SimpleNamespace(embedding_models=["embedding-v2"]), memory=SimpleNamespace(recall_top_k=5))
e = MemoryEngine(SimpleNamespace(), SimpleNamespace(config=cfg), config=cfg)
e.data_path = Path(sys.argv[1]); e.v2_db_path = sys.argv[2]; e._vector_generation = 5
mode = sys.argv[3]
if mode == "a":
    for name, role, generation, status in (("active.index", "active", 5, "active"), ("retired.index", "retired", 3, "retired"), ("overflow.index", "overflow", 4, "retry_wait"), ("candidate.index", "candidate", 1, "candidate_ready"), ("exhausted.index", "retired", 2, "repair_exhausted")):
        path = e.data_path / name; path.write_bytes(b"x")
        d = e._record_vector_resource_descriptor(path, role=role, generation=generation, resource_status=status, last_repair_status=("repair_exhausted" if status == "repair_exhausted" else ""))
        e._persist_vector_resource_descriptor_sync(d)
    (e.data_path / "unknown.index").write_bytes(b"x")
    print(json.dumps({"written": True}))
else:
    async def main():
            await e._load_vector_resource_descriptors()
            await e._load_index_delete_repairs()
            await e._scan_vector_resources_on_startup()
            print(json.dumps({"orphans": e._vector_orphan_indexes, "repairs": {k: v.get("status") for k, v in e._vector_index_delete_repairs.items()}, "status": e._vector_startup_scan_status, "exists": {p.name: p.exists() for p in e.data_path.glob("*.index")}}))
    asyncio.run(main())
'''
        subprocess.run([sys.executable, "-c", worker, str(data_path), str(db_path), "a"], cwd=root, check=True, capture_output=True, text=True)
        completed = subprocess.run([sys.executable, "-c", worker, str(data_path), str(db_path), "b"], cwd=root, check=True, capture_output=True, text=True)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["orphans"][str((data_path / "active.index").resolve())]["status"], "protected")
        self.assertEqual(result["orphans"][str((data_path / "candidate.index").resolve())]["status"], "cleanup_blocked")
        self.assertEqual(result["orphans"][str((data_path / "unknown.index").resolve())]["status"], "unknown")
        self.assertEqual(result["orphans"][str((data_path / "exhausted.index").resolve())]["status"], "repair_exhausted")
        self.assertEqual(result["orphans"][str((data_path / "retired.index").resolve())]["status"], "cleanup_allowed")
        self.assertEqual(result["orphans"][str((data_path / "overflow.index").resolve())]["status"], "cleanup_allowed")
        self.assertEqual(
            {status for repair_id, status in result["repairs"].items() if "resource-" in repair_id},
            {"deleted"},
        )
        self.assertTrue((data_path / "active.index").exists())
        self.assertTrue((data_path / "unknown.index").exists())
        self.assertTrue((data_path / "candidate.index").exists())
        self.assertTrue((data_path / "exhausted.index").exists())
        self.assertFalse((data_path / "retired.index").exists())
        self.assertFalse((data_path / "overflow.index").exists())

    def test_two_process_scans_are_repair_idempotent(self):
        root = Path(__file__).resolve().parents[3]
        data_path = Path(self.tmp.name)
        db_path = data_path / "memory_v2.db"
        worker = """\
import asyncio, json, sys
from pathlib import Path
from types import SimpleNamespace
from tests.helpers.astrbot_stubs import install_astrbot_stubs
install_astrbot_stubs(sys.argv[1])
from astrmai.memory.services.memory_engine import MemoryEngine
cfg = SimpleNamespace(provider=SimpleNamespace(embedding_models=['embedding-v2']), memory=SimpleNamespace(recall_top_k=5))
e = MemoryEngine(SimpleNamespace(), SimpleNamespace(config=cfg), config=cfg)
e.data_path = Path(sys.argv[1]); e.v2_db_path = sys.argv[2]; e._vector_generation = 2
async def main():
    await e._load_vector_resource_descriptors(); await e._load_index_delete_repairs(); await e._scan_vector_resources_on_startup()
    print(json.dumps({'repairs': sorted(e._vector_index_delete_repairs), 'statuses': {k:v.get('status') for k,v in e._vector_index_delete_repairs.items()}}))
asyncio.run(main())
"""
        retired = data_path / "retired.index"; retired.write_bytes(b"x")
        seed = self.engine(); seed._vector_generation = 2
        d = seed._record_vector_resource_descriptor(retired, role="retired", generation=1, resource_status="retired"); seed._persist_vector_resource_descriptor_sync(d)
        first = subprocess.Popen([sys.executable, "-c", worker, str(data_path), str(db_path)], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen([sys.executable, "-c", worker, str(data_path), str(db_path)], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out1, err1 = first.communicate(timeout=20); out2, err2 = second.communicate(timeout=20)
        self.assertEqual(first.returncode, 0, err1); self.assertEqual(second.returncode, 0, err2)
        result1 = json.loads(out1.strip().splitlines()[-1]); result2 = json.loads(out2.strip().splitlines()[-1])
        all_repairs = set(result1["repairs"]) | set(result2["repairs"])
        self.assertEqual(len(all_repairs), 1)
        self.assertTrue(retired.exists() is False)
        with sqlite3.connect(db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM vector_index_delete_repairs").fetchone()[0], 1)
