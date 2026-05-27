import importlib.util
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def wrapper_module():
    wrapper_path = Path(__file__).resolve().parents[2] / "src" / "sqlite-wrapper" / "wrapper.py"
    spec = importlib.util.spec_from_file_location("sqlite_wrapper_module", wrapper_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_executemany_logs_each_execution(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.executemany(
            "INSERT INTO users (id, name, email) VALUES (?, ?, ?)",
            [
                (1, "Alice", "alice@example.com"),
                (2, "Bob", "bob@example.com"),
            ],
        )

        log_entries = wrapper.get_log()
        assert len(log_entries) == 2
        assert [entry["sql_text"].rstrip(";") for entry in log_entries] == [
            "INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com')",
            "INSERT INTO users (id, name, email) VALUES (2, 'Bob', 'bob@example.com')",
        ]
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


def test_parameterized_nondeterministic_expression_is_marked_unsafe(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("INSERT INTO events (id, name, created_at) VALUES (1, 'Alice', '2026-01-01')")
        wrapper.execute(
            "UPDATE events SET created_at = datetime('now', ?) WHERE id = 1",
            ("-7 days",),
        )

        entry = wrapper.get_log()[1]
        assert entry["original_sql_text"] == (
            "UPDATE events SET created_at = datetime('now', '-7 days') WHERE id = 1"
        )
        assert entry["to_replay_sql_text"] == entry["original_sql_text"]
        assert entry["is_replay_safe"] == 0
    finally:
        wrapper.close()


def test_parameterized_statement_that_needs_rewrite_logs_expanded_unsafe_sql(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("INSERT INTO events (id, name, created_at) VALUES (1, 'Alice', '2026-01-01')")
        wrapper.execute(
            "UPDATE events SET name = ? WHERE created_at < datetime('now', '+1 day')",
            ("expired",),
        )

        entry = wrapper.get_log()[1]
        assert "?" not in entry["original_sql_text"]
        assert "'expired'" in entry["original_sql_text"]
        assert "datetime('now', '+1 day')" in entry["original_sql_text"]
        assert entry["to_replay_sql_text"] == entry["original_sql_text"]
        assert entry["is_replay_safe"] == 0
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


def test_executemany_rewrite_candidate_is_marked_unsafe_per_execution(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.executemany(
            "INSERT INTO events (id, name, token) VALUES (?, ?, random())",
            [
                (1, "Alice"),
                (2, "Bob"),
            ],
        )

        entries = wrapper.get_log()
        assert len(entries) == 2
        assert [entry["is_replay_safe"] for entry in entries] == [0, 0]
        assert [entry["original_sql_text"] for entry in entries] == [
            "INSERT INTO events (id, name, token) VALUES (1, 'Alice', random())",
            "INSERT INTO events (id, name, token) VALUES (2, 'Bob', random())",
        ]
        assert [entry["to_replay_sql_text"] for entry in entries] == [
            entry["original_sql_text"] for entry in entries
        ]
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


def test_failed_autocommit_statement_is_not_logged_without_trace(tmp_path, wrapper_module):
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


def test_transaction_statement_error_marks_failed_statement_unsafe(tmp_path, wrapper_module):
    wrapper = make_wrapper(tmp_path, wrapper_module)
    try:
        wrapper.execute("BEGIN")
        wrapper.execute("INSERT INTO users (id, name) VALUES (1, 'Alice')")
        with pytest.raises(sqlite3.IntegrityError):
            wrapper.execute("INSERT INTO users (id, name) VALUES (1, 'Duplicate')")
        wrapper.commit()

        entries = wrapper.get_log()
        assert len(entries) == 2
        assert {entry["transaction_id"] for entry in entries} == {
            entries[0]["transaction_id"]
        }
        assert entries[0]["is_replay_safe"] == 0
        assert entries[1]["is_replay_safe"] == 0
        assert "execution error" in entries[0]["replay_block_reason"]
        assert "execution error" in entries[1]["replay_block_reason"]
    finally:
        wrapper.close()
