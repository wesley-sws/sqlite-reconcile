from __future__ import annotations

import sqlite3
from collections.abc import Container, Iterable
from typing import TYPE_CHECKING

from sqlglot import expressions as exp, parse_one
from sqlglot.errors import ParseError

if TYPE_CHECKING:
    from .sql_metadata import StatementMetadata

ALL_COLUMNS = "*"
TableColumns = dict[str, set[str]]
TablePrimaryKeyColumns = dict[str, tuple[str, ...]]
TableKeyColumnSets = dict[str, tuple[set[str], ...]]


def is_sql_expression(value: object) -> bool:
    """Return whether value is a sqlglot expression."""

    return isinstance(value, exp.Expression)


def sql_expression_to_sql(value: exp.Expression) -> str:
    """Render a sqlglot expression back to SQLite-flavoured SQL."""

    return value.sql(dialect="sqlite")


def table_expression(expression: exp.Expression | None) -> exp.Table | None:
    """Return the table node from a table or INSERT schema target."""

    if isinstance(expression, exp.Table):
        return expression

    schema_target = expression.this if isinstance(expression, exp.Schema) else None
    if isinstance(schema_target, exp.Table):
        return schema_target

    return None


def table_name(expression: exp.Expression | None) -> str | None:
    """Return a table expression's concrete table name."""

    table = table_expression(expression)
    if table is not None:
        return table.name
    return None


def is_delete_statement(metadata: StatementMetadata) -> bool:
    """Return whether statement metadata belongs to a DELETE statement."""

    return isinstance(metadata.parsed_sql_text, exp.Delete)


def is_insert_statement(metadata: StatementMetadata) -> bool:
    """Return whether statement metadata belongs to an INSERT statement."""

    return isinstance(metadata.parsed_sql_text, exp.Insert)


def is_update_statement(metadata: StatementMetadata) -> bool:
    """Return whether statement metadata belongs to an UPDATE statement."""

    return isinstance(metadata.parsed_sql_text, exp.Update)


def row_value(row: sqlite3.Row | tuple, key: str, index: int):
    """Read a sqlite row by key when available, otherwise by tuple index."""

    if isinstance(row, sqlite3.Row):
        return row[key]
    return row[index]


