from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Collection
from typing import Literal

from .accepted_replay import (
    _advance_transaction_on_main_and_control,
    _control_sql_for,
    _execute_statement_on_control,
    _execute_statement_on_main,
    _replay_failure_conflict,
)
from .conflict_kinds import kind_enabled
from .execution_based_analysis import (
    SQLiteReplayFailure,
    WriteReadProbeResult,
    _affected_primary_key_select,
    _create_probe_result_table,
    _is_update_from_statement,
    _probe_has_duplicate_target_rows,
    _read_probe_result,
    _select_difference_query,
    _temp_probe_result_table_name,
    _write_read_conflict_message,
)
from .models import (
    BranchName,
    ConflictCheckContext,
    ConflictCheckResult,
    ConflictKind,
    LoggedStatement,
    LoggedTransaction,
    StatementConflict,
    transaction_label,
)
from .static_analysis import write_read_candidate_indexes, write_write_candidate_pairs
from .utils import (
    is_delete_statement,
    is_update_statement,
    quote_identifier,
    rollback_savepoint,
)

CurrentWriteProbeTables = dict[str, dict[int, str]]


class OrderedRemainingExecutionScanner:
    """Rolling execution checker for current transaction vs opposite suffix."""

    def __init__(
        self,
        context: ConflictCheckContext,
        *,
        current_transaction: LoggedTransaction,
        current_branch: BranchName,
        enabled_kinds: Collection[ConflictKind],
    ) -> None:
        self.context = context
        self.current_transaction = current_transaction
        self.current_branch: BranchName = current_branch
        self.enabled_kinds: set[ConflictKind] = set(enabled_kinds)
        self._savepoint: str | None = None
        self._current_write_probe_tables: CurrentWriteProbeTables | None = None

    def start(self) -> ConflictCheckResult:
        """Start one scan by replaying current on the attached control DB."""

        cursor = self.context.base_cursor
        self._savepoint = quote_identifier(
            f"sqlite_merge_remaining_scan_{uuid.uuid4().hex}"
        )
        cursor.execute(f"SAVEPOINT {self._savepoint}")

        # Main is the comparison state without the current transaction. Capture
        # current write probes while replaying current on control so later edits
        # to the opposite queue can enable write/write checking without
        # restarting the scan.
        failure = self._start_control_with_current_write_probes()
        if failure is not None:
            return ConflictCheckResult((_replay_failure_conflict(failure),))

        return ConflictCheckResult()

    def enable_kinds(self, enabled_kinds: Collection[ConflictKind]) -> None:
        """Allow this scan to check additional conflict kinds."""

        self.enabled_kinds.update(enabled_kinds)

    def check_next(
        self,
        ours_transaction: LoggedTransaction,
        theirs_transaction: LoggedTransaction,
        static_result: ConflictCheckResult,
    ) -> ConflictCheckResult:
        """Check one opposite transaction, then advance both scan states."""

        other_transaction = (
            theirs_transaction if self.current_branch == "ours" else ours_transaction
        )

        savepoint = quote_identifier(
            f"sqlite_merge_remaining_next_{uuid.uuid4().hex}"
        )
        self.context.base_cursor.execute(f"SAVEPOINT {savepoint}")
        try:
            result = static_result
            if kind_enabled(self.enabled_kinds, "write_write"):
                result = self._check_write_write(
                    ours_transaction,
                    theirs_transaction,
                    result,
                )
                if result.has_kind("write_write"):
                    rollback_savepoint(self.context.base_cursor, savepoint)
                    return result

            if kind_enabled(self.enabled_kinds, "write_read"):
                result = self._check_write_read(other_transaction, result)
                if result.has_kind("write_read"):
                    rollback_savepoint(self.context.base_cursor, savepoint)
                    return result

            if result.has_conflict:
                rollback_savepoint(self.context.base_cursor, savepoint)
                return result

            advance_result = _advance_transaction_on_main_and_control(
                self.context,
                self.current_transaction,
                other_transaction,
                order_label=(
                    f"{transaction_label(self.current_transaction)} before "
                    f"{transaction_label(other_transaction)}"
                ),
                scope="pair",
                check_constraint_resolution=(
                    kind_enabled(self.enabled_kinds, "integrity")
                ),
            )
            if advance_result.has_conflict:
                rollback_savepoint(self.context.base_cursor, savepoint)
                return advance_result

            self.context.base_cursor.execute(f"RELEASE {savepoint}")
            return result
        except Exception:
            rollback_savepoint(self.context.base_cursor, savepoint)
            raise

    def accept_next(
        self,
        other_transaction: LoggedTransaction,
        accepted_result: ConflictCheckResult,
    ) -> ConflictCheckResult:
        """Advance scan state after the user accepts a reviewable pair conflict."""

        savepoint = quote_identifier(
            f"sqlite_merge_remaining_accept_{uuid.uuid4().hex}"
        )
        self.context.base_cursor.execute(f"SAVEPOINT {savepoint}")
        try:
            result = _advance_transaction_on_main_and_control(
                self.context,
                self.current_transaction,
                other_transaction,
                order_label=(
                    f"{transaction_label(self.current_transaction)} before "
                    f"{transaction_label(other_transaction)}"
                ),
                scope="pair",
                check_constraint_resolution=False,
            )
            if result.has_conflict:
                rollback_savepoint(self.context.base_cursor, savepoint)
                return accepted_result.add_conflicts(*result.conflicts)

            self.context.base_cursor.execute(f"RELEASE {savepoint}")
            return ConflictCheckResult()
        except Exception:
            rollback_savepoint(self.context.base_cursor, savepoint)
            raise

    def close(self) -> None:
        """Discard current and opposite-suffix effects from the scan."""

        if self._savepoint is None:
            return
        rollback_savepoint(self.context.base_cursor, self._savepoint)
        self._savepoint = None

    def _check_write_write(
        self,
        ours_transaction: LoggedTransaction,
        theirs_transaction: LoggedTransaction,
        result: ConflictCheckResult,
    ) -> ConflictCheckResult:
        """Use rolling-prefix affected PK probes for write/write conflicts."""

        if not result.has_kind("write_write"):
            return result
        if _has_cascade_metadata_conflict(result, "write_write"):
            return result

        candidate_pairs = write_write_candidate_pairs(
            self.context,
            ours_transaction.metadata,
            theirs_transaction.metadata,
        )
        if not candidate_pairs:
            return result.without_kind("write_write")

        if self.current_branch == "ours":
            current_indexes = {ours_index for ours_index, _ in candidate_pairs}
        else:
            current_indexes = {theirs_index for _, theirs_index in candidate_pairs}

        if self._current_write_probe_tables is None:
            return result
        available_indexes = _current_write_probe_indexes(
            self._current_write_probe_tables,
        )
        missing_indexes = current_indexes - available_indexes
        if missing_indexes:
            return result

        overlap = _find_write_write_overlap(
            self.context,
            current_branch=self.current_branch,
            current_write_probe_tables=self._current_write_probe_tables,
            ours_transaction=ours_transaction,
            theirs_transaction=theirs_transaction,
            candidate_pairs=candidate_pairs,
        )
        if overlap is None:
            return result.without_kind("write_write")
        if overlap == "not_refined":
            return result

        first_statement, second_statement = overlap
        first_label = _transaction_scoped_statement_label(
            ours_transaction,
            first_statement,
        )
        second_label = _transaction_scoped_statement_label(
            theirs_transaction,
            second_statement,
        )
        return result.replace_kind(
            "write_write",
            (
                StatementConflict(
                    kind="write_write",
                    message=(
                        f"{first_label} and {second_label} update/delete "
                        "overlapping rows"
                    ),
                ),
            ),
        )

    def _start_control_with_current_write_probes(
        self,
    ) -> SQLiteReplayFailure | None:
        """Replay current on control while saving its write probes as temp tables."""

        self._current_write_probe_tables = {}
        for index, statement in enumerate(self.current_transaction.statements):
            if (
                is_update_statement(statement.metadata)
                or is_delete_statement(statement.metadata)
            ):
                target_table = statement.metadata.table_updated
                assert target_table is not None
                temp_table = _statement_write_probe_table(
                    self.context,
                    statement,
                    use_control=True,
                )
                if temp_table is not None:
                    self._current_write_probe_tables.setdefault(
                        target_table,
                        {},
                    )[index] = temp_table

            failure = _execute_statement_on_control(
                self.context,
                statement,
                scope=self.current_branch,
                order_label=f"control {transaction_label(self.current_transaction)}",
            )
            if failure is not None:
                return failure
        return None

    def _check_write_read(
        self,
        other_transaction: LoggedTransaction,
        result: ConflictCheckResult,
    ) -> ConflictCheckResult:
        """Use main/control read probes for current -> opposite read conflicts."""

        if not result.has_kind("write_read"):
            return result
        if _has_cascade_metadata_conflict(result, "write_read"):
            return result

        check = _rolling_write_read_dependency(
            self.context,
            writer_transaction=self.current_transaction,
            reader_transaction=other_transaction,
        )
        if check.status == "not_refined":
            return result
        if check.status == "unaffected":
            return result.without_kind("write_read")
        return result.replace_kind(
            "write_read",
            (
                StatementConflict(
                    kind="write_read",
                    message=_write_read_conflict_message(
                        self.current_transaction,
                        other_transaction,
                        check,
                    ),
                ),
            ),
        )

