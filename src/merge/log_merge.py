from __future__ import annotations

import shutil
import sqlite3
from collections import defaultdict
from contextlib import closing
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal, cast
import uuid

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
    load_key_column_sets,
    load_primary_key_columns,
    load_table_columns as load_user_table_columns,
    rollback_savepoint,
    table_exists,
)


LOG_TABLE = "_sqlite_merge_log"
TX_TABLE = "_sqlite_merge_transactions"
METADATA_PARSE_ERROR_REASON = "statement could not be parsed for merge analysis"
ACKNOWLEDGEABLE_REPLAY_REASONS = (
    "nondeterministic expression cannot be safely materialized",
    "parameterized statement needs nondeterministic rewrite",
)
UPDATE_FROM_DUPLICATE_TARGET_WARNING = (
    "UPDATE FROM has multiple source rows for the same target row"
)

BranchName = Literal["ours", "theirs"]
ConflictScope = Literal["pair", "ours", "theirs", "both"]
ConflictKind = Literal[
    "write_write",
    "write_read",
    "implicit_insert_key",
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
    replay_warnings: tuple[str, ...] = ()

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
    conflicts_by_kind: defaultdict[ConflictKind, list[StatementConflict]]

    def __init__(
        self,
        conflicts: Sequence[StatementConflict] = (),
        *,
        conflicts_by_kind: Mapping[
            ConflictKind,
            Sequence[StatementConflict],
        ] | None = None,
    ):
        grouped: defaultdict[ConflictKind, list[StatementConflict]] = defaultdict(list)
        if conflicts_by_kind is None:
            for conflict in conflicts:
                grouped[conflict.kind].append(conflict)
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
            self.conflicts_by_kind[conflict.kind].append(conflict)
        return self


ConflictDetector = Callable[
    [ConflictCheckContext, "LoggedStatement", "LoggedStatement"],
    ConflictCheckResult,
]
StatementApplier = Callable[
    [ConflictCheckContext, Sequence["LoggedStatement"]],
    StatementConflict | None,
]


def default_conflict_detector() -> ConflictDetector:
    """Load the default detector lazily to avoid circular imports."""

    from .conflict_detection import statements_conflict

    return statements_conflict


@dataclass(frozen=True)
class FrontierCandidate:
    name: str
    ours_count: int
    theirs_count: int
    next_conflict: ConflictPair | None
    scope: ConflictScope = "pair"

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
    replay_warnings: Sequence[str] = (),
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
        replay_warnings=tuple(replay_warnings),
    )


def acknowledgeable_replay_warning(statement: LoggedStatement) -> str | None:
    """Return a warning for unsafe logged SQL that can still be run manually."""

    if statement.is_replay_safe or statement.replay_block_reason is None:
        return None

    reason = statement.replay_block_reason
    if any(reason.startswith(prefix) for prefix in ACKNOWLEDGEABLE_REPLAY_REASONS):
        return reason
    return None