def quote_identifier(identifier: str) -> str:
    """Quote a SQLite identifier for PRAGMA and schema introspection SQL."""

    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    """Return whether a real table exists in the database schema."""

    row = cursor.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def load_table_columns(
    cursor: sqlite3.Cursor,
    *,
    ignored_tables: Container[str] = (),
) -> TableColumns:
    """Return non-SQLite table names mapped to columns, excluding ignored tables."""

    table_columns: TableColumns = {}
    table_rows = cursor.execute(
        """
        SELECT name
        FROM sqlite_schema
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    for table_row in table_rows:
        table = str(row_value(table_row, "name", 0))
        if table in ignored_tables:
            continue

        pragma_rows = cursor.execute(
            f"PRAGMA table_info({quote_identifier(table)})"
        ).fetchall()
        table_columns[table] = {
            str(row_value(pragma_row, "name", 1))
            for pragma_row in pragma_rows
        }
    return table_columns


def load_primary_key_columns(
    cursor: sqlite3.Cursor,
    tables: Iterable[str],
) -> TablePrimaryKeyColumns:
    """Return table names mapped to ordered primary-key columns."""

    return {
        table: primary_key_columns(cursor, table)
        for table in tables
    }


def load_key_column_sets(
    cursor: sqlite3.Cursor,
    tables: Iterable[str],
) -> TableKeyColumnSets:
    """Return table names mapped to primary-key and unique-key column sets."""

    return {
        table: key_column_sets(cursor, table)
        for table in tables
    }


def rollback_savepoint(
    cursor: sqlite3.Connection | sqlite3.Cursor,
    savepoint: str,
) -> None:
    """Roll back and release an already-created SQLite savepoint."""

    try:
        cursor.execute(f"ROLLBACK TO {savepoint}")
    finally:
        cursor.execute(f"RELEASE {savepoint}")


def key_columns(cursor: sqlite3.Cursor, table: str | None) -> set[str]:
    """Return the union of primary-key and unique-key columns for a table."""

    if table is None:
        return set()

    return set().union(*key_column_sets(cursor, table))


def primary_key_columns(cursor: sqlite3.Cursor, table: str | None) -> tuple[str, ...]:
    """Return primary-key columns for table in key order."""

    if table is None:
        return ()

    columns = [
        (
            int(row_value(row, "pk", 5) or 0),
            str(row_value(row, "name", 1)),
        )
        for row in cursor.execute(
            f"PRAGMA table_info({quote_identifier(table)})"
        ).fetchall()
        if int(row_value(row, "pk", 5) or 0) > 0
    ]
    return tuple(column for _, column in sorted(columns))


def key_column_sets(cursor: sqlite3.Cursor, table: str) -> tuple[set[str], ...]:
    """Return primary-key and unique-index column sets for a table."""

    key_sets: list[set[str]] = []
    pk_columns = {
        str(row_value(row, "name", 1))
        for row in cursor.execute(
            f"PRAGMA table_info({quote_identifier(table)})"
        ).fetchall()
        if int(row_value(row, "pk", 5) or 0) > 0
    }
    if pk_columns:
        key_sets.append(pk_columns)

    index_rows = cursor.execute(
        f"PRAGMA index_list({quote_identifier(table)})"
    ).fetchall()
    for index_row in index_rows:
        if int(row_value(index_row, "unique", 2) or 0) != 1:
            continue

        index_name = str(row_value(index_row, "name", 1))
        is_partial = bool(int(row_value(index_row, "partial", 4) or 0))
        index_columns = _unique_index_key_columns(
            cursor,
            index_name,
            is_partial=is_partial,
        )
        if index_columns:
            key_sets.append(index_columns)

    return tuple(key_sets)


def _unique_index_key_columns(
    cursor: sqlite3.Cursor,
    index_name: str,
    *,
    is_partial: bool,
) -> set[str]:
    """Return columns that may affect one unique index."""

    needs_sql_parse = is_partial
    columns: set[str] = set()
    # index_xinfo returns both real index key entries and auxiliary entries
    # such as the rowid. Only rows with key=1 define uniqueness.
    xinfo_rows = cursor.execute(
        f"PRAGMA index_xinfo({quote_identifier(index_name)})"
    ).fetchall()

    for xinfo_row in xinfo_rows:
        if int(row_value(xinfo_row, "key", 5) or 0) != 1:
            continue

        column_name = row_value(xinfo_row, "name", 2)
        column_id = int(row_value(xinfo_row, "cid", 1) or 0)
        if column_name is None or column_id < 0:
            # cid=-2 is an expression index entry, so the column dependencies
            # only exist in the stored CREATE INDEX statement.
            needs_sql_parse = True
            continue

        columns.add(str(column_name))

    if not needs_sql_parse:
        return columns

    parsed_columns = _columns_referenced_by_index_sql(cursor, index_name)
    if parsed_columns:
        return parsed_columns

    return {ALL_COLUMNS}


def _columns_referenced_by_index_sql(
    cursor: sqlite3.Cursor,
    index_name: str,
) -> set[str] | None:
    """Parse CREATE INDEX SQL and return referenced columns when possible."""

    # Explicit CREATE INDEX statements have SQL text here. Autoindexes created
    # for inline UNIQUE/PK constraints usually have NULL SQL; those are handled
    # by index_xinfo above unless they use features outside our supported scope.
    row = cursor.execute(
        """
        SELECT sql
        FROM sqlite_schema
        WHERE type = 'index'
          AND name = ?
        """,
        (index_name,),
    ).fetchone()
    if row is None:
        return None

    sql_text = row_value(row, "sql", 0)
    if sql_text is None:
        return None

    try:
        expression = parse_one(str(sql_text), read="sqlite")
    except ParseError:
        return None

    columns = {
        column.name
        for column in expression.find_all(exp.Column)
        if column.name
    }
    return columns or None
