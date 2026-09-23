"""CLI adapter for the offline learning evaluation replay runner."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astrmai.learning.evaluation.contracts import ReplayManifest
from astrmai.learning.evaluation.replay_runner import (
    ReplayInputError,
    ReplaySecurityError,
    run_replay,
    sample_manifest_hash,
    sha256_file,
    snapshot_integrity,
    validate_output_file,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a read-only deterministic/recorded learning replay")
    parser.add_argument("--source-snapshot", required=True)
    parser.add_argument("--memory-v2-snapshot")
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--seed", type=int, default=20260915)
    parser.add_argument("--sample-manifest", required=True)
    parser.add_argument("--recordings")
    parser.add_argument("--provider-allowlist", default="")
    parser.add_argument("--provider-id", default="none")
    parser.add_argument("--model-id", default="none")
    parser.add_argument("--pipeline-version", required=True)
    parser.add_argument("--extractor-version", required=True)
    parser.add_argument("--fingerprint-version", required=True)
    parser.add_argument("--prompt-version", default="none")
    parser.add_argument("--network-policy", choices=("loopback-only", "staging-allowlist"), default="loopback-only")
    parser.add_argument("--json-out", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary_path: Path | None = None
    if args.mode not in {"deterministic", "recorded", "staging"}:
        summary = {"status": "blocked", "reason": "unknown_replay_mode"}
        try:
            summary_path = validate_output_file(args.source_snapshot, args.json_out)
        except ReplaySecurityError as exc:
            summary = {"status": "blocked", "reason": str(exc)}
            print(json.dumps(summary, sort_keys=True))
            return 4
        try:
            summary_path.write_text(json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            summary_path = None
        print(json.dumps(summary, sort_keys=True))
        return 3
    try:
        source_input = Path(args.source_snapshot).expanduser()
        json_out = validate_output_file(source_input, args.json_out)
        source = source_input.resolve()
        memory_v2_input = Path(args.memory_v2_snapshot).expanduser() if args.memory_v2_snapshot else None
        if memory_v2_input is not None and memory_v2_input.is_symlink():
            raise ReplaySecurityError("memory v2 snapshot may not be a symlink")
        repo = Path(__file__).resolve().parents[1]
        try:
            json_out.relative_to(repo)
        except ValueError:
            pass
        else:
            raise ReplaySecurityError("json output must be outside repository")
        summary_path = json_out
        integrity = snapshot_integrity(source)
        memory_integrity = snapshot_integrity(memory_v2_input.resolve()) if memory_v2_input else None
        sample = Path(args.sample_manifest).expanduser().resolve()
        sample_hash = sample_manifest_hash(sample)
        if args.mode != "recorded" and args.recordings:
            raise ReplayInputError("recordings are only valid in recorded mode")
        if args.mode == "recorded" and (not args.recordings or args.provider_id == "none" or args.model_id == "none"):
            raise ReplayInputError("recorded mode requires recordings, provider-id, and model-id")
        if args.mode == "staging":
            # The manifest is still constructed so the runner can emit its
            # structured blocked result; no staging connection is attempted.
            pass
        created_at = __import__("datetime").datetime.fromtimestamp(
            int(integrity.get("mtime_ns", 0)) / 1_000_000_000,
            tz=__import__("datetime").timezone.utc,
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        manifest = ReplayManifest(
            run_id=f"eval-{integrity.get('sha256', '')[:12]}-{args.seed}", mode=args.mode,
            source_snapshot_hash=str(integrity.get("sha256") or "0" * 64), source_path_is_read_only=True,
            output_root=str(Path(args.output_root).expanduser()), source_schema_version=int(integrity.get("user_version", 0)),
            execution_schema_version=int(integrity.get("user_version", 0)), migration_path=(),
            pipeline_version=args.pipeline_version, extractor_version=args.extractor_version,
            fingerprint_version=args.fingerprint_version, prompt_version=args.prompt_version,
            provider_id=args.provider_id, model_id=args.model_id,
            request_fixture_hash=sha256_file(Path(args.recordings).expanduser().resolve()) if args.recordings else None,
            seed=args.seed,
            network_policy=args.network_policy, sample_manifest_hash=sample_hash,
            created_at=created_at,
            source_snapshot_path=str(source_input), sample_manifest_path=str(sample), recordings_path=args.recordings,
            provider_allowlist=tuple(item.strip() for item in args.provider_allowlist.split(",") if item.strip()),
            memory_v2_snapshot_path=(str(memory_v2_input.resolve()) if memory_v2_input else None),
            memory_v2_snapshot_hash=(memory_integrity["sha256"] if memory_integrity else None),
            memory_v2_schema_version=(
                int(memory_integrity["memory_v2_schema_version"])
                if memory_integrity and memory_integrity.get("memory_v2_schema_version") is not None else None
            ),
        )
        result = run_replay(manifest)
        summary = {"run_id": result.run_id, "status": result.status, "artifacts": dict(result.artifacts)}
        json_out.write_text(json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(summary, sort_keys=True))
        return 0 if result.status == "completed" else 2 if result.status == "partial" else 3
    except ReplaySecurityError as exc:
        summary = {"status": "blocked", "reason": str(exc)}
        if summary_path is not None:
            summary_path.write_text(json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(summary, sort_keys=True))
        return 4
    except (ReplayInputError, OSError, ValueError, json.JSONDecodeError) as exc:
        summary = {"status": "blocked", "reason": type(exc).__name__}
        if summary_path is not None:
            summary_path.write_text(json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(summary, sort_keys=True))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
