from __future__ import annotations

import copy
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from pathlib import Path
from collections.abc import Sequence

from sqlglot import expressions as exp

from .log_merge import (
    ConflictCheckContext,
    ConflictCheckResult,
    LoggedStatement,
    StatementConflict,
)
from .statement_metadata import ALL_COLUMNS, UNQUALIFIED_TABLE, StatementMetadata
from .utils import (
    is_delete_statement,
    is_update_statement,
    primary_key_columns,
    quote_identifier,
    table_expression,
)


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
        ours_statement.metadata,
        theirs_statement.metadata,
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

    if not _has_conflict_kind(result, "write_write"):
        return result

    row_overlap = update_delete_write_write_row_overlap(
        context,
        ours_metadata,
        theirs_metadata,
    )
    if row_overlap is None:
        return result
    if not row_overlap:
        return _without_conflict_kind(result, "write_write")

    preserved = _without_conflict_kind(result, "write_write").conflicts
    return ConflictCheckResult((
        *preserved,
        StatementConflict(
            kind="write_write",
            message="write-write row overlap",
        ),
    ))


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
    if (
        _update_from_has_duplicate_target_rows(context, ours_metadata, ours_select)
        or _update_from_has_duplicate_target_rows(context, theirs_metadata, theirs_select)
    ):
        return None

    query = (
        "SELECT 1 FROM ("
        f"SELECT * FROM ({ours_select}) "
        "INTERSECT "
        f"SELECT * FROM ({theirs_select})"
        ") LIMIT 1"
    )
    return context.base_cursor.execute(query).fetchone() is not None


def write_read_dependency_changed(
    context: ConflictCheckContext,
    writer_metadata: StatementMetadata,
    reader_metadata: StatementMetadata,
) -> bool | None:
    """Return whether reader output changes after simulating writer on temp table."""

    if not _metadata_has_write_read_overlap(writer_metadata, reader_metadata):
        return False
    if not _can_simulate_writer(writer_metadata):
        return None

    table = writer_metadata.table_updated
    if table is None:
        return None

    base_probe = _read_probe_select(context, reader_metadata)
    if base_probe is None:
        return None

    temp_table = _temp_table_name(table)
    cursor = context.base_cursor
    try:
        _create_temp_table_copy(cursor, table, temp_table)
        writer_sql = _replace_table_references_sql(
            writer_metadata.parsed_sql_text,
            table,
            temp_table,
        )
        if writer_sql is None:
            return None

        cursor.execute(writer_sql)
        temp_probe = _replace_table_references_sql(
            base_probe,
            table,
            temp_table,
        )
        if temp_probe is None:
            return None

        if _update_from_has_duplicate_target_rows(context, reader_metadata, base_probe):
            return None
        if _update_from_has_duplicate_target_rows(context, reader_metadata, temp_probe):
            return None

        query = _probe_difference_query(base_probe, temp_probe)
        return cursor.execute(query).fetchone() is not None
    except sqlite3.Error:
        return None
    finally:
        cursor.execute(f"DROP TABLE IF EXISTS {quote_identifier(temp_table)}")


def _check_write_read(
    context: ConflictCheckContext,
    ours_metadata: StatementMetadata,
    theirs_metadata: StatementMetadata,
    result: ConflictCheckResult,
) -> ConflictCheckResult:
    """Use read probes to clear supported write/read conflicts."""

    if not _has_conflict_kind(result, "write_read"):
        return result

    checks: list[tuple[str, bool | None]] = []
    if _metadata_has_write_read_overlap(ours_metadata, theirs_metadata):
        checks.append((
            "ours writes; theirs reads",
            write_read_dependency_changed(context, ours_metadata, theirs_metadata),
        ))
    if _metadata_has_write_read_overlap(theirs_metadata, ours_metadata):
        checks.append((
            "theirs writes; ours reads",
            write_read_dependency_changed(context, theirs_metadata, ours_metadata),
        ))

    if not checks or any(changed is None for _, changed in checks):
        return result
    if not any(changed for _, changed in checks):
        return _without_conflict_kind(result, "write_read")

    preserved = _without_conflict_kind(result, "write_read").conflicts
    conflicts = [
        StatementConflict(kind="write_read", message=f"write-read dependency: {label}")
        for label, changed in checks
        if changed
    ]
    return ConflictCheckResult((*preserved, *conflicts))