def _has_cascade_metadata_conflict(
    result: ConflictCheckResult,
    kind: ConflictKind,
) -> bool:
    """Return whether a static conflict depends on cascade-derived metadata."""

    return any(
        ("metadata_source", "cascade") in conflict.details
        for conflict in result.of_kind(kind)
    )


def _statement_write_probe_table(
    context: ConflictCheckContext,
    statement: LoggedStatement,
    *,
    use_control: bool,
) -> str | None:
    """Store one statement's affected primary keys in a temp table."""

    probe = _statement_write_probe_sql(
        context,
        statement,
        use_control=use_control,
    )
    if probe is None:
        return None

    table_name = _temp_probe_result_table_name()
    try:
        _create_probe_result_table(context.base_cursor, table_name, probe)
    except sqlite3.Error:
        return None
    return table_name


def _statement_write_probe_sql(
    context: ConflictCheckContext,
    statement: LoggedStatement,
    *,
    use_control: bool,
) -> str | None:
    """Return one statement's affected primary-key probe SQL."""

    probe = _affected_primary_key_select(context, statement.metadata)
    if probe is None:
        return None

    if use_control:
        return _control_sql_for(context, probe)
    return probe


def _current_write_probe_indexes(
    probe_tables: CurrentWriteProbeTables,
) -> set[int]:
    """Return current statement indexes that have materialized write probes."""

    return {
        statement_index
        for tables_by_statement in probe_tables.values()
        for statement_index in tables_by_statement
    }


