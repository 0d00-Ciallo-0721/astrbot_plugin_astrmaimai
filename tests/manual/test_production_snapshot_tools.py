from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


preflight = _load("production_snapshot_preflight", ROOT / "tests/manual/production_snapshot_preflight.py")
archive = _load("production_snapshot_archive", ROOT / "tests/manual/production_snapshot_archive.py")
release_gate = _load("production_release_gate", ROOT / "tests/manual/production_release_gate.py")
migrate = _load("production_snapshot_migrate", ROOT / "tests/manual/production_snapshot_migrate.py")


class ProductionSnapshotToolTests(unittest.TestCase):
    def test_preflight_has_unified_sections_and_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / archive.CLONE_MARKER).write_text("{}", encoding="utf-8")
            (root / "memory").mkdir()
            (root / "cache").mkdir()
            db = sqlite3.connect(root / "astrmai.db")
            db.execute("PRAGMA user_version = 128")
            db.commit()
            db.close()
            before = (root / "astrmai.db").read_bytes()
            report = preflight.build_report(root)
            self.assertIn("preflight_status", report)
            self.assertIn("database_summary", report)
            self.assertIn("vector_summary", report)
            self.assertIn("migration_plan", report)
            self.assertEqual(before, (root / "astrmai.db").read_bytes())

    def test_snapshot_fixture_counts_projectable_records_not_all_canonical(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / archive.CLONE_MARKER).write_text("{}", encoding="utf-8")
            (root / "memory").mkdir()
            db = sqlite3.connect(root / "memory" / "memory_v2.db")
            db.execute("CREATE TABLE canonical_memories (id TEXT PRIMARY KEY, status TEXT, visibility TEXT)")
            db.execute("CREATE TABLE canonical_fts (memory_id TEXT, content TEXT)")
            db.execute("CREATE TABLE documents (id TEXT PRIMARY KEY, metadata TEXT)")
            rows = [(f"m{i}", "active" if i < 63 else "deleted", "auto_and_tool") for i in range(788)]
            db.executemany("INSERT INTO canonical_memories VALUES (?, ?, ?)", rows)
            db.executemany("INSERT INTO canonical_fts VALUES (?, ?)", [(f"m{i}", "x") for i in range(63)])
            db.executemany("INSERT INTO documents VALUES (?, ?)", [(f"d{i}", '{"canonical_id": "m' + str(i) + '"}') for i in range(63)])
            db.commit()
            db.close()
            report = preflight.build_report(root)
            summary = report["database_summary"]["memory_v2"]
            self.assertEqual(summary["canonical_status"]["active"], 63)
            self.assertEqual(summary["canonical_projection"]["fts"], 63)

    def test_archive_dry_run_does_not_move_and_apply_records_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / archive.CLONE_MARKER).write_text("{}", encoding="utf-8")
            (root / "memory").mkdir()
            legacy = root / "memory" / "vectors-pre-320663f.index"
            legacy.write_bytes(b"legacy")
            plan = archive.build_archive_plan(root)
            self.assertEqual(len(plan["items"]), 1)
            self.assertTrue(legacy.exists())
            result = archive.apply_archive_plan(plan)
            self.assertFalse(legacy.exists())
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["items"][0]["sha256"], plan["items"][0]["sha256"])

    def test_archive_rejects_production_snapshot_path(self):
        with self.assertRaises(ValueError):
            archive.build_archive_plan(Path(tempfile.gettempdir()) / "astrmai_prod_snapshot" / "astrmai")

    def test_archive_requires_clone_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                archive.build_archive_plan(Path(temp))

    def test_archive_destination_hash_conflict_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / archive.CLONE_MARKER).write_text("{}", encoding="utf-8")
            (root / "memory").mkdir()
            source = root / "memory" / "vectors-pre-320663f.index"
            source.write_bytes(b"new")
            destination = root / "archive" / "pre_migration"
            destination.mkdir(parents=True)
            (destination / source.name).write_bytes(b"old")
            with self.assertRaises(ValueError):
                archive.build_archive_plan(root)
            self.assertTrue(source.exists())

    def test_release_gate_reports_required_files_and_data_boundary(self):
        report = release_gate.build_release_report(ROOT)
        self.assertIn("required_files_tracked", report)
        self.assertIn("forbidden_tracked_data", report)
        self.assertIn("rollback_pair", report)

    def test_migrate_clone_is_idempotent_and_initializes_v2_tables(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / archive.CLONE_MARKER).write_text("{}", encoding="utf-8")
            (root / "memory").mkdir()
            db = sqlite3.connect(root / "astrmai.db")
            db.execute("PRAGMA user_version = 128")
            db.commit()
            db.close()
            result = migrate.migrate_clone(root, archive_legacy=False)
            self.assertEqual(result["main"]["user_version"], 128)
            self.assertTrue(result["memory_v2"]["required_tables_present"])
            again = migrate.migrate_clone(root, archive_legacy=False)
            self.assertEqual(again["main"]["before_user_version"], 128)

    def test_failed_migration_restores_database_and_archived_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / archive.CLONE_MARKER).write_text("{}", encoding="utf-8")
            (root / "memory").mkdir()
            legacy = root / "memory" / "vectors-pre-320663f.index"
            legacy.write_bytes(b"legacy")
            db = sqlite3.connect(root / "astrmai.db")
            db.execute("PRAGMA user_version = 128")
            db.commit()
            db.close()
            original = (root / "astrmai.db").read_bytes()
            with patch.object(migrate, "_main_db_summary", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    migrate.migrate_clone(root)
            self.assertEqual(original, (root / "astrmai.db").read_bytes())
            self.assertTrue(legacy.exists())

    def test_migrate_rejects_production_snapshot_path(self):
        with self.assertRaises(ValueError):
            migrate.migrate_clone(Path(tempfile.gettempdir()) / "astrmai_prod_snapshot" / "astrmai")


if __name__ == "__main__":
    unittest.main()
