#!/usr/bin/env python3
"""Run focused microbenchmarks for sqlite-reconcile conflict checking.

The main evaluation benchmark measures the whole fixed-order merge loop. This
script isolates three smaller costs:

- statement metadata extraction;
- static transaction-pair checking once metadata exists;
- execution-based refinement after static overlap is known;
- the pair-check pipeline for one statically overlapping transaction pair.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, stdev


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "evaluation" / "results"
DEFAULT_ROW_COUNTS = (10, 100, 1000, 10000, 100000)
DEFAULT_REPEATS = 10
INNER_LOOPS = {
    "metadata": 50,
    "static": 1000,
}

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from merge import log_merge  # noqa: E402
from merge.conflict_detection import OrderedRemainingConflictScanner  # noqa: E402
from merge.control_db import _open_merge_working_context  # noqa: E402
from merge.models import (  # noqa: E402
    BranchName,
    ConflictCheckResult,
    ConflictKind,
    LoggedTransaction,
)
from merge.remaining_execution import OrderedRemainingExecutionScanner  # noqa: E402
from merge.sql_metadata import parse_statement_metadata_for_context  # noqa: E402
from merge.static_analysis import (  # noqa: E402
    static_analysis_matching,
)


@dataclass(frozen=True)
class StatementPair:
    name: str
    current_sql: str
    other_sql: str
    static_kinds: tuple[ConflictKind, ...]


@dataclass(frozen=True)
class MicrobenchmarkResult:
    case: str
    rows: int
    repeats: int
    metadata_pair_mean_ms: float
    metadata_pair_stdev_ms: float
    static_pair_mean_ms: float
    static_pair_stdev_ms: float
    probe_refinement_mean_ms: float
    probe_refinement_stdev_ms: float
    pair_check_pipeline_mean_ms: float
    pair_check_pipeline_stdev_ms: float
    static_conflict_kinds: str
    probe_conflict_kinds: str
    pipeline_conflict_kinds: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="directory for generated CSV artifacts",
    )
    parser.add_argument(
        "--rows",
        default=",".join(str(value) for value in DEFAULT_ROW_COUNTS),
        help="comma-separated row/affected-row sizes",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=DEFAULT_REPEATS,
        help="number of outer repeats for each case",
    )
    args = parser.parse_args()

    if args.repeats < 1:
        raise ValueError("--repeats must be at least 1")

    rows = tuple(int(value.strip()) for value in args.rows.split(",") if value)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    run_microbenchmark(args.output_dir, rows, repeats=args.repeats)
    return 0


def run_microbenchmark(
    output_dir: Path,
    rows: tuple[int, ...],
    *,
    repeats: int,
) -> list[MicrobenchmarkResult]:
    if repeats < 1:
        raise ValueError("--repeats must be at least 1")

    results: list[MicrobenchmarkResult] = []
    for row_count in rows:
        for pair in _statement_pairs(row_count):
            results.append(
                _measure_statement_pair_case(pair, row_count, repeats=repeats)
            )
    _write_csv(output_dir / "microbenchmark_results.csv", results)
    return results


def _measure_statement_pair_case(
    pair: StatementPair,
    row_count: int,
    *,
    repeats: int,
) -> MicrobenchmarkResult:
    """Measure all microbenchmark stages for one SQL-pair shape and row count."""

    with tempfile.TemporaryDirectory(prefix="sqlite_reconcile_microbench_") as tmp_name:
        db_path = Path(tmp_name) / "base.db"
        _create_base_database(db_path, row_count=row_count)
        with sqlite3.connect(db_path) as schema_con:
            schema_cache = log_merge.load_schema_cache(schema_con.cursor())

        with _open_merge_working_context(db_path, schema_cache) as context:
            # Convert the two benchmark SQL statements into the same
            # LoggedTransaction objects used by the merge implementation.
            current = _transaction_for_sql(
                context,
                branch="ours",
                transaction_id=1,
                sql_text=pair.current_sql,
            )
            other = _transaction_for_sql(
                context,
                branch="theirs",
                transaction_id=1,
                sql_text=pair.other_sql,
            )
            static_result = _static_result_for_pair(context, current, other, pair)
            static_conflict_kinds = _conflict_kinds(static_result)

            # Metadata and static timings are repeated many times because they are
            # cheap enough for timer noise to matter at small row counts.
            metadata_timings = _repeat_timing(
                repeats,
                INNER_LOOPS["metadata"],
                lambda: (
                    parse_statement_metadata_for_context(
                        pair.current_sql,
                        context,
                    ),
                    parse_statement_metadata_for_context(
                        pair.other_sql,
                        context,
                    ),
                ),
            )
            static_timings = _repeat_timing(
                repeats,
                INNER_LOOPS["static"],
                lambda: static_analysis_matching(
                    context,
                    current.metadata,
                    other.metadata,
                    enabled_kinds=pair.static_kinds,
                    current_branch="ours",
                ),
            )
            probe_timings, probe_result = _time_probe_refinement_stage(
                context,
                current,
                other,
                static_result,
                repeats=repeats,
            )
            pipeline_timings, pipeline_result = _time_pair_check_pipeline(
                context,
                current,
                other,
                pair,
                repeats=repeats,
            )

    return MicrobenchmarkResult(
        case=pair.name,
        rows=row_count,
        repeats=repeats,
        metadata_pair_mean_ms=mean(metadata_timings) * 1000,
        metadata_pair_stdev_ms=(
            stdev(metadata_timings) * 1000 if len(metadata_timings) > 1 else 0.0
        ),
        static_pair_mean_ms=mean(static_timings) * 1000,
        static_pair_stdev_ms=(
            stdev(static_timings) * 1000 if len(static_timings) > 1 else 0.0
        ),
        probe_refinement_mean_ms=mean(probe_timings) * 1000,
        probe_refinement_stdev_ms=(
            stdev(probe_timings) * 1000 if len(probe_timings) > 1 else 0.0
        ),
        pair_check_pipeline_mean_ms=mean(pipeline_timings) * 1000,
        pair_check_pipeline_stdev_ms=(
            stdev(pipeline_timings) * 1000
            if len(pipeline_timings) > 1
            else 0.0
        ),
        static_conflict_kinds=static_conflict_kinds,
        probe_conflict_kinds=_conflict_kinds(probe_result),
        pipeline_conflict_kinds=_conflict_kinds(pipeline_result),
    )


def _statement_pairs(row_count: int) -> tuple[StatementPair, ...]:
    current_range = f"id BETWEEN 1 AND {row_count}"
    return (
        StatementPair(
            name="simple_write_write",
            current_sql=(
                "UPDATE items "
                "SET value = value + 1 "
                f"WHERE {current_range}"
            ),
            other_sql=(
                "UPDATE items "
                "SET value = value + 1 "
                f"WHERE {current_range}"
            ),
            static_kinds=("write_write",),
        ),
        StatementPair(
            name="aggregate_write_read",
            current_sql=(
                "UPDATE orders "
                "SET amount = amount + 1 "
                f"WHERE {current_range}"
            ),
            other_sql=(
                "UPDATE category_stats "
                "SET total_amount = ("
                "SELECT SUM(amount) FROM orders "
                "WHERE orders.category_id = category_stats.category_id"
                ") "
                "WHERE category_id = 1"
            ),
            static_kinds=("write_read",),
        ),
        StatementPair(
            name="cte_write_read",
            current_sql=(
                "UPDATE orders "
                "SET amount = amount + 1 "
                f"WHERE {current_range}"
            ),
            other_sql=(
                "WITH recent AS ("
                "SELECT customer_id, SUM(amount) AS total "
                "FROM orders "
                "WHERE status = 'open' "
                "GROUP BY customer_id"
                "), adjusted AS ("
                "SELECT customer_id, total + ("
                "SELECT COUNT(*) FROM items WHERE items.category_id = 1"
                ") AS score "
                "FROM recent"
                ") "
                "UPDATE customer_stats "
                "SET score = ("
                "SELECT COALESCE(MAX(score), 0) "
                "FROM adjusted "
                "WHERE adjusted.customer_id = customer_stats.customer_id"
                ") "
                "WHERE customer_id BETWEEN 1 AND 20"
            ),
            static_kinds=("write_read",),
        ),
    )


def _static_result_for_pair(
    context,
    current: LoggedTransaction,
    other: LoggedTransaction,
    pair: StatementPair,
) -> ConflictCheckResult:
    return static_analysis_matching(
        context,
        current.metadata,
        other.metadata,
        enabled_kinds=pair.static_kinds,
        current_branch="ours",
    )


def _create_base_database(path: Path, *, row_count: int) -> None:
    with sqlite3.connect(path) as con:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute(
            "CREATE TABLE items ("
            "id INTEGER PRIMARY KEY, "
            "category_id INTEGER NOT NULL, "
            "value INTEGER NOT NULL"
            ")"
        )
        con.execute(
            "CREATE TABLE orders ("
            "id INTEGER PRIMARY KEY, "
            "category_id INTEGER NOT NULL, "
            "customer_id INTEGER NOT NULL, "
            "amount INTEGER NOT NULL, "
            "status TEXT NOT NULL"
            ")"
        )
        con.execute(
            "CREATE TABLE category_stats ("
            "category_id INTEGER PRIMARY KEY, "
            "total_amount INTEGER NOT NULL"
            ")"
        )
        con.execute(
            "CREATE TABLE customer_stats ("
            "customer_id INTEGER PRIMARY KEY, "
            "score INTEGER NOT NULL"
            ")"
        )

        total_rows = row_count * 2
        con.executemany(
            "INSERT INTO items VALUES (?, ?, ?)",
            (
                (index, 1 if index <= row_count else 2, index % 17)
                for index in range(1, total_rows + 1)
            ),
        )
        con.executemany(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?)",
            (
                (
                    index,
                    1 if index <= row_count else 2,
                    (index % 50) + 1,
                    index % 23,
                    "open" if index % 3 else "closed",
                )
                for index in range(1, total_rows + 1)
            ),
        )
        con.executemany(
            "INSERT INTO category_stats VALUES (?, 0)",
            ((1,), (2,)),
        )
        con.executemany(
            "INSERT INTO customer_stats VALUES (?, 0)",
            ((index,) for index in range(1, 51)),
        )
        con.commit()


def _transaction_for_sql(
    context,
    *,
    branch: BranchName,
    transaction_id: int,
    sql_text: str,
) -> LoggedTransaction:
    statement = log_merge.make_logged_statement(
        branch=branch,
        branch_index=transaction_id - 1,
        transaction_id=transaction_id,
        committed_at="2026-01-01T00:00:00",
        sql_text=sql_text,
        metadata_context=context,
    )
    return log_merge.group_logged_transactions([statement])[0]


def _repeat_timing(
    repeats: int,
    inner_loops: int,
    operation,
) -> list[float]:
    timings = []
    for _ in range(repeats):
        start = time.perf_counter()
        for _ in range(inner_loops):
            operation()
        elapsed = time.perf_counter() - start
        timings.append(elapsed / inner_loops)
    return timings


def _time_pair_check_pipeline(
    context,
    current: LoggedTransaction,
    other: LoggedTransaction,
    pair: StatementPair,
    *,
    repeats: int,
) -> tuple[list[float], ConflictCheckResult]:
    """Time the normal static-check then execution-refinement pair path."""

    timings = []
    last_result = ConflictCheckResult()
    for _ in range(repeats):
        _clear_context_rewrite_caches(context)
        start = time.perf_counter()
        # This uses the same scanner layer as the real suffix check, but with
        # a one-transaction suffix so the timing isolates one pair.
        scanner = OrderedRemainingConflictScanner(
            context,
            current,
            current_branch="ours",
            enabled_kinds=pair.static_kinds,
        )
        try:
            conflict = scanner.next_conflict([other])
            last_result = ConflictCheckResult() if conflict is None else conflict[1]
        finally:
            scanner.close()
        elapsed = time.perf_counter() - start
        timings.append(elapsed)
    return timings, last_result


def _time_probe_refinement_stage(
    context,
    current: LoggedTransaction,
    other: LoggedTransaction,
    static_result: ConflictCheckResult,
    *,
    repeats: int,
) -> tuple[list[float], ConflictCheckResult]:
    """Time refinement checks after static analysis has already found overlap."""

    timings = []
    last_result = static_result
    scanner = OrderedRemainingExecutionScanner(
        context,
        current_transaction=current,
        current_branch="ours",
        enabled_kinds=set(static_result.conflicts_by_kind),
    )
    try:
        start_result = scanner.start()
        if start_result.has_conflict:
            return [0.0 for _ in range(repeats)], start_result

        for _ in range(repeats):
            _clear_context_rewrite_caches(context)
            start = time.perf_counter()
            last_result = _refine_static_result_once(scanner, current, other, static_result)
            elapsed = time.perf_counter() - start
            timings.append(elapsed)
    finally:
        scanner.close()

    return timings, last_result


def _refine_static_result_once(
    scanner: OrderedRemainingExecutionScanner,
    current: LoggedTransaction,
    other: LoggedTransaction,
    static_result: ConflictCheckResult,
) -> ConflictCheckResult:
    """Run the scanner's source refinement methods without suffix advancement."""

    result = static_result
    if static_result.has_kind("write_write"):
        result = scanner._check_write_write(current, other, result)
    if static_result.has_kind("write_read"):
        result = scanner._check_write_read(other, result)
    return result


def _clear_context_rewrite_caches(context) -> None:
    context.affected_pk_probe_cache.clear()
    context.read_probe_result_cache.clear()
    context.control_sql_cache.clear()


def _conflict_kinds(result: ConflictCheckResult) -> str:
    kinds = sorted(kind for kind in result.conflicts_by_kind if result.has_kind(kind))
    return ",".join(kinds) if kinds else "none"


def _write_csv(path: Path, results: list[MicrobenchmarkResult]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(asdict(results[0]).keys()))
        writer.writeheader()
        for result in results:
            writer.writerow(asdict(result))


if __name__ == "__main__":
    raise SystemExit(main())
