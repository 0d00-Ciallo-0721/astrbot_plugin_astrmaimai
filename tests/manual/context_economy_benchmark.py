from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astrmai.infrastructure.runtime.context_economy_benchmark import (
    aggregate_benchmark_samples,
    build_benchmark_meta,
    load_benchmark_samples,
    write_benchmark_artifacts,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a Context Economy baseline report from benchmark samples.")
    parser.add_argument("--samples", required=True, help="Benchmark sample file path or parent directory.")
    parser.add_argument(
        "--output-root",
        default="artifacts/context_economy_benchmarks",
        help="Directory that receives benchmark baseline artifacts.",
    )
    parser.add_argument("--workload-family", default="", help="Optional exact workload family filter.")
    parser.add_argument("--template", default="", help="Optional template id/template key contains filter.")
    parser.add_argument("--start-ts", type=float, default=None, help="Optional lower bound for created_at.")
    parser.add_argument("--end-ts", type=float, default=None, help="Optional upper bound for created_at.")
    parser.add_argument("--label", default="baseline", help="Optional label appended to the output run id.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    samples = load_benchmark_samples(
        args.samples,
        start_ts=args.start_ts,
        end_ts=args.end_ts,
        workload_family=args.workload_family,
        template_filter=args.template,
    )
    summary = aggregate_benchmark_samples(samples)
    meta = build_benchmark_meta(
        sample_path=args.samples,
        sample_count=len(samples),
        start_ts=args.start_ts,
        end_ts=args.end_ts,
        workload_family=args.workload_family,
        template_filter=args.template,
    )
    run_dir = write_benchmark_artifacts(
        output_root=Path(args.output_root),
        summary=summary,
        meta=meta,
        label=args.label,
        repo_root=REPO_ROOT,
    )
    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
