from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from collections import Counter
from contextlib import closing
from pathlib import Path
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from sqlglot import expressions as exp
from sqlite_conflict_resolution import strict_conflict_resolution_rewrite

from .log_merge import (
    ConflictCheckContext,
    ConflictScope,
    ConflictKind,
    ConflictCheckResult,
    LoggedTransaction,
    LoggedStatement,
    StatementConflict,
)
from .models import (
    statement_label,
    transaction_label,
)
from .static_analysis import write_read_candidate_indexes, write_write_candidate_pairs
from .sql_metadata import StatementMetadata
from .utils import (
    is_delete_statement,
    is_insert_statement,
    is_update_statement,
    primary_key_columns as schema_primary_key_columns,
    quote_identifier,
    rollback_savepoint,
    table_expression,
)

WriteReadProbeStatus = Literal["affected", "unaffected", "not_refined"]
ReadProbeStatus = Literal["ok", "no_read_dependency", "not_refined"]


@dataclass(frozen=True)
class WriteReadProbeResult:
    """Result of trying to refine one static write-read conflict."""

    status: WriteReadProbeStatus
    reason: str | None = None
    affected_reader_indexes: tuple[int, ...] = ()


@dataclass(frozen=True)
class ReadProbeResult:
    """A read probe, or why no probe should be run."""

    status: ReadProbeStatus
    sql: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class SQLiteReplayFailure:
    """SQLite failure observed while trying one replay order."""

    kind: ConflictKind
    scope: ConflictScope
    message: str
    order_label: str
    statement: LoggedStatement | None = None


def commutativity_check(
    context: ConflictCheckContext,
    first: LoggedStatement,
    second: LoggedStatement,
) -> ConflictCheckResult:
    """Replay A->B and B->A on base copies, then compare with sqldiff."""

    with tempfile.TemporaryDirectory() as tmp_dir:
        first_then_second = Path(tmp_dir) / "first_then_second.db"
        second_then_first = Path(tmp_dir) / "second_then_first.db"
        shutil.copy2(context.base_db_path, first_then_second)
        shutil.copy2(context.base_db_path, second_then_first)

        error = _replay_statements(first_then_second, (first, second))
        if error is not None:
            return ConflictCheckResult((
                StatementConflict(kind="replay_error", message=error),
            ))

        error = _replay_statements(second_then_first, (second, first))
        if error is not None:
            return ConflictCheckResult((
                StatementConflict(kind="replay_error", message=error),
            ))

        diff = _sqldiff(first_then_second, second_then_first)
        if diff is None:
            return ConflictCheckResult((
                StatementConflict(kind="replay_error", message="sqldiff not found"),
            ))
        if diff:
            return ConflictCheckResult((
                StatementConflict(kind="non_commutative", message="commutativity check"),
            ))

    return ConflictCheckResult()


def execution_based_matching(
    context: ConflictCheckContext,
    ours_transaction: LoggedTransaction,
    theirs_transaction: LoggedTransaction,
    static_result: ConflictCheckResult,
    *,
    check_integrity: bool = True,
) -> ConflictCheckResult:
    """Recheck static conflicts with targeted SQLite execution where possible."""

    if check_integrity:
        conflicts = sqlite_replay_conflicts(context, ours_transaction, theirs_transaction)
        if conflicts:
            return ConflictCheckResult(conflicts)

    result = static_result
    result = _check_write_read(
        context,
        ours_transaction,
        theirs_transaction,
        result,
    )
    result = _check_write_write(
        context,
        ours_transaction,
        theirs_transaction,
        result,
    )
    return result


def sqlite_replay_conflicts(
    context: ConflictCheckContext,
    ours_transaction: LoggedTransaction,
    theirs_transaction: LoggedTransaction,
) -> tuple[StatementConflict, ...]:
    """Return integrity or replay conflicts from trying both pair orders."""

    failures = _pair_replay_failures(context, ours_transaction, theirs_transaction)
    if not failures:
        return _constraint_resolution_conflicts(
            context,
            ours_transaction,
            theirs_transaction,
        )

    branch_failures = [
        failure
        for failure in failures
        if failure.scope in {"ours", "theirs"}
    ]
    if branch_failures:
        return tuple(
            _standalone_replay_conflict(failure)
            for failure in branch_failures
        )

    failure = failures[0]
    return (
        StatementConflict(
            kind=failure.kind,
            message=f"{failure.order_label}: {failure.message}",
        ),
    )