def acknowledge_replay_warning(statement: LoggedStatement) -> LoggedStatement:
    """Mark an acknowledgeable replay warning as accepted for this merge run."""

    warning = acknowledgeable_replay_warning(statement)
    if warning is None:
        return statement

    warnings = statement.replay_warnings
    if warning not in warnings:
        warnings = (*warnings, warning)
    return replace(
        statement,
        is_replay_safe=True,
        replay_block_reason=None,
        replay_warnings=warnings,
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


def load_schema_metadata(
    cursor: sqlite3.Cursor,
) -> tuple[TableColumns, TablePrimaryKeyColumns, TableKeyColumnSets]:
    """Load table columns, primary keys, and candidate key sets together."""

    table_columns = load_table_columns(cursor)
    return (
        table_columns,
        load_primary_key_columns(cursor, table_columns),
        load_key_column_sets(cursor, table_columns),
    )


def load_schema_metadata_from_db(
    db_path: str | Path,
) -> tuple[TableColumns, TablePrimaryKeyColumns, TableKeyColumnSets]:
    """Open a database and load schema metadata with a closed connection."""

    with closing(sqlite3.connect(db_path)) as con:
        return load_schema_metadata(con.cursor())


def load_merge_inputs(
    base_db_path: str | Path,
    ours_db_path: str | Path,
    theirs_db_path: str | Path,
) -> tuple[
    int,
    list[LoggedStatement],
    list[LoggedStatement],
    TableColumns,
    TablePrimaryKeyColumns,
    TableKeyColumnSets,
]:
    """Load logged branch statements and schema metadata for one merge."""

    with closing(sqlite3.connect(base_db_path)) as base_conn, \
         closing(sqlite3.connect(ours_db_path)) as ours_conn, \
         closing(sqlite3.connect(theirs_db_path)) as theirs_conn:
        base_conn.row_factory = sqlite3.Row
        ours_conn.row_factory = sqlite3.Row
        theirs_conn.row_factory = sqlite3.Row

        base_cursor = base_conn.cursor()
        table_columns, primary_key_columns, key_column_sets = load_schema_metadata(
            base_cursor,
        )
        base_transaction_id = get_base_watermark(base_cursor, base_db_path)
        ours = load_logged_statements(
            ours_conn.cursor(),
            "ours",
            base_transaction_id,
            ours_db_path,
            table_columns=table_columns,
        )
        theirs = load_logged_statements(
            theirs_conn.cursor(),
            "theirs",
            base_transaction_id,
            theirs_db_path,
            table_columns=table_columns,
        )

    return (
        base_transaction_id,
        ours,
        theirs,
        table_columns,
        primary_key_columns,
        key_column_sets,
    )


def _conflict_pair(
    ours: Sequence[LoggedStatement],
    theirs: Sequence[LoggedStatement],
    ours_index: int,
    theirs_index: int,
    result: ConflictCheckResult | None = None,
) -> ConflictPair:
    """Build report-friendly conflict data using original SQL for display."""

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
    conflict_detector: ConflictDetector | None = None,
    statement_applier: StatementApplier | None = None,
) -> ConflictPair | None:
    """Compare X1/Y1, X2/Y2, ..., applying clean pairs as it advances."""

    conflict_detector = conflict_detector or default_conflict_detector()
    statement_applier = statement_applier or apply_statement_sequence
    for index, (ours_statement, theirs_statement) in enumerate(zip(ours, theirs)):
        result = conflict_detector(context, ours_statement, theirs_statement)
        if result.has_conflict:
            return _conflict_pair(ours, theirs, index, index, result)

        # Execution-based checks for later pairs must see earlier accepted
        # effects, so planning advances the working database after each clean pair.
        replay_error = statement_applier(context, (ours_statement, theirs_statement))
        if replay_error is not None:
            return _conflict_pair(
                ours,
                theirs,
                index,
                index,
                ConflictCheckResult((replay_error,)),
            )
    return None


def apply_statement_sequence(
    context: ConflictCheckContext,
    statements: Sequence[LoggedStatement],
) -> StatementConflict | None:
    """
    The conflict detector checks the current pair, but prefix replay can still 
    fail because the greedy algorithm does not validate every cross-branch pair 
    inside the prefix.
    """

    savepoint = f"sqlite_merge_plan_{uuid.uuid4().hex}"
    cursor = context.base_cursor
    cursor.execute(f"SAVEPOINT {savepoint}")
    try:
        for statement in statements:
            cursor.execute(statement.sql_text)
        cursor.execute(f"RELEASE {savepoint}")
    except sqlite3.Error as exc:
        rollback_savepoint(cursor, savepoint)
        return StatementConflict(kind="replay_error", message=str(exc))
    return None


def _conflict_after_prefix(
    ours: Sequence[LoggedStatement],
    theirs: Sequence[LoggedStatement],
    ours_index: int,
    theirs_index: int,
    context: ConflictCheckContext,
    conflict_detector: ConflictDetector,
    statement_applier: StatementApplier,
) -> ConflictCheckResult:
    """Check one pair after applying the prefix that would precede it."""

    prefix = ordered_statement_plan(
        ours,
        theirs,
        FrontierCandidate(
            name="prefix",
            ours_count=ours_index,
            theirs_count=theirs_index,
            next_conflict=None,
        ),
    )
    savepoint = f"sqlite_merge_backtrack_{uuid.uuid4().hex}"
    context.base_cursor.execute(f"SAVEPOINT {savepoint}")
    try:
        replay_error = statement_applier(context, prefix)
        if replay_error is not None:
            return ConflictCheckResult((replay_error,))
        return conflict_detector(context, ours[ours_index], theirs[theirs_index])
    finally:
        rollback_savepoint(context.base_cursor, savepoint)


