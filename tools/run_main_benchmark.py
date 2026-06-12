#!/usr/bin/env python3
"""Run the synthetic end-to-end merge benchmark."""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import tempfile
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, stdev


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "evaluation" / "results"
MAIN_BENCHMARK_WORKLOADS = (
    "no_overlap",
    "overlap_10",
    "overlap_30",
    "overlap_70",
    "dense_overlap",
)
MIN_BENCHMARK_ROWS = 50
PAIR_CHECK_FRACTIONS = {
    "overlap_10": 0.10,
    "overlap_30": 0.30,
    "overlap_70": 0.70,
}

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from merge import log_merge, remaining_metadata, terminal_mergetool  # noqa: E402
from merge.control_db import _open_merge_working_context  # noqa: E402
from merge.models import LoggedTransaction, SchemaCache  # noqa: E402


@dataclass(frozen=True)
class MainBenchmarkResult:
    workload: str
    transactions_per_branch: int
    repeats: int
    elapsed_seconds: float
    elapsed_stdev_seconds: float
    naive_pair_slots: int
    metadata_index_checks: int
    suffix_scans_started: int
    individual_transaction_checks: int
    pairs_skipped_by_metadata: int
    merge_exit_code: int


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="directory for generated CSV artifacts",
    )
    parser.add_argument(
        "--sizes",
        default="10,100,500",
        help="comma-separated transactions-per-branch sizes",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="number of times to repeat each synthetic benchmark",
    )
    args = parser.parse_args()

    sizes = tuple(int(value.strip()) for value in args.sizes.split(",") if value)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = run_main_benchmark(args.output_dir, sizes, repeats=args.repeats)
    return 1 if any(result.merge_exit_code != 0 for result in results) else 0


def run_main_benchmark(
    output_dir: Path,
    sizes: tuple[int, ...],
    *,
    repeats: int,
) -> list[MainBenchmarkResult]:
    if repeats < 1:
        raise ValueError("--repeats must be at least 1")

    results: list[MainBenchmarkResult] = []
    for size in sizes:
        for workload in MAIN_BENCHMARK_WORKLOADS:
            results.append(_measure_benchmark_case(size, workload, repeats=repeats))

    _write_csv(output_dir / "main_benchmark_results.csv", results)
    return results


def _measure_benchmark_case(
    size: int,
    workload: str,
    *,
    repeats: int,
) -> MainBenchmarkResult:
    timings: list[float] = []
    exit_code = 0
    stats: dict[str, int] | None = None

    for _ in range(repeats):
        elapsed, exit_code, run_stats = _measure_single_merge_trial(size, workload)
        timings.append(elapsed)
        stats = run_stats

    assert stats is not None
    return MainBenchmarkResult(
        workload=workload,
        transactions_per_branch=size,
        repeats=repeats,
        elapsed_seconds=mean(timings),
        elapsed_stdev_seconds=stdev(timings) if len(timings) > 1 else 0.0,
        naive_pair_slots=stats["naive_pair_slots"],
        metadata_index_checks=stats["metadata_index_checks"],
        suffix_scans_started=stats["suffix_scans_started"],
        individual_transaction_checks=stats["individual_transaction_checks"],
        pairs_skipped_by_metadata=(
            stats["naive_pair_slots"] - stats["individual_transaction_checks"]
        ),
        merge_exit_code=exit_code,
    )


def _measure_single_merge_trial(
    size: int,
    workload: str,
) -> tuple[float, int, dict[str, int]]:
    with tempfile.TemporaryDirectory(prefix="sqlite_reconcile_perf_") as tmp_name:
        tmp = Path(tmp_name)
        base_path = tmp / "base.db"
        merged_path = tmp / "merged.db"
        row_count = max(size * 2, MIN_BENCHMARK_ROWS)
        _create_benchmark_base(base_path, row_count=row_count)
        schema_cache = _schema_cache_for(base_path)
        ours, theirs = _benchmark_transactions(base_path, size, workload)

        with _open_merge_working_context(base_path, schema_cache) as context:
            stats = _metadata_filter_stats(context, ours, theirs)

        start = time.perf_counter()
        exit_code = terminal_mergetool._resolve_merge_transactions(
            base_path=base_path,
            merged_path=merged_path,
            ours_transactions=ours,
            theirs_transactions=theirs,
            schema_cache=schema_cache,
        )
        elapsed = time.perf_counter() - start

        return elapsed, exit_code, stats


def _create_benchmark_base(path: Path, *, row_count: int) -> None:
    with sqlite3.connect(path) as con:
        _create_log_tables(con)
        con.execute("CREATE TABLE local_items (id INTEGER PRIMARY KEY, value INTEGER)")
        con.execute("CREATE TABLE remote_items (id INTEGER PRIMARY KEY, value INTEGER)")
        con.execute("CREATE TABLE shared_items (id INTEGER PRIMARY KEY, value INTEGER)")
        con.executemany(
            "INSERT INTO local_items VALUES (?, 0)",
            ((index,) for index in range(1, row_count + 1)),
        )
        con.executemany(
            "INSERT INTO remote_items VALUES (?, 0)",
            ((index,) for index in range(1, row_count + 1)),
        )
        con.executemany(
            "INSERT INTO shared_items VALUES (?, 0)",
            ((index,) for index in range(1, row_count + 1)),
        )
        con.commit()