def _constraint_resolution_conflicts(
    context: ConflictCheckContext,
    ours_transaction: LoggedTransaction,
    theirs_transaction: LoggedTransaction,
) -> tuple[StatementConflict, ...]:
    """
    Detect successful statements that only succeed due to conflict resolution.

    The original SQL has already replayed successfully. Replaying a strict
    version without OR IGNORE / OR REPLACE / UPSERT tells us whether SQLite's
    conflict-resolution path was required under the current prefix.
    """

    conflicts: list[StatementConflict] = []
    context.base_cursor.execute("PRAGMA foreign_keys = ON")
    for transaction, other_transaction in (
        (ours_transaction, theirs_transaction),
        (theirs_transaction, ours_transaction),
    ):
        strict_transaction = _strict_conflict_resolution_transaction(transaction)
        if strict_transaction is None:
            continue

        standalone_failure = _savepoint_replay_failure(
            context.base_cursor,
            strict_transaction.statements,
            (),
            f"strict {transaction_label(transaction)}",
        )
        if standalone_failure is not None:
            conflicts.append(
                _constraint_resolution_conflict(
                    transaction,
                    standalone_failure,
                    scope=transaction.branch,
                    after_transaction=None,
                )
            )
            continue

        pair_failure = _savepoint_replay_failure(
            context.base_cursor,
            other_transaction.statements,
            strict_transaction.statements,
            f"{transaction_label(other_transaction)} then strict "
            f"{transaction_label(transaction)}",
        )
        if pair_failure is not None:
            conflicts.append(
                _constraint_resolution_conflict(
                    transaction,
                    pair_failure,
                    scope="pair",
                    after_transaction=other_transaction,
                )
            )

    return tuple(conflicts)


def _strict_conflict_resolution_transaction(
    transaction: LoggedTransaction,
) -> LoggedTransaction | None:
    """Return a copy with reviewable conflict-resolution syntax stripped."""

    changed = False
    statements: list[LoggedStatement] = []
    for statement in transaction.statements:
        rewrite = strict_conflict_resolution_rewrite(statement.sql_text)
        if rewrite is None:
            statements.append(statement)
            continue

        changed = True
        statements.append(
            replace(
                statement,
                to_replay_sql_text=rewrite.sql,
                replay_warnings=(
                    *statement.replay_warnings,
                    f"strict replay removed {rewrite.label}",
                ),
            )
        )

    if not changed:
        return None
    return replace(transaction, statements=tuple(statements))


def _constraint_resolution_conflict(
    transaction: LoggedTransaction,
    failure: SQLiteReplayFailure,
    *,
    scope: ConflictScope,
    after_transaction: LoggedTransaction | None,
) -> StatementConflict:
    """Build a reviewable conflict-resolution warning from strict replay."""

    if failure.kind != "integrity":
        return StatementConflict(
            kind=failure.kind,
            scope=scope,
            message=f"strict replay probe failed: {failure.message}",
        )

    after_text = (
        f" after {transaction_label(after_transaction)}"
        if after_transaction is not None
        else " under the current prefix"
    )
    return StatementConflict(
        kind="constraint_resolution",
        scope="pair",
        message=(
            f"{transaction_label(transaction)} uses SQLite conflict-resolution "
            f"syntax; removing it fails{after_text}: {failure.message}. "
            "The original SQL succeeds, so this is reviewable rather than a "
            "hard replay error."
        ),
    )


def _check_write_write(
    context: ConflictCheckContext,
    ours_transaction: LoggedTransaction,
    theirs_transaction: LoggedTransaction,
    result: ConflictCheckResult,
) -> ConflictCheckResult:
    """Use affected primary-key rows to clear supported write/write conflicts."""

    if not result.has_kind("write_write"):
        return result

    if len(ours_transaction.statements) == 1 and len(theirs_transaction.statements) == 1:
        row_overlap = statement_write_write_row_overlap(
            context,
            ours_transaction.statements[0].metadata,
            theirs_transaction.statements[0].metadata,
        )
        if row_overlap is None:
            return result
        if not row_overlap:
            return result.without_kind("write_write")

        return result.replace_kind(
            "write_write",
            (
                StatementConflict(
                    kind="write_write",
                    message=(
                        f"{statement_label(ours_transaction.statements[0])} and "
                        f"{statement_label(theirs_transaction.statements[0])} "
                        "update/delete overlapping rows"
                    ),
                ),
            ),
        )

    return _check_transaction_write_write(
        context,
        ours_transaction,
        theirs_transaction,
        result,
    )


