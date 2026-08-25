"""Run bounded Provider probe tiers with a global call budget.

The command is dry-run by default. Pass ``--execute`` only after confirming the
current key, model route, and spend limit. Each case is run sequentially so a
failed/auth-expired tier can stop the matrix before the next tier starts.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "tests" / "manual" / "live_llm_probe.py"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _csv_ints(value: str, *, maximum: int = 8) -> list[int]:
    result: list[int] = []
    for item in str(value or "").split(","):
        try:
            parsed = int(item.strip())
        except ValueError:
            continue
        if parsed < 1 or parsed > maximum:
            raise ValueError(f"level must be between 1 and {maximum}: {parsed}")
        if parsed not in result:
            result.append(parsed)
    if not result:
        raise ValueError("at least one concurrency level is required")
    return result


def _cases(value: str) -> list[tuple[str, int]]:
    cases: list[tuple[str, int]] = []
    for item in str(value or "").split(","):
        profile, _, rounds = item.partition(":")
        profile = profile.strip().lower()
        if profile not in {"short", "medium", "long", "xlong"}:
            raise ValueError(f"unknown context profile: {profile}")
        try:
            round_count = max(1, int(rounds or "1"))
        except ValueError:
            raise ValueError(f"invalid rounds value: {rounds}") from None
        cases.append((profile, round_count))
    if not cases:
        raise ValueError("at least one matrix case is required")
    return cases


def build_cases(*, profiles: str, levels: list[int], calls_per_level: int, max_total_calls: int) -> list[dict[str, Any]]:
    cases = []
    planned = 0
    per_case = len(levels) * max(1, int(calls_per_level))
    for index, (profile, rounds) in enumerate(_cases(profiles), start=1):
        planned += per_case
        cases.append({
            "case_index": index,
            "context_profile": profile,
            "rounds": rounds,
            "levels": levels,
            "calls_per_level": max(1, int(calls_per_level)),
            "requested_calls": per_case,
            "cumulative_requested_calls": planned,
            "within_global_budget": planned <= max_total_calls,
        })
    return cases


def _manifest_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Provider Matrix Run",
        "",
        f"- status: `{payload['status']}`",
        f"- execute: `{payload['execute']}`",
        f"- max_total_calls: `{payload['max_total_calls']}`",
        f"- planned_calls: `{payload['planned_calls']}`",
        f"- started_calls: `{payload['started_calls']}`",
        "",
        "| Case | Profile | Rounds | Levels | Requested | Status | Run ID |",
        "| ---: | --- | ---: | --- | ---: | --- | --- |",
    ]
    for item in payload["cases"]:
        lines.append(
            f"| {item['case_index']} | {item['context_profile']} | {item['rounds']} | "
            f"{','.join(map(str, item['levels']))} | {item['requested_calls']} | "
            f"{item.get('status', 'planned')} | {item.get('run_id', '-')} |"
        )
    return "\n".join(lines) + "\n"


def run_matrix(args: argparse.Namespace) -> dict[str, Any]:
    levels = _csv_ints(args.levels)
    cases = build_cases(
        profiles=args.profiles,
        levels=levels,
        calls_per_level=args.calls_per_level,
        max_total_calls=args.max_total_calls,
    )
    planned_calls = sum(item["requested_calls"] for item in cases)
    payload: dict[str, Any] = {
        "schema_version": "provider-matrix-v1",
        "generated_at": _utc_now(),
        "execute": bool(args.execute),
        "status": "planned",
        "model": args.model or None,
        "levels": levels,
        "max_total_calls": args.max_total_calls,
        "planned_calls": planned_calls,
        "started_calls": 0,
        "cases": cases,
    }
    if planned_calls > args.max_total_calls:
        payload["status"] = "budget_plan_exceeded"
    elif not args.execute:
        payload["status"] = "dry_run"
    else:
        payload["status"] = "running"
        output_dir = Path(args.output_dir).resolve()
        for item in cases:
            if not item["within_global_budget"]:
                item["status"] = "budget_not_started"
                payload["status"] = "budget_exhausted"
                break
            command = [
                sys.executable,
                str(PROBE),
                "--levels", ",".join(map(str, levels)),
                "--calls-per-level", str(args.calls_per_level),
                "--max-calls", str(item["requested_calls"]),
                "--timeout-sec", str(args.timeout_sec),
                "--context-profile", item["context_profile"],
                "--rounds", str(item["rounds"]),
                "--max-tokens", str(args.max_tokens),
                "--output-dir", str(output_dir),
            ]
            if args.model:
                command.extend(["--model", args.model])
            for flag, enabled in (("--stream", args.stream), ("--json", args.json), ("--tool-call", args.tool_call), ("--vision", args.vision)):
                if enabled:
                    command.append(flag)
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
            item["returncode"] = completed.returncode
            item["command"] = [str(part) for part in command if part != str(args.output_dir)]
            public_stdout = completed.stdout.strip().splitlines()
            result = None
            for line in reversed(public_stdout):
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    result = candidate
                    break
            if result:
                item.update({
                    "status": result.get("status", "unknown"),
                    "run_id": result.get("run_dir", "").split("\\")[-1] or result.get("run_id"),
                    "budget": result.get("budget"),
                })
                payload["started_calls"] += int((result.get("budget") or {}).get("calls_started", 0) or 0)
            else:
                item["status"] = "runner_error"
            if item.get("status") != "passed":
                payload["status"] = "stopped_on_failure"
                break
        else:
            payload["status"] = "passed"
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Run bounded Provider context/concurrency matrix.")
    parser.add_argument("--profiles", default="medium:4,long:8,xlong:8", help="Comma-separated profile:rounds cases.")
    parser.add_argument("--levels", default="1,2,3,4")
    parser.add_argument("--calls-per-level", type=int, default=15)
    parser.add_argument("--max-total-calls", type=int, default=120)
    parser.add_argument("--timeout-sec", type=float, default=45.0)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--model", default="")
    parser.add_argument("--output-dir", default=str(ROOT / "artifacts" / "live_validation"))
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--tool-call", action="store_true")
    parser.add_argument("--vision", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Actually call the Provider; default is dry-run.")
    args = parser.parse_args()
    args.max_total_calls = max(1, int(args.max_total_calls))
    args.calls_per_level = max(1, int(args.calls_per_level))
    payload = run_matrix(args)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = output_dir / f"provider_matrix_{stamp}.json"
    md_path = output_dir / f"provider_matrix_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_manifest_markdown(payload), encoding="utf-8")
    print(json.dumps({"status": payload["status"], "planned_calls": payload["planned_calls"], "started_calls": payload["started_calls"], "json": str(json_path), "markdown": str(md_path)}))
    return 0 if payload["status"] in {"dry_run", "passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
