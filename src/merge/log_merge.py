from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from contextlib import closing
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from sqlglot.errors import ParseError

from .statement_metadata import (
    StatementMetadata,
    parse_statement_metadata,
    unsupported_statement_metadata,
)
from .utils import (
    ALL_COLUMNS,
    TableKeyColumnSets,
    TableColumns,
    TablePrimaryKeyColumns,
    is_sql_expression,
    load_key_column_sets,
    load_primary_key_columns,
    load_table_columns as load_user_table_columns,
    rollback_savepoint,
    sql_expression_to_sql,
    table_exists,
)


LOG_TABLE = "_sqlite_merge_log"
TX_TABLE = "_sqlite_merge_transactions"
METADATA_PARSE_ERROR_REASON = "statement could not be parsed for merge analysis"

BranchName = Literal["ours", "theirs"]
ConflictScope = Literal["pair", "ours", "theirs"]
ConflictKind = Literal[
    "write_write",
    "write_read",
    "implicit_insert_key",
    "unsafe_replay",
    "integrity",
    "non_commutative",
    "replay_error",
]


@dataclass(frozen=True)
class LoggedStatement:
    branch: BranchName
    branch_index: int
    log_id: int
    transaction_id: int
    committed_at: str
    original_sql_text: str
    to_replay_sql_text: str
    is_replay_safe: bool
    replay_block_reason: str | None
    metadata: StatementMetadata

    @property
    def sql_text(self) -> str:
        """Return the deterministic SQL used for analysis and replay."""

        return self.to_replay_sql_text


@dataclass(frozen=True)
class ConflictPair:
    ours_index: int
    theirs_index: int
    ours_sql: str
    theirs_sql: str
    conflicts: tuple["StatementConflict", ...] = ()


@dataclass(frozen=True)
class ConflictCheckContext:
    base_cursor: sqlite3.Cursor
    base_db_path: str | Path
    table_columns: TableColumns
    primary_key_columns: TablePrimaryKeyColumns = field(default_factory=dict)
    key_column_sets: TableKeyColumnSets = field(default_factory=dict)


@dataclass(frozen=True)
class StatementConflict:
    kind: ConflictKind
    message: str
    scope: ConflictScope = "pair"
    details: tuple[tuple[str, str], ...] = ()


@dataclass(init=False)
class ConflictCheckResult:
    conflicts_by_kind: dict[ConflictKind, list[StatementConflict]]

    def __init__(
        self,
        conflicts: Sequence[StatementConflict] = (),
        *,
        conflicts_by_kind: Mapping[
            ConflictKind,
            Sequence[StatementConflict],
        ] | None = None,
    ):
        grouped: dict[ConflictKind, list[StatementConflict]] = {}
        if conflicts_by_kind is None:
            for conflict in conflicts:
                grouped.setdefault(conflict.kind, []).append(conflict)
        else:
            for kind, kind_conflicts in conflicts_by_kind.items():
                if kind_conflicts:
                    grouped[kind] = list(kind_conflicts)

        self.conflicts_by_kind = grouped

    @property
    def conflicts(self) -> tuple[StatementConflict, ...]:
        """Return conflicts flattened in insertion order."""

        return tuple(
            conflict
            for kind_conflicts in self.conflicts_by_kind.values()
            for conflict in kind_conflicts
        )

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts_by_kind)

    def of_kind(self, kind: ConflictKind) -> tuple[StatementConflict, ...]:
        """Return conflicts of one kind."""

        return tuple(self.conflicts_by_kind.get(kind, ()))

    def has_kind(self, kind: ConflictKind) -> bool:
        """Return whether any conflicts of kind are present."""

        return kind in self.conflicts_by_kind

    def without_kind(self, kind: ConflictKind) -> ConflictCheckResult:
        """Remove conflicts of kind and return this result."""

        self.conflicts_by_kind.pop(kind, None)
        return self

    def replace_kind(
        self,
        kind: ConflictKind,
        conflicts: Sequence[StatementConflict],
    ) -> ConflictCheckResult:
        """Replace conflicts of kind and return this result."""

        self.conflicts_by_kind.pop(kind, None)
        if conflicts:
            self.conflicts_by_kind[kind] = list(conflicts)
        return self

    def add_conflicts(
        self,
        *conflicts: StatementConflict,
    ) -> ConflictCheckResult:
        """Append conflicts to their kind groups and return this result."""

        for conflict in conflicts:
            self.conflicts_by_kind.setdefault(conflict.kind, []).append(conflict)
        return self