def statement_write_write_row_overlap(
    context: ConflictCheckContext,
    ours_metadata: StatementMetadata,
    theirs_metadata: StatementMetadata,
) -> bool | None:
    """Return whether supported single statements affect overlapping PK rows."""

    ours_select = _affected_primary_key_select(context, ours_metadata)
    theirs_select = _affected_primary_key_select(context, theirs_metadata)
    if ours_select is None or theirs_select is None:
        return None

    query = (
        "SELECT 1 FROM ("
        f"SELECT * FROM ({ours_select}) "
        "INTERSECT "
        f"SELECT * FROM ({theirs_select})"
        ") LIMIT 1"
    )
    return context.base_cursor.execute(query).fetchone() is not None


def statement_write_read_dependency_outcome(
    context: ConflictCheckContext,
    writer_statement: LoggedStatement,
    reader_metadata: StatementMetadata,
) -> WriteReadProbeResult:
    """Return whether the writer affects reader output in a savepoint."""

    writer_metadata = writer_statement.metadata
    if not _can_simulate_writer(writer_metadata):
        return WriteReadProbeResult(
            "not_refined",
            "writer statement cannot be simulated safely",
        )

    probe_result = _read_probe_result(context, reader_metadata)
    if probe_result.status == "no_read_dependency":
        return WriteReadProbeResult("unaffected")
    if probe_result.status == "not_refined" or probe_result.sql is None:
        return WriteReadProbeResult("not_refined", probe_result.reason)

    base_probe = probe_result.sql
    cursor = context.base_cursor
    before_table = _temp_probe_result_table_name()
    savepoint = quote_identifier(f"sqlite_merge_write_read_{uuid.uuid4().hex}")
    savepoint_started = False
    try:
        _create_probe_result_table(cursor, before_table, base_probe)
        cursor.execute(f"SAVEPOINT {savepoint}")
        savepoint_started = True

        cursor.execute(writer_statement.sql_text)

        if _is_update_from_statement(
            reader_metadata,
        ) and _probe_has_duplicate_target_rows(context, reader_metadata, base_probe):
            return WriteReadProbeResult(
                "not_refined",
                "reader UPDATE FROM has multiple source rows after writer replay",
            )

        query = _stored_probe_difference_query(before_table, base_probe)
        if cursor.execute(query).fetchone() is not None:
            return WriteReadProbeResult("affected", affected_reader_indexes=(0,))
        return WriteReadProbeResult("unaffected")
    except sqlite3.Error:
        return WriteReadProbeResult(
            "not_refined",
            "write-read probe failed during SQLite execution",
        )
    finally:
        if savepoint_started:
            rollback_savepoint(cursor, savepoint)
        cursor.execute(f"DROP TABLE IF EXISTS {quote_identifier(before_table)}")


def _check_write_read(
    context: ConflictCheckContext,
    ours_transaction: LoggedTransaction,
    theirs_transaction: LoggedTransaction,
    result: ConflictCheckResult,
) -> ConflictCheckResult:
    """Use read probes to clear supported write/read conflicts."""

    directions = _write_read_directions(result.of_kind("write_read"))
    if not directions:
        return result

    if len(ours_transaction.statements) != 1 or len(theirs_transaction.statements) != 1:
        return _check_transaction_write_read(
            context,
            ours_transaction,
            theirs_transaction,
            result,
            directions,
        )

    transactions_by_label = {
        "ours": ours_transaction,
        "theirs": theirs_transaction,
    }
    checks: list[tuple[LoggedTransaction, LoggedTransaction, WriteReadProbeResult]] = []
    for writer_label, reader_label in directions:
        writer_transaction = transactions_by_label[writer_label]
        reader_transaction = transactions_by_label[reader_label]
        checks.append((
            writer_transaction,
            reader_transaction,
            statement_write_read_dependency_outcome(
                context,
                writer_transaction.statements[0],
                reader_transaction.statements[0].metadata,
            ),
        ))
    if any(check.status == "not_refined" for _, _, check in checks):
        return result

    affected_conflicts = [
        StatementConflict(
            kind="write_read",
            message=_write_read_conflict_message(
                writer_transaction,
                reader_transaction,
                check,
            ),
        )
        for writer_transaction, reader_transaction, check in checks
        if check.status == "affected"
    ]
    if not affected_conflicts:
        return result.without_kind("write_read")

    return result.replace_kind("write_read", affected_conflicts)


