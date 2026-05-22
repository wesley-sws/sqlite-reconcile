"""SQLite connection wrapper that logs write statements into merge tables.

How it works:
- Wraps a ``sqlite3.Connection`` and installs a trace callback.
- Buffers write statements between ``BEGIN`` and ``COMMIT``.
- Writes one transaction row to ``_sqlite_merge_transactions`` and one log row
    per statement in ``_sqlite_merge_log``.
- Log rows store the original SQL for display and the replay SQL used by the
    merge driver. Unsafe nondeterministic statements are marked as blocked from
    automatic replay.
- A statement executed outside an explicit ``BEGIN``/``COMMIT`` block
    is treated as an implicit single-statement transaction.
- Tracks savepoints so ``ROLLBACK TO`` can truncate the buffered statements.

Limitations:
- Only logs statements that start with the configured DML/DDL prefixes.
- Does not log ``SELECT``, ``PRAGMA``, transaction-control statements, or
    writes against the internal merge tables.
- Nondeterministic rewrite handling is limited to known SQLite built-ins; it
    does not inspect application-defined functions.
- If a committed explicit transaction had an execution error, its log entries
    are marked unsafe for automatic replay. Failed autocommit statements that
    SQLite never traces are not logged because they made no database change.
"""

import re
import sqlite3
from dataclasses import dataclass, replace
import sqlglot
from sqlglot import exp

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


@dataclass(frozen=True)
class LogEntry:
    original_sql_text: str
    to_replay_sql_text: str
    is_replay_safe: bool
    replay_block_reason: str | None = None


PARSE_ERROR_REASON = "statement could not be parsed for replay preparation"
UNSAFE_NONDETERMINISTIC_REASON = "nondeterministic expression cannot be safely materialized"
PARAMETERIZED_REWRITE_REASON = "parameterized statement needs nondeterministic rewrite"
EXECUTION_ERROR_REASON = "statement raised an execution error"


def _unsafe_log_entry(sql: str, reason: str) -> LogEntry:
    return LogEntry(sql, sql, False, reason)


def _quote_identifier(identifier: str) -> str:
    '''
    safely quotes table/column name as SQLite identifier
    the replace escapes the internal quote by doubling it
    '''
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
        # function has no args so treated as current time, eg datetime()
        return True

    rendered_args = [expression.sql(dialect="sqlite").lower() for expression in expressions]
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
    only if it is in INSERT VALUES and not within a SELECT is it safe
    The check is deliberately conservative and simple and may block certain 
    statements even though they are safe, eg
    UPDATE users SET token = (SELECT random()) WHERE id = 1;
    proving whether certain expressions is only evaluated at least once is 
    nontrivial and requires detailed expression analysis - out of scope
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
    for node in root.find_all(exp.Anonymous, exp.CurrentDate, exp.CurrentTime, exp.CurrentTimestamp):
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
    for node in list(root.find_all(exp.Anonymous, exp.CurrentDate, exp.CurrentTime, exp.CurrentTimestamp)):
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
    except sqlglot.errors.ParseError:
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
            [*target.expressions, *(exp.to_identifier(column) for column in columns)],
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
    first boolean indicates whether statement is safe for replacement of
    non-deterministic functions, second indicates whether statement has changed
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
    except sqlglot.errors.ParseError:
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
        # Cannot safely evaluate this because execute parameters are not available here.
        return _unsafe_log_entry(sql, PARAMETERIZED_REWRITE_REASON)
    return LogEntry(sql, tree.sql(dialect="sqlite"), True)


def prepare_logged_sql(sql: str, conn: sqlite3.Connection) -> LogEntry:
    """Build the original/replay log entry for one statement before execution."""

    try:
        tree = sqlglot.parse_one(sql, dialect="sqlite")
    except sqlglot.errors.ParseError:
        default_values_entry = _rewrite_default_values_insert(sql, conn)
        if default_values_entry is not None:
            return default_values_entry
        # otherwise it means statement can't be parsed - hence replay is not safe
        return _unsafe_log_entry(sql, PARSE_ERROR_REASON)

    if _unsafe_nondeterministic_functions(tree):
        return _unsafe_log_entry(sql, UNSAFE_NONDETERMINISTIC_REASON)

    try:
        changed = _replace_safe_nondeterministic_functions(tree, conn)
        defaults_safe, defaults_changed = _rewrite_insert_defaults(tree, conn)
    except sqlite3.Error:
        # Cannot safely evaluate this because execute parameters are not available here.
        return _unsafe_log_entry(sql, PARAMETERIZED_REWRITE_REASON)
    if not defaults_safe:
        return _unsafe_log_entry(sql, UNSAFE_NONDETERMINISTIC_REASON)
    changed = changed or defaults_changed

    return LogEntry(sql, tree.sql(dialect="sqlite") if changed else sql, True)

