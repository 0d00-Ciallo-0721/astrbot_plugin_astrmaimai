import sqlite3

from astrmai.infrastructure.persistence.persistence_schema import _run_migrations


def test_visual_asset_timing_migration_upgrades_existing_table(tmp_path):
    db_path = tmp_path / "visual-timing.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            CREATE TABLE visualasset (
                asset_id TEXT PRIMARY KEY,
                description TEXT DEFAULT ''
            )
            """
        )
        db.execute("PRAGMA user_version = 85")
        _run_migrations(db)
        columns = {
            row[1]: row
            for row in db.execute("PRAGMA table_info(visualasset)").fetchall()
        }
        version = db.execute("PRAGMA user_version").fetchone()[0]

    assert version == 88
    assert "initial_recognition_elapsed_ms" in columns
    assert "reuse_count" in columns
    assert float(columns["initial_recognition_elapsed_ms"][4]) == 0.0


def test_visual_asset_timing_migration_bootstraps_sparse_database(tmp_path):
    db_path = tmp_path / "visual-timing-sparse.db"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version = 85")
        _run_migrations(db)
        columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(visualasset)").fetchall()
        }
        version = db.execute("PRAGMA user_version").fetchone()[0]

    assert version == 88
    assert "asset_id" in columns
    assert "initial_recognition_elapsed_ms" in columns
    assert "reuse_count" in columns
