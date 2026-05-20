from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from pathlib import Path
from collections.abc import Sequence
from typing import Literal

from sqlglot import expressions as exp

from .log_merge import (
    ConflictCheckContext,
    ConflictCheckResult,
    LoggedStatement,
    StatementConflict,
)
from .statement_metadata import StatementMetadata
from .utils import (
    is_delete_statement,
    is_insert_statement,
    is_update_statement,
    primary_key_columns,
    quote_identifier,
    rollback_savepoint,
    table_expression,
)

WriteReadProbeOutcome = Literal["changed", "unchanged", "unsupported"]


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
    ours_statement: LoggedStatement,
    theirs_statement: LoggedStatement,
    static_result: ConflictCheckResult,
) -> ConflictCheckResult:
    """Recheck static conflicts with targeted SQLite execution where possible."""

    result = static_result
    result = _check_write_read(
        context,
        ours_statement,
        theirs_statement,
        result,
    )
    result = _check_write_write(
        context,
        ours_statement.metadata,
        theirs_statement.metadata,
        result,
    )
    return _with_integrity_conflict(
        context,
        ours_statement,
        theirs_statement,
        result,
    )


def _check_write_write(
    context: ConflictCheckContext,
    ours_metadata: StatementMetadata,
    theirs_metadata: StatementMetadata,
    result: ConflictCheckResult,
) -> ConflictCheckResult:
    """Use affected primary-key rows to clear supported write/write conflicts."""

    if not result.has_kind("write_write"):
        return result

    row_overlap = update_delete_write_write_row_overlap(
        context,
        ours_metadata,
        theirs_metadata,
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
                message="write-write row overlap",
            ),
        ),
    )


def update_delete_write_write_row_overlap(
    context: ConflictCheckContext,
    ours_metadata: StatementMetadata,
    theirs_metadata: StatementMetadata,
) -> bool | None:
    """Return whether UPDATE/DELETE statements affect overlapping PK rows."""

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


def write_read_dependency_outcome(
    context: ConflictCheckContext,
    writer_statement: LoggedStatement,
    reader_metadata: StatementMetadata,
) -> WriteReadProbeOutcome:
    """Return whether reader output changes after writer runs in a savepoint."""

    writer_metadata = writer_statement.metadata
    if not _can_simulate_writer(writer_metadata):
        return "unsupported"

    base_probe = _read_probe_select(context, reader_metadata)
    if base_probe is None:
        return "unsupported"

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
            return "unsupported"

        query = _stored_probe_difference_query(before_table, base_probe)
        return (
            "changed"
            if cursor.execute(query).fetchone() is not None
            else "unchanged"
        )
    except sqlite3.Error:
        return "unsupported"
    finally:
        if savepoint_started:
            rollback_savepoint(cursor, savepoint)
        cursor.execute(f"DROP TABLE IF EXISTS {quote_identifier(before_table)}")


def _check_write_read(
    context: ConflictCheckContext,
    ours_statement: LoggedStatement,
    theirs_statement: LoggedStatement,
    result: ConflictCheckResult,
) -> ConflictCheckResult:
    """Use read probes to clear supported write/read conflicts."""

    if not result.has_kind("write_read"):
        return result

    directions = _write_read_directions(result.of_kind("write_read"))
    if not directions:
        return result

    metadata_by_label = {
        "ours": ours_statement.metadata,
        "theirs": theirs_statement.metadata,
    }
    statement_by_label = {
        "ours": ours_statement,
        "theirs": theirs_statement,
    }
    checks: list[tuple[str, WriteReadProbeOutcome]] = []
    for writer_label, reader_label in directions:
        checks.append((
            f"{writer_label} writes; {reader_label} reads",
            write_read_dependency_outcome(
                context,
                statement_by_label[writer_label],
                metadata_by_label[reader_label],
            ),
        ))

    if not checks or any(outcome == "unsupported" for _, outcome in checks):
        return result

    changed_labels = [
        label
        for label, outcome in checks
        if outcome == "changed"
    ]
    if not changed_labels:
        return result.without_kind("write_read")

    conflicts = [
        StatementConflict(kind="write_read", message=f"write-read dependency: {label}")
        for label in changed_labels
    ]
    return result.replace_kind("write_read", conflicts)