def _check_transaction_write_read(
    context: ConflictCheckContext,
    ours_transaction: LoggedTransaction,
    theirs_transaction: LoggedTransaction,
    result: ConflictCheckResult,
    directions: Sequence[tuple[str, str]],
) -> ConflictCheckResult:
    """Use transaction-prefix read probes to clear write/read conflicts."""

    transactions_by_label = {
        "ours": ours_transaction,
        "theirs": theirs_transaction,
    }
    checks: list[tuple[LoggedTransaction, LoggedTransaction, WriteReadProbeResult]] = []
    for writer_label, reader_label in directions:
        writer_transaction = transactions_by_label[writer_label]
        reader_transaction = transactions_by_label[reader_label]
        checks.append((
            writer_transaction,
            reader_transaction,
            transaction_write_read_dependency_outcome(
                context,
                writer_transaction=writer_transaction,
                reader_transaction=reader_transaction,
            ),
        ))
    if not checks or any(check.status == "not_refined" for _, _, check in checks):
        return result

    affected_conflicts = [
        StatementConflict(
            kind="write_read",
            message=_write_read_conflict_message(
                writer_transaction,
                reader_transaction,
                check,
            ),
        )
        for writer_transaction, reader_transaction, check in checks
        if check.status == "affected"
    ]
    if not affected_conflicts:
        return result.without_kind("write_read")

    return result.replace_kind("write_read", affected_conflicts)


def _write_read_conflict_message(
    writer_transaction: LoggedTransaction,
    reader_transaction: LoggedTransaction,
    result: WriteReadProbeResult,
) -> str:
    """Return a UI-facing write/read message with statement labels."""

    reader_statements = tuple(
        reader_transaction.statements[index]
        for index in result.affected_reader_indexes
    )
    reader_label = (
        _statement_list_label(reader_statements)
        if reader_statements
        else transaction_label(reader_transaction)
    )
    return (
        f"{reader_label} reads values affected by "
        f"{transaction_label(writer_transaction)}"
    )


def _statement_list_label(statements: Sequence[LoggedStatement]) -> str:
    """Return comma-separated labels for non-contiguous statement lists."""

    return ", ".join(statement_label(statement) for statement in statements)


def transaction_write_read_dependency_outcome(
    context: ConflictCheckContext,
    *,
    writer_transaction: LoggedTransaction,
    reader_transaction: LoggedTransaction,
) -> WriteReadProbeResult:
    """Return whether the writer transaction affects reader probes."""

    if any(
        not _can_simulate_writer(statement.metadata)
        for statement in writer_transaction.statements
    ):
        return WriteReadProbeResult(
            "not_refined",
            "writer transaction cannot be simulated safely",
        )

    indexes = set(
        write_read_candidate_indexes(
            writer_transaction.metadata,
            reader_transaction.metadata,
        )
    )
    if not indexes:
        return WriteReadProbeResult("unaffected")

    cursor = context.base_cursor
    before_results: dict[int, Counter[tuple[object, ...]]] = {}
    baseline_savepoint = quote_identifier(
        f"sqlite_merge_tx_read_base_{uuid.uuid4().hex}"
    )
    cursor.execute(f"SAVEPOINT {baseline_savepoint}")
    try:
        for index, statement in enumerate(reader_transaction.statements):
            if index in indexes:
                probe = _read_probe_result(context, statement.metadata)
                if probe.status == "no_read_dependency":
                    indexes.discard(index)
                elif probe.status == "not_refined" or probe.sql is None:
                    return WriteReadProbeResult("not_refined", probe.reason)
                else:
                    before_results[index] = _fetch_probe_rows(cursor, probe.sql)
            cursor.execute(statement.sql_text)
    except sqlite3.Error:
        return WriteReadProbeResult(
            "not_refined",
            "baseline transaction read probe failed during SQLite execution",
        )
    finally:
        rollback_savepoint(cursor, baseline_savepoint)

    if not before_results:
        return WriteReadProbeResult("unaffected")

    after_savepoint = quote_identifier(
        f"sqlite_merge_tx_read_after_{uuid.uuid4().hex}"
    )
    cursor.execute(f"SAVEPOINT {after_savepoint}")
    try:
        for statement in writer_transaction.statements:
            cursor.execute(statement.sql_text)

        affected_reader_indexes: list[int] = []
        for index, statement in enumerate(reader_transaction.statements):
            if index in before_results:
                probe = _read_probe_result(context, statement.metadata)
                if probe.status != "ok" or probe.sql is None:
                    return WriteReadProbeResult("not_refined", probe.reason)
                if _is_update_from_statement(
                    statement.metadata,
                ) and _probe_has_duplicate_target_rows(
                    context,
                    statement.metadata,
                    probe.sql,
                ):
                    return WriteReadProbeResult(
                        "not_refined",
                        "reader UPDATE FROM has multiple source rows after writer replay",
                    )
                if _fetch_probe_rows(cursor, probe.sql) != before_results[index]:
                    affected_reader_indexes.append(index)
            cursor.execute(statement.sql_text)

        if affected_reader_indexes:
            return WriteReadProbeResult(
                "affected",
                affected_reader_indexes=tuple(affected_reader_indexes),
            )
    except sqlite3.Error:
        return WriteReadProbeResult(
            "not_refined",
            "transaction write-read probe failed during SQLite execution",
        )
    finally:
        rollback_savepoint(cursor, after_savepoint)

    return WriteReadProbeResult("unaffected")