ConflictDetector = Callable[
    [ConflictCheckContext, "LoggedStatement", "LoggedStatement"],
    ConflictCheckResult,
]


@dataclass(frozen=True)
class FrontierCandidate:
    name: str
    ours_count: int
    theirs_count: int
    next_conflict: ConflictPair | None

    @property
    def score(self) -> int:
        return self.ours_count + self.theirs_count


@dataclass(frozen=True)
class ReplayFailure:
    statement: dict[str, object] | None
    error: str


@dataclass(frozen=True)
class ReplayResult:
    ok: bool
    output_path: str
    applied_count: int
    failure: ReplayFailure | None = None
    integrity_errors: list[str] | None = None


@dataclass(frozen=True)
class MergePlan:
    status: Literal["clean", "conflict"]
    base_transaction_id: int
    ours: list[LoggedStatement]
    theirs: list[LoggedStatement]
    selected: FrontierCandidate
    statement_plan: list[LoggedStatement]
    first_conflict: ConflictPair | None = None
    candidates: list[FrontierCandidate] | None = None


@dataclass(frozen=True)
class MergeOutcome:
    plan: MergePlan
    replay: ReplayResult
    report_path: str | None


class MergeNotApplicableError(Exception):
    """Raised when a database was not prepared with sqlite-reconcile logging."""

    def __init__(self, db_path: str | Path, role: str, missing_tables: Sequence[str]):
        self.db_path = str(db_path)
        self.role = role
        self.missing_tables = list(missing_tables)
        missing = ", ".join(self.missing_tables)
        super().__init__(
            f"{role} database is not applicable for log-based SQLite merge: "
            f"{self.db_path} is missing {missing}"
        )


def statements_conflict(
    context: ConflictCheckContext,
    ours_statement: LoggedStatement,
    theirs_statement: LoggedStatement,
) -> ConflictCheckResult:
    """Default per-statement conflict detector."""

    from .conflict_detection import conflict_detection

    return conflict_detection(context, ours_statement, theirs_statement)


def make_logged_statement(
    branch: BranchName,
    branch_index: int,
    log_id: int,
    transaction_id: int,
    committed_at: str,
    sql_text: str,
    table_columns: TableColumns | None = None,
    original_sql_text: str | None = None,
    is_replay_safe: bool = True,
    replay_block_reason: str | None = None,
) -> LoggedStatement:
    try:
        metadata = parse_statement_metadata(sql_text, table_columns=table_columns)
    except ParseError as exc:
        metadata = unsupported_statement_metadata(sql_text)
        is_replay_safe = False
        replay_block_reason = (
            replay_block_reason
            or f"{METADATA_PARSE_ERROR_REASON}: {exc}"
        )

    return LoggedStatement(
        branch=branch,
        branch_index=branch_index,
        log_id=log_id,
        transaction_id=transaction_id,
        committed_at=committed_at,
        original_sql_text=original_sql_text or sql_text,
        to_replay_sql_text=sql_text,
        is_replay_safe=is_replay_safe,
        replay_block_reason=replay_block_reason,
        metadata=metadata,
    )


def load_table_columns(cursor: sqlite3.Cursor) -> TableColumns:
    """Return non-internal table columns, excluding merge log tables."""

    return load_user_table_columns(cursor, ignored_tables={LOG_TABLE, TX_TABLE})


def _require_log_tables(cursor: sqlite3.Cursor, db_path: str | Path, role: str) -> None:
    missing_tables = [
        table_name
        for table_name in (TX_TABLE, LOG_TABLE)
        if not table_exists(cursor, table_name)
    ]
    if missing_tables:
        raise MergeNotApplicableError(db_path, role, missing_tables)