# Table names - prefixed to avoid collision with user tables
LOG_TABLE = "_sqlite_merge_log"
TX_TABLE = "_sqlite_merge_transactions"

# Statements to exclude from logging
EXCLUDED = (
    "SELECT", "PRAGMA", "EXPLAIN",
    "BEGIN", "COMMIT", "ROLLBACK",
    "SAVEPOINT", "RELEASE",
)

# Statements to include
INCLUDED = (
    "INSERT", "UPDATE", "DELETE", "REPLACE",  # DML
    "CREATE", "ALTER", "DROP",                 # DDL
)

INIT_SQL = [
    f"""CREATE TABLE IF NOT EXISTS {TX_TABLE} (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );""",
    f"""CREATE TABLE IF NOT EXISTS {LOG_TABLE} (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_id     INTEGER NOT NULL REFERENCES {TX_TABLE}(id),
        original_sql_text  TEXT    NOT NULL,
        to_replay_sql_text TEXT    NOT NULL,
        is_replay_safe     INTEGER NOT NULL DEFAULT 1,
        replay_block_reason TEXT
    );"""
]


def _should_log(sql_upper: str) -> bool:
    if LOG_TABLE.upper() in sql_upper or TX_TABLE.upper() in sql_upper:
        return False
    return sql_upper.startswith(INCLUDED)


def _log_entry_if_logged(sql: str, conn: sqlite3.Connection) -> LogEntry | None:
    """Prepare a log entry only for statements that should be logged."""

    stripped = sql.strip()
    if not _should_log(stripped.upper()):
        return None
    return prepare_logged_sql(stripped, conn)


def _needs_pending_entry(entry: LogEntry) -> bool:
    """Return whether trace fallback needs a prepared log entry."""

    return (
        not entry.is_replay_safe
        or entry.original_sql_text != entry.to_replay_sql_text
    )


def _has_parameters(parameters) -> bool:
    """Return whether execute parameters were supplied."""

    if parameters is None:
        return False
    try:
        return bool(parameters)
    except TypeError:
        return True


def _split_sql_script(sql_script: str) -> list[str]:
    """Split a SQL script into complete statements using SQLite's parser check."""

    statements = []
    buffer = []
    for char in sql_script:
        buffer.append(char)
        candidate = "".join(buffer)
        if sqlite3.complete_statement(candidate):
            statement = candidate.strip()
            if statement.rstrip(";").strip():
                statements.append(statement)
            buffer.clear()

    trailing = "".join(buffer).strip()
    if trailing:
        statements.append(trailing)
    return statements


