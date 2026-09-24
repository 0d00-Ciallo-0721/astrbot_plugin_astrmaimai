from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    "__init__.py",
    "main.py",
    "config.py",
    "_conf_schema.json",
    "metadata.yaml",
    "requirements.txt",
    "README.md",
    "CHANGELOG.md",
)
REQUIRED_ARCHITECTURE_FILES = (
    "astrmai/conversation/contracts/conversation_event.py",
    "astrmai/conversation/contracts/turn_target.py",
    "astrmai/conversation/contracts/committed_reply.py",
    "astrmai/conversation/contracts/context_package.py",
    "astrmai/conversation/planning/message_renderer.py",
    "astrmai/conversation/runtime/architecture_rollout.py",
    "astrmai/conversation/runtime/architecture_trace.py",
)
PAGE_SUFFIXES = {".html", ".css", ".js"}
FORBIDDEN_PARTS = {
    "__pycache__",
    ".agent",
    ".claude",
    ".git",
    ".pytest_cache",
    "tests",
    "plan",
    "specs",
    "venv",
    ".venv",
    "plugin_data",
    "cache",
    "turn_trace",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".jsonl",
    ".pyc",
    ".pyo",
}
TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".html", ".css", ".js", ".txt"}
FORBIDDEN_TEXT_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\r\n]+", re.IGNORECASE),
    re.compile(r"[A-Za-z]:\\ai_robot\\", re.IGNORECASE),
    re.compile(r"/root/astrbot(?:/|$)", re.IGNORECASE),
    re.compile(r"BEGIN (?:RSA|OPENSSH|EC) PRIVATE KEY"),
)


@dataclass(frozen=True, slots=True)
class ExcludedSourceRule:
    path: str
    classification: str
    reason: str
    prefix: bool = False


EXCLUDED_SOURCE_RULES = (
    ExcludedSourceRule(
        "scripts/audit_learning_data.py",
        "ops",
        "offline learning data audit and cleanup utility",
    ),
    ExcludedSourceRule(
        "astrmai/webui/backend/server.py",
        "independent_service",
        "standalone FastAPI/uvicorn entrypoint; Native Plugin Page uses plugin_pages.py",
    ),
    ExcludedSourceRule(
        "astrmai/webui/backend/routes.py",
        "independent_service",
        "standalone FastAPI route aggregator",
    ),
    ExcludedSourceRule(
        "astrmai/webui/backend/routes/",
        "independent_service",
        "standalone FastAPI route stack",
        prefix=True,
    ),
    ExcludedSourceRule(
        "astrmai/conversation/loop/scheduler_benchmark.py",
        "benchmark",
        "offline scheduler benchmark harness",
    ),
    ExcludedSourceRule(
        "astrmai/conversation/replay/",
        "replay",
        "offline conversation architecture replay package",
        prefix=True,
    ),
    ExcludedSourceRule(
        "astrmai/infrastructure/persistence/architecture_migration_audit.py",
        "ops",
        "one-shot database architecture migration audit CLI",
    ),
    ExcludedSourceRule(
        "astrmai/infrastructure/runtime/business_kpis.py",
        "ops",
        "offline KPI aggregation used only by test/audit callers",
    ),
    ExcludedSourceRule(
        "astrmai/infrastructure/runtime/context_economy_benchmark.py",
        "benchmark",
        "offline context economy report generator; runtime sample store is retained",
    ),
    ExcludedSourceRule(
        "astrmai/learning/evaluation/",
        "replay",
        "offline learning evaluation, gold sampling, and replay package",
        prefix=True,
    ),
)


def _match_exclusion(relative: str) -> ExcludedSourceRule | None:
    for rule in EXCLUDED_SOURCE_RULES:
        if relative == rule.path or (rule.prefix and relative.startswith(rule.path)):
            return rule
    return None


def _classify_included(relative: str) -> tuple[str, str]:
    if relative.startswith("pages/"):
        return "page", "Native Plugin Page static resource"
    if relative == "astrmai/webui/plugin_pages.py" or relative.startswith("astrmai/webui/backend/"):
        return "page", "Native Plugin Page request, adapter, database, or service dependency"
    if relative.endswith(".py"):
        return "runtime", "AstrBot plugin runtime Python module"
    return "resource", "plugin metadata, configuration, dependency declaration, or documentation"