def get_base_watermark(base_cursor: sqlite3.Cursor, base_db_path: str | Path) -> int:
    """Return the last transaction id known to the merge base."""
    _require_log_tables(base_cursor, base_db_path, "base")
    row = base_cursor.execute(f"SELECT COALESCE(MAX(id), 0) FROM {TX_TABLE}").fetchone()
    return int(row[0])


def load_logged_statements(
    cursor: sqlite3.Cursor,
    branch: BranchName,
    since_transaction_id: int,
    db_path: str | Path,
    table_columns: TableColumns | None = None,
) -> list[LoggedStatement]:
    """Load branch log entries after the merge-base transaction watermark."""
    _require_log_tables(cursor, db_path, branch)
    rows = cursor.execute(
        f"""
        SELECT l.id AS log_id,
               l.transaction_id,
               t.committed_at,
               l.original_sql_text,
               l.to_replay_sql_text,
               l.is_replay_safe,
               l.replay_block_reason
        FROM {LOG_TABLE} AS l
        JOIN {TX_TABLE} AS t ON t.id = l.transaction_id
        WHERE l.transaction_id > ?
        ORDER BY l.transaction_id, l.id
        """,
        (since_transaction_id,),
    ).fetchall()

    return [
        make_logged_statement(
            branch=branch,
            branch_index=index,
            log_id=int(row["log_id"]),
            transaction_id=int(row["transaction_id"]),
            committed_at=str(row["committed_at"]),
            sql_text=str(row["to_replay_sql_text"]),
            original_sql_text=str(row["original_sql_text"]),
            is_replay_safe=bool(row["is_replay_safe"]),
            replay_block_reason=row["replay_block_reason"],
            table_columns=table_columns,
        )
        for index, row in enumerate(rows)
    ]


def _conflict_pair(
    ours: Sequence[LoggedStatement],
    theirs: Sequence[LoggedStatement],
    ours_index: int,
    theirs_index: int,
    result: ConflictCheckResult | None = None,
) -> ConflictPair:
    return ConflictPair(
        ours_index=ours_index,
        theirs_index=theirs_index,
        ours_sql=ours[ours_index].original_sql_text,
        theirs_sql=theirs[theirs_index].original_sql_text,
        conflicts=() if result is None else result.conflicts,
    )


def find_first_pairwise_conflict(
    ours: Sequence[LoggedStatement],
    theirs: Sequence[LoggedStatement],
    context: ConflictCheckContext,
    conflict_detector: ConflictDetector = statements_conflict,
) -> ConflictPair | None:
    """Compare X1/Y1, X2/Y2, ... and return the first conflict."""
    for index, (ours_statement, theirs_statement) in enumerate(zip(ours, theirs)):
        result = conflict_detector(context, ours_statement, theirs_statement)
        if result.has_conflict:
            return _conflict_pair(ours, theirs, index, index, result)
    return None


def search_by_backtracking_ours(
    ours: Sequence[LoggedStatement],
    theirs: Sequence[LoggedStatement],
    conflict_index: int,
    context: ConflictCheckContext,
    conflict_detector: ConflictDetector = statements_conflict,
) -> FrontierCandidate:
    """
    Keep backing up the local side and advance the remote side.

    If X(i) conflicts with Y(i), this starts by comparing X(i-1) with Y(i),
    then moves forward through Y. When that conflicts, it backs up to X(i-2)
    and tries the same Y again. The search stops once X1 conflicts or the
    remote side is exhausted.
    """
    if conflict_index == 0:
        result = conflict_detector(context, ours[0], theirs[conflict_index])
        return FrontierCandidate(
            name="backtrack_ours",
            ours_count=0,
            theirs_count=conflict_index,
            next_conflict=_conflict_pair(ours, theirs, 0, conflict_index, result),
        )
    ours_index = conflict_index - 1
    theirs_index = conflict_index

    while theirs_index < len(theirs):
        result = conflict_detector(context, ours[ours_index], theirs[theirs_index])
        if result.has_conflict:
            if ours_index == 0:
                return FrontierCandidate(
                    name="backtrack_ours",
                    ours_count=0,
                    theirs_count=theirs_index,
                    next_conflict=_conflict_pair(
                        ours,
                        theirs,
                        ours_index,
                        theirs_index,
                        result,
                    ),
                )
            ours_index -= 1
            continue
        theirs_index += 1

    return FrontierCandidate(
        name="backtrack_ours",
        ours_count=ours_index + 1,
        theirs_count=len(theirs),
        next_conflict=None,
    )


