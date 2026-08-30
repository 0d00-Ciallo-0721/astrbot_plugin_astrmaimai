"""Single-core calibration benchmark for AstrMai startup checkpoints.

This is a local, provider-free benchmark.  It exercises the production
``MemoryEngine._startup_checkpoint`` implementation while applying a
deterministic Python transformation workload that approximates startup
metadata/import work.  Results are advisory and should be recorded together
with the CPU model and affinity information; they are not a production SLO by
themselves.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - manual tool dependency
    psutil = None

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass(slots=True)
class BenchmarkOptions:
    records: tuple[int, ...] = (1000, 5000, 10000)
    runs: int = 10
    warmup_runs: int = 2
    batch_size: int = 32
    cpu_slice_ms: float = 25.0
    yield_sec: float = 0.001
    heartbeat_interval_sec: float = 0.01
    cpu_index: int = 0
    apply_cpu_affinity: bool = True
    work_rounds: int = 4
    report_path: str = ""


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * ratio)))
    return round(ordered[index], 3)


def _set_affinity(options: BenchmarkOptions) -> tuple[bool, list[int] | None]:
    if not options.apply_cpu_affinity or psutil is None:
        return False, None
    process = psutil.Process()
    available = list(process.cpu_affinity())
    if options.cpu_index not in available:
        raise ValueError(
            f"cpu_index={options.cpu_index} is unavailable; available={available}"
        )
    process.cpu_affinity([options.cpu_index])
    return True, list(process.cpu_affinity())


def _cpu_transform(index: int, rounds: int) -> str:
    value = f"astrmai-startup-record-{index}".encode("utf-8")
    for round_index in range(max(1, rounds)):
        value = hashlib.sha256(value + round_index.to_bytes(2, "little")).digest()
    return value.hex()


async def _run_case(options: BenchmarkOptions, records: int) -> dict[str, float | int]:
    from astrmai.memory.services.memory_engine import MemoryEngine

    engine = MemoryEngine.__new__(MemoryEngine)
    engine.STARTUP_CPU_SLICE_MS = float(options.cpu_slice_ms)
    engine.STARTUP_YIELD_SEC = float(options.yield_sec)
    engine._startup_last_yield = time.monotonic()
    engine._startup_yield_count = 0

    stop = asyncio.Event()
    heartbeat_lag_ms: list[float] = []

    async def heartbeat() -> None:
        deadline = time.monotonic() + options.heartbeat_interval_sec
        while not stop.is_set():
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            now = time.monotonic()
            heartbeat_lag_ms.append(max(0.0, now - deadline) * 1000.0)
            deadline += options.heartbeat_interval_sec

    heartbeat_task = asyncio.create_task(heartbeat(), name="startup-benchmark-heartbeat")
    started = time.monotonic()
    checksum = ""
    try:
        for start in range(0, records, max(1, options.batch_size)):
            end = min(records, start + max(1, options.batch_size))
            for index in range(start, end):
                checksum = _cpu_transform(index, options.work_rounds)
            await engine._startup_checkpoint(force=True)
    finally:
        stop.set()
        await asyncio.gather(heartbeat_task, return_exceptions=True)

    elapsed_ms = (time.monotonic() - started) * 1000.0
    return {
        "records": records,
        "elapsed_ms": round(elapsed_ms, 3),
        "yield_count": int(engine._startup_yield_count),
        "heartbeat_samples": len(heartbeat_lag_ms),
        "heartbeat_p50_ms": _percentile(heartbeat_lag_ms, 0.50),
        "heartbeat_p95_ms": _percentile(heartbeat_lag_ms, 0.95),
        "heartbeat_p99_ms": _percentile(heartbeat_lag_ms, 0.99),
        "heartbeat_max_ms": round(max(heartbeat_lag_ms), 3) if heartbeat_lag_ms else 0.0,
        "checksum_tail": checksum[-12:],
    }


async def _run(options: BenchmarkOptions) -> dict[str, object]:
    if options.runs < 1 or options.warmup_runs < 0:
        raise ValueError("runs must be positive and warmup_runs cannot be negative")
    if options.batch_size < 1 or options.cpu_slice_ms <= 0 or options.yield_sec < 0:
        raise ValueError("batch_size and cpu_slice_ms must be positive; yield_sec cannot be negative")
    affinity_applied, affinity = _set_affinity(options)
    all_results: list[dict[str, float | int]] = []
    for _ in range(options.warmup_runs):
        for records in options.records:
            await _run_case(options, records)
    for _ in range(options.runs):
        for records in options.records:
            all_results.append(await _run_case(options, records))

    grouped: dict[str, list[dict[str, float | int]]] = {}
    for result in all_results:
        grouped.setdefault(str(result["records"]), []).append(result)
    summaries: dict[str, dict[str, float | int]] = {}
    for records, values in grouped.items():
        summaries[records] = {
            "runs": len(values),
            "elapsed_p50_ms": round(statistics.median(float(v["elapsed_ms"]) for v in values), 3),
            "elapsed_p95_ms": _percentile([float(v["elapsed_ms"]) for v in values], 0.95),
            "yield_count_median": int(statistics.median(int(v["yield_count"]) for v in values)),
            "heartbeat_p95_ms": _percentile([float(v["heartbeat_p95_ms"]) for v in values], 0.95),
            "heartbeat_p99_ms": _percentile([float(v["heartbeat_p99_ms"]) for v in values], 0.99),
            "heartbeat_max_ms": round(max(float(v["heartbeat_max_ms"]) for v in values), 3),
        }
    return {
        "benchmark": "single_cpu_startup_checkpoint",
        "options": asdict(options),
        "affinity_applied": affinity_applied,
        "affinity": affinity,
        "results": all_results,
        "summaries": summaries,
    }


def _parse_args() -> BenchmarkOptions:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", nargs="+", type=int, default=[1000, 5000, 10000])
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--warmup-runs", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--cpu-slice-ms", type=float, default=25.0)
    parser.add_argument("--yield-sec", type=float, default=0.001)
    parser.add_argument("--heartbeat-interval-sec", type=float, default=0.01)
    parser.add_argument("--cpu-index", type=int, default=0)
    parser.add_argument("--no-cpu-affinity", action="store_true")
    parser.add_argument("--work-rounds", type=int, default=4)
    parser.add_argument("--report", dest="report_path", default="")
    args = parser.parse_args()
    return BenchmarkOptions(
        records=tuple(args.records),
        runs=args.runs,
        warmup_runs=args.warmup_runs,
        batch_size=args.batch_size,
        cpu_slice_ms=args.cpu_slice_ms,
        yield_sec=args.yield_sec,
        heartbeat_interval_sec=args.heartbeat_interval_sec,
        cpu_index=args.cpu_index,
        apply_cpu_affinity=not args.no_cpu_affinity,
        work_rounds=args.work_rounds,
        report_path=args.report_path,
    )


def main() -> int:
    options = _parse_args()
    report = asyncio.run(_run(options))
    report_path = Path(options.report_path) if options.report_path else REPO_ROOT / "artifacts" / "single_cpu_startup_benchmark.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path.resolve()), "summaries": report["summaries"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
