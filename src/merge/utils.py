from __future__ import annotations

import sqlite3
from collections.abc import Container
from typing import TYPE_CHECKING

from sqlglot import expressions as exp

if TYPE_CHECKING:
    from .statement_metadata import StatementMetadata

ALL_COLUMNS = "*"
TableColumns = dict[str, set[str]]


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

    if isinstance(expression, exp.Schema) and isinstance(expression.this, exp.Table):
        return expression.this

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
    """Return user table names mapped to column names."""

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
        index_columns = {
            str(row_value(info_row, "name", 2))
            for info_row in cursor.execute(
                f"PRAGMA index_info({quote_identifier(index_name)})"
            ).fetchall()
            if row_value(info_row, "name", 2) is not None
        }
        if index_columns:
            key_sets.append(index_columns)

    return tuple(key_sets)
