"""SQLite connection wrapper that logs write statements into merge tables.

How it works:
- Wraps a ``sqlite3.Connection`` and installs a trace callback.
- Buffers write statements between ``BEGIN`` and ``COMMIT``.
- Writes one transaction row to ``_sqlite_merge_transactions`` and one log row
    per statement in ``_sqlite_merge_log``.
- A tatement executed outside an explicit ``BEGIN``/``COMMIT`` block
    is treated as an implicit single-statement transaction.
- Tracks savepoints so ``ROLLBACK TO`` can truncate the buffered statements.

Limitations:
- Only logs statements that start with the configured DML/DDL prefixes.
- Does not log ``SELECT``, ``PRAGMA``, transaction-control statements, or
    writes against the internal merge tables.
- Depends on SQLite trace output, so the recorded SQL text is the executed
    statement text rather than a semantic parse tree.
"""

import sqlite3
from datetime import datetime

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
        committed_at TEXT NOT NULL
    );""",
    f"""CREATE TABLE IF NOT EXISTS {LOG_TABLE} (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_id INTEGER NOT NULL REFERENCES {TX_TABLE}(id),
        sql_text       TEXT    NOT NULL
    );"""
]


def _should_log(sql_upper: str) -> bool:
    if LOG_TABLE.upper() in sql_upper or TX_TABLE.upper() in sql_upper:
        return False
    return sql_upper.startswith(INCLUDED)


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
        self._buffer = []        # list of sql_text strings pending commit
        self._savepoints = []    # list of (name, buffer_length_at_savepoint)

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
        '''called for every statement SQLite executes'''
        sql = sql.strip()
        sql_upper = sql.upper()

        # --- Transaction boundary handling ---
        if sql_upper.startswith("BEGIN"):
            self._in_transaction = True
            self._buffer.clear()
            self._savepoints.clear()
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

        if self._in_transaction:
            # Buffer until COMMIT
            self._buffer.append(sql)
        else:
            # Autocommit — write immediately as its own transaction
            self._write([sql])

    # Buffer management

    def _flush(self):
        """Write buffered statements on COMMIT."""
        if self._buffer:
            self._write(self._buffer)
        self._discard()

    def _discard(self):
        """Discard buffer on ROLLBACK."""
        self._buffer.clear()
        self._savepoints.clear()
        self._in_transaction = False

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

    def _write(self, statements: list):
        """
        Insert a transaction row and log all statements under it.
        Temporarily removes trace callback to avoid recursive logging.
        """
        self._conn.set_trace_callback(None)
        try:
            # Insert transaction record and get its id
            cursor = self._conn.execute(
                f"INSERT INTO {TX_TABLE} (committed_at) VALUES (?)",
                (datetime.now().isoformat(),)
            )
            tx_id = cursor.lastrowid

            # Insert all statements referencing this transaction
            self._conn.executemany(
                f"INSERT INTO {LOG_TABLE} (transaction_id, sql_text) VALUES (?, ?)",
                [(tx_id, sql) for sql in statements]
            )
        finally:
            self._conn.set_trace_callback(self._trace)

    # Public API - mirrors sqlite3.Connection

    def execute(self, sql, parameters=()):
        return self._conn.execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        return self._conn.executemany(sql, seq_of_parameters)

    def executescript(self, sql_script):
        return self._conn.executescript(sql_script)

    def cursor(self):
        return self._conn.cursor()

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
        Returns list of dicts with transaction_id, committed_at, sql_text.
        """
        cursor = self._conn.execute(f"""
            SELECT l.id, l.transaction_id, t.committed_at, l.sql_text
            FROM {LOG_TABLE} l
            JOIN {TX_TABLE} t ON l.transaction_id = t.id
            WHERE l.transaction_id > ?
            ORDER BY l.id
        """, (since_transaction_id,))
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]