def _write_read_directions(
    conflicts: Sequence[StatementConflict],
) -> tuple[tuple[str, str], ...] | None:
    """Return write/read branch directions from static conflict details."""

    directions: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for conflict in conflicts:
        if len(conflict.details) < 2:
            return None

        direction = (conflict.details[0][1], conflict.details[1][1])

        if direction not in seen:
            seen.add(direction)
            directions.append(direction)
    return tuple(directions)


def _check_transaction_write_write(
    context: ConflictCheckContext,
    ours_transaction: LoggedTransaction,
    theirs_transaction: LoggedTransaction,
    result: ConflictCheckResult,
) -> ConflictCheckResult:
    """Use transaction-local affected PKs to clear write/write conflicts."""

    candidate_pairs = write_write_candidate_pairs(
        ours_transaction.metadata,
        theirs_transaction.metadata,
    )
    if not candidate_pairs:
        return result.without_kind("write_write")

    ours_indexes = {ours_index for ours_index, _ in candidate_pairs}
    theirs_indexes = {theirs_index for _, theirs_index in candidate_pairs}
    ours_footprints = _transaction_write_footprints(
        context,
        ours_transaction,
        ours_indexes,
    )
    theirs_footprints = _transaction_write_footprints(
        context,
        theirs_transaction,
        theirs_indexes,
    )
    if ours_footprints is None or theirs_footprints is None:
        return result

    for ours_index, theirs_index in candidate_pairs:
        ours_rows = ours_footprints.get(ours_index)
        theirs_rows = theirs_footprints.get(theirs_index)
        if ours_rows is None or theirs_rows is None:
            return result
        if ours_rows & theirs_rows:
            return result.replace_kind(
                "write_write",
                (
                    StatementConflict(
                        kind="write_write",
                        message=(
                            f"{statement_label(ours_transaction.statements[ours_index])} "
                            "and "
                            f"{statement_label(theirs_transaction.statements[theirs_index])} "
                            "update/delete overlapping rows"
                        ),
                    ),
                ),
            )

    return result.without_kind("write_write")


def _transaction_write_footprints(
    context: ConflictCheckContext,
    transaction: LoggedTransaction,
    candidate_indexes: set[int],
) -> dict[int, set[tuple[object, ...]]] | None:
    """Collect affected primary keys under a transaction's own prefix."""

    footprints: dict[int, set[tuple[object, ...]]] = {}
    cursor = context.base_cursor
    savepoint = quote_identifier(f"sqlite_merge_tx_write_{uuid.uuid4().hex}")
    cursor.execute(f"SAVEPOINT {savepoint}")
    try:
        for index, statement in enumerate(transaction.statements):
            if index in candidate_indexes:
                probe = _affected_primary_key_select(context, statement.metadata)
                if probe is None:
                    return None
                footprints[index] = set(_fetch_probe_rows(cursor, probe))
            cursor.execute(statement.sql_text)
    except sqlite3.Error:
        return None
    finally:
        rollback_savepoint(cursor, savepoint)
    return footprints


def _fetch_probe_rows(
    cursor: sqlite3.Cursor,
    probe: str,
) -> Counter[tuple[object, ...]]:
    """Fetch probe output into a multiset for Python-side comparison."""

    return Counter(tuple(row) for row in cursor.execute(probe))


def _affected_primary_key_select(
    context: ConflictCheckContext,
    metadata: StatementMetadata,
) -> str | None:
    """Build a SELECT returning primary keys affected by UPDATE/DELETE."""

    return _target_row_select(context, metadata)


def _target_row_select(
    context: ConflictCheckContext,
    metadata: StatementMetadata,
    extra_select_expressions: Sequence[str] = (),
) -> str | None:
    """Build a SELECT over rows targeted by an UPDATE/DELETE statement."""

    expression = metadata.parsed_sql_text
    if not isinstance(expression, (exp.Update, exp.Delete)):
        return None

    table = metadata.table_updated
    pk_columns = _primary_key_columns(context, table)
    target_table = table_expression(expression.this)
    if table is None or not pk_columns or target_table is None:
        return None

    with_expression = expression.args.get("with")

    qualifier = target_table.alias_or_name
    select_columns = ", ".join(
        [
            *(
                f"{quote_identifier(qualifier)}.{quote_identifier(column)}"
                for column in pk_columns
            ),
            *extra_select_expressions,
        ]
    )
    from_sources = [target_table.sql(dialect="sqlite")]
    from_expression = expression.args.get("from")
    if isinstance(expression, exp.Update) and from_expression is not None:
        from_sources.append(_from_expression_sql(from_expression))

    where_expression = expression.args.get("where")
    where_sql = (
        f" WHERE {where_expression.this.sql(dialect='sqlite')}"
        if where_expression is not None
        else ""
    )
    with_sql = (
        f"{with_expression.sql(dialect='sqlite')} "
        if with_expression is not None
        else ""
    )
    return (
        f"{with_sql}SELECT {select_columns} "
        f"FROM {', '.join(from_sources)}"
        f"{where_sql}"
    )


