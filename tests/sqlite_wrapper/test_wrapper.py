import sqlite3
import sys
from pathlib import Path

import pytest

SRC_PATH = Path(__file__).resolve().parents[2] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

import sqlite_wrapper  # noqa: E402
import sqlite_wrapper.wrapper as sqlite_wrapper_module  # noqa: E402


@pytest.fixture(scope="module")
def wrapper_module():
    return sqlite_wrapper_module


def test_import_friendly_wrapper_package():
    assert sqlite_wrapper.SQLiteWrapper.__name__ == "SQLiteWrapper"


def create_user_db(db_path: Path):
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                token INTEGER
            )
            """
        )
        con.execute(
            """
            CREATE TABLE random_events (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                token INTEGER DEFAULT (random())
            )
            """
        )
        con.commit()


def make_wrapper(tmp_path: Path, wrapper_module):
    db_path = tmp_path / "test.db"
    create_user_db(db_path)
    return wrapper_module.SQLiteWrapper(db_path)


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("INSERT INTO users VALUES (1, 'Alice', 'a@example.com')", True),
        ("UPDATE users SET name = 'Alice' WHERE id = 1", True),
        ("DELETE FROM users WHERE id = 1", True),
        ("SELECT * FROM users", False),
        ("WITH c AS (SELECT 1) SELECT * FROM c", False),
        ("CREATE TABLE extra (id INTEGER PRIMARY KEY)", False),
        ("ALTER TABLE users ADD COLUMN nickname TEXT", False),
        ("DROP TABLE users", False),
        (
            "WITH c AS (SELECT 1 AS id) "
            "INSERT INTO users (id, name) SELECT id, 'Alice' FROM c",
            True,
        ),
        ("PRAGMA table_info(users)", False),
        ("BEGIN TRANSACTION", False),
        ("COMMIT", False),
    ],
)
def test_should_log_filters_expected_statements(wrapper_module, sql, expected):
    assert wrapper_module._should_log(sql) is expected


def test_autocommit_statements_are_logged_as_one_transaction(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com')")

        log_entries = wrapper.get_log()
        assert len(log_entries) == 1
        assert log_entries[0]["sql_text"] == "INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com')"
        assert log_entries[0]["transaction_id"] >= 1
    finally:
        wrapper.close()


def test_with_insert_is_logged(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute(
            "WITH c AS (SELECT 1 AS id, 'Alice' AS name) "
            "INSERT INTO users (id, name) SELECT id, name FROM c"
        )

        log_entries = wrapper.get_log()
        assert len(log_entries) == 1
        assert log_entries[0]["sql_text"].startswith("WITH c AS")
    finally:
        wrapper.close()


def test_update_or_clause_is_logged_as_replay_safe(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute(
            "INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'a@example.com')"
        )
        wrapper.execute(
            "UPDATE OR IGNORE users SET email = 'b@example.com' WHERE id = 1"
        )

        entry = wrapper.get_log()[-1]
        assert entry["sql_text"] == (
            "UPDATE OR IGNORE users SET email = 'b@example.com' WHERE id = 1"
        )
        assert entry["is_replay_safe"] == 1
        assert entry["replay_block_reason"] is None
    finally:
        wrapper.close()


def test_replace_into_is_logged_as_replay_safe(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute(
            "REPLACE INTO users (id, name, email) VALUES (1, 'Alice', 'a@example.com')"
        )

        entry = wrapper.get_log()[0]
        assert entry["sql_text"] == (
            "REPLACE INTO users (id, name, email) VALUES (1, 'Alice', 'a@example.com')"
        )
        assert entry["is_replay_safe"] == 1
        assert entry["replay_block_reason"] is None
    finally:
        wrapper.close()


def test_explicit_transaction_buffers_until_commit(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("BEGIN")
        wrapper.execute("INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com')")
        wrapper.execute("UPDATE users SET email = 'alice@new.example.com' WHERE id = 1")

        assert wrapper.get_log() == []

        wrapper.commit()

        log_entries = wrapper.get_log()
        assert len(log_entries) == 2
        assert {entry["transaction_id"] for entry in log_entries} == {log_entries[0]["transaction_id"]}
        assert [entry["sql_text"] for entry in log_entries] == [
            "INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com')",
            "UPDATE users SET email = 'alice@new.example.com' WHERE id = 1",
        ]
    finally:
        wrapper.close()


def test_rollback_discards_buffered_statements(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("BEGIN")
        wrapper.execute("INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com')")
        wrapper.rollback()

        assert wrapper.get_log() == []
    finally:
        wrapper.close()


def test_savepoint_rollback_keeps_earlier_buffered_statements(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("BEGIN")
        wrapper.execute("INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com')")
        wrapper.execute("SAVEPOINT sp1")
        wrapper.execute("INSERT INTO users (id, name, email) VALUES (2, 'Bob', 'bob@example.com')")
        wrapper.execute("ROLLBACK TO sp1")
        wrapper.commit()

        log_entries = wrapper.get_log()
        assert len(log_entries) == 1
        assert log_entries[0]["sql_text"] == "INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com')"
    finally:
        wrapper.close()


def test_excluded_statements_are_not_logged(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("SELECT * FROM users")
        wrapper.execute("PRAGMA table_info(users)")

        assert wrapper.get_log() == []
    finally:
        wrapper.close()


def test_context_manager_commits_on_clean_exit(tmp_path, wrapper_module):
    db_path = tmp_path / "test.db"
    create_user_db(db_path)

    with wrapper_module.SQLiteWrapper(db_path) as wrapper:
        wrapper.execute("INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com')")

    with wrapper_module.SQLiteWrapper(db_path) as wrapper:
        log_entries = wrapper.get_log()
        assert len(log_entries) == 1
        assert log_entries[0]["sql_text"] == "INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com')"


def test_get_log_since_transaction_id_filters_older_entries(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com')")
        first_tx_id = wrapper.get_log()[0]["transaction_id"]

        wrapper.execute("INSERT INTO users (id, name, email) VALUES (2, 'Bob', 'bob@example.com')")
        all_entries = wrapper.get_log()
        filtered_entries = wrapper.get_log(since_transaction_id=first_tx_id)

        assert len(all_entries) == 2
        assert len(filtered_entries) == 1
        assert filtered_entries[0]["sql_text"] == "INSERT INTO users (id, name, email) VALUES (2, 'Bob', 'bob@example.com')"
    finally:
        wrapper.close()


def test_executemany_rejects_parameterized_dml(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        with pytest.raises(sqlite3.ProgrammingError, match="bound parameters"):
            wrapper.executemany(
                "INSERT INTO users (id, name, email) VALUES (?, ?, ?)",
                [
                    (1, "Alice", "alice@example.com"),
                    (2, "Bob", "bob@example.com"),
                ],
            )

        assert wrapper.get_log() == []
        assert wrapper.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    finally:
        wrapper.close()


def test_executescript_logs_multiple_statements(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.executescript(
            """
            INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com');
            INSERT INTO users (id, name, email) VALUES (2, 'Bob', 'bob@example.com');
            """
        )

        log_entries = wrapper.get_log()
        assert len(log_entries) == 2
        assert [entry["sql_text"].rstrip(";") for entry in log_entries] == [
            "INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com')",
            "INSERT INTO users (id, name, email) VALUES (2, 'Bob', 'bob@example.com')",
        ]
    finally:
        wrapper.close()


def test_executescript_preserves_explicit_transaction(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.executescript(
            """
            BEGIN;
            INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com');
            INSERT INTO users (id, name, email) VALUES (2, 'Bob', 'bob@example.com');
            COMMIT;
            """
        )

        log_entries = wrapper.get_log()
        assert len(log_entries) == 2
        assert {entry["transaction_id"] for entry in log_entries} == {
            log_entries[0]["transaction_id"]
        }
    finally:
        wrapper.close()


def test_executescript_split_preserves_semicolon_inside_string(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.executescript(
            """
            INSERT INTO users (id, name, email) VALUES (1, 'Alice; A', 'alice@example.com');
            INSERT INTO users (id, name, email) VALUES (2, 'Bob', 'bob@example.com');
            """
        )

        log_entries = wrapper.get_log()
        assert len(log_entries) == 2
        assert log_entries[0]["sql_text"].rstrip(";") == (
            "INSERT INTO users (id, name, email) VALUES (1, 'Alice; A', 'alice@example.com')"
        )
    finally:
        wrapper.close()


def test_cursor_proxies_execute_and_fetch(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        cursor = wrapper.cursor()
        cursor.execute("INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com')")
        row = cursor.execute("SELECT name, email FROM users WHERE id = 1").fetchone()

        assert row == ("Alice", "alice@example.com")
        assert len(wrapper.get_log()) == 1
        assert wrapper.get_log()[0]["sql_text"] == "INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com')"
    finally:
        wrapper.close()


def test_cursor_execute_uses_replay_sql_preparation(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        cursor = wrapper.cursor()
        cursor.execute("UPDATE events SET token = random()")

        entry = wrapper.get_log()[0]
        assert entry["original_sql_text"] == "UPDATE events SET token = random()"
        assert entry["is_replay_safe"] == 0
    finally:
        wrapper.close()


def test_logs_original_and_replay_sql_for_current_time_expression(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute(
            "UPDATE events "
            "SET name = 'expired' "
            "WHERE created_at < datetime('now', '-7 days')"
        )

        entry = wrapper.get_log()[0]
        assert entry["original_sql_text"] == (
            "UPDATE events "
            "SET name = 'expired' "
            "WHERE created_at < datetime('now', '-7 days')"
        )
        assert "datetime('now', '-7 days')" not in entry["to_replay_sql_text"]
        assert "created_at <" in entry["to_replay_sql_text"]
        assert entry["is_replay_safe"] == 1
    finally:
        wrapper.close()


def test_update_or_clause_is_preserved_when_replay_sql_is_rewritten(
    tmp_path,
    wrapper_module,
):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute(
            "UPDATE OR IGNORE events "
            "SET name = 'expired' "
            "WHERE created_at < datetime('now', '-7 days')"
        )

        entry = wrapper.get_log()[0]
        assert entry["original_sql_text"] == (
            "UPDATE OR IGNORE events "
            "SET name = 'expired' "
            "WHERE created_at < datetime('now', '-7 days')"
        )
        assert "UPDATE OR IGNORE events" in entry["to_replay_sql_text"]
        assert "datetime('now', '-7 days')" not in entry["to_replay_sql_text"]
        assert entry["is_replay_safe"] == 1
    finally:
        wrapper.close()


def test_replace_into_is_rendered_as_insert_or_replace_when_replay_sql_is_rewritten(
    tmp_path,
    wrapper_module,
):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("REPLACE INTO random_events (id, name) VALUES (1, 'Alice')")

        entry = wrapper.get_log()[0]
        assert entry["original_sql_text"] == (
            "REPLACE INTO random_events (id, name) VALUES (1, 'Alice')"
        )
        assert entry["to_replay_sql_text"].startswith(
            "INSERT OR REPLACE INTO random_events"
        )
        assert "token" in entry["to_replay_sql_text"]
        assert "RANDOM()" not in entry["to_replay_sql_text"]
        assert entry["is_replay_safe"] == 1
    finally:
        wrapper.close()


def test_parameterized_select_is_allowed(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("INSERT INTO events (id, name, created_at) VALUES (1, 'Alice', '2026-01-01')")

        rows = wrapper.execute(
            "SELECT name FROM events WHERE id = ?",
            (1,),
        ).fetchall()

        assert rows == [("Alice",)]
        assert len(wrapper.get_log()) == 1
    finally:
        wrapper.close()


def test_parameterized_dml_is_rejected_before_execution(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("INSERT INTO events (id, name, created_at) VALUES (1, 'Alice', '2026-01-01')")
        with pytest.raises(sqlite3.ProgrammingError, match="bound parameters"):
            wrapper.execute(
                "UPDATE events SET created_at = datetime('now', ?) WHERE id = 1",
                ("-7 days",),
            )

        assert wrapper.execute(
            "SELECT created_at FROM events WHERE id = 1"
        ).fetchone()[0] == "2026-01-01"
        assert len(wrapper.get_log()) == 1
    finally:
        wrapper.close()


def test_parameterized_statement_that_needs_rewrite_is_rejected(
    tmp_path,
    wrapper_module,
):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("INSERT INTO events (id, name, created_at) VALUES (1, 'Alice', '2026-01-01')")
        with pytest.raises(sqlite3.ProgrammingError, match="bound parameters"):
            wrapper.execute(
                "UPDATE events SET name = ? WHERE created_at < datetime('now', '+1 day')",
                ("expired",),
            )

        assert wrapper.execute(
            "SELECT name FROM events WHERE id = 1"
        ).fetchone()[0] == "Alice"
        assert len(wrapper.get_log()) == 1
    finally:
        wrapper.close()


def test_insert_omitted_current_timestamp_default_is_materialized(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("INSERT INTO events (id, name) VALUES (1, 'Alice')")

        entry = wrapper.get_log()[0]
        assert entry["original_sql_text"] == "INSERT INTO events (id, name) VALUES (1, 'Alice')"
        assert "created_at" in entry["to_replay_sql_text"]
        assert "CURRENT_TIMESTAMP" not in entry["to_replay_sql_text"]
        assert entry["is_replay_safe"] == 1
    finally:
        wrapper.close()


def test_insert_omitted_parenthesized_random_default_is_materialized(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("INSERT INTO random_events (id, name) VALUES (1, 'Alice')")

        entry = wrapper.get_log()[0]
        assert entry["original_sql_text"] == "INSERT INTO random_events (id, name) VALUES (1, 'Alice')"
        assert "token" in entry["to_replay_sql_text"]
        assert "RANDOM()" not in entry["to_replay_sql_text"]
        assert entry["is_replay_safe"] == 1
    finally:
        wrapper.close()


def test_single_values_random_is_materialized(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("INSERT INTO events (id, name, token) VALUES (1, 'Alice', random())")

        entry = wrapper.get_log()[0]
        assert entry["original_sql_text"] == (
            "INSERT INTO events (id, name, token) VALUES (1, 'Alice', random())"
        )
        assert "RANDOM()" not in entry["to_replay_sql_text"]
        assert entry["is_replay_safe"] == 1
    finally:
        wrapper.close()


def test_executemany_rewrite_candidate_is_rejected(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        with pytest.raises(sqlite3.ProgrammingError, match="bound parameters"):
            wrapper.executemany(
                "INSERT INTO events (id, name, token) VALUES (?, ?, random())",
                [
                    (1, "Alice"),
                    (2, "Bob"),
                ],
            )

        assert wrapper.get_log() == []
        assert wrapper.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    finally:
        wrapper.close()


def test_row_level_random_update_is_marked_unsafe(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("UPDATE events SET token = random()")

        entry = wrapper.get_log()[0]
        assert entry["original_sql_text"] == "UPDATE events SET token = random()"
        assert entry["to_replay_sql_text"] == "UPDATE events SET token = random()"
        assert entry["is_replay_safe"] == 0
    finally:
        wrapper.close()


def test_failed_autocommit_statement_without_effect_is_not_logged(
    tmp_path,
    wrapper_module,
):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("INSERT INTO users (id, name) VALUES (1, 'Alice')")
        with pytest.raises(sqlite3.IntegrityError):
            wrapper.execute("INSERT INTO users (id, name) VALUES (1, 'Duplicate')")

        entries = wrapper.get_log()
        assert len(entries) == 1
        assert entries[0]["is_replay_safe"] == 1
    finally:
        wrapper.close()


def test_failed_autocommit_statement_with_partial_effect_is_logged_unsafe(
    tmp_path,
    wrapper_module,
):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, code INTEGER UNIQUE)"
        )
        wrapper.execute("INSERT INTO items (id, code) VALUES (1, 1)")
        wrapper.execute("INSERT INTO items (id, code) VALUES (2, 2)")
        initial_log_count = len(wrapper.get_log())

        with pytest.raises(sqlite3.IntegrityError):
            wrapper.execute("UPDATE OR FAIL items SET code = 99")

        entries = wrapper.get_log()
        assert len(entries) == initial_log_count + 1
        assert entries[-1]["original_sql_text"] == (
            "UPDATE OR FAIL items SET code = 99"
        )
        assert entries[-1]["is_replay_safe"] == 0
        assert "execution error" in entries[-1]["replay_block_reason"]
        assert wrapper.execute(
            "SELECT id, code FROM items ORDER BY id"
        ).fetchall() == [(1, 99), (2, 2)]
    finally:
        wrapper.close()


def test_transaction_statement_error_marks_committed_statements_unsafe(
    tmp_path,
    wrapper_module,
):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("BEGIN")
        wrapper.execute("INSERT INTO users (id, name) VALUES (1, 'Alice')")
        with pytest.raises(sqlite3.IntegrityError):
            wrapper.execute("INSERT INTO users (id, name) VALUES (1, 'Duplicate')")
        wrapper.commit()

        entries = wrapper.get_log()
        assert len(entries) == 1
        assert entries[0]["is_replay_safe"] == 0
        assert "execution error" in entries[0]["replay_block_reason"]
    finally:
        wrapper.close()


def test_transaction_statement_error_with_partial_effect_logs_failed_statement(
    tmp_path,
    wrapper_module,
):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, code INTEGER UNIQUE)"
        )
        wrapper.execute("INSERT INTO items (id, code) VALUES (1, 1)")
        wrapper.execute("INSERT INTO items (id, code) VALUES (2, 2)")
        initial_log_count = len(wrapper.get_log())

        wrapper.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            wrapper.execute("UPDATE OR FAIL items SET code = 77")
        wrapper.commit()

        entries = wrapper.get_log()
        new_entries = entries[initial_log_count:]
        assert len(new_entries) == 1
        assert new_entries[0]["original_sql_text"] == (
            "UPDATE OR FAIL items SET code = 77"
        )
        assert new_entries[0]["is_replay_safe"] == 0
        assert "execution error" in new_entries[0]["replay_block_reason"]
        assert wrapper.execute(
            "SELECT id, code FROM items ORDER BY id"
        ).fetchall() == [(1, 77), (2, 2)]
    finally:
        wrapper.close()


def test_transaction_error_that_rolls_back_discards_buffer(
    tmp_path,
    wrapper_module,
):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, code INTEGER UNIQUE)"
        )
        wrapper.execute("INSERT INTO items (id, code) VALUES (1, 1)")
        wrapper.execute("INSERT INTO items (id, code) VALUES (2, 2)")
        initial_log_count = len(wrapper.get_log())

        wrapper.execute("BEGIN")
        wrapper.execute("INSERT INTO items (id, code) VALUES (3, 3)")
        with pytest.raises(sqlite3.IntegrityError):
            wrapper.execute("UPDATE OR ROLLBACK items SET code = 99")

        assert wrapper._conn.in_transaction is False
        assert wrapper._in_transaction is False
        assert wrapper._buffer == []
        assert wrapper.execute(
            "SELECT id, code FROM items ORDER BY id"
        ).fetchall() == [(1, 1), (2, 2)]
        assert len(wrapper.get_log()) == initial_log_count

        wrapper.execute("INSERT INTO items (id, code) VALUES (4, 4)")
        assert len(wrapper.get_log()) == initial_log_count + 1
    finally:
        wrapper.close()
