"""Read-only release package and rollback gate for AstrMai."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any


REQUIRED_FILES = (
    "tests/manual/production_snapshot_preflight.py",
    "tests/manual/production_snapshot_archive.py",
    "tests/manual/production_snapshot_migrate.py",
    "astrmai/memory/services/vector_migration_decision.py",
)
FORBIDDEN_PREFIXES = ("data/", "artifacts/", "cache/", "memory/")
FORBIDDEN_SUFFIXES = (".db", ".index", ".sqlite", ".sqlite3", ".db-wal", ".db-shm")


def _git(root: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def build_release_report(root: Path) -> dict[str, Any]:
    root = root.resolve()
    status = _git(root, "status", "--short")
    tracked = _git(root, "ls-files")
    tracked_lower = [item.replace("\\", "/").lower() for item in tracked]
    forbidden = [
        item
        for item in tracked_lower
        if item.startswith(FORBIDDEN_PREFIXES) or item.endswith(FORBIDDEN_SUFFIXES)
    ]
    missing = [item for item in REQUIRED_FILES if item.lower() not in tracked_lower]
    return {
        "root": str(root),
        "workspace_clean": not status,
        "status": status,
        "required_files_tracked": not missing,
        "missing_required_files": missing,
        "forbidden_tracked_data": forbidden,
        "rollback_pair": "old_code+old_data or new_code+v128_data",
        "release_ready": not status and not missing and not forbidden,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, nargs="?", default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps(build_release_report(args.root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
