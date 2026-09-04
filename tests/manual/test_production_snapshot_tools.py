from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astrmai.infrastructure.persistence.architecture_migration_audit import (
    LATEST_ARCHITECTURE_SCHEMA_VERSION,
)


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

    def test_archive_same_hash_removes_live_duplicate_and_rollback_restores(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / archive.CLONE_MARKER).write_text("{}", encoding="utf-8")
            (root / "memory").mkdir()
            source = root / "memory" / "vectors-pre-320663f.index"
            source.write_bytes(b"same")
            destination = root / "archive" / "pre_migration"
            destination.mkdir(parents=True)
            (destination / source.name).write_bytes(b"same")

            plan = archive.build_archive_plan(root)
            result = archive.apply_archive_plan(plan)
            self.assertFalse(source.exists())
            self.assertTrue((destination / source.name).exists())
            archive.rollback_archive_plan(result)
            self.assertTrue(source.exists())
            self.assertEqual(source.read_bytes(), b"same")

    def test_archive_destination_only_is_safe_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / archive.CLONE_MARKER).write_text("{}", encoding="utf-8")
            (root / "memory").mkdir()
            destination = root / "archive" / "pre_migration"
            destination.mkdir(parents=True)
            (destination / "vectors-pre-320663f.index").write_bytes(b"archived")
            plan = archive.build_archive_plan(root)
            self.assertEqual(len(plan["items"]), 1)
            self.assertFalse(plan["items"][0]["source_exists"])

    def test_archive_partial_apply_and_internal_rollback_failure_preserves_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / archive.CLONE_MARKER).write_text("{}", encoding="utf-8")
            (root / "memory").mkdir()
            first = root / "memory" / "vectors-pre-320663f.index"
            second = root / "astrmai-pre-320663f.db"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            plan = archive.build_archive_plan(root)
            real_move = archive.shutil.move
            move_calls = 0

            def fail_second_move(source, destination):
                nonlocal move_calls
                move_calls += 1
                if move_calls == 2:
                    raise OSError("archive move boom")
                return real_move(source, destination)

            with patch.object(archive.shutil, "move", side_effect=fail_second_move):
                with patch.object(
                    archive,
                    "rollback_archive_plan",
                    side_effect=OSError("archive rollback boom"),
                ):
                    with self.assertRaises(archive.ArchiveApplyError) as raised:
                        archive.apply_archive_plan(plan)
            error = raised.exception
            self.assertEqual(str(error.original_error), "archive move boom")
            self.assertEqual(str(error.rollback_error), "archive rollback boom")
            self.assertEqual(len(error.partial_result["moved"]), 1)
            self.assertEqual(error.partial_result["moved"][0]["source"], str(Path("memory/vectors-pre-320663f.index")))

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
            self.assertEqual(
                result["main"]["user_version"], LATEST_ARCHITECTURE_SCHEMA_VERSION
            )
            self.assertIn("diary_checkpoints", result["main"].get("tables", []))
            self.assertTrue(result["memory_v2"]["required_tables_present"])
            before = json.loads(Path(result["manifests"]["before"]).read_text(encoding="utf-8"))
            self.assertEqual(before["rollback_status"], "available_from_run_backup")
            again = migrate.migrate_clone(root, archive_legacy=False)
            self.assertEqual(
                again["main"]["before_user_version"],
                LATEST_ARCHITECTURE_SCHEMA_VERSION,
            )

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
            failed = list((root / "archive" / "pre_migration").glob("migration_*_failed_manifest.json"))
            self.assertEqual(len(failed), 1)
            payload = json.loads(failed[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["error_type"], "RuntimeError")
            self.assertEqual(payload["error"], "boom")
            self.assertEqual(payload["failed_stage"], "migration")

    def test_repeated_failed_migration_restores_second_run_starting_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / archive.CLONE_MARKER).write_text("{}", encoding="utf-8")
            (root / "memory").mkdir()
            db = sqlite3.connect(root / "astrmai.db")
            db.execute("PRAGMA user_version = 128")
            db.commit()
            db.close()
            first = migrate.migrate_clone(root, archive_legacy=False)
            second_result = migrate.migrate_clone(root, archive_legacy=False)
            main_before_failure = (root / "astrmai.db").read_bytes()
            v2_before_failure = (root / "memory" / "memory_v2.db").read_bytes()
            self.assertNotEqual(first["run_id"], second_result["run_id"])
            self.assertNotEqual(first["backup_files"]["main"], second_result["backup_files"]["main"])
            for manifest_path in first["manifests"].values():
                if str(manifest_path).endswith(".json"):
                    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
                    self.assertEqual(payload["run_id"], first["run_id"])
            with patch.object(migrate, "_main_db_summary", side_effect=RuntimeError("second run boom")):
                with self.assertRaises(RuntimeError):
                    migrate.migrate_clone(root, archive_legacy=False)
            self.assertEqual(main_before_failure, (root / "astrmai.db").read_bytes())
            self.assertEqual(v2_before_failure, (root / "memory" / "memory_v2.db").read_bytes())

    def test_migration_rollback_failure_is_reported_with_run_manifest(self):
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
            with patch.object(migrate, "_main_db_summary", side_effect=RuntimeError("migration boom")):
                with patch.object(migrate, "rollback_archive_plan", side_effect=RuntimeError("rollback boom")):
                    with self.assertRaisesRegex(RuntimeError, "rollback failed"):
                        migrate.migrate_clone(root)
            failed = list((root / "archive" / "pre_migration").glob("migration_*_failed_manifest.json"))
            self.assertEqual(len(failed), 1)
            payload = json.loads(failed[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["migration_status"], "failed")
            self.assertEqual(payload["rollback_status"], "rollback_failed")
            self.assertTrue(payload["run_id"])
            self.assertEqual(payload["error_type"], "RuntimeError")
            self.assertEqual(payload["error"], "migration boom")
            self.assertEqual(payload["rollback_error"], "rollback boom")
            self.assertEqual(payload["failed_stage"], "rollback")

    def test_archive_internal_rollback_failure_is_not_overwritten_by_outer_rollback(self):
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
            partial_result = {
                "root": str(root),
                "moved": [{"source": "memory/vectors-pre-320663f.index", "sha256": "digest"}],
            }
            apply_error = migrate.ArchiveApplyError(
                RuntimeError("archive move boom"),
                RuntimeError("archive rollback boom"),
                partial_result,
            )
            with patch.object(migrate, "apply_archive_plan", side_effect=apply_error):
                with patch.object(migrate, "rollback_archive_plan") as outer_rollback:
                    with self.assertRaisesRegex(RuntimeError, "archive rollback failed"):
                        migrate.migrate_clone(root)
            outer_rollback.assert_not_called()
            failed = list((root / "archive" / "pre_migration").glob("migration_*_failed_manifest.json"))
            self.assertEqual(len(failed), 1)
            payload = json.loads(failed[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["rollback_status"], "rollback_failed")
            self.assertEqual(payload["error"], "archive move boom")
            self.assertEqual(payload["rollback_error"], "archive rollback boom")
            self.assertEqual(payload["failed_stage"], "archive_rollback")
            self.assertEqual(payload["archive_partial_result"]["moved"], partial_result["moved"])

    def test_missing_main_db_writes_initialization_failed_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / archive.CLONE_MARKER).write_text("{}", encoding="utf-8")
            (root / "memory").mkdir()
            with self.assertRaisesRegex(ValueError, "missing clone main database"):
                migrate.migrate_clone(root, archive_legacy=False)
            failed = list((root / "archive" / "pre_migration").glob("migration_*_failed_manifest.json"))
            self.assertEqual(len(failed), 1)
            payload = json.loads(failed[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["migration_status"], "failed")
            self.assertEqual(payload["rollback_status"], "not_started")
            self.assertEqual(payload["failed_stage"], "initialization")
            self.assertEqual(payload["error_type"], "ValueError")
            self.assertIn("missing clone main database", payload["error"])
            before = list((root / "archive" / "pre_migration").glob("migration_*_before_manifest.json"))
            self.assertEqual(len(before), 1)
            self.assertEqual(
                json.loads(before[0].read_text(encoding="utf-8"))["rollback_status"],
                "backup_pending",
            )

    def test_backup_failure_writes_initialization_failed_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / archive.CLONE_MARKER).write_text("{}", encoding="utf-8")
            (root / "memory").mkdir()
            db = sqlite3.connect(root / "astrmai.db")
            db.execute("PRAGMA user_version = 128")
            db.commit()
            db.close()
            with patch.object(migrate.shutil, "copy2", side_effect=OSError("backup denied")):
                with self.assertRaisesRegex(OSError, "backup denied"):
                    migrate.migrate_clone(root, archive_legacy=False)
            failed = list((root / "archive" / "pre_migration").glob("migration_*_failed_manifest.json"))
            self.assertEqual(len(failed), 1)
            payload = json.loads(failed[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["failed_stage"], "initialization")
            self.assertEqual(payload["error_type"], "OSError")
            self.assertEqual(payload["error"], "backup denied")
            before = list((root / "archive" / "pre_migration").glob("migration_*_before_manifest.json"))
            self.assertEqual(len(before), 1)
            self.assertEqual(
                json.loads(before[0].read_text(encoding="utf-8"))["rollback_status"],
                "backup_pending",
            )

    def test_migrate_rejects_production_snapshot_path(self):
        with self.assertRaises(ValueError):
            migrate.migrate_clone(Path(tempfile.gettempdir()) / "astrmai_prod_snapshot" / "astrmai")

    def test_migrate_cli_reports_successful_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / archive.CLONE_MARKER).write_text("{}", encoding="utf-8")
            (root / "memory").mkdir()
            db = sqlite3.connect(root / "astrmai.db")
            db.execute("PRAGMA user_version = 128")
            db.commit()
            db.close()
            with patch.object(migrate, "_main_db_summary", side_effect=RuntimeError("migration boom")):
                with patch.object(sys, "argv", ["production_snapshot_migrate.py", str(root)]):
                    with patch("builtins.print") as output:
                        self.assertEqual(migrate.main(), 1)
            payload = json.loads(output.call_args.args[0])
            self.assertEqual(payload["rollback_status"], "rolled_back")
            self.assertEqual(payload["failed_stage"], "migration")
            self.assertEqual(payload["error_type"], "RuntimeError")
            self.assertEqual(payload["error"], "migration boom")
            self.assertTrue(payload["run_id"])
            self.assertTrue(Path(payload["failed_manifest"]).is_file())
            self.assertIsNone(payload["rollback_error"])

    def test_migrate_cli_preserves_rollback_failure(self):
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
            with patch.object(migrate, "_main_db_summary", side_effect=RuntimeError("migration boom")):
                with patch.object(migrate, "rollback_archive_plan", side_effect=RuntimeError("rollback boom")):
                    with patch.object(sys, "argv", ["production_snapshot_migrate.py", str(root)]):
                        with patch("builtins.print") as output:
                            self.assertEqual(migrate.main(), 1)
            payload = json.loads(output.call_args.args[0])
            self.assertEqual(payload["rollback_status"], "rollback_failed")
            self.assertEqual(payload["failed_stage"], "rollback")
            self.assertEqual(payload["error"], "migration boom")
            self.assertEqual(payload["rollback_error"], "rollback boom")
            self.assertTrue(payload["run_id"])
            self.assertTrue(Path(payload["failed_manifest"]).is_file())

    def test_migrate_cli_preserves_archive_partial_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / archive.CLONE_MARKER).write_text("{}", encoding="utf-8")
            (root / "memory").mkdir()
            db = sqlite3.connect(root / "astrmai.db")
            db.execute("PRAGMA user_version = 128")
            db.commit()
            db.close()
            partial_result = {
                "root": str(root),
                "moved": [
                    {
                        "source": "memory/vectors-pre-320663f.index",
                        "sha256": "digest",
                    }
                ],
            }
            apply_error = migrate.ArchiveApplyError(
                RuntimeError("archive move boom"),
                RuntimeError("archive rollback boom"),
                partial_result,
            )
            with patch.object(migrate, "apply_archive_plan", side_effect=apply_error):
                with patch.object(sys, "argv", ["production_snapshot_migrate.py", str(root)]):
                    with patch("builtins.print") as output:
                        self.assertEqual(migrate.main(), 1)
            payload = json.loads(output.call_args.args[0])
            self.assertEqual(payload["rollback_status"], "rollback_failed")
            self.assertEqual(payload["failed_stage"], "archive_rollback")
            self.assertEqual(payload["error"], "archive move boom")
            self.assertEqual(payload["rollback_error"], "archive rollback boom")
            self.assertEqual(payload["archive_partial_result"]["moved"], partial_result["moved"])
            self.assertTrue(Path(payload["failed_manifest"]).is_file())

    def test_migrate_cli_reports_pre_report_clone_failure_as_not_available(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(sys, "argv", ["production_snapshot_migrate.py", str(root)]):
                with patch("builtins.print") as output:
                    self.assertEqual(migrate.main(), 1)
            payload = json.loads(output.call_args.args[0])
            self.assertEqual(payload["rollback_status"], "not_available")
            self.assertEqual(payload["failed_stage"], "pre_report")
            self.assertEqual(payload["error_type"], "ValueError")
            self.assertIn("clone marker is required", payload["error"])
            self.assertIsNone(payload["run_id"])
            self.assertIsNone(payload["failed_manifest"])
            self.assertIsNone(payload["rollback_error"])


if __name__ == "__main__":
    unittest.main()
