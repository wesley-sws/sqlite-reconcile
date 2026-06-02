"""SQLite connection wrapper that logs write statements into merge tables.

How it works:
- Wraps a ``sqlite3.Connection`` and installs a trace callback.
- Buffers write statements between ``BEGIN`` and ``COMMIT``.
- Writes one transaction row to ``_sqlite_merge_transactions`` and one log row
    per statement in ``_sqlite_merge_log``.
- Log rows store the original SQL for display and the replay SQL used during
    merge. Unsafe nondeterministic statements are marked as blocked from
    automatic replay.
- A statement executed outside an explicit ``BEGIN``/``COMMIT`` block
    is treated as an implicit single-statement transaction.
- Tracks savepoints so ``ROLLBACK TO`` can truncate the buffered statements.

Limitations:
- Only logs configured DML statements, including DML preceded by CTEs.
- DDL statements are intentionally out of scope for this project.
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
import sys
from dataclasses import replace
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

_WRAPPER_DIR = Path(__file__).resolve().parent
_SRC_DIR = _WRAPPER_DIR.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from sqlite_replay_preparation import (  # noqa: E402
    LogEntry,
    PARAMETERIZED_REWRITE_REASON,
    prepare_logged_sql,
    unsafe_log_entry,
)

EXECUTION_ERROR_REASON = "statement raised an execution error"

# Table names - prefixed to avoid collision with user tables
LOG_TABLE = "_sqlite_merge_log"
TX_TABLE = "_sqlite_merge_transactions"

# Statements to exclude from logging
EXCLUDED = (
    "SELECT", "PRAGMA", "EXPLAIN",
    "BEGIN", "COMMIT", "ROLLBACK",
    "SAVEPOINT", "RELEASE",
)

# Statements to include. DDL is intentionally out of scope for this project.
INCLUDED = (
    "INSERT", "UPDATE", "DELETE", "REPLACE",  # DML
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


def _should_log(sql: str) -> bool:
    sql = sql.strip()
    sql_upper = sql.upper()
    if LOG_TABLE.upper() in sql_upper or TX_TABLE.upper() in sql_upper:
        return False
    if sql_upper.startswith(INCLUDED):
        return True
    if not re.match(r"WITH\b", sql_upper):
        return False

    try:
        parsed = sqlglot.parse_one(sql, dialect="sqlite")
    except ParseError:
        # Prefer logging an unsafe unusual CTE statement over silently missing
        # a write because its leading token is WITH.
        return True
    return isinstance(parsed, (exp.Insert, exp.Update, exp.Delete))


def _log_entry_if_logged(sql: str, conn: sqlite3.Connection) -> LogEntry | None:
    """Prepare a log entry only for statements that should be logged."""

    stripped = sql.strip()
    if not _should_log(stripped):
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
    Wraps sqlite3.Connection to intercept and log DML statements.

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
        if not _should_log(sql):
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
            if tx_id is None:
                raise RuntimeError("SQLite did not return a transaction row id")

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
                entry = unsafe_log_entry(sql, PARAMETERIZED_REWRITE_REASON)
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
