from __future__ import annotations

import sqlite3
from contextlib import closing
from collections.abc import Sequence
from dataclasses import replace
from itertools import groupby
from pathlib import Path

from sqlglot.errors import ParseError

from .conflict_detection import (
    ConflictResolutionKey,
    OrderedRemainingConflictScanner,
)
from .models import (
    BranchName,
    ConflictCheckContext,
    ConflictCheckResult,
    ConflictPair,
    LoggedStatement,
    LoggedTransaction,
    MergeNotApplicableError,
)
from .sql_metadata import (
    parse_statement_metadata,
    transaction_metadata,
    unsupported_statement_metadata,
)
from .remaining_metadata import (
    RemainingMetadataIndex,
    remaining_individual_check_kinds,
)
from .utils import (
    TableKeyColumnSets,
    TableColumns,
    TablePrimaryKeyColumns,
    load_key_column_sets,
    load_primary_key_columns,
    load_table_columns as load_user_table_columns,
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

def make_logged_statement(
    branch: BranchName,
    branch_index: int,
    transaction_id: int,
    committed_at: str,
    sql_text: str,
    table_columns: TableColumns | None = None,
    original_sql_text: str | None = None,
    is_replay_safe: bool = True,
    replay_block_reason: str | None = None,
    replay_warnings: Sequence[str] = (),
    accepted_replay_warnings: Sequence[str] | frozenset[str] = (),
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
        transaction_id=transaction_id,
        committed_at=committed_at,
        original_sql_text=original_sql_text or sql_text,
        to_replay_sql_text=sql_text,
        is_replay_safe=is_replay_safe,
        replay_block_reason=replay_block_reason,
        metadata=metadata,
        replay_warnings=tuple(replay_warnings),
        accepted_replay_warnings=frozenset(accepted_replay_warnings),
    )


def replay_warning_reason(statement: LoggedStatement) -> str | None:
    """Return the reviewable warning reason for unsafe logged SQL."""

    if statement.is_replay_safe or statement.replay_block_reason is None:
        return None

    reason = statement.replay_block_reason
    if any(reason.startswith(prefix) for prefix in ACKNOWLEDGEABLE_REPLAY_REASONS):
        return reason
    return None


def pending_replay_warning(statement: LoggedStatement) -> str | None:
    """Return an unaccepted warning for unsafe logged SQL."""

    warning = replay_warning_reason(statement)
    if warning is None or warning in statement.accepted_replay_warnings:
        return None
    return warning


def unresolved_replay_block_reason(statement: LoggedStatement) -> str | None:
    """Return why a statement still cannot replay automatically."""

    if statement.is_replay_safe:
        return None

    warning = replay_warning_reason(statement)
    if warning is not None and warning in statement.accepted_replay_warnings:
        return None

    return statement.replay_block_reason or "statement is unsafe for replay"


def accept_replay_warning(
    statement: LoggedStatement,
    warning: str,
) -> LoggedStatement:
    """Record that the user accepted one replay warning for this merge run."""

    if warning in statement.accepted_replay_warnings:
        return statement
    return replace(
        statement,
        accepted_replay_warnings=statement.accepted_replay_warnings | {warning},
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


def require_valid_base(con: sqlite3.Connection, base_db_path: str | Path) -> None:
    """Reject merge bases with pre-existing integrity or FK errors."""

    errors = validate_database(con)
    if errors:
        raise MergeNotApplicableError(
            base_db_path,
            "base",
            reason="has pre-existing integrity errors",
            details=errors,
        )


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
        SELECT l.transaction_id,
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


def load_logged_transactions(
    cursor: sqlite3.Cursor,
    branch: BranchName,
    since_transaction_id: int,
    db_path: str | Path,
    table_columns: TableColumns | None = None,
) -> list[LoggedTransaction]:
    """Load branch log entries grouped by committed transaction."""

    return group_logged_transactions(
        load_logged_statements(
            cursor,
            branch,
            since_transaction_id,
            db_path,
            table_columns,
        )
    )


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
    list[LoggedTransaction],
    list[LoggedTransaction],
    TableColumns,
    TablePrimaryKeyColumns,
    TableKeyColumnSets,
]:
    """Load logged branch transactions and schema metadata for one merge."""

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
        require_valid_base(base_conn, base_db_path)
        ours = load_logged_transactions(
            ours_conn.cursor(),
            "ours",
            base_transaction_id,
            ours_db_path,
            table_columns=table_columns,
        )
        theirs = load_logged_transactions(
            theirs_conn.cursor(),
            "theirs",
            base_transaction_id,
            theirs_db_path,
            table_columns=table_columns,
        )

    return (
        ours,
        theirs,
        table_columns,
        primary_key_columns,
        key_column_sets,
    )


def group_logged_transactions(
    statements: Sequence[LoggedStatement],
) -> list[LoggedTransaction]:
    """Group consecutive logged statements by transaction id."""

    return [
        _logged_transaction(list(group), branch_index=index)
        for index, (_, group) in enumerate(
            groupby(statements, key=lambda statement: statement.transaction_id)
        )
    ]


def _logged_transaction(
    statements: Sequence[LoggedStatement],
    *,
    branch_index: int,
) -> LoggedTransaction:
    """Build one transaction wrapper from a non-empty statement sequence."""

    first = statements[0]
    return LoggedTransaction(
        branch=first.branch,
        branch_index=branch_index,
        transaction_id=first.transaction_id,
        committed_at=first.committed_at,
        statements=tuple(statements),
        metadata=transaction_metadata(
            tuple(statement.metadata for statement in statements)
        ),
    )


def _current_conflict_pair(
    current: LoggedTransaction,
    remaining_other: Sequence[LoggedTransaction],
    *,
    current_branch: BranchName,
    other_index: int | None,
    result: ConflictCheckResult | None = None,
    resolution_key: ConflictResolutionKey | None = None,
) -> ConflictPair:
    """Build report-friendly conflict data using replay SQL for display."""

    other = None if other_index is None else remaining_other[other_index]
    ours_sql = (
        current.sql_text
        if current_branch == "ours"
        else "" if other is None else other.sql_text
    )
    theirs_sql = (
        current.sql_text
        if current_branch == "theirs"
        else "" if other is None else other.sql_text
    )
    return ConflictPair(
        current_branch=current_branch,
        other_index=other_index,
        ours_sql=ours_sql,
        theirs_sql=theirs_sql,
        conflicts=() if result is None else result.conflicts,
        resolution_key=resolution_key,
        is_standalone=other_index is None,
    )


class _RemainingCurrentConflictScan:
    """Build queue-relative conflict pairs while preserving one rolling scan."""

    def __init__(
        self,
        current: LoggedTransaction,
        *,
        current_branch: BranchName,
        context: ConflictCheckContext,
        remaining_other_index: RemainingMetadataIndex,
        accepted_pair_keys: set[ConflictResolutionKey] | None = None,
    ) -> None:
        self.current = current
        self.current_branch: BranchName = current_branch
        self.context = context
        self._accepted_pair_keys = (
            accepted_pair_keys if accepted_pair_keys is not None else set()
        )
        self._enabled_kinds = remaining_individual_check_kinds(
            context,
            current,
            remaining_other_index,
        )
        self._scanner = (
            None
            if not self._enabled_kinds
            else OrderedRemainingConflictScanner(
                context,
                current,
                current_branch=current_branch,
                enabled_kinds=self._enabled_kinds,
                accepted_pair_keys=self._accepted_pair_keys,
            )
        )

    def next_conflict(
        self,
        remaining_other: Sequence[LoggedTransaction],
    ) -> ConflictPair | None:
        """Return the next conflict from the current scanner state."""

        if self._scanner is None:
            return None

        conflict = self._scanner.next_conflict(remaining_other)
        if conflict is None:
            return None

        return self._conflict_pair(remaining_other, conflict)

    def accept_current_conflict(
        self,
        conflict: ConflictPair,
        remaining_other: Sequence[LoggedTransaction],
    ) -> ConflictPair | None:
        """Accept one pair conflict and return any failure from advancing it."""

        if self._scanner is None:
            raise RuntimeError("cannot accept without an active scanner")
        if conflict.other_index is None:
            raise RuntimeError("cannot accept a standalone replay conflict")

        accepted_conflict = self._scanner.accept_current_conflict(
            remaining_other,
            conflict.other_index,
            ConflictCheckResult(conflict.conflicts),
            conflict.resolution_key,
        )
        if accepted_conflict is None:
            return None
        return self._conflict_pair(remaining_other, accepted_conflict)

    def _conflict_pair(
        self,
        remaining_other: Sequence[LoggedTransaction],
        conflict: tuple[int | None, ConflictCheckResult, ConflictResolutionKey | None],
    ) -> ConflictPair:
        """Convert scanner-internal conflict data to UI-facing conflict data."""

        other_index, result, resolution_key = conflict
        return _current_conflict_pair(
            self.current,
            remaining_other,
            current_branch=self.current_branch,
            other_index=other_index,
            result=result,
            resolution_key=resolution_key,
        )

    def enable_checks_after_other_edit(
        self,
        remaining_other_index: RemainingMetadataIndex,
    ) -> None:
        """Enable any new conflict kinds introduced by editing the other side.

        This is called only after this scan has already reported a pair conflict,
        so the execution scanner must exist. Deleting the other transaction does
        not need this because there is no edited SQL to recheck.
        """

        if self._scanner is None:
            raise RuntimeError("cannot enable checks without an active scanner")

        needed_kinds = remaining_individual_check_kinds(
            self.context,
            self.current,
            remaining_other_index,
        )
        self._scanner.enable_kinds(needed_kinds)

    def close(self) -> None:
        """Discard scan-only effects."""

        if self._scanner is not None:
            self._scanner.close()


def _remaining_conflict_for_current(
    current: LoggedTransaction,
    remaining_other: Sequence[LoggedTransaction],
    *,
    current_branch: BranchName,
    context: ConflictCheckContext,
    remaining_other_index: "RemainingMetadataIndex",
    accepted_pair_keys: set[ConflictResolutionKey] | None = None,
) -> ConflictPair | None:
    """Return the first conflict between current and the other remaining side."""

    scan = _RemainingCurrentConflictScan(
        current,
        current_branch=current_branch,
        context=context,
        remaining_other_index=remaining_other_index,
        accepted_pair_keys=accepted_pair_keys,
    )
    try:
        return scan.next_conflict(remaining_other)
    finally:
        scan.close()


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