def _write_read_directions(
    conflicts: Sequence[StatementConflict],
) -> tuple[tuple[str, str], ...] | None:
    """Return writer/reader directions already reported by static analysis."""

    directions: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for conflict in conflicts:
        details = dict(conflict.details)
        pair = (details.get("writer"), details.get("reader"))
        if pair not in {("ours", "theirs"), ("theirs", "ours")}:
            return None

        if pair not in seen:
            seen.add(pair)
            directions.append(pair)

    return tuple(directions)


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
    pk_columns = primary_key_columns(context.base_cursor, table)
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


def _read_probe_select(
    context: ConflictCheckContext,
    metadata: StatementMetadata,
) -> str | None:
    """Build a SELECT probe for reads performed by DML statements."""

    expression = metadata.parsed_sql_text
    if isinstance(expression, exp.Update):
        return _update_read_probe_select(context, metadata)
    if isinstance(expression, exp.Delete):
        return _affected_primary_key_select(context, metadata)
    if isinstance(expression, exp.Insert):
        return _insert_probe_select(context, expression)
    return None


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
        for row_index, row in enumerate(rows, start=1)
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
            f"{row_index} AS __row_index, "
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
) -> str | None:
    """Build a read probe for INSERT VALUES or INSERT SELECT."""

    insert_expression = expression.expression
    if isinstance(insert_expression, exp.Values):
        return _insert_values_probe_select(expression)
    if insert_expression is not None:
        return _insert_select_probe_select(context, expression)
    return None


def _insert_select_probe_select(
    context: ConflictCheckContext,
    expression: exp.Insert,
) -> str | None:
    """Build an INSERT SELECT probe that preserves duplicate source rows."""

    source_sql = _insert_source_select_sql(expression)
    if source_sql is None:
        return None

    output_count = _select_output_count(context.base_cursor, source_sql)
    if output_count is None or output_count == 0:
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
    return len(probe_cursor.description or ())


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

    pk_count = len(primary_key_columns(context.base_cursor, metadata.table_updated))
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
    Unsupported or unsafe statements are kept as static conflicts instead.
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


def _with_integrity_conflict(
    context: ConflictCheckContext,
    first: LoggedStatement,
    second: LoggedStatement,
    result: ConflictCheckResult,
) -> ConflictCheckResult:
    """Append an integrity conflict if either pair replay order fails constraints."""

    integrity_error = _pair_integrity_error(context, first, second)
    if integrity_error is None:
        return result

    return result.add_conflicts(
        StatementConflict(kind="integrity", message=integrity_error)
    )


def _pair_integrity_error(
    context: ConflictCheckContext,
    first: LoggedStatement,
    second: LoggedStatement,
) -> str | None:
    """Return the first integrity error found when replaying both pair orders."""

    cursor = context.base_cursor
    cursor.execute("PRAGMA foreign_keys = ON")
    for label, statements in (
        ("ours then theirs", (first, second)),
        ("theirs then ours", (second, first)),
    ):
        error = _savepoint_integrity_error(cursor, statements)
        if error is not None:
            return f"{label}: {error}"

    return None


def _savepoint_integrity_error(
    cursor: sqlite3.Cursor,
    statements: Sequence[LoggedStatement],
) -> str | None:
    """Try statements inside a savepoint, then discard all effects."""

    savepoint = quote_identifier(f"sqlite_merge_integrity_{uuid.uuid4().hex}")
    cursor.execute(f"SAVEPOINT {savepoint}")
    try:
        for statement in statements:
            cursor.execute(statement.sql_text)
        return _foreign_key_check_error(cursor)
    except sqlite3.IntegrityError as exc:
        return str(exc)
    finally:
        rollback_savepoint(cursor, savepoint)


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
        with sqlite3.connect(db_path) as con:
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
