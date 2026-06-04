"""Prepare replay-safe SQL text for the SQLite merge wrapper."""

import re
import sqlite3
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from sqlite_conflict_resolution import (
    CompatibleSQL,
    normalize_sql_for_sqlglot,
    restore_update_conflict_resolution,
)

STATEMENT_LEVEL_NONDETERMINISTIC = {
    "changes",
    "last_insert_rowid",
    "sqlite_source_id",
    "sqlite_version",
    "sqlite3_version",
    "total_changes",
}

PER_EVALUATION_NONDETERMINISTIC = {
    "random",
    "randomblob",
}

CURRENT_TIME_KEYWORDS = {
    "current_date",
    "current_time",
    "current_timestamp",
}

DATE_TIME_FUNCTIONS = {
    "date",
    "datetime",
    "julianday",
    "strftime",
    "time",
    "unixepoch",
}

DEFAULT_VALUES_PATTERN = re.compile(
    r"\bDEFAULT\s+VALUES\b",
    flags=re.IGNORECASE | re.DOTALL,
)

PARSE_ERROR_REASON = "statement could not be parsed for replay preparation"
UNSAFE_NONDETERMINISTIC_REASON = "nondeterministic expression cannot be safely materialized"
PARAMETERIZED_REWRITE_REASON = (
    "parameterized statement cannot be replayed without bound parameters"
)


@dataclass(frozen=True)
class LogEntry:
    original_sql_text: str
    to_replay_sql_text: str
    is_replay_safe: bool
    replay_block_reason: str | None = None


def unsafe_log_entry(sql: str, reason: str) -> LogEntry:
    """Return a blocked log entry that preserves the original SQL text."""

    return LogEntry(sql, sql, False, reason)


def _quote_identifier(identifier: str) -> str:
    """
    Safely quote a table/column name as a SQLite identifier.
    Internal double quotes are escaped by doubling them.
    """

    return '"' + identifier.replace('"', '""') + '"'


def _literal(value):
    """Convert a Python value returned by SQLite into a SQL literal node."""

    if value is None:
        return exp.Null()
    if isinstance(value, bool):
        return exp.Literal.number("1" if value else "0")
    if isinstance(value, (int, float)):
        return exp.Literal.number(str(value))
    return exp.Literal.string(str(value))


def _table_name_from_target(target: exp.Expression) -> str | None:
    if isinstance(target, exp.Table):
        return target.name
    if isinstance(target, exp.Schema) and isinstance(target.this, exp.Table):
        return target.this.name
    return None


def _explicit_insert_columns(insert: exp.Insert, conn: sqlite3.Connection) -> set[str]:
    """Return target columns supplied by INSERT, or all table columns if omitted."""

    target = insert.this
    if isinstance(target, exp.Schema):
        return {expression.name for expression in target.expressions if expression.name}

    table = _table_name_from_target(target)
    if table is None:
        return set()

    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})")
    }


def _function_name(node: exp.Expression) -> str | None:
    if isinstance(node, exp.CurrentDate):
        return "current_date"
    if isinstance(node, exp.CurrentTime):
        return "current_time"
    if isinstance(node, exp.CurrentTimestamp):
        return "current_timestamp"
    if isinstance(node, exp.Anonymous):
        return node.name.lower()
    return None


def _date_time_function_is_current_time(node: exp.Expression, name: str | None) -> bool:
    """Return whether a SQLite date/time function depends on execution time."""

    if name not in DATE_TIME_FUNCTIONS:
        return False

    expressions = list(node.expressions)
    if not expressions:
        # Function has no args so is treated as current time, e.g. datetime().
        return True

    rendered_args = [
        expression.sql(dialect="sqlite").lower()
        for expression in expressions
    ]
    return any(
        "'now'" in argument
        or "localtime" in argument
        or "utc" in argument
        for argument in rendered_args
    )


def _is_nondeterministic_function(node: exp.Expression) -> bool:
    """Return whether a parsed function call should be materialized or blocked."""

    name = _function_name(node)
    return bool(
        name in CURRENT_TIME_KEYWORDS
        or name in STATEMENT_LEVEL_NONDETERMINISTIC
        or name in PER_EVALUATION_NONDETERMINISTIC
        or _date_time_function_is_current_time(node, name)
    )


