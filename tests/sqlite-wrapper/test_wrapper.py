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