def _runtime_sources() -> list[Path]:
    sources = [PROJECT_ROOT / name for name in ROOT_FILES]
    sources.extend(
        path
        for path in (PROJECT_ROOT / "astrmai").rglob("*.py")
        if not FORBIDDEN_PARTS.intersection(path.relative_to(PROJECT_ROOT).parts)
        and _match_exclusion(path.relative_to(PROJECT_ROOT).as_posix()) is None
    )
    sources.extend(
        path
        for path in (PROJECT_ROOT / "pages").rglob("*")
        if path.is_file() and path.suffix.lower() in PAGE_SUFFIXES
    )
    return sorted(set(sources))


def _excluded_sources() -> list[tuple[Path, ExcludedSourceRule]]:
    candidates = [PROJECT_ROOT / "scripts" / "audit_learning_data.py"]
    candidates.extend((PROJECT_ROOT / "astrmai").rglob("*.py"))
    excluded: list[tuple[Path, ExcludedSourceRule]] = []
    for source in candidates:
        if not source.is_file():
            continue
        relative = source.relative_to(PROJECT_ROOT).as_posix()
        rule = _match_exclusion(relative)
        if rule is not None:
            excluded.append((source, rule))
    return sorted(excluded, key=lambda item: item[0].relative_to(PROJECT_ROOT).as_posix())


def validate_release_candidate(output_dir: Path) -> list[str]:
    errors: list[str] = []
    if not output_dir.is_dir():
        return [f"release candidate does not exist: {output_dir}"]

    for name in ROOT_FILES:
        if not (output_dir / name).is_file():
            errors.append(f"missing required file: {name}")
    for name in REQUIRED_ARCHITECTURE_FILES:
        if not (output_dir / Path(name)).is_file():
            errors.append(f"missing architecture runtime file: {name}")
    for source, rule in _excluded_sources():
        relative = source.relative_to(PROJECT_ROOT)
        if (output_dir / relative).exists():
            errors.append(
                f"excluded {rule.classification} source present: {relative.as_posix()}"
            )

    for path in output_dir.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(output_dir)
        lowered_parts = {part.lower() for part in relative.parts}
        if lowered_parts.intersection(FORBIDDEN_PARTS):
            errors.append(f"forbidden path: {relative.as_posix()}")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f"forbidden file type: {relative.as_posix()}")
        if path.suffix.lower() in TEXT_SUFFIXES:
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                errors.append(f"non-UTF-8 release text file: {relative.as_posix()}")
                continue
            if any(pattern.search(content) for pattern in FORBIDDEN_TEXT_PATTERNS):
                errors.append(f"sensitive or machine-local text: {relative.as_posix()}")
        classification, _ = _classify_included(relative.as_posix())
        if classification not in {"runtime", "page", "resource"}:
            errors.append(f"unknown release classification: {relative.as_posix()}")

    if not any((output_dir / "astrmai").rglob("*.py")):
        errors.append("runtime package contains no AstrMai Python modules")
    if not (output_dir / "pages" / "admin" / "index.html").is_file():
        errors.append("Plugin Page entry is missing")
    return errors


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def manifest_path_for(output_dir: Path) -> Path:
    output_dir = Path(output_dir).resolve()
    return output_dir.parent / f"{output_dir.name}.candidate_manifest.json"


def _baseline_files(path: Path | None) -> tuple[dict[str, str], str | None]:
    if path is None:
        return {}, None
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_files = payload.get("files") or payload.get("candidate_files") or []
    files = {
        str(item["path"]): str(item.get("sha256", ""))
        for item in raw_files
        if isinstance(item, dict) and item.get("path")
    }
    return files, _sha256_file(Path(path))


