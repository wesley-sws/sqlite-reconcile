from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import ParseError

from sqlite_conflict_resolution import restore_update_conflict_resolution

from .models import ConflictCheckContext, LoggedStatement
from .sql_ast import (
    child_expressions,
    cte_aliases,
)
from .utils import TableColumns, table_expression

CONTROL_DB_SCHEMA = "control"


def clean_control_schema_references(message: str, schema: str | None) -> str:
    """Hide attached-control schema names in user-facing messages."""

    if schema is None:
        return message

    cleaned = message
    for prefix in (
        f"{schema}.",
        f'"{schema}".',
        f"[{schema}].",
        f"`{schema}`.",
    ):
        cleaned = cleaned.replace(prefix, "main.")
    return cleaned


def _load_working_base_copy(base_path: Path) -> sqlite3.Connection:
    """Return an open in-memory copy of base; the caller must close it."""

    working_conn = sqlite3.connect(":memory:")
    try:
        with closing(sqlite3.connect(base_path)) as base_conn:
            base_conn.backup(working_conn)
        working_conn.row_factory = sqlite3.Row
        working_conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        working_conn.close()
        raise
    return working_conn


@contextmanager
def _open_merge_working_connection(base_path: Path) -> Iterator[sqlite3.Connection]:
    """Open the mutable base copy with one attached control copy."""

    working_conn = _load_working_base_copy(base_path)
    control_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="sqlite_merge_control_",
            suffix=".db",
            delete=False,
        ) as control_file:
            control_path = Path(control_file.name)

        with (
            closing(sqlite3.connect(base_path)) as base_conn,
            closing(sqlite3.connect(control_path)) as control_conn,
        ):
            base_conn.backup(control_conn)

        working_conn.execute(
            f"ATTACH DATABASE ? AS {CONTROL_DB_SCHEMA}",
            (str(control_path),),
        )
        yield working_conn
    finally:
        try:
            working_conn.execute(f"DETACH DATABASE {CONTROL_DB_SCHEMA}")
        except sqlite3.Error:
            pass
        working_conn.close()
        if control_path is not None:
            control_path.unlink(missing_ok=True)


def _rewrite_sql_for_control_db(
    sql: str | LoggedStatement,
    schema: str = CONTROL_DB_SCHEMA,
    table_columns: TableColumns | None = None,
) -> str | None:
    """Return SQL with persistent table references qualified by control schema."""

    if isinstance(sql, LoggedStatement):
        return _rewrite_logged_statement_for_control_db(
            sql,
            schema=schema,
            table_columns=table_columns,
        )

    return _rewrite_parseable_sql_for_control_db(
        sql,
        schema=schema,
        table_columns=table_columns,
    )


def _rewrite_logged_statement_for_control_db(
    statement: LoggedStatement,
    *,
    schema: str,
    table_columns: TableColumns | None,
) -> str | None:
    """Return statement replay SQL rewritten for the attached control schema."""

    compatible = statement.metadata.compatible_sql
    # UPSERT is intentionally replayed on control as the strict INSERT form
    # stored in metadata. If that fails, the UPSERT path would have mattered and
    # the merge reports a conflict instead of trying to model DO UPDATE effects.
    rewritten = _rewrite_expression_for_control_db(
        statement.metadata.parsed_sql_text.copy(),
        schema=schema,
        table_columns=table_columns,
    )
    if rewritten is None:
        return None
    return restore_update_conflict_resolution(
        rewritten,
        compatible.conflict_resolution,
    )


def _rewrite_parseable_sql_for_control_db(
    sql_text: str,
    *,
    schema: str,
    table_columns: TableColumns | None,
) -> str | None:
    """Rewrite SQL once it is in a form sqlglot can parse directly."""

    try:
        expression = sqlglot.parse_one(sql_text, read="sqlite")
    except ParseError:
        return None

    return _rewrite_expression_for_control_db(
        expression,
        schema=schema,
        table_columns=table_columns,
    )