def search_by_backtracking_theirs(
    ours: Sequence[LoggedStatement],
    theirs: Sequence[LoggedStatement],
    conflict_index: int,
    context: ConflictCheckContext,
    conflict_detector: ConflictDetector = statements_conflict,
) -> FrontierCandidate:
    """Mirror of search_by_backtracking_ours, keeping more local statements."""
    theirs_index = conflict_index - 1
    ours_index = conflict_index

    if theirs_index < 0:
        result = conflict_detector(context, ours[conflict_index], theirs[0])
        return FrontierCandidate(
            name="backtrack_theirs",
            ours_count=conflict_index,
            theirs_count=0,
            next_conflict=_conflict_pair(ours, theirs, conflict_index, 0, result),
        )

    while ours_index < len(ours):
        result = conflict_detector(context, ours[ours_index], theirs[theirs_index])
        if result.has_conflict:
            if theirs_index == 0:
                return FrontierCandidate(
                    name="backtrack_theirs",
                    ours_count=ours_index,
                    theirs_count=0,
                    next_conflict=_conflict_pair(
                        ours,
                        theirs,
                        ours_index,
                        theirs_index,
                        result,
                    ),
                )
            theirs_index -= 1
            continue
        ours_index += 1

    return FrontierCandidate(
        name="backtrack_theirs",
        ours_count=len(ours),
        theirs_count=theirs_index + 1,
        next_conflict=None,
    )


def choose_frontier(candidates: Sequence[FrontierCandidate]) -> FrontierCandidate:
    """Pick the candidate with the most applied statements, then the least skew."""
    return max(
        candidates,
        key=lambda candidate: (
            candidate.score,
            min(candidate.ours_count, candidate.theirs_count),
            -abs(candidate.ours_count - candidate.theirs_count),
        ),
    )


def frontier_candidates_for_conflict(
    ours: Sequence[LoggedStatement],
    theirs: Sequence[LoggedStatement],
    first_conflict: ConflictPair,
    context: ConflictCheckContext,
    conflict_detector: ConflictDetector = statements_conflict,
) -> list[FrontierCandidate]:
    """Return backtracking candidates, narrowing unsafe replay to its branch."""

    unsafe_branches = _unsafe_replay_branches(first_conflict)
    candidates: list[FrontierCandidate] = []
    if not unsafe_branches or "ours" in unsafe_branches:
        candidates.append(
            search_by_backtracking_ours(
                ours,
                theirs,
                first_conflict.ours_index,
                context,
                conflict_detector,
            )
        )
    if not unsafe_branches or "theirs" in unsafe_branches:
        candidates.append(
            search_by_backtracking_theirs(
                ours,
                theirs,
                first_conflict.ours_index,
                context,
                conflict_detector,
            )
        )
    return candidates


def _unsafe_replay_branches(conflict: ConflictPair) -> set[BranchName]:
    """Return branches that are blocked by standalone unsafe replay conflicts."""

    branches: set[BranchName] = set()
    for statement_conflict in conflict.conflicts:
        if statement_conflict.kind != "unsafe_replay":
            continue

        if statement_conflict.scope in {"ours", "theirs"}:
            branches.add(statement_conflict.scope)
    return branches


def ordered_statement_plan(
    ours: Sequence[LoggedStatement],
    theirs: Sequence[LoggedStatement],
    frontier: FrontierCandidate,
) -> list[LoggedStatement]:
    ours_prefix = list(ours[: frontier.ours_count])
    theirs_prefix = list(theirs[: frontier.theirs_count])
    return ours_prefix + theirs_prefix


