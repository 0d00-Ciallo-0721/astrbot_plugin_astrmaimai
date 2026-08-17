import sqlite3

from sqlmodel import SQLModel, create_engine

from astrmai.infrastructure.persistence.persistence_schema import (
    PersistenceSchemaMixin,
    _dedupe_sqlmodel_metadata_indexes,
    _run_migrations,
)
from astrmai.infrastructure.persistence import orm_models  # noqa: F401


def test_visual_asset_timing_migration_upgrades_v87_to_v88(tmp_path):
    db_path = tmp_path / "visual-timing.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            CREATE TABLE visualasset (
                asset_id TEXT PRIMARY KEY,
                description TEXT DEFAULT '',
                initial_recognition_elapsed_ms REAL DEFAULT 0
            )
            """
        )
        db.execute("PRAGMA user_version = 87")
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


def test_visual_asset_timing_migration_bootstraps_fresh_database(tmp_path):
    db_path = tmp_path / "visual-timing-fresh.db"

    class _SchemaHarness(PersistenceSchemaMixin):
        pass

    harness = _SchemaHarness()
    harness.db_path = str(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    _dedupe_sqlmodel_metadata_indexes()
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    harness._init_db_sync()

    with sqlite3.connect(db_path) as db:
        columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(visualasset)").fetchall()
        }
        version = db.execute("PRAGMA user_version").fetchone()[0]

    assert version == 88
    assert "asset_id" in columns
    assert "initial_recognition_elapsed_ms" in columns
    assert "reuse_count" in columns
