from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from sqlglot import expressions as exp
from sqlite_conflict_resolution import strict_conflict_resolution_rewrite

from .models import (
    ConflictCheckContext,
    ConflictScope,
    ConflictKind,
    LoggedTransaction,
    LoggedStatement,
    StatementConflict,
    statement_label,
    transaction_label,
)
from .sql_metadata import StatementMetadata
from .utils import (
    is_update_statement,
    primary_key_columns as schema_primary_key_columns,
    quote_identifier,
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


def _strict_conflict_resolution_statement(
    statement: LoggedStatement,
) -> LoggedStatement | None:
    """Return a statement copy with reviewable conflict syntax stripped."""

    rewrite = strict_conflict_resolution_rewrite(statement.sql_text)
    if rewrite is None:
        return None

    return replace(
        statement,
        to_replay_sql_text=rewrite.sql,
        replay_warnings=(
            *statement.replay_warnings,
            f"strict replay removed {rewrite.label}",
        ),
    )


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


def _sqlite_error_conflict_kind(error: sqlite3.Error) -> ConflictKind:
    """Map SQLite exceptions to merge conflict kinds."""

    if isinstance(error, sqlite3.IntegrityError):
        return "integrity"
    return "replay_error"


def _foreign_key_check_error(
    cursor: sqlite3.Cursor,
    *,
    schema: str | None = None,
) -> str | None:
    """Return a deferred foreign-key error detected after replaying statements."""

    pragma = (
        "PRAGMA foreign_key_check"
        if schema is None
        else f"PRAGMA {quote_identifier(schema)}.foreign_key_check"
    )
    row = cursor.execute(pragma).fetchone()
    if row is None:
        return None
    return f"foreign_key_check failed: {tuple(row)}"
