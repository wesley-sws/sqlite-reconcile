import json
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from merge import driver, log_merge


def init_logged_db(path):
    with closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        con.execute(
            f"""
            CREATE TABLE {log_merge.TX_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        con.execute(
            f"""
            CREATE TABLE {log_merge.LOG_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER NOT NULL REFERENCES {log_merge.TX_TABLE}(id),
                original_sql_text TEXT NOT NULL,
                to_replay_sql_text TEXT NOT NULL,
                is_replay_safe INTEGER NOT NULL DEFAULT 1,
                replay_block_reason TEXT
            )
            """
        )
        con.commit()


def init_logged_update_from_db(path):
    init_logged_db(path)
    with closing(sqlite3.connect(path)) as con:
        con.execute(
            "CREATE TABLE products ("
            "id INTEGER PRIMARY KEY, category_id INTEGER, discount INTEGER)"
        )
        con.execute("CREATE TABLE categories (id INTEGER, rate INTEGER)")
        con.execute("INSERT INTO products VALUES (1, 1, 0)")
        con.execute("INSERT INTO categories VALUES (1, 5)")
        con.execute("INSERT INTO categories VALUES (1, 7)")
        con.commit()


def append_log(path, sql_text, *, is_replay_safe=True, reason=None):
    with closing(sqlite3.connect(path)) as con:
        cursor = con.execute(f"INSERT INTO {log_merge.TX_TABLE} DEFAULT VALUES")
        con.execute(
            f"""
            INSERT INTO {log_merge.LOG_TABLE} (
                transaction_id,
                original_sql_text,
                to_replay_sql_text,
                is_replay_safe,
                replay_block_reason
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                cursor.lastrowid,
                sql_text,
                sql_text,
                int(is_replay_safe),
                reason,
            ),
        )
        con.commit()


def test_driver_writes_session_for_acknowledgeable_replay_warning(tmp_path):
    base = tmp_path / "base.db"
    ours = tmp_path / "ours.db"
    theirs = tmp_path / "theirs.db"
    merged = tmp_path / "merged.db"
    session = tmp_path / "merge-session.json"
    init_logged_db(base)
    shutil.copy2(base, ours)
    shutil.copy2(base, theirs)

    append_log(
        ours,
        "UPDATE users SET name = random() WHERE id = 1",
        is_replay_safe=False,
        reason="nondeterministic expression cannot be safely materialized",
    )
    append_log(theirs, "INSERT INTO users(id, name) VALUES (2, 'remote')")

    outcome = driver.merge_databases(
        base,
        ours,
        theirs,
        session_path=session,
        merged_db_path=merged,
    )

    assert outcome.plan.status == "conflict"
    assert outcome.plan.selected.name == "branch_replay"
    assert outcome.report_path == str(session)
    payload = json.loads(session.read_text(encoding="utf-8"))
    assert payload["status"] == "conflict"
    statement = payload["ours_transactions"][0]["statements"][0]
    assert statement["is_replay_safe"] is True
    assert statement["replay_warnings"] == [
        "nondeterministic expression cannot be safely materialized"
    ]


def test_driver_keeps_hard_unsafe_replay_as_blocking(tmp_path):
    base = tmp_path / "base.db"
    ours = tmp_path / "ours.db"
    theirs = tmp_path / "theirs.db"
    merged = tmp_path / "merged.db"
    session = tmp_path / "merge-session.json"
    init_logged_db(base)
    shutil.copy2(base, ours)
    shutil.copy2(base, theirs)

    append_log(
        ours,
        "NOT VALID SQL",
        is_replay_safe=False,
        reason="statement could not be parsed for replay preparation",
    )

    outcome = driver.merge_databases(
        base,
        ours,
        theirs,
        session_path=session,
        merged_db_path=merged,
    )

    assert outcome.plan.status == "conflict"
    assert outcome.plan.selected.name == "branch_replay"
    payload = json.loads(session.read_text(encoding="utf-8"))
    statement = payload["ours_transactions"][0]["statements"][0]
    assert statement["is_replay_safe"] is False


def test_driver_writes_session_for_update_from_warning(tmp_path):
    base = tmp_path / "base.db"
    ours = tmp_path / "ours.db"
    theirs = tmp_path / "theirs.db"
    merged = tmp_path / "merged.db"
    session = tmp_path / "merge-session.json"
    init_logged_update_from_db(base)
    shutil.copy2(base, ours)
    shutil.copy2(base, theirs)

    append_log(
        ours,
        "UPDATE products "
        "SET discount = categories.rate "
        "FROM categories "
        "WHERE products.category_id = categories.id",
    )

    outcome = driver.merge_databases(
        base,
        ours,
        theirs,
        session_path=session,
        merged_db_path=merged,
    )

    assert outcome.plan.status == "conflict"
    assert outcome.plan.selected.name == "branch_replay"
    payload = json.loads(session.read_text(encoding="utf-8"))
    statement = payload["ours_transactions"][0]["statements"][0]
    assert statement["is_replay_safe"] is True
    assert statement["replay_warnings"] == [
        "UPDATE FROM has multiple source rows for the same target row"
    ]
