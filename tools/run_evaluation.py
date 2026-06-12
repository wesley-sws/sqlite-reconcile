#!/usr/bin/env python3
"""Run the evaluation artifact generators for sqlite-reconcile."""

from __future__ import annotations

import argparse
from pathlib import Path

from run_correctness_scenarios import DEFAULT_OUTPUT_DIR, run_correctness
from run_git_smoke import run_smoke
from run_main_benchmark import run_main_benchmark
from run_microbenchmark import DEFAULT_ROW_COUNTS, run_microbenchmark


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "all",
            "correctness",
            "performance",
            "main-benchmark",
            "microbenchmark",
            "smoke",
        ),
        help="evaluation artifact to generate",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="directory for generated JSON/CSV artifacts",
    )
    parser.add_argument(
        "--sizes",
        default="10,100,500",
        help="comma-separated transactions-per-branch sizes for the main benchmark",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="number of times to repeat each main benchmark workload",
    )
    parser.add_argument(
        "--micro-rows",
        default=",".join(str(value) for value in DEFAULT_ROW_COUNTS),
        help="comma-separated row/affected-row sizes for the microbenchmark",
    )
    parser.add_argument(
        "--micro-repeats",
        type=int,
        default=10,
        help="number of times to repeat each microbenchmark case",
    )
    parser.add_argument(
        "--keep-smoke-repo",
        action="store_true",
        help="keep the temporary Git smoke-test repository after the script exits",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.command in {"all", "correctness"}:
        correctness = run_correctness(args.output_dir)
        if any(result.status == "fail" for result in correctness):
            return 1

    if args.command in {"all", "performance", "main-benchmark"}:
        sizes = tuple(int(value.strip()) for value in args.sizes.split(",") if value)
        benchmark = run_main_benchmark(args.output_dir, sizes, repeats=args.repeats)
        if any(result.merge_exit_code != 0 for result in benchmark):
            return 1

    if args.command in {"all", "microbenchmark"}:
        rows = tuple(int(value.strip()) for value in args.micro_rows.split(",") if value)
        run_microbenchmark(
            args.output_dir,
            rows,
            repeats=args.micro_repeats,
        )

    if args.command in {"all", "smoke"}:
        smoke = run_smoke(args.output_dir, keep_repo=args.keep_smoke_repo)
        if smoke.status == "fail":
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