def _frontier_candidate(
    name: str,
    ours_count: int,
    theirs_count: int,
    next_conflict: ConflictPair,
) -> FrontierCandidate:
    """Build a frontier candidate with scope copied from its conflict."""

    return FrontierCandidate(
        name=name,
        ours_count=ours_count,
        theirs_count=theirs_count,
        next_conflict=next_conflict,
        scope=_conflict_scope(next_conflict),
    )


def search_by_backtracking_ours(
    ours: Sequence[LoggedStatement],
    theirs: Sequence[LoggedStatement],
    initial_conflict: ConflictPair,
    context: ConflictCheckContext,
    conflict_detector: ConflictDetector | None = None,
    statement_applier: StatementApplier | None = None,
) -> FrontierCandidate:
    """
    Keep backing up the local side and advance the remote side.

    If X(i) conflicts with Y(i), this starts by comparing X(i-1) with Y(i),
    then moves forward through Y. When that conflicts, it backs up to X(i-2)
    and tries the same Y again. The search stops once X1 conflicts or the
    remote side is exhausted.
    """
    conflict_detector = conflict_detector or default_conflict_detector()
    statement_applier = statement_applier or apply_statement_sequence
    candidates = _initial_frontier_candidates(
        initial_conflict,
        fixed_branch="theirs",
    )
    ours_index = initial_conflict.ours_index - 1
    theirs_index = initial_conflict.theirs_index
    if ours_index < 0 and not candidates:
        return _frontier_candidate(
            "standalone_replay",
            initial_conflict.ours_index,
            initial_conflict.theirs_index,
            initial_conflict,
        )

    while ours_index >= 0 and theirs_index < len(theirs):
        result = _conflict_after_prefix(
            ours,
            theirs,
            ours_index,
            theirs_index,
            context,
            conflict_detector,
            statement_applier,
        )
        if result.has_conflict:
            if "theirs" in _standalone_replay_branches(result):
                # Do not treat this as a pair conflict: the remote statement is
                # blocked before the local candidate is applied.
                if ours_index == 0:
                    candidates.append(
                        _frontier_candidate(
                            "standalone_replay",
                            ours_index,
                            theirs_index,
                            _conflict_pair(
                                ours,
                                theirs,
                                ours_index,
                                theirs_index,
                                result,
                            ),
                        )
                    )
                ours_index -= 1
                continue

            candidates.append(
                _frontier_candidate(
                    "backtrack_ours",
                    ours_index,
                    theirs_index,
                    _conflict_pair(
                        ours,
                        theirs,
                        ours_index,
                        theirs_index,
                        result,
                    ),
                )
            )
            ours_index -= 1
            continue
        theirs_index += 1

    if theirs_index >= len(theirs):
        candidates.append(
            FrontierCandidate(
                name="backtrack_ours",
                ours_count=ours_index,
                theirs_count=len(theirs),
                next_conflict=None,
            )
        )
    return choose_frontier(candidates)


