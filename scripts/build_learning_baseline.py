from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from audit_learning_data import (
        build_gold_sampling_frame,
        build_learning_funnel,
        build_memory_baseline,
        build_plugin_baseline,
        canonical_json_bytes,
        inspect_sqlite_source,
        inspect_trace_source,
        open_sqlite_read_only,
        snapshot_file_state,
        scan_artifact_for_sensitive_values,
        _trace_summary,
    )
except ModuleNotFoundError:
    from scripts.audit_learning_data import (
        build_gold_sampling_frame,
        build_learning_funnel,
        build_memory_baseline,
        build_plugin_baseline,
        canonical_json_bytes,
        inspect_sqlite_source,
        inspect_trace_source,
        open_sqlite_read_only,
        snapshot_file_state,
        scan_artifact_for_sensitive_values,
        _trace_summary,
    )


QUERY_CATALOG = {
    "version": "learning-baseline-v1",
    "queries": [
        {"query_id": "plugin.schema.inventory.v1", "source": "plugin_db", "window_kind": "as_of_snapshot"},
        {"query_id": "plugin.message.total.v1", "source": "plugin_db", "window_kind": "as_of_snapshot"},
        {"query_id": "plugin.message.window.v1", "source": "plugin_db", "window_kind": "event_window"},
        {"query_id": "plugin.message.structure.v1", "source": "plugin_db", "window_kind": "as_of_snapshot"},
        {"query_id": "plugin.message.sender_scope.v1", "source": "plugin_db", "window_kind": "as_of_snapshot"},
        {"query_id": "plugin.checkpoint.snapshot.v1", "source": "plugin_db", "window_kind": "as_of_snapshot"},
        {"query_id": "plugin.checkpoint.pending_count.v1", "source": "plugin_db", "window_kind": "as_of_snapshot"},
        {"query_id": "plugin.learning_run.status.v1", "source": "plugin_db", "window_kind": "as_of_snapshot"},
        {"query_id": "plugin.learning_run.funnel.v1", "source": "plugin_db", "window_kind": "as_of_snapshot"},
        {"query_id": "plugin.learning_run.diagnostics_completeness.v1", "source": "plugin_db", "window_kind": "as_of_snapshot"},
        {"query_id": "memory.schema.inventory.v1", "source": "memory_db", "window_kind": "as_of_snapshot"},
        {"query_id": "memory.learning_canonical.lifecycle.v1", "source": "memory_db", "window_kind": "as_of_snapshot"},
        {"query_id": "memory.learning_canonical.provenance.v1", "source": "memory_db", "window_kind": "as_of_snapshot"},
        {"query_id": "memory.learning_canonical.dedup.v1", "source": "memory_db", "window_kind": "as_of_snapshot"},
        {"query_id": "memory.consistency_repair.status.v1", "source": "memory_db", "window_kind": "as_of_snapshot"},
        {"query_id": "memory.retrieval.soft_join.v1", "source": "memory_db", "window_kind": "as_of_snapshot"},
        {"query_id": "trace.turn.outcome.v1", "source": "turn_trace", "window_kind": "event_window"},
        {"query_id": "trace.call.learning_workload.v1", "source": "turn_trace", "window_kind": "event_window"},
        {"query_id": "trace.parse_health.v1", "source": "turn_trace", "window_kind": "as_of_snapshot"},
        {"query_id": "cross_source.learning_funnel.v1", "source": "mixed", "window_kind": "as_of_snapshot"},
        {"query_id": "sampling.learning_frame.v1", "source": "plugin_db", "window_kind": "as_of_snapshot"},
    ],
}