def _affected_primary_key_select(
    context: ConflictCheckContext,
    metadata: StatementMetadata,
) -> str | None:
    """Build a SELECT returning primary keys affected by UPDATE/DELETE."""

    expression = metadata.parsed_sql_text
    if not isinstance(expression, (exp.Update, exp.Delete)):
        return None

    table = metadata.table_updated
    pk_columns = primary_key_columns(context.base_cursor, table)
    target_table = table_expression(expression.this)
    if table is None or not pk_columns or target_table is None:
        return None

    with_expression = expression.args.get("with")
    if with_expression is not None and with_expression.args.get("recursive"):
        return None

    qualifier = target_table.alias_or_name
    select_columns = ", ".join(
        f"{quote_identifier(qualifier)}.{quote_identifier(column)}"
        for column in pk_columns
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

    if _affected_primary_key_select(context, metadata) is None:
        return None

    pk_columns = primary_key_columns(context.base_cursor, metadata.table_updated)
    target_table = table_expression(expression.this)
    if target_table is None:
        return None

    qualifier = target_table.alias_or_name
    select_expressions = [
        f"{quote_identifier(qualifier)}.{quote_identifier(column)}"
        for column in pk_columns
    ]
    select_expressions.extend(_assignment_read_expressions(expression))

    with_expression = expression.args.get("with")
    with_sql = (
        f"{with_expression.sql(dialect='sqlite')} "
        if with_expression is not None
        else ""
    )
    from_sources = [target_table.sql(dialect="sqlite")]
    from_expression = expression.args.get("from")
    if from_expression is not None:
        from_sources.append(_from_expression_sql(from_expression))

    where_expression = expression.args.get("where")
    where_sql = (
        f" WHERE {where_expression.this.sql(dialect='sqlite')}"
        if where_expression is not None
        else ""
    )

    return (
        f"{with_sql}SELECT {', '.join(select_expressions)} "
        f"FROM {', '.join(from_sources)}"
        f"{where_sql}"
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


def _probe_difference_query(base_probe: str, temp_probe: str) -> str:
    """Return SQL that detects any set difference between two probes."""

    return (
        "SELECT 1 FROM ("
        "SELECT 1 FROM ("
        f"SELECT * FROM ({temp_probe}) "
        "EXCEPT "
        f"SELECT * FROM ({base_probe})"
        ") "
        "UNION ALL "
        "SELECT 1 FROM ("
        f"SELECT * FROM ({base_probe}) "
        "EXCEPT "
        f"SELECT * FROM ({temp_probe})"
        ")"
        ") LIMIT 1"
    )


def _update_from_has_duplicate_target_rows(
    context: ConflictCheckContext,
    metadata: StatementMetadata,
    probe: str,
) -> bool:
    """Return whether an UPDATE FROM probe has multiple rows for a target PK."""

    if not is_update_statement(metadata):
        return False
    if metadata.parsed_sql_text.args.get("from") is None:
        return False

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


def _from_expression_sql(from_expression: exp.Expression) -> str:
    """Return FROM contents without the leading FROM keyword."""

    if isinstance(from_expression, exp.From) and from_expression.this is not None:
        return from_expression.this.sql(dialect="sqlite")
    return from_expression.sql(dialect="sqlite").removeprefix("FROM ").strip()


def _metadata_has_write_read_overlap(
    writer: StatementMetadata,
    reader: StatementMetadata,
) -> bool:
    """Return whether metadata says reader may read columns written by writer."""

    table = writer.table_updated
    if table is None:
        return False

    written_columns = writer.columns_updated
    if not written_columns:
        return False

    references = reader.tables_referenced_to_columns_referenced
    return bool(
        _column_overlap(references.get(table, set()), written_columns)
        or _column_overlap(references.get(UNQUALIFIED_TABLE, set()), written_columns)
    )


def _column_overlap(left: set[str], right: set[str]) -> set[str]:
    """Return overlapping columns, treating '*' as all columns."""

    if not left or not right:
        return set()

    if ALL_COLUMNS in left and ALL_COLUMNS in right:
        return {ALL_COLUMNS}
    if ALL_COLUMNS in left:
        return set(right)
    if ALL_COLUMNS in right:
        return set(left)
    return left & right


def _can_simulate_writer(metadata: StatementMetadata) -> bool:
    """Return whether writer can safely be replayed against one temp table."""

    return is_update_statement(metadata) or is_delete_statement(metadata)


def _temp_table_name(table: str) -> str:
    """Return a unique temporary table name for one pair check."""

    safe_table = "".join(character if character.isalnum() else "_" for character in table)
    return f"__sqlite_merge_{safe_table}_{uuid.uuid4().hex}"


def _create_temp_table_copy(
    cursor: sqlite3.Cursor,
    table: str,
    temp_table: str,
) -> None:
    """Create a TEMP table initialized with the base rows from table."""

    cursor.execute(
        "CREATE TEMP TABLE "
        f"{quote_identifier(temp_table)} AS "
        f"SELECT * FROM {quote_identifier(table)}"
    )


def _replace_table_references_sql(
    sql_or_expression: str | exp.Expression,
    table: str,
    replacement_table: str,
) -> str | None:
    """Render SQL with table references rewritten to a replacement table."""

    if isinstance(sql_or_expression, str):
        try:
            expression = exp.maybe_parse(sql_or_expression, dialect="sqlite")
        except Exception:
            return None
    else:
        expression = copy.deepcopy(sql_or_expression)

    if expression is None or _cte_shadows_table(expression, table):
        return None

    def replace(node: exp.Expression) -> exp.Expression:
        if not isinstance(node, exp.Table) or node.name != table:
            return node

        replacement = node.copy()
        replacement.set("this", exp.to_identifier(replacement_table))
        if replacement.args.get("alias") is None:
            replacement.set(
                "alias",
                exp.TableAlias(this=exp.to_identifier(table)),
            )
        return replacement

    return expression.transform(replace).sql(dialect="sqlite")


def _cte_shadows_table(expression: exp.Expression, table: str) -> bool:
    """Return whether a CTE name could be confused with the rewritten table."""

    return any(
        cte.alias == table
        for cte in expression.find_all(exp.CTE)
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

    return ConflictCheckResult((
        *result.conflicts,
        StatementConflict(kind="integrity", message=integrity_error),
    ))


def _pair_integrity_error(
    context: ConflictCheckContext,
    first: LoggedStatement,
    second: LoggedStatement,
) -> str | None:
    """Return the first integrity error found when replaying both pair orders."""

    with tempfile.TemporaryDirectory() as tmp_dir:
        for label, statements in (
            ("ours then theirs", (first, second)),
            ("theirs then ours", (second, first)),
        ):
            db_path = Path(tmp_dir) / f"{label.replace(' ', '_')}.db"
            shutil.copy2(context.base_db_path, db_path)
            error = _replay_integrity_error(db_path, statements)
            if error is not None:
                return f"{label}: {error}"

    return None


def _replay_integrity_error(
    db_path: Path,
    statements: Sequence[LoggedStatement],
) -> str | None:
    """Apply statements and return an integrity error message on failure."""

    try:
        with sqlite3.connect(db_path) as con:
            con.execute("PRAGMA foreign_keys = ON")
            for statement in statements:
                con.execute(statement.sql_text)
            con.commit()
    except sqlite3.IntegrityError as exc:
        return str(exc)
    return None


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


def _has_conflict_kind(result: ConflictCheckResult, kind: str) -> bool:
    """Return whether result contains a conflict of kind."""

    return any(conflict.kind == kind for conflict in result.conflicts)


def _without_conflict_kind(
    result: ConflictCheckResult,
    kind: str,
) -> ConflictCheckResult:
    """Return result without conflicts of kind."""

    return ConflictCheckResult(tuple(
        conflict
        for conflict in result.conflicts
        if conflict.kind != kind
    ))
