"""Aggregate public live-validation summaries without exposing secrets."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "live-rounds-aggregate-v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _public_summary(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    scope = str(payload.get("measurement_scope") or "unknown")
    item: dict[str, Any] = {
        "run_id": payload.get("run_id") or path.parent.name,
        "measurement_scope": scope,
        "status": payload.get("status"),
        "summary_path": str(path),
    }
    if scope == "provider_probe":
        item.update({
            "scenario": payload.get("scenario"),
            "requests_started": payload.get("requests_started", payload.get("total_requests", 0)),
            "success_count": payload.get("success_count", 0),
            "failure_count": payload.get("failure_count", 0),
            "timeout_count": payload.get("timeout_count", 0),
            "p50_ms": payload.get("p50_ms"),
            "p95_ms": payload.get("p95_ms"),
            "p99_ms": payload.get("p99_ms"),
            "observed_max_concurrency": payload.get("observed_max_concurrency"),
            "error_counts": {
                key: payload.get(key, 0)
                for key in (
                    "auth_error_count",
                    "region_error_count",
                    "network_error_count",
                    "read_timeout_count",
                    "connect_timeout_count",
                    "http_429_count",
                    "provider_error_count",
                    "invalid_response_count",
                )
                if payload.get(key, 0)
            },
        })
    else:
        item.update({
            "scenario_count": payload.get("scenario_count", 0),
            "passed_count": payload.get("passed_count", 0),
            "not_configured_count": payload.get("not_configured_count", 0),
            "measurement_incomplete_count": payload.get("measurement_incomplete_count", 0),
            "failed_count": payload.get("failed_count", 0),
            "turn_count": payload.get("turn_count", 0),
            "runtime_sample_count": payload.get("runtime_sample_count", 0),
            "peak_gateway_queue": payload.get("peak_gateway_queue"),
            "peak_background_active": payload.get("peak_background_active"),
        })
    return item


def aggregate(input_dir: Path) -> dict[str, Any]:
    rounds: list[dict[str, Any]] = []
    for path in sorted(input_dir.glob("*/summary.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("run_id"):
            rounds.append(_public_summary(path, payload))

    by_scope: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "runs": 0,
        "statuses": Counter(),
        "requests_started": 0,
        "success_count": 0,
        "failure_count": 0,
        "turn_count": 0,
        "runtime_sample_count": 0,
    })
    for item in rounds:
        bucket = by_scope[item["measurement_scope"]]
        bucket["runs"] += 1
        bucket["statuses"][str(item.get("status") or "unknown")] += 1
        for key in ("requests_started", "success_count", "failure_count", "turn_count", "runtime_sample_count"):
            bucket[key] += int(item.get(key) or 0)

    scopes = {}
    for scope, bucket in by_scope.items():
        scopes[scope] = {
            **{key: value for key, value in bucket.items() if key != "statuses"},
            "statuses": dict(bucket["statuses"]),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "input_dir": str(input_dir),
        "round_count": len(rounds),
        "scopes": scopes,
        "rounds": rounds,
    }


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Live Validation Aggregate",
        "",
        f"- generated_at: `{payload['generated_at']}`",
        f"- round_count: `{payload['round_count']}`",
        "",
        "| Scope | Runs | Statuses | Requests | Success | Failure | Turns | Runtime samples |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for scope, item in payload["scopes"].items():
        statuses = ", ".join(f"{key}={value}" for key, value in item["statuses"].items())
        lines.append(
            f"| {scope} | {item['runs']} | {statuses} | {item['requests_started']} | "
            f"{item['success_count']} | {item['failure_count']} | {item['turn_count']} | {item['runtime_sample_count']} |"
        )
    lines.extend(["", "| Run | Scope | Status | Requests/Scenarios | Success/Passed | Failure/Incomplete |", "| --- | --- | --- | ---: | ---: | ---: |"])
    for item in payload["rounds"]:
        if item["measurement_scope"] == "provider_probe":
            total = item.get("requests_started", 0)
            success = item.get("success_count", 0)
            failure = item.get("failure_count", 0)
        else:
            total = item.get("scenario_count", 0)
            success = item.get("passed_count", 0)
            failure = item.get("failed_count", 0) + item.get("measurement_incomplete_count", 0)
        lines.append(f"| {item['run_id']} | {item['measurement_scope']} | {item.get('status')} | {total} | {success} | {failure} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate public live-validation summary artifacts.")
    parser.add_argument("--input-dir", default="artifacts/live_validation")
    parser.add_argument("--output", default="artifacts/live_validation/rounds_aggregate.json")
    parser.add_argument("--markdown", default="artifacts/live_validation/rounds_aggregate.md")
    args = parser.parse_args()
    payload = aggregate(Path(args.input_dir).resolve())
    output = Path(args.output).resolve()
    markdown = Path(args.markdown).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown.write_text(_markdown(payload), encoding="utf-8")
    print(json.dumps({"round_count": payload["round_count"], "output": str(output), "markdown": str(markdown)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