class SQLiteCursorWrapper:
    """Cursor proxy that routes writes through the wrapper log preparation."""

    def __init__(self, wrapper, cursor):
        self._wrapper = wrapper
        self._cursor = cursor

    def execute(self, sql, parameters=()):
        """Execute SQL through the parent wrapper's replay preparation."""

        self._wrapper._execute_on(self._cursor.execute, sql, parameters)
        return self

    def executemany(self, sql, seq_of_parameters):
        """Execute each parameter set through the parent wrapper."""

        for parameters in seq_of_parameters:
            self.execute(sql, parameters)
        return self

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class SQLiteWrapper:
    """
    Wraps sqlite3.Connection to intercept and log DML/DDL statements.

    Architecture:
    - Uses SQLite's trace callback to intercept every statement
    - Buffers statements between BEGIN and COMMIT
    - On COMMIT: inserts a row into _sqlite_merge_transactions,
      then inserts all buffered statements into _sqlite_merge_log
      referencing that transaction id
    - On ROLLBACK: discards the buffer
    - Autocommit statements (no explicit BEGIN) each get their
      own transaction row
    - Savepoints are tracked so ROLLBACK TO SAVEPOINT only discards
      statements added after that savepoint
    """

    def __init__(self, database, **kwargs):
        # isolation_level=None gives us full manual transaction control
        self._conn = sqlite3.connect(database, isolation_level=None, **kwargs)
        self._in_transaction = False
        self._buffer = []        # list of LogEntry objects PENDING commit
        # Prepared metadata for the next trace event. Public multi-statement
        # APIs route through one execute call at a time, so one slot is enough.
        self._pending_log_entry = None
        # Transaction id of an autocommit log row written during the current
        # execute call. executemany/executescript both route through execute.
        self._current_autocommit_tx_id = None
        self._savepoints = []    # list of (name, buffer_length_at_savepoint)
        self._transaction_error_reason = None

        # Create log tables then install trace callback
        self._init_log_tables()
        self._conn.set_trace_callback(self._trace)

    # Initialisation

    def _init_log_tables(self):
        """Create log tables if they don't exist."""
        for statement in INIT_SQL:
            statement = statement.strip()
            if statement:
                self._conn.execute(statement)

    def _trace(self, sql: str):
        """Buffer or immediately log statements observed by SQLite trace."""

        sql = sql.strip()
        sql_upper = sql.upper()

        # --- Transaction boundary handling ---
        if sql_upper.startswith("BEGIN"):
            self._in_transaction = True
            self._buffer.clear()
            self._savepoints.clear()
            self._transaction_error_reason = None
            return

        if sql_upper.startswith("COMMIT"):
            self._flush()
            return

        if sql_upper.startswith("ROLLBACK TO") or sql_upper.startswith("ROLLBACK TO SAVEPOINT"):
            self._rollback_to_savepoint(sql)
            return

        if sql_upper.startswith("ROLLBACK"):
            self._discard()
            return

        if sql_upper.startswith("SAVEPOINT"):
            name = sql.split()[-1].strip("\"'`;")
            self._savepoints.append((name, len(self._buffer)))
            return

        if sql_upper.startswith("RELEASE"):
            if self._savepoints:
                self._savepoints.pop()
            return

        # --- Decide whether to log ---
        if not _should_log(sql_upper):
            return

        if self._pending_log_entry is not None:
            '''
            Pair the next loggable trace event with the entry prepared
            by the public execute path. The trace callback has no side channel
            for ids or context, so this assumes no trigger/cascade interleaving.
            '''
            entry = self._entry_from_trace(self._pending_log_entry, sql)
            self._pending_log_entry = None
        else:
            entry = LogEntry(sql, sql, True)

        if self._in_transaction:
            # Buffer until COMMIT
            self._buffer.append(entry)
        else:
            # Autocommit — write immediately as its own transaction
            tx_id = self._write([entry])
            self._current_autocommit_tx_id = tx_id

    # Buffer management

    def _flush(self):
        """Write buffered statements on COMMIT."""
        if self._buffer:
            if self._transaction_error_reason:
                self._buffer = [
                    replace(
                        entry,
                        is_replay_safe=False,
                        replay_block_reason=(
                            entry.replay_block_reason
                            or self._transaction_error_reason
                        ),
                    )
                    for entry in self._buffer
                ]
            self._write(self._buffer)
        self._discard()

    def _discard(self):
        """Discard buffer on ROLLBACK."""
        self._buffer.clear()
        self._savepoints.clear()
        self._in_transaction = False
        self._transaction_error_reason = None

    def _rollback_to_savepoint(self, sql: str):
        """Discard statements added after the named savepoint."""
        name = sql.split()[-1].strip("\"'`;")
        for i in range(len(self._savepoints) - 1, -1, -1):
            sp_name, buf_idx = self._savepoints[i]
            if sp_name.upper() == name.upper():
                self._buffer = self._buffer[:buf_idx]
                self._savepoints = self._savepoints[:i + 1]
                return

    # Writing to log tables

    def _write(self, statements: list) -> int:
        """
        Insert a transaction row and log all statements under it.
        Temporarily removes trace callback to avoid recursive logging.
        """
        self._conn.set_trace_callback(None)
        try:
            # Insert transaction record and let SQLite fill committed_at.
            cursor = self._conn.execute(
                f"INSERT INTO {TX_TABLE} DEFAULT VALUES"
            )
            tx_id = cursor.lastrowid

            # Insert all statements referencing this transaction
            self._conn.executemany(
                f"""
                INSERT INTO {LOG_TABLE} (
                    transaction_id,
                    original_sql_text,
                    to_replay_sql_text,
                    is_replay_safe,
                    replay_block_reason
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        tx_id,
                        statement.original_sql_text,
                        statement.to_replay_sql_text,
                        int(statement.is_replay_safe),
                        statement.replay_block_reason,
                    )
                    for statement in statements
                ],
            )
            return tx_id
        finally:
            self._conn.set_trace_callback(self._trace)

    def _entry_from_trace(self, entry: LogEntry, traced_sql: str) -> LogEntry:
        """Combine a prepared log entry with the SQL text observed by trace."""

        if not entry.is_replay_safe:
            return LogEntry(
                traced_sql,
                traced_sql,
                False,
                entry.replay_block_reason,
            )
        if entry.original_sql_text != entry.to_replay_sql_text:
            return LogEntry(entry.original_sql_text, traced_sql, True)
        return entry

    def _mark_execution_error(
        self,
        autocommit_tx_id: int | None,
        error: sqlite3.Error,
    ) -> None:
        """Mark log entries observed during a failed SQLite call as unsafe."""

        reason = f"{EXECUTION_ERROR_REASON}: {error}"
        if self._in_transaction:
            # If user commits after an error, the whole transaction depends on
            # application-level error handling, so keep it blocked from replay.
            self._transaction_error_reason = self._transaction_error_reason or reason
            return

        if autocommit_tx_id is None:
            return

        # Autocommit trace writes the log row immediately, so update it in-place
        # if the surrounding SQLite call later raises.
        self._conn.set_trace_callback(None)
        try:
            self._conn.execute(
                f"""
                UPDATE {LOG_TABLE}
                SET is_replay_safe = 0,
                    replay_block_reason = COALESCE(replay_block_reason, ?)
                WHERE transaction_id = ?
                """,
                (reason, autocommit_tx_id),
            )
        finally:
            self._conn.set_trace_callback(self._trace)

    def _run_with_error_tracking(self, operation):
        """Run SQLite work and mark traced statements unsafe if it fails."""

        self._current_autocommit_tx_id = None
        try:
            return operation()
        except sqlite3.Error as error:
            self._mark_execution_error(self._current_autocommit_tx_id, error)
            raise
        finally:
            self._current_autocommit_tx_id = None

    def _run_with_pending_entry(self, entry: LogEntry, operation):
        """Run SQLite work that expects trace to consume one prepared entry."""

        self._pending_log_entry = entry
        try:
            return self._run_with_error_tracking(operation)
        finally:
            # If SQLite errors before trace consumes the entry, remove it so the
            # next statement cannot be paired with stale replay metadata.
            if self._pending_log_entry is entry:
                self._pending_log_entry = None

    def _execute_on(self, execute, sql, parameters=()):
        """Shared implementation for connection and cursor execute."""

        entry = _log_entry_if_logged(sql, self._conn)
        if entry is None:
            return self._run_with_error_tracking(lambda: execute(sql, parameters))

        if _needs_pending_entry(entry):
            if _has_parameters(parameters) and entry.original_sql_text != entry.to_replay_sql_text:
                # Do not rewrite parameterized SQL; trace gives the bound SQL for UI, but replay is unsafe.
                entry = _unsafe_log_entry(sql, PARAMETERIZED_REWRITE_REASON)
                return self._run_with_pending_entry(
                    entry,
                    lambda: execute(sql, parameters),
                )

            return self._run_with_pending_entry(
                entry,
                lambda: execute(entry.to_replay_sql_text, parameters),
            )
        return self._run_with_error_tracking(lambda: execute(sql, parameters))

    # Public API - mirrors sqlite3.Connection

    def execute(self, sql, parameters=()):
        """Execute one statement after preparing replay-safe log metadata."""

        return self._execute_on(self._conn.execute, sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        """Execute each parameter set through the normal execute path."""

        cursor = self._conn.cursor()
        for parameters in seq_of_parameters:
            cursor = self.execute(sql, parameters)
        return cursor

    def executescript(self, sql_script):
        """Execute script statements through the same preparation as execute."""

        cursor = self._conn.cursor()
        for statement in _split_sql_script(sql_script):
            cursor = self._execute_on(self._conn.execute, statement)
        return cursor

    def cursor(self):
        return SQLiteCursorWrapper(self, self._conn.cursor())

    def commit(self):
        try:
            self._conn.execute("COMMIT")
        except sqlite3.OperationalError:
            pass

    def rollback(self):
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    def close(self):
        self._conn.set_trace_callback(None)
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
        return False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    # Utility

    def get_log(self, since_transaction_id=0):
        """
        Return log entries after a given transaction_id.
        Returns list of dicts with transaction_id, committed_at, and SQL text.
        """
        cursor = self._conn.execute(f"""
            SELECT l.id,
                   l.transaction_id,
                   t.committed_at,
                   l.original_sql_text,
                   l.to_replay_sql_text,
                   l.to_replay_sql_text AS sql_text,
                   l.is_replay_safe,
                   l.replay_block_reason
            FROM {LOG_TABLE} l
            JOIN {TX_TABLE} t ON l.transaction_id = t.id
            WHERE l.transaction_id > ?
            ORDER BY l.id
        """, (since_transaction_id,))
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