def _manifest_payload(output_dir: Path, baseline_manifest: Path | None) -> dict[str, Any]:
    file_entries: list[dict[str, Any]] = []
    classification_counts = {"runtime": 0, "page": 0, "resource": 0}
    for path in sorted(item for item in output_dir.rglob("*") if item.is_file()):
        relative = path.relative_to(output_dir).as_posix()
        classification, reason = _classify_included(relative)
        classification_counts[classification] += 1
        file_entries.append(
            {
                "path": relative,
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
                "classification": classification,
                "disposition": "included",
                "reason": reason,
            }
        )

    excluded_entries = [
        {
            "path": source.relative_to(PROJECT_ROOT).as_posix(),
            "sha256": _sha256_file(source),
            "size": source.stat().st_size,
            "classification": rule.classification,
            "disposition": "excluded",
            "reason": rule.reason,
        }
        for source, rule in _excluded_sources()
    ]
    baseline_files, baseline_manifest_sha256 = _baseline_files(baseline_manifest)
    candidate_files = {item["path"]: item["sha256"] for item in file_entries}
    baseline_paths = set(baseline_files)
    candidate_paths = set(candidate_files)
    content_digest = _canonical_digest(
        [{"path": item["path"], "sha256": item["sha256"]} for item in file_entries]
    )
    manifest: dict[str, Any] = {
        "schema": "astrmai.release-candidate-manifest",
        "version": 1,
        "candidate": {
            "file_count": len(file_entries),
            "content_sha256": content_digest,
            "classification_counts": classification_counts,
        },
        "baseline": {
            "file_count": len(baseline_files),
            "manifest_sha256": baseline_manifest_sha256,
        },
        "comparison": {
            "added": sorted(candidate_paths - baseline_paths),
            "removed": sorted(baseline_paths - candidate_paths),
            "retained": sorted(candidate_paths & baseline_paths),
        },
        "files": file_entries,
        "excluded_files": excluded_entries,
        "unknown_files": [],
    }
    manifest["stable_digest_sha256"] = _canonical_digest(manifest)
    return manifest


def _write_release_manifest(
    output_dir: Path,
    *,
    baseline_manifest: Path | None = None,
) -> Path:
    manifest_path = manifest_path_for(output_dir)
    payload = _manifest_payload(output_dir, baseline_manifest)
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def validate_release_manifest(output_dir: Path, manifest_path: Path) -> list[str]:
    errors: list[str] = []
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid release manifest: {exc}"]

    entries = manifest.get("files") or []
    declared = {str(item.get("path", "")): item for item in entries if isinstance(item, dict)}
    actual = {
        path.relative_to(output_dir).as_posix(): path
        for path in output_dir.rglob("*")
        if path.is_file()
    }
    if set(declared) != set(actual):
        errors.append("manifest paths do not match candidate files")
    for relative, path in actual.items():
        item = declared.get(relative, {})
        if item.get("sha256") != _sha256_file(path):
            errors.append(f"manifest hash mismatch: {relative}")
        if item.get("size") != path.stat().st_size:
            errors.append(f"manifest size mismatch: {relative}")
        classification, _ = _classify_included(relative)
        if item.get("classification") != classification:
            errors.append(f"manifest classification mismatch: {relative}")
    if manifest.get("candidate", {}).get("file_count") != len(actual):
        errors.append("manifest candidate file count mismatch")
    if manifest.get("unknown_files"):
        errors.append("manifest contains unknown classifications")
    stable_digest = manifest.pop("stable_digest_sha256", None)
    if stable_digest != _canonical_digest(manifest):
        errors.append("manifest stable digest mismatch")
    return errors


def _copy_release_source(source: Path, target: Path) -> None:
    if source.suffix.lower() in TEXT_SUFFIXES:
        content = source.read_bytes()
        if content.startswith(b"\xef\xbb\xbf"):
            target.write_bytes(content[3:])
            shutil.copystat(source, target)
            return
    shutil.copy2(source, target)


def build_release_candidate(
    output_dir: Path,
    *,
    baseline_manifest: Path | None = None,
) -> Path:
    output_dir = output_dir.resolve()
    project_root = PROJECT_ROOT.resolve()
    if output_dir == project_root or project_root in output_dir.parents:
        raise ValueError("release output must be outside the source workspace")

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    for source in _runtime_sources():
        relative = source.relative_to(PROJECT_ROOT)
        target = output_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_release_source(source, target)

    errors = validate_release_candidate(output_dir)
    if errors:
        raise RuntimeError("invalid release candidate:\n- " + "\n- ".join(errors))
    manifest_path = _write_release_manifest(
        output_dir,
        baseline_manifest=baseline_manifest,
    )
    manifest_errors = validate_release_manifest(output_dir, manifest_path)
    if manifest_errors:
        raise RuntimeError("invalid release manifest:\n- " + "\n- ".join(manifest_errors))
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a clean AstrMai runtime release candidate.")
    parser.add_argument("output", type=Path, help="output directory outside the source workspace")
    parser.add_argument(
        "--baseline-manifest",
        type=Path,
        help="optional previous candidate manifest used for stable added/removed/retained comparison",
    )
    args = parser.parse_args()
    output = build_release_candidate(
        args.output,
        baseline_manifest=args.baseline_manifest,
    )
    file_count = sum(1 for path in output.rglob("*") if path.is_file())
    print(f"release candidate ready: {output} ({file_count} files)")
    print(f"manifest: {manifest_path_for(output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