def search_by_backtracking_theirs(
    ours: Sequence[LoggedStatement],
    theirs: Sequence[LoggedStatement],
    initial_conflict: ConflictPair,
    context: ConflictCheckContext,
    conflict_detector: ConflictDetector | None = None,
    statement_applier: StatementApplier | None = None,
) -> FrontierCandidate:
    """Mirror of search_by_backtracking_ours, keeping more local statements."""

    conflict_detector = conflict_detector or default_conflict_detector()
    statement_applier = statement_applier or apply_statement_sequence
    candidates = _initial_frontier_candidates(
        initial_conflict,
        fixed_branch="ours",
    )
    theirs_index = initial_conflict.theirs_index - 1
    ours_index = initial_conflict.ours_index
    if theirs_index < 0 and not candidates:
        return _frontier_candidate(
            "standalone_replay",
            initial_conflict.ours_index,
            initial_conflict.theirs_index,
            initial_conflict,
        )

    while theirs_index >= 0 and ours_index < len(ours):
        result = _conflict_after_prefix(
            ours,
            theirs,
            ours_index,
            theirs_index,
            context,
            conflict_detector,
            statement_applier,
        )
        if result.has_conflict:
            if "ours" in _standalone_replay_branches(result):
                # Do not treat this as a pair conflict: the local statement is
                # blocked before the remote candidate is applied.
                if theirs_index == 0:
                    candidates.append(
                        _frontier_candidate(
                            "standalone_replay",
                            ours_index,
                            theirs_index,
                            _conflict_pair(
                                ours,
                                theirs,
                                ours_index,
                                theirs_index,
                                result,
                            ),
                        )
                    )
                theirs_index -= 1
                continue

            candidates.append(
                _frontier_candidate(
                    "backtrack_theirs",
                    ours_index,
                    theirs_index,
                    _conflict_pair(
                        ours,
                        theirs,
                        ours_index,
                        theirs_index,
                        result,
                    ),
                )
            )
            theirs_index -= 1
            continue
        ours_index += 1

    if ours_index >= len(ours):
        candidates.append(
            FrontierCandidate(
                name="backtrack_theirs",
                ours_count=len(ours),
                theirs_count=theirs_index,
                next_conflict=None,
            )
        )
    return choose_frontier(candidates)


def choose_frontier(candidates: Sequence[FrontierCandidate]) -> FrontierCandidate:
    """Pick the candidate with the most applied statements, then the least skew."""
    for candidate in candidates:
        print(candidate.ours_count + 1, candidate.theirs_count + 1)
    return max(
        candidates,
        key=lambda candidate: (
            candidate.score,
            min(candidate.ours_count, candidate.theirs_count),
            -abs(candidate.ours_count - candidate.theirs_count),
        ),
    )


def _initial_frontier_candidates(
    initial_conflict: ConflictPair,
    fixed_branch: BranchName,
) -> list[FrontierCandidate]:
    """Return the original conflict as a candidate when one is available."""

    standalone_branches = _standalone_replay_branches(initial_conflict)
    if fixed_branch in standalone_branches:
        return []
    return [
        _frontier_candidate(
            "standalone_replay" if standalone_branches else "pairwise",
            initial_conflict.ours_index,
            initial_conflict.theirs_index,
            initial_conflict,
        )
    ]


def frontier_candidates_for_conflict(
    ours: Sequence[LoggedStatement],
    theirs: Sequence[LoggedStatement],
    first_conflict: ConflictPair,
    context: ConflictCheckContext,
    conflict_detector: ConflictDetector | None = None,
    statement_applier: StatementApplier | None = None,
) -> list[FrontierCandidate]:
    """Return candidate stopping points for a detected conflict."""

    conflict_detector = conflict_detector or default_conflict_detector()
    statement_applier = statement_applier or apply_statement_sequence

    return [
        search_by_backtracking_ours(
            ours,
            theirs,
            first_conflict,
            context,
            conflict_detector,
            statement_applier,
        ),
        search_by_backtracking_theirs(
            ours,
            theirs,
            first_conflict,
            context,
            conflict_detector,
            statement_applier,
        ),
    ]


def _standalone_replay_branches(
    conflict: ConflictPair | ConflictCheckResult,
) -> set[BranchName]:
    """Return branches blocked by already-replayed state, not by the pair."""

    conflicts = conflict.conflicts
    return {
        statement_conflict.scope
        for statement_conflict in conflicts
        if statement_conflict.kind in {"integrity", "replay_error"}
        and statement_conflict.scope in {"ours", "theirs"}
    } # type: ignore


