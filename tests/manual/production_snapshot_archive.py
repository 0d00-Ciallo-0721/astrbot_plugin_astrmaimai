"""Archive legacy snapshot files outside the live index scan directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ITEMS = (
    Path("memory/vectors-pre-320663f.index"),
    Path("astrmai-pre-320663f.db"),
    Path("memory/docs-pre-320663f.db"),
    Path("memory/memory_v2-pre-320663f.db"),
    Path("cleanup-320663f-manifest.json"),
    Path("backups"),
    Path("memory/backup_before_v2_20260714_200712"),
)
CLONE_MARKER = ".astrmai_clone"


def _assert_clone(root: Path) -> Path:
    resolved = root.resolve()
    normalized = str(resolved).lower().replace("/", "\\")
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        resolved.relative_to(temp_root)
    except ValueError as exc:
        raise ValueError(f"clone must be inside the system temporary directory: {resolved}") from exc
    if resolved == temp_root or "prod_snapshot" in normalized or ".git" in resolved.parts:
        raise ValueError(f"refusing production-looking path: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"clone directory does not exist: {resolved}")
    marker = resolved / CLONE_MARKER
    if not marker.is_file():
        raise ValueError(f"clone marker is required: {marker}")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    else:
        for child in sorted(item for item in path.rglob("*") if item.is_file()):
            digest.update(str(child.relative_to(path)).encode("utf-8"))
            with child.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def build_archive_plan(root: Path) -> dict[str, Any]:
    root = _assert_clone(root)
    archive = root / "archive" / "pre_migration"
    items: list[dict[str, Any]] = []
    for relative in DEFAULT_ITEMS:
        source = root / relative
        destination = archive / relative.name
        if not source.exists() and not destination.exists():
            continue
        source_hash = _sha256(source) if source.exists() else None
        destination_hash = _sha256(destination) if destination.exists() else None
        if source_hash is not None and destination_hash is not None and destination_hash != source_hash:
            raise ValueError(f"archive destination hash mismatch: {destination}")
        items.append(
            {
                "source": str(relative),
                "destination": str(destination.relative_to(root)),
                "kind": "directory" if source.is_dir() else "file",
                "size": (
                    sum(item.stat().st_size for item in source.rglob("*") if item.is_file())
                    if source.is_dir()
                    else source.stat().st_size
                ) if source.exists() else (
                    sum(item.stat().st_size for item in destination.rglob("*") if item.is_file())
                    if destination.is_dir()
                    else destination.stat().st_size
                ),
                "sha256": source_hash or destination_hash or "",
                "destination_exists": destination.exists(),
                "destination_sha256": destination_hash,
                "source_exists": source.exists(),
            }
        )
    return {"root": str(root), "archive": str(archive), "items": items}


def apply_archive_plan(plan: dict[str, Any]) -> dict[str, Any]:
    root = _assert_clone(Path(str(plan["root"])))
    archive = root / "archive" / "pre_migration"
    archive.mkdir(parents=True, exist_ok=True)
    moved: list[dict[str, Any]] = []
    try:
        for item in plan.get("items", []):
            source = root / str(item["source"])
            destination = archive / Path(str(item["source"])).name
            if not source.exists():
                if destination.exists() and _sha256(destination) == str(item["sha256"]):
                    moved.append({**item, "already_archived": True, "applied_at": datetime.now(timezone.utc).isoformat()})
                continue
            if destination.exists():
                if _sha256(destination) != str(item["sha256"]):
                    raise ValueError(f"archive destination hash mismatch: {destination}")
                continue
            shutil.move(str(source), str(destination))
            moved.append({**item, "applied_at": datetime.now(timezone.utc).isoformat()})
        manifest = archive / "archive_manifest.json"
        payload = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "items": moved,
        }
        manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        rollback_archive_plan({"root": str(root), "moved": moved})
        raise
    return {**plan, "moved": moved, "manifest": str(manifest)}


def rollback_archive_plan(result: dict[str, Any]) -> list[str]:
    """Restore files moved by ``apply_archive_plan`` when a clone run fails."""
    root = _assert_clone(Path(str(result["root"])))
    restored: list[str] = []
    for item in reversed(result.get("moved", [])):
        if item.get("already_archived"):
            continue
        source = root / str(item["source"])
        destination = root / "archive" / "pre_migration" / Path(str(item["source"])).name
        if not destination.exists():
            continue
        if source.exists():
            raise ValueError(f"cannot rollback archive; source already exists: {source}")
        if _sha256(destination) != str(item["sha256"]):
            raise ValueError(f"cannot rollback archive; hash changed: {destination}")
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(destination), str(source))
        restored.append(str(item["source"]))
    return restored


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--apply-to-clone", action="store_true")
    args = parser.parse_args()
    plan = build_archive_plan(args.root)
    result = apply_archive_plan(plan) if args.apply_to_clone else plan
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