def _rewrite_expression_for_control_db(
    expression: exp.Expression,
    *,
    schema: str,
    table_columns: TableColumns | None,
) -> str | None:
    """Rewrite a parsed SQL AST for the attached control schema."""

    if not isinstance(expression, (exp.Select, exp.Insert, exp.Update, exp.Delete)):
        return None

    persistent_tables: set[str] = set(table_columns or ())
    tables = _persistent_table_references(expression, persistent_tables, schema)
    if tables is None:
        return None

    for table in tables:
        _qualify_table_reference(table, schema)

    return expression.sql(dialect="sqlite")


def _persistent_table_references(
    expression: exp.Expression,
    persistent_tables: set[str],
    schema: str,
) -> list[exp.Table] | None:
    """Return table nodes that should point at the attached control database."""

    tables: list[exp.Table] = []
    target_table = _statement_target_table(expression)
    if not _collect_persistent_table_references(
        expression,
        visible_ctes=set(),
        persistent_tables=persistent_tables,
        schema=schema,
        target_table=target_table,
        tables=tables,
    ):
        return None
    return tables


def _collect_persistent_table_references(
    expression: exp.Expression,
    *,
    visible_ctes: set[str],
    persistent_tables: set[str],
    schema: str,
    target_table: exp.Table | None,
    tables: list[exp.Table],
) -> bool:
    """DFS through SQL scopes, collecting real tables and skipping CTE refs."""

    if isinstance(expression, exp.Table):
        if (
            expression is not target_table
            and not expression.db
            and expression.name in visible_ctes
        ):
            return True

        if not _is_rewritable_persistent_table(expression, persistent_tables, schema):
            return False
        tables.append(expression)
        return True

    current_ctes = cte_aliases(expression)
    # SQLite lets CTEs in the same WITH block reference each other, including
    # later CTEs, so the normal children and the WITH child both see the full
    # block's aliases.
    visible_in_children = visible_ctes | current_ctes

    for child in child_expressions(expression):
        if not _collect_persistent_table_references(
            child,
            visible_ctes=visible_in_children,
            persistent_tables=persistent_tables,
            schema=schema,
            target_table=target_table,
            tables=tables,
        ):
            return False

    return True


def _statement_target_table(expression: exp.Expression) -> exp.Table | None:
    """Return the DML target table, if this expression writes one table."""

    if not isinstance(expression, (exp.Insert, exp.Update, exp.Delete)):
        return None
    return table_expression(expression.this)


def _is_rewritable_persistent_table(
    table: exp.Table,
    persistent_tables: set[str],
    schema: str,
) -> bool:
    """Return whether table can be safely qualified with the control schema."""

    return (
        table.name in persistent_tables
        and (not table.db or table.db in {"main", schema})
    )


def _qualify_table_reference(table: exp.Table, schema: str) -> None:
    """Qualify a table while preserving its original table name as an alias."""

    original_name = table.name
    has_alias = table.args.get("alias") is not None
    table.set("db", exp.to_identifier(schema))
    if not has_alias:
        table.set("alias", exp.TableAlias(this=exp.to_identifier(original_name)))


def _merge_context(
    working_conn: sqlite3.Connection,
    table_columns,
    primary_key_columns,
    key_column_sets,
) -> ConflictCheckContext:
    """Build the merge-analysis context around the live working copy."""

    return ConflictCheckContext(
        base_cursor=working_conn.cursor(),
        base_db_path=":memory:",
        table_columns=table_columns,
        primary_key_columns=primary_key_columns,
        key_column_sets=key_column_sets,
        control_schema=CONTROL_DB_SCHEMA,
        control_sql_rewriter=lambda sql: _rewrite_sql_for_control_db(
            sql,
            CONTROL_DB_SCHEMA,
            table_columns,
        ),
    )


@contextmanager
def _open_merge_working_context(
    base_path: Path,
    table_columns,
    primary_key_columns,
    key_column_sets,
) -> Iterator[ConflictCheckContext]:
    """Open the live working context used for the whole terminal merge."""

    with _open_merge_working_connection(base_path) as working_conn:
        yield _merge_context(
            working_conn,
            table_columns,
            primary_key_columns,
            key_column_sets,
        )