def _conflict_scope(conflict: ConflictPair | ConflictCheckResult) -> ConflictScope:
    """Return a report-level scope for a conflict result."""

    scopes = {
        statement_conflict.scope
        for statement_conflict in conflict.conflicts
        if statement_conflict.scope in {"ours", "theirs"}
    }
    if scopes == {"ours", "theirs"}:
        return "both"
    if len(scopes) == 1:
        return cast(ConflictScope, next(iter(scopes)))
    return "pair"


def ordered_statement_plan(
    ours: Sequence[LoggedStatement],
    theirs: Sequence[LoggedStatement],
    frontier: FrontierCandidate,
) -> list[LoggedStatement]:
    """Return the replay order used by pairwise planning: X1, Y1, X2, Y2."""

    plan: list[LoggedStatement] = []
    for index in range(max(frontier.ours_count, frontier.theirs_count)):
        if index < frontier.ours_count:
            plan.append(ours[index])
        if index < frontier.theirs_count:
            plan.append(theirs[index])
    return plan


def _replay_error_conflict(error: sqlite3.Error, scope: BranchName) -> StatementConflict:
    """Return a scoped replay failure conflict from a SQLite exception."""

    return StatementConflict(
        kind="integrity" if isinstance(error, sqlite3.IntegrityError) else "replay_error",
        message=str(error),
        scope=scope,
    )


def _validation_error_conflict(errors: list[str], scope: BranchName) -> StatementConflict:
    """Return a scoped replay failure conflict from database validation errors."""

    return StatementConflict(
        kind="integrity",
        message="database validation failed after statement: " + "\n".join(errors),
        scope=scope,
    )


def _standalone_replay_failure_frontier(
    base_conn: sqlite3.Connection,
    ours: Sequence[LoggedStatement],
    theirs: Sequence[LoggedStatement],
    statement_plan: Sequence[LoggedStatement],
) -> FrontierCandidate | None:
    """Return the first standalone failure found while replaying a plan."""

    ours_count = 0
    theirs_count = 0
    with closing(sqlite3.connect(":memory:")) as replay_conn:
        base_conn.backup(replay_conn)
        replay_conn.execute("PRAGMA foreign_keys = ON")

        for statement in statement_plan:
            if statement.branch == "ours":
                statement_ours_index = ours_count
                statement_theirs_index = max(min(theirs_count, len(theirs) - 1), 0)
            else:
                statement_ours_index = max(min(ours_count, len(ours) - 1), 0)
                statement_theirs_index = theirs_count

            try:
                replay_conn.execute(statement.sql_text)
            except sqlite3.Error as exc:
                conflict = ConflictPair(
                    ours_index=statement_ours_index,
                    theirs_index=statement_theirs_index,
                    ours_sql=(
                        ours[statement_ours_index].original_sql_text
                        if ours else ""
                    ),
                    theirs_sql=(
                        theirs[statement_theirs_index].original_sql_text
                        if theirs else ""
                    ),
                    conflicts=(
                        _replay_error_conflict(exc, statement.branch),
                    ),
                )
                return FrontierCandidate(
                    name="standalone_replay",
                    ours_count=ours_count,
                    theirs_count=theirs_count,
                    next_conflict=conflict,
                    scope=statement.branch,
                )

            validation_errors = validate_database(replay_conn)
            if validation_errors:
                conflict = ConflictPair(
                    ours_index=statement_ours_index,
                    theirs_index=statement_theirs_index,
                    ours_sql=(
                        ours[statement_ours_index].original_sql_text
                        if ours else ""
                    ),
                    theirs_sql=(
                        theirs[statement_theirs_index].original_sql_text
                        if theirs else ""
                    ),
                    conflicts=(
                        _validation_error_conflict(validation_errors, statement.branch),
                    ),
                )
                return FrontierCandidate(
                    name="standalone_replay",
                    ours_count=ours_count,
                    theirs_count=theirs_count,
                    next_conflict=conflict,
                    scope=statement.branch,
                )

            if statement.branch == "ours":
                ours_count += 1
            else:
                theirs_count += 1

    return None