def _transaction_scoped_statement_label(
    transaction: LoggedTransaction,
    statement: LoggedStatement,
) -> str:
    """Return the statement label shown by the transaction conflict editor."""

    transaction_prefix = transaction_label(transaction)
    try:
        statement_index = transaction.statements.index(statement) + 1
    except ValueError:
        return transaction_prefix
    return f"{transaction_prefix}.{statement_index}"


def _find_write_write_overlap(
    context: ConflictCheckContext,
    *,
    current_branch: BranchName,
    current_write_probe_tables: CurrentWriteProbeTables,
    ours_transaction: LoggedTransaction,
    theirs_transaction: LoggedTransaction,
    candidate_pairs: tuple[tuple[int, int], ...],
) -> tuple[LoggedStatement, LoggedStatement] | Literal["not_refined"] | None:
    """Compare current write temp tables with opposite write probes."""

    other_transaction = (
        theirs_transaction if current_branch == "ours" else ours_transaction
    )
    savepoint = quote_identifier(
        f"sqlite_merge_remaining_write_probe_{uuid.uuid4().hex}"
    )
    context.base_cursor.execute(f"SAVEPOINT {savepoint}")
    try:
        for other_index, statement in enumerate(other_transaction.statements):
            matching_pairs = _candidate_pairs_for_other_statement(
                current_branch,
                candidate_pairs,
                other_index,
            )
            if matching_pairs:
                target_table = statement.metadata.table_updated
                assert target_table is not None

                current_probe_tables = current_write_probe_tables.get(target_table)
                if current_probe_tables is None:
                    return "not_refined"

                other_probe = _statement_write_probe_sql(
                    context,
                    statement,
                    use_control=False,
                )
                if other_probe is None:
                    return "not_refined"

                other_table = None
                try:
                    if len(matching_pairs) > 1:
                        other_table = _temp_probe_result_table_name()
                        try:
                            _create_probe_result_table(
                                context.base_cursor,
                                other_table,
                                other_probe,
                            )
                        except sqlite3.Error:
                            return "not_refined"

                    for ours_index, theirs_index in matching_pairs:
                        current_index = (
                            ours_index if current_branch == "ours" else theirs_index
                        )
                        current_table = current_probe_tables.get(current_index)
                        if current_table is None:
                            return "not_refined"
                        if other_table is None:
                            overlap = _temp_table_intersects_probe(
                                context.base_cursor,
                                current_table,
                                other_probe,
                            )
                        else:
                            overlap = _temp_tables_intersect(
                                context.base_cursor,
                                current_table,
                                other_table,
                            )
                        if overlap is None:
                            return "not_refined"
                        if overlap:
                            return (
                                ours_transaction.statements[ours_index],
                                theirs_transaction.statements[theirs_index],
                            )
                finally:
                    if other_table is not None:
                        context.base_cursor.execute(
                            f"DROP TABLE IF EXISTS {quote_identifier(other_table)}"
                        )

            failure = _execute_statement_on_main(
                context,
                statement,
                scope="pair",
                order_label=transaction_label(other_transaction),
            )
            if failure is not None:
                return "not_refined"
        return None
    finally:
        rollback_savepoint(context.base_cursor, savepoint)