def _primary_key_columns(
    context: ConflictCheckContext,
    table: str | None,
) -> tuple[str, ...]:
    """Return cached primary-key columns, falling back to schema lookup."""

    if table is None:
        return ()

    columns = context.primary_key_columns.get(table)
    if columns is not None:
        return columns

    return schema_primary_key_columns(context.base_cursor, table)


def _read_probe_result(
    context: ConflictCheckContext,
    metadata: StatementMetadata,
) -> ReadProbeResult:
    """Build a read probe, or report that no probe is needed/supported."""

    expression = metadata.parsed_sql_text
    if isinstance(expression, exp.Update):
        return _probe_result(_update_read_probe_select(context, metadata))
    if isinstance(expression, exp.Delete):
        return _probe_result(_affected_primary_key_select(context, metadata))
    if isinstance(expression, exp.Insert):
        return _insert_probe_select(context, expression)
    return ReadProbeResult("not_refined", reason="reader statement has no supported probe")


def _probe_result(sql: str | None) -> ReadProbeResult:
    """Return an ok/not-refined probe result for UPDATE and DELETE probes."""

    if sql is None:
        return ReadProbeResult(
            "not_refined",
            reason="reader probe could not be built",
        )
    return ReadProbeResult("ok", sql)


def _update_read_probe_select(
    context: ConflictCheckContext,
    metadata: StatementMetadata,
) -> str | None:
    """Build an UPDATE probe returning target PKs plus read-dependent SET values."""

    expression = metadata.parsed_sql_text
    if not isinstance(expression, exp.Update):
        return None

    return _target_row_select(
        context,
        metadata,
        extra_select_expressions=_assignment_read_expressions(expression),
    )


def _assignment_read_expressions(expression: exp.Update) -> list[str]:
    """Return SET expressions that read from columns or subqueries."""

    read_expressions: list[str] = []
    for index, assignment in enumerate(expression.expressions, start=1):
        if not isinstance(assignment, exp.EQ):
            continue

        value = assignment.expression
        if value is None or not _expression_has_read(value):
            continue

        read_expressions.append(
            f"{value.sql(dialect='sqlite')} AS __set_expr_{index}"
        )
    return read_expressions


def _insert_values_probe_select(expression: exp.Insert) -> str | None:
    """Build a probe for read-dependent INSERT VALUES expressions."""

    insert_expression = expression.expression
    if not isinstance(insert_expression, exp.Values):
        return None

    rows = [_value_row_expressions(row) for row in insert_expression.expressions]
    read_rows = [
        (row_index, row)
        for row_index, row in enumerate(rows)
        if any(_expression_has_read(value) for value in row)
    ]
    if not read_rows:
        return None

    value_indexes = sorted({
        value_index
        for _, row in read_rows
        for value_index, value in enumerate(row)
        if _expression_has_read(value)
    })
    selects = []
    for row_index, row in read_rows:
        selected_values = [
            f"{row[value_index].sql(dialect='sqlite')} AS __value_{value_index + 1}"
            for value_index in value_indexes
            if value_index < len(row)
        ]
        selects.append(
            "SELECT "
            f"{row_index + 1} AS __row_index, "
            + ", ".join(selected_values)
        )

    with_expression = expression.args.get("with")
    with_sql = (
        f"{with_expression.sql(dialect='sqlite')} "
        if with_expression is not None
        else ""
    )
    return with_sql + " UNION ALL ".join(selects)


def _insert_probe_select(
    context: ConflictCheckContext,
    expression: exp.Insert,
) -> ReadProbeResult:
    """Build a read probe for INSERT VALUES or INSERT SELECT."""

    insert_expression = expression.expression
    if isinstance(insert_expression, exp.Values):
        probe = _insert_values_probe_select(expression)
        if probe is None:
            return ReadProbeResult("no_read_dependency")
        return ReadProbeResult("ok", probe)
    if insert_expression is not None:
        return _probe_result(_insert_select_probe_select(context, expression))
    return ReadProbeResult("no_read_dependency")