def build_merge_plan(
    base_db_path: str | Path,
    ours_db_path: str | Path,
    theirs_db_path: str | Path,
    conflict_detector: ConflictDetector | None = None,
    *,
    search_frontier: bool = True,
) -> MergePlan:
    (
        base_transaction_id,
        ours,
        theirs,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) = load_merge_inputs(base_db_path, ours_db_path, theirs_db_path)
    with closing(sqlite3.connect(base_db_path)) as base_conn:
        base_conn.row_factory = sqlite3.Row
        return build_merge_plan_from_connection(
            base_conn,
            str(base_db_path),
            base_transaction_id,
            ours,
            theirs,
            table_columns,
            primary_key_columns,
            key_column_sets,
            conflict_detector,
            search_frontier=search_frontier,
        )


def build_merge_plan_from_connection(
    base_conn: sqlite3.Connection,
    base_db_path: str,
    base_transaction_id: int,
    ours: list[LoggedStatement],
    theirs: list[LoggedStatement],
    table_columns: TableColumns,
    primary_key_columns: TablePrimaryKeyColumns,
    key_column_sets: TableKeyColumnSets,
    conflict_detector: ConflictDetector | None = None,
    *,
    search_frontier: bool = True,
) -> MergePlan:
    """Build a plan from an open database connection."""

    conflict_detector = conflict_detector or default_conflict_detector()

    # Plan on an in-memory copy so accepted pairs can affect later checks
    # without mutating the caller's database connection.
    with closing(sqlite3.connect(":memory:")) as planning_conn:
        base_conn.backup(planning_conn)
        planning_conn.row_factory = sqlite3.Row
        planning_conn.execute("PRAGMA foreign_keys = ON")
        context = ConflictCheckContext(
            base_cursor=planning_conn.cursor(),
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
        statement_plan = ordered_statement_plan(ours, theirs, selected)
        replay_failure = _standalone_replay_failure_frontier(
            base_conn,
            ours,
            theirs,
            statement_plan,
        )
        if replay_failure is not None:
            return MergePlan(
                status="conflict",
                base_transaction_id=base_transaction_id,
                ours=ours,
                theirs=theirs,
                selected=replay_failure,
                statement_plan=ordered_statement_plan(ours, theirs, replay_failure),
                first_conflict=replay_failure.next_conflict,
                candidates=[replay_failure],
            )

        return MergePlan(
            status="clean",
            base_transaction_id=base_transaction_id,
            ours=ours,
            theirs=theirs,
            selected=selected,
            statement_plan=statement_plan,
        )

    if not search_frontier:
        selected = FrontierCandidate(
            name="first_conflict",
            ours_count=first_conflict.ours_index,
            theirs_count=first_conflict.theirs_index,
            next_conflict=first_conflict,
            scope=_conflict_scope(first_conflict),
        )
        return MergePlan(
            status="conflict",
            base_transaction_id=base_transaction_id,
            ours=ours,
            theirs=theirs,
            selected=selected,
            statement_plan=ordered_statement_plan(ours, theirs, selected),
            first_conflict=first_conflict,
        )

    # Backtracking checks each candidate frontier against the prefix state
    # it would actually replay, then rolls that candidate state back.
    with closing(sqlite3.connect(":memory:")) as backtrack_conn:
        base_conn.backup(backtrack_conn)
        backtrack_conn.row_factory = sqlite3.Row
        backtrack_conn.execute("PRAGMA foreign_keys = ON")
        context = ConflictCheckContext(
            base_cursor=backtrack_conn.cursor(),
            base_db_path=base_db_path,
            table_columns=table_columns,
            primary_key_columns=primary_key_columns,
            key_column_sets=key_column_sets,
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
    """Record a replayed statement in the output database's merge log."""

    cursor = con.execute(
        f"INSERT INTO {TX_TABLE} DEFAULT VALUES",
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
    """Return post-statement integrity errors, including deferred FK failures."""

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

    with closing(sqlite3.connect(output_path)) as con:
        con.execute("PRAGMA foreign_keys = ON")

        for applied_count, statement in enumerate(statement_plan):
            if not statement.is_replay_safe:
                # Planning should already exclude unsafe statements, but replay
                # is the final guard before mutating the output database.
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
                # Some problems, especially deferred foreign keys, are only
                # visible after SQLite accepts the statement itself.
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