def _candidate_pairs_for_other_statement(
    current_branch: BranchName,
    candidate_pairs: tuple[tuple[int, int], ...],
    other_index: int,
) -> tuple[tuple[int, int], ...]:
    """Return write/write candidate pairs involving one opposite statement."""

    return tuple(
        (ours_index, theirs_index)
        for ours_index, theirs_index in candidate_pairs
        if (
            theirs_index if current_branch == "ours" else ours_index
        ) == other_index
    )


def _temp_table_intersects_probe(
    cursor: sqlite3.Cursor,
    table_name: str,
    probe_sql: str,
) -> bool | None:
    """Return whether a temp probe table overlaps an inline probe."""

    query = (
        "SELECT 1 FROM ("
        f"SELECT * FROM {quote_identifier(table_name)} "
        "INTERSECT "
        f"SELECT * FROM ({probe_sql})"
        ") LIMIT 1"
    )
    try:
        return cursor.execute(query).fetchone() is not None
    except sqlite3.Error:
        return None


def _temp_tables_intersect(
    cursor: sqlite3.Cursor,
    first_table: str,
    second_table: str,
) -> bool | None:
    """Return whether two temp probe tables share at least one row."""

    query = (
        "SELECT 1 FROM ("
        f"SELECT * FROM {quote_identifier(first_table)} "
        "INTERSECT "
        f"SELECT * FROM {quote_identifier(second_table)}"
        ") LIMIT 1"
    )
    try:
        return cursor.execute(query).fetchone() is not None
    except sqlite3.Error:
        return None