def _insert_select_probe_select(
    context: ConflictCheckContext,
    expression: exp.Insert,
) -> str | None:
    """Build an INSERT SELECT probe that preserves duplicate source rows."""

    source_sql = _insert_source_select_sql(expression)
    if source_sql is None:
        return None

    output_count = _select_output_count(context.base_cursor, source_sql)
    if output_count is None:
        return None

    group_columns = ", ".join(str(index) for index in range(1, output_count + 1))
    return (
        "SELECT *, COUNT(*) AS __count "
        f"FROM ({source_sql}) "
        f"GROUP BY {group_columns}"
    )


def _insert_source_select_sql(expression: exp.Insert) -> str | None:
    """Return the source SELECT SQL from INSERT ... SELECT."""

    insert_expression = expression.expression
    if insert_expression is None or isinstance(insert_expression, exp.Values):
        return None

    with_expression = expression.args.get("with")
    with_sql = (
        f"{with_expression.sql(dialect='sqlite')} "
        if with_expression is not None
        else ""
    )
    return with_sql + insert_expression.sql(dialect="sqlite")


def _select_output_count(cursor: sqlite3.Cursor, select_sql: str) -> int | None:
    """Return number of output columns produced by select_sql."""

    try:
        probe_cursor = cursor.execute(f"SELECT * FROM ({select_sql}) LIMIT 0")
    except sqlite3.Error:
        return None
    return len(probe_cursor.description)


def _value_row_expressions(row: exp.Expression) -> list[exp.Expression]:
    """Return one VALUES row as a list of value expressions."""

    if isinstance(row, exp.Tuple):
        return list(row.expressions)
    return [row]


def _expression_has_read(expression: exp.Expression) -> bool:
    """Return whether expression contains a column read or nested SELECT."""

    return (
        isinstance(expression, (exp.Column, exp.Select, exp.Subquery))
        or any(expression.find_all(exp.Column, exp.Select, exp.Subquery))
    )


def _select_difference_query(first_select: str, second_select: str) -> str:
    """Return SQL that detects any set difference between two SELECTs."""

    return (
        "SELECT 1 FROM ("
        "SELECT 1 FROM ("
        f"{second_select} "
        "EXCEPT "
        f"{first_select}"
        ") "
        "UNION ALL "
        "SELECT 1 FROM ("
        f"{first_select} "
        "EXCEPT "
        f"{second_select}"
        ")"
        ") LIMIT 1"
    )


def _stored_probe_difference_query(before_table: str, after_probe: str) -> str:
    """Return SQL comparing stored probe rows with current probe rows."""

    return _select_difference_query(
        f"SELECT * FROM {quote_identifier(before_table)}",
        f"SELECT * FROM ({after_probe})",
    )


def update_from_has_duplicate_target_rows(
    context: ConflictCheckContext,
    metadata: StatementMetadata,
    probe: str | None = None,
) -> bool:
    """Return whether an UPDATE FROM probe has multiple rows for a target PK."""

    if not _is_update_from_statement(metadata):
        return False

    probe = probe or _affected_primary_key_select(context, metadata)
    if probe is None:
        return False

    try:
        return _probe_has_duplicate_target_rows(context, metadata, probe)
    except sqlite3.Error:
        return False


def _probe_has_duplicate_target_rows(
    context: ConflictCheckContext,
    metadata: StatementMetadata,
    probe: str,
) -> bool:
    """Return whether probe returns duplicate target PK rows."""

    pk_count = len(_primary_key_columns(context, metadata.table_updated))
    if pk_count == 0:
        return True

    group_columns = ", ".join(str(index) for index in range(1, pk_count + 1))
    query = (
        f"SELECT 1 FROM ({probe}) "
        f"GROUP BY {group_columns} "
        "HAVING COUNT(*) > 1 "
        "LIMIT 1"
    )
    return context.base_cursor.execute(query).fetchone() is not None


def _is_update_from_statement(metadata: StatementMetadata) -> bool:
    """Return whether metadata is for an UPDATE with a FROM clause."""

    return (
        is_update_statement(metadata)
        and metadata.parsed_sql_text.args.get("from") is not None
    )


def _from_expression_sql(from_expression: exp.Expression) -> str:
    """Return FROM contents without the leading FROM keyword."""
    return from_expression.sql(dialect="sqlite").removeprefix("FROM ").strip()


def _can_simulate_writer(metadata: StatementMetadata) -> bool:
    """
    Return whether writer can be replayed inside a rollback-only savepoint.
    Unsupported writers are kept as static conflicts instead.
    """

    return (
        is_insert_statement(metadata)
        or is_update_statement(metadata)
        or is_delete_statement(metadata)
    )