def _parse_time(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.timestamp()


def _path_overlaps(left: Path, right: Path) -> bool:
    try:
        left = left.resolve()
        right = right.resolve()
        return left == right or left.is_relative_to(right) or right.is_relative_to(left)
    except OSError:
        return True


def _write_atomic(path: Path, payload: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def _artifact_hashes(output: Path) -> tuple[dict[str, dict[str, object]], int]:
    entries: dict[str, dict[str, object]] = {}
    for path in sorted(output.iterdir(), key=lambda item: item.name):
        if path.name.endswith(".tmp") or path.name == "baseline_manifest.json":
            continue
        data = path.read_bytes()
        entries[path.name] = {"relative_path": path.name, "sha256": hashlib.sha256(data).hexdigest(),
                              "rows": data.count(b"\n") if path.suffix == ".jsonl" else 0}
    return entries, sum(int(item["rows"]) for item in entries.values())


def _metric_status_counts(value: object) -> dict[str, int]:
    counts = {"observed": 0, "derived": 0, "partial": 0, "unavailable": 0}
    if isinstance(value, dict):
        for child in value.values():
            if isinstance(child, dict) and "status" in child:
                status = str(child["status"])
                if status in counts:
                    counts[status] += 1
            else:
                nested = _metric_status_counts(child)
                for key, count in nested.items():
                    counts[key] += count
    elif isinstance(value, list):
        for child in value:
            nested = _metric_status_counts(child)
            for key, count in nested.items():
                counts[key] += count
    return counts


def _schema_blocked(report: object) -> bool:
    if not isinstance(report, dict):
        return True
    schema = report.get("schema", {})
    if isinstance(schema, dict):
        for value in schema.values():
            if isinstance(value, dict) and value.get("status") in {"blocked", "unavailable"}:
                return True
        if schema.get("missing_required"):
            return True
    return False


def _portable_value(value: object, *, key: str = "") -> object:
    portable_keys = {"id", "chat_id", "sender_id", "group_id", "platform_id", "target_id", "candidate_id", "memory_id",
                     "source_row_id", "source_row_ids", "source_message_id", "source_message_ids", "event_id",
                     "platform_message_id", "dedup_key", "turn_id", "trace_id"}
    if (key in portable_keys or key.endswith("_id")) and value not in (None, ""):
        if isinstance(value, list):
            return [f"sha256:{hashlib.sha256(f'{key}:{item}'.encode('utf-8')).hexdigest()}" for item in value]
        return f"sha256:{hashlib.sha256(f'{key}:{value}'.encode('utf-8')).hexdigest()}"
    if isinstance(value, dict):
        return {name: _portable_value(child, key=name) for name, child in value.items()
                if name not in {"path", "source_paths", "mtime_ns", "generated_at", "generated_at_utc",
                                "artifact_path"}}
    if isinstance(value, list):
        return [_portable_value(child, key=key) for child in value]
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a read-only AstrMai learning baseline.")
    parser.add_argument("--plugin-db", type=Path, required=True)
    parser.add_argument("--memory-db", type=Path, required=True)
    parser.add_argument("--turn-trace", type=Path, required=True)
    parser.add_argument("--raw-trace", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--query-version", required=True)
    parser.add_argument("--code-revision", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.query_version != QUERY_CATALOG["version"]:
            return 3
        start, end = _parse_time(args.window_start), _parse_time(args.window_end)
        if start >= end or not (0 <= args.seed <= 2**63 - 1):
            raise ValueError("invalid window or seed")
        if len(args.code_revision) != 40 or any(c not in "0123456789abcdefABCDEF" for c in args.code_revision):
            raise ValueError("code revision must be a full commit hash")
        repo = Path(__file__).resolve().parents[1]
        sources = [args.plugin_db, args.memory_db, args.turn_trace, args.raw_trace]
        if any(not path.is_file() for path in sources):
            return 3
        artifact_root = args.artifact_root.expanduser().resolve()
        if _path_overlaps(artifact_root, repo) or any(_path_overlaps(artifact_root, path) for path in sources):
            return 4
        run_id = f"baseline-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
        output = artifact_root / run_id
        if output.exists():
            return 3
        artifact_root.mkdir(parents=True, exist_ok=True)
        output.mkdir(parents=True)
        window = (start, end)
        source_manifest = [inspect_sqlite_source(path, name) for path, name in ((args.plugin_db, "plugin_db"), (args.memory_db, "memory_db"))]
        if any(item.get("status") == "blocked" for item in source_manifest):
            return 4
        with open_sqlite_read_only(args.plugin_db) as db:
            plugin = build_plugin_baseline(db, window=window, query_catalog=QUERY_CATALOG)
        with open_sqlite_read_only(args.memory_db) as db:
            memory = build_memory_baseline(db, window=window, query_catalog=QUERY_CATALOG)
        trace_before = snapshot_file_state(args.turn_trace)
        raw_trace_before = snapshot_file_state(args.raw_trace)
        trace = _trace_summary(args.turn_trace, window)
        raw_trace = _trace_summary(args.raw_trace, window)
        trace_after = snapshot_file_state(args.turn_trace)
        raw_trace_after = snapshot_file_state(args.raw_trace)
        source_unchanged = all(item.get("unchanged") for item in source_manifest) and trace_before == trace_after and raw_trace_before == raw_trace_after
        turn_source = inspect_trace_source(args.turn_trace, "turn_trace", trace, before=trace_before, after=trace_after)
        raw_source = inspect_trace_source(args.raw_trace, "raw_trace", raw_trace, before=raw_trace_before, after=raw_trace_after)
        frame_rows = plugin.get("candidates", [])
        frame = build_gold_sampling_frame(frame_rows, seed=args.seed, query_version=args.query_version)
        funnel = build_learning_funnel(plugin, memory, {"selected": trace.get("selected", [])})
        checkpoint_lag = "".join(json.dumps(_portable_value(row), ensure_ascii=False, sort_keys=True) + "\n" for row in plugin.get("checkpoint_backlog", []))
        unknowns = []
        for name, summary in (("turn_trace", trace), ("raw_trace", raw_trace)):
            for issue, count in (("invalid_record", summary.get("invalid_count", 0)),
                                 ("oversized_record", summary.get("oversized_count", 0)),
                                 ("duplicate_conflict", summary.get("conflict_count", 0)),
                                 ("unknown_time", summary.get("unknown_time_count", 0)),
                                 ("unknown_identity", summary.get("unknown_identity_count", 0))):
                if count:
                    unknowns.append({"source": name, "query_id": "trace.parse_health.v1", "reason": issue, "count": count})
        unknowns_payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in unknowns)
        artifacts = {
            "baseline_metrics.json": _portable_value({"plugin": plugin, "memory": memory, "trace": {"turn": trace, "raw": raw_trace}}),
            "checkpoint_lag.jsonl": checkpoint_lag,
            "learning_funnel.json": _portable_value(funnel),
            "gold_sampling_frame.jsonl": "".join(json.dumps(_portable_value(row), ensure_ascii=False, sort_keys=True) + "\n" for row in frame),
            "unknowns.jsonl": unknowns_payload,
            "query_catalog.json": {**QUERY_CATALOG, "version": args.query_version},
            "run_checks.json": {
                "source_unchanged": all(item.get("unchanged") for item in source_manifest) and trace_before == trace_after and raw_trace_before == raw_trace_after,
                "trace_source_unchanged": trace_before == trace_after and raw_trace_before == raw_trace_after,
                "provider_calls": 0,
                "runtime_start_count": 0,
                "database_write_count": 0,
                "source_paths": {"plugin_db": str(args.plugin_db.resolve()), "memory_db": str(args.memory_db.resolve()), "turn_trace": str(args.turn_trace.resolve()), "raw_trace": str(args.raw_trace.resolve())},
                "source_before": {"turn_trace": trace_before, "raw_trace": raw_trace_before},
                "source_after": {"turn_trace": trace_after, "raw_trace": raw_trace_after},
            },
            "baseline_summary.md": "# Learning baseline\n\nstatus: observed\n",
        }
        for name, value in artifacts.items():
            path = output / name
            payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            _write_atomic(path, payload)
        findings = []
        for path in output.iterdir():
            if path.name in {"run_checks.json", "baseline_manifest.json"} or path.suffix == ".tmp":
                continue
            findings.extend(scan_artifact_for_sensitive_values(path))
        artifact_entries, _ = _artifact_hashes(output)
        manifest_path = output / "baseline_manifest.json"
        portable_input = {"schema_version": "learning-baseline-manifest-v1", "query_version": args.query_version,
                          "code_revision": args.code_revision, "window": [start, end], "seed": args.seed,
                          "sources": [_portable_value(item) for item in source_manifest + [turn_source, raw_source]],
                          "artifacts": {name: item["sha256"] for name, item in artifact_entries.items() if name != "run_checks.json"}}
        status_counts = _metric_status_counts({"plugin": plugin, "memory": memory})
        incomplete = status_counts["partial"] > 0 or status_counts["unavailable"] > 0 or bool(unknowns) or any(
            edge.get("status") in {"partial", "unavailable"} for edge in funnel.get("edges", {}).values() if isinstance(edge, dict)
        ) or any(source.get("status") in {"partial", "unavailable"} for source in (turn_source, raw_source))
        blocked = (not source_unchanged or any(item.get("status") == "blocked" for item in source_manifest)
                   or _schema_blocked(plugin) or _schema_blocked(memory) or bool(findings))
        manifest = {
            "schema_version": "learning-baseline-manifest-v1",
            "run_id": run_id, "status": "blocked" if blocked else ("partial" if incomplete else "passed"),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "identity": {"code_revision": args.code_revision, "working_tree_dirty": None, "query_version": args.query_version, "seed": args.seed, "config_fingerprint": None},
            "window": {"start_utc": args.window_start, "end_utc": args.window_end, "interval": "[start,end)"},
            "sources": [{key: value for key, value in item.items() if key != "path"} for item in source_manifest] + [turn_source, raw_source],
            "artifacts": [{"logical_name": name.rsplit(".", 1)[0], **entry} for name, entry in artifact_entries.items()],
            "metric_summary": {**status_counts, "unknown_total": len(unknowns)},
            "safety": {"database_write_count": 0, "provider_call_count": 0, "runtime_start_count": 0, "source_changed": not source_unchanged, "pii_scan_findings": len(findings)},
            "trace_counts": {"turn": _portable_value(trace), "raw": _portable_value(raw_trace)},
            "warnings": [],
            "portable_manifest_hash": hashlib.sha256(canonical_json_bytes(portable_input)).hexdigest(),
        }
        manifest_tmp = output / "baseline_manifest.json.tmp"
        manifest_tmp.write_bytes(canonical_json_bytes(manifest) + b"\n")
        manifest_tmp.replace(manifest_path)
        if findings:
            shutil.rmtree(output, ignore_errors=True)
            return 4
        print(json.dumps({"run_id": run_id, "status": manifest["status"], "artifact_path": str(output), "portable_manifest_hash": manifest["portable_manifest_hash"]}, ensure_ascii=False))
        return 0 if manifest["status"] == "passed" else 2
    except (OSError, ValueError, TypeError, json.JSONDecodeError, sqlite3.Error):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