def build_merge_plan(
    base_db_path: str | Path,
    ours_db_path: str | Path,
    theirs_db_path: str | Path,
    conflict_detector: ConflictDetector = statements_conflict,
) -> MergePlan:
    with closing(sqlite3.connect(base_db_path)) as base_conn, \
         closing(sqlite3.connect(ours_db_path)) as ours_conn, \
         closing(sqlite3.connect(theirs_db_path)) as theirs_conn:
        base_conn.row_factory = sqlite3.Row
        ours_conn.row_factory = sqlite3.Row
        theirs_conn.row_factory = sqlite3.Row

        base_cursor = base_conn.cursor()
        ours_cursor = ours_conn.cursor()
        theirs_cursor = theirs_conn.cursor()

        table_columns = load_table_columns(base_cursor)
        primary_key_columns = load_primary_key_columns(
            base_cursor,
            table_columns,
        )
        key_column_sets = load_key_column_sets(
            base_cursor,
            table_columns,
        )
        base_transaction_id = get_base_watermark(base_cursor, base_db_path)
        ours = load_logged_statements(
            ours_cursor,
            "ours",
            base_transaction_id,
            ours_db_path,
            table_columns=table_columns,
        )
        theirs = load_logged_statements(
            theirs_cursor,
            "theirs",
            base_transaction_id,
            theirs_db_path,
            table_columns=table_columns,
        )

        context = ConflictCheckContext(
            base_cursor=base_cursor,
            base_db_path=base_db_path,
            table_columns=table_columns,
            primary_key_columns=primary_key_columns,
            key_column_sets=key_column_sets,
        )
        first_conflict = find_first_pairwise_conflict(
            ours,
            theirs,
            context,
            conflict_detector,
        )
        if first_conflict is None:
            selected = FrontierCandidate(
                name="clean",
                ours_count=len(ours),
                theirs_count=len(theirs),
                next_conflict=None,
            )
            return MergePlan(
                status="clean",
                base_transaction_id=base_transaction_id,
                ours=ours,
                theirs=theirs,
                selected=selected,
                statement_plan=ordered_statement_plan(ours, theirs, selected),
            )

        candidates = frontier_candidates_for_conflict(
            ours,
            theirs,
            first_conflict,
            context,
            conflict_detector,
        )
        selected = choose_frontier(candidates)
        return MergePlan(
            status="conflict",
            base_transaction_id=base_transaction_id,
            ours=ours,
            theirs=theirs,
            selected=selected,
            statement_plan=ordered_statement_plan(ours, theirs, selected),
            first_conflict=first_conflict,
            candidates=candidates,
        )


