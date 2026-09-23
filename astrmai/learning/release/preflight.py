from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import shutil
import sqlite3
from typing import Callable, Iterable


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class SQLiteSnapshot:
    path: Path
    sha256: str
    schema_version: int
    integrity_ok: bool
    foreign_keys_ok: bool
    sidecar_hashes: tuple[tuple[str, str], ...]
    schema_version_ok: bool
    role: str = "main"
    pragma_user_version: int = 0
    meta_schema_version: int | None = None

    @classmethod
    def capture(
        cls,
        path: Path,
        *,
        role: str = "main",
        expected_schema_version: int | None = None,
        expected_meta_schema_version: int | None = None,
    ) -> "SQLiteSnapshot":
        path = Path(path)
        if role not in {"main", "memory_v2"}:
            raise ValueError("sqlite snapshot role must be main or memory_v2")
        if not path.is_file() or path.is_symlink():
            raise ValueError("sqlite snapshot must be a regular file")
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            foreign = not list(db.execute("PRAGMA foreign_key_check"))
            pragma_version = int(db.execute("PRAGMA user_version").fetchone()[0])
            meta_version = None
            if role == "memory_v2":
                table = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='memory_v2_meta'"
                ).fetchone()
                if table is not None:
                    row = db.execute(
                        "SELECT value FROM memory_v2_meta WHERE key='schema_version'"
                    ).fetchone()
                    try:
                        meta_version = int(row[0]) if row is not None else None
                    except (TypeError, ValueError):
                        meta_version = None
        if role == "main":
            if expected_schema_version is None:
                raise ValueError("main schema contract requires expected_schema_version")
            version = pragma_version
            schema_ok = version == expected_schema_version
        else:
            if expected_meta_schema_version is None:
                raise ValueError(
                    "memory_v2 schema contract requires expected_meta_schema_version"
                )
            version = -1 if meta_version is None else meta_version
            schema_ok = meta_version == expected_meta_schema_version
        sidecars = []
        for suffix in ("-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            if candidate.exists():
                sidecars.append((suffix, _sha256(candidate)))
        return cls(
            path,
            _sha256(path),
            version,
            integrity,
            foreign,
            tuple(sidecars),
            schema_ok,
            role,
            pragma_version,
            meta_version,
        )

    def unchanged(self) -> bool:
        if not self.path.is_file() or self.path.is_symlink() or _sha256(self.path) != self.sha256:
            return False
        current_sidecars = []
        expected_by_suffix = dict(self.sidecar_hashes)
        for suffix in ("-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if suffix not in expected_by_suffix:
                if candidate.exists():
                    return False
                continue
            if not candidate.is_file() or _sha256(candidate) != expected_by_suffix[suffix]:
                return False
            current_sidecars.append((suffix, expected_by_suffix[suffix]))
        return tuple(current_sidecars) == self.sidecar_hashes


@dataclass(frozen=True, slots=True)
class PreflightChecks:
    schema_migration: bool
    cas_revision: bool
    review_admission_revision: bool
    vector_generation: bool
    retrieval_provenance: bool
    restart_recovery: bool
    shutdown_recovery: bool
    lease_recovery: bool
    learning_lane_isolated: bool
    runtime_budget_isolated: bool
    dialog_reserve_isolated: bool
    kill_switch_checkpoints: bool
    source_immutable: bool
    production_write_blocked: bool
    cursor_advance_blocked: bool

    @classmethod
    def all_passed(cls) -> "PreflightChecks":
        return cls(*(True for _ in cls.__dataclass_fields__))


@dataclass(frozen=True, slots=True)
class DatabasePreflight:
    path: Path
    sha256: str
    schema_version: int
    integrity_ok: bool
    foreign_keys_ok: bool
    sidecar_hashes: tuple[tuple[str, str], ...]
    schema_version_ok: bool
    unchanged: bool
    role: str
    pragma_user_version: int
    meta_schema_version: int | None


@dataclass(frozen=True, slots=True)
class PreflightResult:
    passed: bool
    reasons: tuple[str, ...]
    databases: tuple[DatabasePreflight, ...]
    provider_calls: int
    network_calls: int


@dataclass(frozen=True, slots=True)
class MigrationResult:
    source_sha256: str
    source_unchanged: bool
    working_copy: SQLiteSnapshot


def migrate_temporary_sqlite(
    *,
    source: Path,
    working_copy: Path,
    expected_schema_version: int,
    migrate: Callable[[sqlite3.Connection], None],
) -> MigrationResult:
    """Migrate an isolated copy and prove the source snapshot stayed unchanged."""
    source = Path(source)
    working_copy = Path(working_copy)
    if not source.is_file() or source.is_symlink():
        raise ValueError("migration source must be a regular sqlite file")
    if working_copy.exists() or working_copy.is_symlink():
        raise ValueError("migration working copy must be a new path")
    try:
        if source.resolve(strict=True) == working_copy.resolve(strict=False):
            raise ValueError("migration working copy overlaps source")
    except OSError as exc:
        raise ValueError("migration paths are invalid") from exc
    source_hash = _sha256(source)
    working_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, working_copy)
    with sqlite3.connect(working_copy) as db:
        db.execute("PRAGMA foreign_keys=ON")
        migrate(db)
        db.commit()
    snapshot = SQLiteSnapshot.capture(
        working_copy,
        expected_schema_version=expected_schema_version,
    )
    return MigrationResult(source_hash, _sha256(source) == source_hash, snapshot)


def run_preflight(
    *,
    snapshots: Iterable[SQLiteSnapshot],
    checks: PreflightChecks,
    provider_mode: str,
    provider_calls: int,
    network_calls: int,
) -> PreflightResult:
    snapshots = tuple(snapshots)
    reasons: list[str] = []
    if provider_mode not in {"fake", "recorded"}:
        reasons.append("provider_mode_not_local")
    if provider_calls != 0:
        reasons.append("provider_calls_must_be_zero")
    if network_calls != 0:
        reasons.append("network_calls_must_be_zero")
    for field_name in checks.__dataclass_fields__:
        if not getattr(checks, field_name):
            reasons.append(field_name)
    databases = tuple(
        DatabasePreflight(
            item.path,
            item.sha256,
            item.schema_version,
            item.integrity_ok,
            item.foreign_keys_ok,
            item.sidecar_hashes,
            item.schema_version_ok,
            item.unchanged(),
            item.role,
            item.pragma_user_version,
            item.meta_schema_version,
        )
        for item in snapshots
    )
    if not databases:
        reasons.append("missing_sqlite_snapshots")
    roles = [item.role for item in snapshots]
    if roles.count("main") != 1:
        reasons.append("main_schema_contract_missing_or_duplicated")
    if roles.count("memory_v2") != 1:
        reasons.append("memory_v2_schema_contract_missing_or_duplicated")
    for item in snapshots:
        if not item.integrity_ok:
            reasons.append(f"integrity:{item.path}")
        if not item.foreign_keys_ok:
            reasons.append(f"foreign_keys:{item.path}")
        if not item.schema_version_ok:
            reasons.append(f"schema_version:{item.path}")
        if not item.unchanged():
            reasons.append(f"snapshot_changed:{item.path}")
    return PreflightResult(not reasons, tuple(dict.fromkeys(reasons)), databases, provider_calls, network_calls)