def _rolling_write_read_dependency(
    context: ConflictCheckContext,
    *,
    writer_transaction: LoggedTransaction,
    reader_transaction: LoggedTransaction,
) -> WriteReadProbeResult:
    """Compare reader probes on main vs control while advancing one transaction."""

    indexes = set(
        write_read_candidate_indexes(
            context,
            writer_transaction.metadata,
            reader_transaction.metadata,
        )
    )
    if not indexes:
        return WriteReadProbeResult("unaffected")

    affected_reader_indexes: list[int] = []
    cursor = context.base_cursor
    savepoint = quote_identifier(
        f"sqlite_merge_remaining_read_probe_{uuid.uuid4().hex}"
    )
    cursor.execute(f"SAVEPOINT {savepoint}")
    try:
        for index, statement in enumerate(reader_transaction.statements):
            if index in indexes:
                probe = _read_probe_result(context, statement.metadata)
                if probe.status == "no_read_dependency":
                    indexes.discard(index)
                elif probe.status == "not_refined" or probe.sql is None:
                    return WriteReadProbeResult("not_refined", probe.reason)
                else:
                    control_probe = _control_sql_for(context, probe.sql)
                    if control_probe is None:
                        return WriteReadProbeResult(
                            "not_refined",
                            "reader probe cannot be rewritten for control database",
                        )
                    if _is_update_from_statement(statement.metadata) and (
                        _probe_has_duplicate_target_rows(
                            context,
                            statement.metadata,
                            probe.sql,
                        )
                        or _probe_has_duplicate_target_rows(
                            context,
                            statement.metadata,
                            control_probe,
                        )
                    ):
                        return WriteReadProbeResult(
                            "not_refined",
                            "reader UPDATE FROM has multiple source rows",
                        )
                    before_table = _temp_probe_result_table_name()
                    after_table = _temp_probe_result_table_name()
                    try:
                        _create_probe_result_table(cursor, before_table, probe.sql)
                        _create_probe_result_table(
                            cursor,
                            after_table,
                            control_probe,
                        )
                        if _temp_tables_differ(
                            cursor,
                            before_table,
                            after_table,
                        ):
                            affected_reader_indexes.append(index)
                    except sqlite3.Error as exc:
                        return WriteReadProbeResult("not_refined", str(exc))
                    finally:
                        cursor.execute(
                            f"DROP TABLE IF EXISTS {quote_identifier(before_table)}"
                        )
                        cursor.execute(
                            f"DROP TABLE IF EXISTS {quote_identifier(after_table)}"
                        )

            failure = _execute_statement_on_main(
                context,
                statement,
                scope="pair",
                order_label=transaction_label(reader_transaction),
            )
            if failure is not None:
                return WriteReadProbeResult("not_refined", failure.message)
            failure = _execute_statement_on_control(
                context,
                statement,
                scope="pair",
                order_label=transaction_label(reader_transaction),
            )
            if failure is not None:
                return WriteReadProbeResult("not_refined", failure.message)
    finally:
        rollback_savepoint(cursor, savepoint)

    if affected_reader_indexes:
        return WriteReadProbeResult(
            "affected",
            affected_reader_indexes=tuple(affected_reader_indexes),
        )
    return WriteReadProbeResult("unaffected")


def _temp_tables_differ(
    cursor: sqlite3.Cursor,
    first_table: str,
    second_table: str,
) -> bool:
    """Return whether two temp probe result tables differ."""

    query = _select_difference_query(
        f"SELECT * FROM {quote_identifier(first_table)}",
        f"SELECT * FROM {quote_identifier(second_table)}",
    )
    return cursor.execute(query).fetchone() is not None