def _append_replayed_log(con: sqlite3.Connection, statement: LoggedStatement) -> None:
    committed_at = statement.committed_at or datetime.now().isoformat()
    cursor = con.execute(
        f"INSERT INTO {TX_TABLE} (committed_at) VALUES (?)",
        (committed_at,),
    )
    con.execute(
        f"""
        INSERT INTO {LOG_TABLE} (
            transaction_id,
            original_sql_text,
            to_replay_sql_text,
            is_replay_safe,
            replay_block_reason
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            cursor.lastrowid,
            statement.original_sql_text,
            statement.to_replay_sql_text,
            int(statement.is_replay_safe),
            statement.replay_block_reason,
        ),
    )


def validate_database(con: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    integrity_row = con.execute("PRAGMA integrity_check").fetchone()
    if integrity_row is not None and integrity_row[0] != "ok":
        errors.append(f"integrity_check: {integrity_row[0]}")

    foreign_key_rows = con.execute("PRAGMA foreign_key_check").fetchall()
    for row in foreign_key_rows:
        errors.append(f"foreign_key_check: {tuple(row)}")
    return errors


def replay_statement_plan(
    base_db_path: str | Path,
    output_db_path: str | Path,
    statement_plan: Sequence[LoggedStatement],
) -> ReplayResult:
    output_path = Path(output_db_path)
    shutil.copy2(base_db_path, output_path)

    with sqlite3.connect(output_path) as con:
        con.execute("PRAGMA foreign_keys = ON")

        for applied_count, statement in enumerate(statement_plan):
            if not statement.is_replay_safe:
                return ReplayResult(
                    ok=False,
                    output_path=str(output_path),
                    applied_count=applied_count,
                    failure=ReplayFailure(
                        statement=asdict(statement),
                        error=(
                            statement.replay_block_reason
                            or "statement is unsafe for automatic replay"
                        ),
                    ),
                )

            savepoint_name = f"replay_statement_{applied_count}"
            con.execute(f"SAVEPOINT {savepoint_name}")
            try:
                con.execute(statement.sql_text)
                integrity_errors = validate_database(con)
                if integrity_errors:
                    rollback_savepoint(con, savepoint_name)
                    return ReplayResult(
                        ok=False,
                        output_path=str(output_path),
                        applied_count=applied_count,
                        failure=ReplayFailure(
                            statement=asdict(statement),
                            error="database validation failed after statement",
                        ),
                        integrity_errors=integrity_errors,
                    )
                _append_replayed_log(con, statement)
                con.execute(f"RELEASE {savepoint_name}")
            except sqlite3.Error as exc:
                rollback_savepoint(con, savepoint_name)
                return ReplayResult(
                    ok=False,
                    output_path=str(output_path),
                    applied_count=applied_count,
                    failure=ReplayFailure(
                        statement=asdict(statement),
                        error=str(exc),
                    ),
                )

        con.commit()

    return ReplayResult(
        ok=True,
        output_path=str(output_path),
        applied_count=len(statement_plan),
    )


def _dataclass_to_dict(value: object) -> object:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if is_sql_expression(value):
        return sql_expression_to_sql(value)
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def write_conflict_report(
    report_path: str | Path,
    plan: MergePlan,
    replay: ReplayResult,
) -> None:
    path = Path(report_path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": plan.status,
        "base_transaction_id": plan.base_transaction_id,
        "first_conflict": plan.first_conflict,
        "selected_frontier": plan.selected,
        "candidates": plan.candidates or [],
        "statement_plan": plan.statement_plan,
        "replay": replay,
    }
    path.write_text(json.dumps(payload, default=_dataclass_to_dict, indent=2), encoding="utf-8")


def write_not_applicable_report(
    report_path: str | Path,
    error: MergeNotApplicableError,
) -> None:
    path = Path(report_path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "not_applicable",
        "message": str(error),
        "database": error.db_path,
        "role": error.role,
        "missing_tables": error.missing_tables,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def merge_databases(
    base_db_path: str | Path,
    ours_db_path: str | Path,
    theirs_db_path: str | Path,
    report_path: str | Path | None = None,
    conflict_detector: ConflictDetector = statements_conflict,
) -> MergeOutcome:
    plan = build_merge_plan(base_db_path, ours_db_path, theirs_db_path, conflict_detector)
    replay = replay_statement_plan(base_db_path, ours_db_path, plan.statement_plan)

    report: str | None = None
    if plan.status == "conflict" or not replay.ok:
        report = str(report_path or f"{ours_db_path}.sqlite-reconcile-conflict.json")
        write_conflict_report(report, plan, replay)

    return MergeOutcome(plan=plan, replay=replay, report_path=report)


def default_report_path(ours_path: str, pathname: str | None) -> str:
    if pathname:
        return f"{pathname}.sqlite-reconcile-conflict.json"
    return f"{ours_path}.sqlite-reconcile-conflict.json"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SQLite statement-log merge driver")
    parser.add_argument("base", metavar="%O", help="Base file")
    parser.add_argument("ours", metavar="%A", help="Ours file; overwritten with merge result")
    parser.add_argument("theirs", metavar="%B", help="Theirs file")
    parser.add_argument("conflict_marker_size", metavar="%L", nargs="?", type=int)
    parser.add_argument("pathname", metavar="%P", nargs="?")
    parser.add_argument("--report", help="Path for the JSON conflict report")
    args = parser.parse_args(argv)

    report_path = args.report or default_report_path(args.ours, args.pathname)
    try:
        outcome = merge_databases(args.base, args.ours, args.theirs, report_path=report_path)
    except MergeNotApplicableError as exc:
        write_not_applicable_report(report_path, exc)
        print(f"sqlite-reconcile: {exc}; report written to {report_path}", file=sys.stderr)
        return 1

    if outcome.plan.status == "conflict" or not outcome.replay.ok:
        if outcome.report_path:
            print(f"sqlite-reconcile: unresolved SQLite merge; report written to {outcome.report_path}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