def _benchmark_transactions(
    base_path: Path,
    size: int,
    workload: str,
) -> tuple[list[LoggedTransaction], list[LoggedTransaction]]:
    table_columns, _, _ = log_merge.load_schema_metadata_from_db(base_path)
    ours_statements = []
    theirs_statements = []
    shared_suffix_count = _shared_suffix_count(size, workload)
    for index in range(1, size + 1):
        is_shared_statement = index > size - shared_suffix_count
        if workload == "no_overlap":
            ours_sql = f"UPDATE local_items SET value = value + 1 WHERE id = {index}"
            theirs_sql = f"UPDATE remote_items SET value = value + 1 WHERE id = {index}"
        elif workload == "dense_overlap" or is_shared_statement:
            ours_sql = f"UPDATE shared_items SET value = value + 1 WHERE id = {index}"
            theirs_sql = (
                "UPDATE shared_items SET value = value + 1 "
                f"WHERE id = {size + index}"
            )
        else:
            ours_sql = f"UPDATE local_items SET value = value + 1 WHERE id = {index}"
            theirs_sql = f"UPDATE remote_items SET value = value + 1 WHERE id = {index}"

        ours_statements.append(
            log_merge.make_logged_statement(
                branch="ours",
                branch_index=index - 1,
                transaction_id=index,
                committed_at="2026-01-01T00:00:00",
                sql_text=ours_sql,
                table_columns=table_columns,
            )
        )
        theirs_statements.append(
            log_merge.make_logged_statement(
                branch="theirs",
                branch_index=index - 1,
                transaction_id=index,
                committed_at="2026-01-01T00:00:00",
                sql_text=theirs_sql,
                table_columns=table_columns,
            )
        )
    return (
        log_merge.group_logged_transactions(ours_statements),
        log_merge.group_logged_transactions(theirs_statements),
    )


def _shared_suffix_count(size: int, workload: str) -> int:
    """Choose a shared-table suffix giving roughly the target kept-pair fraction."""

    target_fraction = PAIR_CHECK_FRACTIONS.get(workload)
    if target_fraction is None:
        return 0

    target_pairs = round((size * size) * target_fraction)
    return min(
        range(size + 1),
        key=lambda count: abs((count * count) - target_pairs),
    )


def _metadata_filter_stats(
    context,
    ours: list[LoggedTransaction],
    theirs: list[LoggedTransaction],
) -> dict[str, int]:
    remaining_ours: deque[LoggedTransaction] = deque(ours)
    remaining_theirs: deque[LoggedTransaction] = deque(theirs)
    indexes = {
        "ours": remaining_metadata.RemainingMetadataIndex.from_transactions(
            context,
            remaining_ours,
        ),
        "theirs": remaining_metadata.RemainingMetadataIndex.from_transactions(
            context,
            remaining_theirs,
        ),
    }
    stats = {
        "naive_pair_slots": 0,
        "metadata_index_checks": 0,
        "suffix_scans_started": 0,
        "individual_transaction_checks": 0,
    }
    while remaining_ours or remaining_theirs:
        for branch in ("ours", "theirs"):
            current_queue = remaining_ours if branch == "ours" else remaining_theirs
            other_queue = remaining_theirs if branch == "ours" else remaining_ours
            other_branch = "theirs" if branch == "ours" else "ours"
            if not current_queue:
                continue
            current = current_queue[0]
            stats["metadata_index_checks"] += 1
            stats["naive_pair_slots"] += len(other_queue)
            kinds = remaining_metadata.remaining_individual_check_kinds(
                context,
                current,
                indexes[other_branch],
            )
            if kinds:
                stats["suffix_scans_started"] += 1
                stats["individual_transaction_checks"] += len(other_queue)
            indexes[branch].remove_transaction(context, current)
            current_queue.popleft()
    return stats


def _schema_cache_for(path: Path) -> SchemaCache:
    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(path)
    )
    return SchemaCache(
        table_columns=table_columns,
        primary_key_columns=primary_key_columns,
        key_column_sets=key_column_sets,
    )


def _create_log_tables(con: sqlite3.Connection) -> None:
    con.execute(
        f"""
        CREATE TABLE {log_merge.TX_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    con.execute(
        f"""
        CREATE TABLE {log_merge.LOG_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL REFERENCES {log_merge.TX_TABLE}(id),
            original_sql_text TEXT NOT NULL,
            to_replay_sql_text TEXT NOT NULL,
            is_replay_safe INTEGER NOT NULL DEFAULT 1,
            replay_block_reason TEXT
        )
        """
    )


def _write_csv(path: Path, results: list[MainBenchmarkResult]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(asdict(results[0]).keys()))
        writer.writeheader()
        for result in results:
            writer.writerow(asdict(result))


if __name__ == "__main__":
    raise SystemExit(main())