def _is_safe_per_evaluation_context(root: exp.Expression, node: exp.Expression) -> bool:
    """
    Return whether random/randomblob can be replaced one call at a time.

    Only INSERT VALUES is accepted, and SELECT/Subquery contexts are blocked.
    The check is deliberately conservative because proving that an expression
    is evaluated once is out of scope for the wrapper.
    """

    if not isinstance(root, exp.Insert):
        return False
    if not isinstance(root.expression, exp.Values):
        return False

    current = node.parent
    while current is not None and current is not root:
        if isinstance(current, (exp.Select, exp.Subquery)):
            return False
        current = current.parent
    return True


def _unsafe_nondeterministic_functions(root: exp.Expression) -> list[exp.Expression]:
    """Return nondeterministic calls that may be evaluated once per affected row."""

    unsafe: list[exp.Expression] = []
    for node in root.find_all(
        exp.Anonymous,
        exp.CurrentDate,
        exp.CurrentTime,
        exp.CurrentTimestamp,
    ):
        name = _function_name(node)
        if name not in PER_EVALUATION_NONDETERMINISTIC:
            continue
        if not _is_safe_per_evaluation_context(root, node):
            unsafe.append(node)
    return unsafe


def _evaluate_expression(conn: sqlite3.Connection, expression_sql: str):
    return conn.execute(f"SELECT {expression_sql}").fetchone()[0]


def _replace_safe_nondeterministic_functions(
    root: exp.Expression,
    conn: sqlite3.Connection,
) -> bool:
    """Evaluate safe nondeterministic functions and replace them with literals."""

    changed = False
    for node in list(
        root.find_all(
            exp.Anonymous,
            exp.CurrentDate,
            exp.CurrentTime,
            exp.CurrentTimestamp,
        )
    ):
        if not _is_nondeterministic_function(node):
            continue

        value = _evaluate_expression(conn, node.sql(dialect="sqlite"))
        node.replace(_literal(value))
        changed = True
    return changed


def _default_is_nondeterministic(default_sql: object) -> bool:
    """Return whether a DEFAULT expression should be materialized for replay."""

    if default_sql is None:
        return False

    try:
        tree = sqlglot.parse_one(str(default_sql), dialect="sqlite")
    except ParseError:
        return False

    return any(
        _is_nondeterministic_function(node)
        for node in tree.find_all(
            exp.Anonymous,
            exp.CurrentDate,
            exp.CurrentTime,
            exp.CurrentTimestamp,
        )
    )


def _nondeterministic_default_columns(
    conn: sqlite3.Connection,
    table: str,
    explicit_columns: set[str],
) -> list[tuple[str, str]]:
    """Return omitted columns whose DEFAULT expression is nondeterministic."""

    columns: list[tuple[str, str]] = []
    for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})"):
        name = str(row[1])
        if name in explicit_columns:
            continue

        default_sql = row[4]
        if _default_is_nondeterministic(default_sql):
            columns.append((name, str(default_sql)))
    return columns


def _append_target_columns(insert: exp.Insert, columns: list[str]) -> None:
    """Add materialized default columns to an INSERT target column list."""

    if not columns:
        return

    target = insert.this
    if isinstance(target, exp.Schema):
        target.set(
            "expressions",
            [
                *target.expressions,
                *(exp.to_identifier(column) for column in columns),
            ],
        )
        return

    if isinstance(target, exp.Table):
        insert.set(
            "this",
            exp.Schema(
                this=target.copy(),
                expressions=[exp.to_identifier(column) for column in columns],
            ),
        )


def _append_values_defaults(
    values: exp.Values,
    defaults: list[tuple[str, str]],
    conn: sqlite3.Connection,
) -> None:
    """Append evaluated default values to each row in an INSERT VALUES clause."""

    new_rows = []
    for row in values.expressions:
        row_values = list(row.expressions) if isinstance(row, exp.Tuple) else [row]
        for _, default_sql in defaults:
            row_values.append(_literal(_evaluate_expression(conn, default_sql)))
        new_rows.append(exp.Tuple(expressions=row_values))

    values.set("expressions", new_rows)