def _temp_probe_result_table_name() -> str:
    """Return a unique temporary table name for stored probe rows."""

    return f"__sqlite_merge_probe_{uuid.uuid4().hex}"


def _create_probe_result_table(
    cursor: sqlite3.Cursor,
    result_table: str,
    probe: str,
) -> None:
    """Store the current output of a read probe in a temporary table."""

    cursor.execute(
        "CREATE TEMP TABLE "
        f"{quote_identifier(result_table)} AS "
        f"SELECT * FROM ({probe})"
    )


def _pair_replay_failures(
    context: ConflictCheckContext,
    first: LoggedTransaction,
    second: LoggedTransaction,
) -> tuple[SQLiteReplayFailure, ...]:
    """Return SQLite failures found from trying both pair orders."""

    cursor = context.base_cursor
    cursor.execute("PRAGMA foreign_keys = ON")
    failures: list[SQLiteReplayFailure] = []
    for label, first_group, second_group in (
        ("ours then theirs", first.statements, second.statements),
        ("theirs then ours", second.statements, first.statements),
    ):
        failure = _savepoint_replay_failure(
            cursor,
            first_group,
            second_group,
            label,
        )
        if failure is not None:
            failures.append(failure)

    return tuple(failures)


def _standalone_replay_conflict(
    failure: SQLiteReplayFailure,
) -> StatementConflict:
    """Return a scoped conflict for a statement blocked by the prefix."""

    details = ()
    if failure.statement is not None:
        details = (("statement_log_id", str(failure.statement.log_id)),)

    return StatementConflict(
        kind=failure.kind,
        scope=failure.scope,
        message=(
            f"{failure.scope} statement cannot be applied under the "
            f"current prefix: {failure.message}"
        ),
        details=details,
    )


def _savepoint_replay_failure(
    cursor: sqlite3.Cursor,
    first_statements: Sequence[LoggedStatement],
    second_statements: Sequence[LoggedStatement],
    order_label: str,
) -> SQLiteReplayFailure | None:
    """Try one pair order inside a savepoint, then discard all effects."""

    savepoint = quote_identifier(f"sqlite_merge_replay_{uuid.uuid4().hex}")
    cursor.execute(f"SAVEPOINT {savepoint}")
    try:
        for group_index, statements in enumerate((first_statements, second_statements)):
            for statement in statements:
                scope: ConflictScope = statement.branch if group_index == 0 else "pair"
                try:
                    cursor.execute(statement.sql_text)
                except sqlite3.Error as exc:
                    return SQLiteReplayFailure(
                        kind=_sqlite_error_conflict_kind(exc),
                        scope=scope,
                        message=str(exc),
                        order_label=order_label,
                        statement=statement,
                    )

                deferred_error = _foreign_key_check_error(cursor)
                if deferred_error is not None:
                    return SQLiteReplayFailure(
                        kind="integrity",
                        scope=scope,
                        message=deferred_error,
                        order_label=order_label,
                        statement=statement,
                    )
        return None
    finally:
        rollback_savepoint(cursor, savepoint)


def _sqlite_error_conflict_kind(error: sqlite3.Error) -> ConflictKind:
    """Map SQLite exceptions to merge conflict kinds."""

    if isinstance(error, sqlite3.IntegrityError):
        return "integrity"
    return "replay_error"


def _foreign_key_check_error(cursor: sqlite3.Cursor) -> str | None:
    """Return a deferred foreign-key error detected after replaying statements."""

    row = cursor.execute("PRAGMA foreign_key_check").fetchone()
    if row is None:
        return None
    return f"foreign key check failed: {tuple(row)}"


def _replay_statements(
    db_path: Path,
    statements: Sequence[LoggedStatement],
) -> str | None:
    """Apply statements to db_path and return an error string on failure."""

    try:
        with closing(sqlite3.connect(db_path)) as con:
            con.execute("PRAGMA foreign_keys = ON")
            for statement in statements:
                con.execute(statement.sql_text)
            con.commit()
    except sqlite3.Error as exc:
        return str(exc)
    return None


def _sqldiff(first_path: Path, second_path: Path) -> str | None:
    """Return sqldiff output, or None when sqldiff is unavailable."""

    sqldiff_path = _sqldiff_path()
    if sqldiff_path is None:
        return None

    completed = subprocess.run(
        [str(sqldiff_path), str(first_path), str(second_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout + completed.stderr


def _sqldiff_path() -> Path | None:
    """Locate sqldiff from PATH or the repository-local fallback."""

    path = shutil.which("sqldiff")
    if path is not None:
        return Path(path)

    local_path = Path(__file__).resolve().parents[2] / "tools" / "bin" / "sqldiff"
    if local_path.exists():
        return local_path
    return None
