"""SQLite connection wrapper that logs write statements into merge tables.

How it works:
- Wraps a ``sqlite3.Connection`` and routes public execute calls through replay
    preparation before execution.
- Buffers write statements that changed database state between ``BEGIN`` and
    ``COMMIT``.
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
    are marked unsafe for automatic replay. Failed statements that made no
    database change are not logged.
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
    prepare_logged_sql,
)

EXECUTION_ERROR_REASON = "statement raised an execution error"
PARAMETERIZED_DML_ERROR = (
    "sqlite-reconcile wrapper does not support bound parameters for logged DML; "
    "execute a literal SQL statement instead"
)

# Table names - prefixed to avoid collision with user tables
LOG_TABLE = "_sqlite_merge_log"
TX_TABLE = "_sqlite_merge_transactions"

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
    Wrap sqlite3.Connection and log committed DML statements.

    Architecture:
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
        self._buffer: list[LogEntry] = []
        self._savepoints: list[tuple[str, int]] = []
        self._transaction_error_reason: str | None = None

        self._init_log_tables()

    # Initialisation

    def _init_log_tables(self):
        """Create log tables if they don't exist."""
        for statement in INIT_SQL:
            statement = statement.strip()
            if statement:
                self._conn.execute(statement)

    # Buffer management

    def _begin(self):
        """Start a new explicit transaction buffer."""

        self._in_transaction = True
        self._buffer.clear()
        self._savepoints.clear()
        self._transaction_error_reason = None

    def _flush(self):
        """Write buffered statements after their transaction commits."""

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

    def _savepoint(self, sql: str):
        """Record a savepoint's current buffer length."""

        if not self._in_transaction:
            self._begin()
        name = sql.split()[-1].strip("\"'`;")
        self._savepoints.append((name, len(self._buffer)))

    def _release_savepoint(self):
        """Forget the most recent savepoint after SQLite releases it."""

        if self._savepoints:
            self._savepoints.pop()
        if self._in_transaction and not self._conn.in_transaction:
            self._flush()

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
        """

        # Insert transaction record and let SQLite fill committed_at.
        cursor = self._conn.execute(
            f"INSERT INTO {TX_TABLE} DEFAULT VALUES"
        )
        tx_id = cursor.lastrowid
        if tx_id is None:
            raise RuntimeError("SQLite did not return a transaction row id")

        # Insert all statements referencing this transaction.
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

    def _mark_transaction_error(self, error: sqlite3.Error) -> None:
        """Remember that a committed transaction needs replay-safety review."""

        if not self._in_transaction:
            return
        reason = f"{EXECUTION_ERROR_REASON}: {error}"
        self._transaction_error_reason = self._transaction_error_reason or reason

    def _entry_with_execution_error(
        self,
        entry: LogEntry,
        error: sqlite3.Error,
    ) -> LogEntry:
        """Mark a statement unsafe because it raised after changing rows."""

        reason = f"{EXECUTION_ERROR_REASON}: {error}"
        if entry.replay_block_reason:
            reason = f"{entry.replay_block_reason}; {reason}"
        return replace(
            entry,
            is_replay_safe=False,
            replay_block_reason=reason,
        )

    def _record_effectful_entry(self, entry: LogEntry | None) -> None:
        """Store one DML statement that changed database state."""

        if entry is None:
            return
        if self._in_transaction:
            self._buffer.append(entry)
        else:
            self._write([entry])

    def _entry_for_execute(
        self,
        sql: str,
        parameters,
    ) -> LogEntry | None:
        """Return the log entry for one public execute call, if any."""

        stripped = sql.strip()
        if not _should_log(stripped):
            return None
        if _has_parameters(parameters):
            raise sqlite3.ProgrammingError(PARAMETERIZED_DML_ERROR)
        return prepare_logged_sql(stripped, self._conn)

    def _execute_sql_for_entry(self, entry: LogEntry | None, sql: str) -> str:
        """Return SQL to execute for a possibly rewritten log entry."""

        if entry is not None and entry.is_replay_safe:
            return entry.to_replay_sql_text
        return sql

    def _execute_on(self, execute, sql, parameters=()):
        """Shared implementation for connection and cursor execute."""

        stripped = sql.strip()
        upper_sql = stripped.upper()
        entry = self._entry_for_execute(stripped, parameters)
        sql_to_execute = self._execute_sql_for_entry(entry, stripped)
        total_changes_before = self._conn.total_changes

        try:
            cursor = execute(sql_to_execute, parameters)
        except sqlite3.Error as error:
            # Some conflict algorithms, e.g. OR ROLLBACK, make SQLite roll
            # back the whole transaction before raising. Any buffered log rows
            # no longer describe committed/effectful work in that case.
            if self._in_transaction and not self._conn.in_transaction:
                self._discard()
                raise
            # OR FAIL can raise after earlier rows from the same statement were
            # changed. Keep that effect in the log, but block automatic replay.
            if (
                entry is not None
                and self._conn.total_changes != total_changes_before
            ):
                self._record_effectful_entry(
                    self._entry_with_execution_error(entry, error)
                )
            self._mark_transaction_error(error)
            raise

        if upper_sql.startswith("BEGIN"):
            self._begin()
        elif upper_sql.startswith("COMMIT"):
            self._flush()
        elif upper_sql.startswith("ROLLBACK TO") or upper_sql.startswith(
            "ROLLBACK TO SAVEPOINT"
        ):
            self._rollback_to_savepoint(stripped)
        elif upper_sql.startswith("ROLLBACK"):
            self._discard()
        elif upper_sql.startswith("SAVEPOINT"):
            self._savepoint(stripped)
        elif upper_sql.startswith("RELEASE"):
            self._release_savepoint()
        else:
            self._record_effectful_entry(entry)

        return cursor

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
            return self._execute_on(self._conn.execute, "COMMIT")
        except sqlite3.OperationalError as exc:
            if "no transaction" not in str(exc).lower():
                raise
        return None

    def rollback(self):
        try:
            return self._execute_on(self._conn.execute, "ROLLBACK")
        except sqlite3.OperationalError as exc:
            if "no transaction" not in str(exc).lower():
                raise
        return None

    def close(self):
        self._discard()
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