def _rewrite_insert_defaults(
    root: exp.Expression,
    conn: sqlite3.Connection,
) -> tuple[bool, bool]:
    """
    Materialize omitted nondeterministic defaults when INSERT VALUES is safe.

    The first boolean says whether the statement is safe for automatic replay;
    the second says whether the SQL tree changed.
    """

    if not isinstance(root, exp.Insert):
        return True, False

    table = _table_name_from_target(root.this)
    if table is None:
        return True, False

    explicit_columns = _explicit_insert_columns(root, conn)
    defaults = _nondeterministic_default_columns(conn, table, explicit_columns)
    if not defaults:
        return True, False

    insert_expression = root.expression
    if isinstance(insert_expression, exp.Values):
        _append_target_columns(root, [column for column, _ in defaults])
        _append_values_defaults(insert_expression, defaults, conn)
        return True, True

    return False, False


def _rewrite_default_values_insert(
    sql: str,
    conn: sqlite3.Connection,
) -> LogEntry | None:
    """Handle INSERT ... DEFAULT VALUES, which sqlglot cannot currently parse."""

    normalized = DEFAULT_VALUES_PATTERN.sub("VALUES ()", sql, count=1)
    if normalized == sql:
        return None

    try:
        tree = sqlglot.parse_one(normalized, dialect="sqlite")
    except ParseError:
        return None

    if not isinstance(tree, exp.Insert):
        return None

    table = _table_name_from_target(tree.this)
    if table is None:
        return None

    defaults = _nondeterministic_default_columns(conn, table, explicit_columns=set())
    if not defaults:
        return LogEntry(sql, sql, True)

    try:
        _append_target_columns(tree, [column for column, _ in defaults])
        _append_values_defaults(tree.expression, defaults, conn)
    except sqlite3.Error:
        # Cannot safely evaluate this because execute parameters are unavailable.
        return unsafe_log_entry(sql, PARAMETERIZED_REWRITE_REASON)
    return LogEntry(sql, tree.sql(dialect="sqlite"), True)


def prepare_logged_sql(sql: str, conn: sqlite3.Connection) -> LogEntry:
    """Build the original/replay log entry for one statement before execution."""

    compatible_sql: CompatibleSQL | None = None
    try:
        tree = sqlglot.parse_one(sql, dialect="sqlite")
    except ParseError:
        default_values_entry = _rewrite_default_values_insert(sql, conn)
        if default_values_entry is not None:
            return default_values_entry
        compatible_sql = normalize_sql_for_sqlglot(sql)
        if compatible_sql.sql == sql:
            return unsafe_log_entry(sql, PARSE_ERROR_REASON)
        try:
            tree = sqlglot.parse_one(compatible_sql.sql, dialect="sqlite")
        except ParseError:
            return unsafe_log_entry(sql, PARSE_ERROR_REASON)

    if _unsafe_nondeterministic_functions(tree):
        return unsafe_log_entry(sql, UNSAFE_NONDETERMINISTIC_REASON)

    try:
        changed = _replace_safe_nondeterministic_functions(tree, conn)
        defaults_safe, defaults_changed = _rewrite_insert_defaults(tree, conn)
    except sqlite3.Error:
        # Cannot safely evaluate this because execute parameters are unavailable.
        return unsafe_log_entry(sql, PARAMETERIZED_REWRITE_REASON)
    if not defaults_safe:
        return unsafe_log_entry(sql, UNSAFE_NONDETERMINISTIC_REASON)
    changed = changed or defaults_changed
    if not changed:
        return LogEntry(sql, sql, True)

    if compatible_sql is not None and compatible_sql.stripped_upsert:
        return unsafe_log_entry(sql, UNSAFE_NONDETERMINISTIC_REASON)

    replay_sql = tree.sql(dialect="sqlite")
    if compatible_sql is not None:
        replay_sql = restore_update_conflict_resolution(
            replay_sql,
            compatible_sql.conflict_resolution,
        )
    return LogEntry(sql, replay_sql, True)
