from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path


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
    "astrmai/conversation/replay/context_architecture_harness.py",
    "astrmai/infrastructure/persistence/architecture_migration_audit.py",
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


def _runtime_sources() -> list[Path]:
    sources = [PROJECT_ROOT / name for name in ROOT_FILES]
    sources.extend(
        path
        for path in (PROJECT_ROOT / "astrmai").rglob("*.py")
        if not FORBIDDEN_PARTS.intersection(path.relative_to(PROJECT_ROOT).parts)
    )
    sources.extend(
        path
        for path in (PROJECT_ROOT / "pages").rglob("*")
        if path.is_file() and path.suffix.lower() in PAGE_SUFFIXES
    )
    return sorted(set(sources))


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

    if not any((output_dir / "astrmai").rglob("*.py")):
        errors.append("runtime package contains no AstrMai Python modules")
    if not (output_dir / "pages" / "admin" / "index.html").is_file():
        errors.append("Plugin Page entry is missing")
    return errors


def build_release_candidate(output_dir: Path) -> Path:
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
        shutil.copy2(source, target)

    errors = validate_release_candidate(output_dir)
    if errors:
        raise RuntimeError("invalid release candidate:\n- " + "\n- ".join(errors))
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a clean AstrMai runtime release candidate.")
    parser.add_argument("output", type=Path, help="output directory outside the source workspace")
    args = parser.parse_args()
    output = build_release_candidate(args.output)
    file_count = sum(1 for path in output.rglob("*") if path.is_file())
    print(f"release candidate ready: {output} ({file_count} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
