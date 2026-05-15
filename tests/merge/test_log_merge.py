import shutil
import sqlite3
import sys
from pathlib import Path
from contextlib import closing

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from merge import log_merge


def make_statement(branch, index):
    return log_merge.LoggedStatement(
        branch=branch,
        branch_index=index,
        log_id=index + 1,
        transaction_id=index + 1,
        committed_at="2026-01-01T00:00:00",
        sql_text=f"{branch.upper()}{index + 1}",
    )


def make_detector(conflicting_pairs):
    def detector(ours_statement, theirs_statement):
        return (ours_statement.sql_text, theirs_statement.sql_text) in conflicting_pairs

    return detector


def init_logged_db(path):
    with closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        con.execute(
            f"""
            CREATE TABLE {log_merge.TX_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                committed_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            f"""
            CREATE TABLE {log_merge.LOG_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER NOT NULL REFERENCES {log_merge.TX_TABLE}(id),
                sql_text TEXT NOT NULL
            )
            """
        )
        con.commit()


def append_log(path, sql_text, committed_at="2026-01-01T00:00:00"):
    with sqlite3.connect(path) as con:
        cursor = con.execute(
            f"INSERT INTO {log_merge.TX_TABLE} (committed_at) VALUES (?)",
            (committed_at,),
        )
        con.execute(
            f"INSERT INTO {log_merge.LOG_TABLE} (transaction_id, sql_text) VALUES (?, ?)",
            (cursor.lastrowid, sql_text),
        )
        con.commit()


def init_unlogged_db(path):
    with closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        con.commit()


def test_base_without_log_tables_is_not_applicable(tmp_path):
    base = tmp_path / "base.db"
    init_unlogged_db(base)

    with closing(sqlite3.connect(base)) as con:
        cursor = con.cursor()
        with pytest.raises(log_merge.MergeNotApplicableError) as exc_info:
            log_merge.get_base_watermark(cursor, base)

    assert exc_info.value.role == "base"
    assert exc_info.value.missing_tables == [
        log_merge.TX_TABLE,
        log_merge.LOG_TABLE,
    ]


def test_branch_without_log_tables_is_not_applicable(tmp_path):
    ours = tmp_path / "ours.db"
    init_unlogged_db(ours)

    with closing(sqlite3.connect(ours)) as con:
        cursor = con.cursor()
        with pytest.raises(log_merge.MergeNotApplicableError) as exc_info:
            log_merge.load_logged_statements(cursor, "ours", 0, ours)

    assert exc_info.value.role == "ours"
    assert exc_info.value.missing_tables == [
        log_merge.TX_TABLE,
        log_merge.LOG_TABLE,
    ]


def test_load_logged_statements_uses_base_transaction_watermark(tmp_path):
    base = tmp_path / "base.db"
    ours = tmp_path / "ours.db"
    init_logged_db(base)
    append_log(base, "INSERT INTO users (id, name) VALUES (1, 'Alice')")
    shutil.copy2(base, ours)

    append_log(ours, "INSERT INTO users (id, name) VALUES (2, 'Bob')")

    with closing(sqlite3.connect(base)) as base_con, \
         closing(sqlite3.connect(ours)) as ours_con:
        base_con.row_factory = sqlite3.Row
        ours_con.row_factory = sqlite3.Row
        watermark = log_merge.get_base_watermark(base_con.cursor(), base)
        statements = log_merge.load_logged_statements(
            ours_con.cursor(),
            "ours",
            watermark,
            ours,
        )

    assert watermark == 1
    assert len(statements) == 1
    assert statements[0].branch == "ours"
    assert statements[0].branch_index == 0
    assert statements[0].sql_text == "INSERT INTO users (id, name) VALUES (2, 'Bob')"


def test_backtracking_ours_keeps_backtracking_after_later_conflict():
    ours = [make_statement("ours", index) for index in range(3)]
    theirs = [make_statement("theirs", index) for index in range(4)]
    detector = make_detector({
        ("OURS3", "THEIRS3"),
        ("OURS2", "THEIRS4"),
    })

    first = log_merge.find_first_pairwise_conflict(ours, theirs, detector)
    candidate = log_merge.search_by_backtracking_ours(
        ours,
        theirs,
        first.ours_index,
        detector,
    )

    assert first == log_merge.ConflictPair(2, 2, "OURS3", "THEIRS3")
    assert candidate.ours_count == 1
    assert candidate.theirs_count == 4
    assert candidate.next_conflict is None


def test_frontier_choice_uses_highest_total_statement_count():
    ours = [make_statement("ours", index) for index in range(5)]
    theirs = [make_statement("theirs", index) for index in range(4)]
    detector = make_detector({
        ("OURS3", "THEIRS3"),
        ("OURS2", "THEIRS4"),
        ("OURS1", "THEIRS4"),
        ("OURS5", "THEIRS2"),
        ("OURS5", "THEIRS1"),
    })

    first = log_merge.find_first_pairwise_conflict(ours, theirs, detector)
    candidates = [
        log_merge.search_by_backtracking_ours(ours, theirs, first.ours_index, detector),
        log_merge.search_by_backtracking_theirs(ours, theirs, first.ours_index, detector),
    ]
    selected = log_merge.choose_frontier(candidates)

    assert selected.name == "backtrack_theirs"
    assert selected.ours_count == 4
    assert selected.theirs_count == 1
    assert selected.score == 5


def test_replay_statement_plan_applies_sql_and_appends_merge_log(tmp_path):
    base = tmp_path / "base.db"
    output = tmp_path / "merged.db"
    init_logged_db(base)
    append_log(base, "INSERT INTO users (id, name) VALUES (1, 'Alice')")

    statement = log_merge.LoggedStatement(
        branch="ours",
        branch_index=0,
        log_id=2,
        transaction_id=2,
        committed_at="2026-01-02T00:00:00",
        sql_text="INSERT INTO users (id, name) VALUES (2, 'Bob')",
    )

    result = log_merge.replay_statement_plan(base, output, [statement])

    assert result.ok
    with closing(sqlite3.connect(output)) as con:
        names = con.execute("SELECT name FROM users ORDER BY id").fetchall()
        log_rows = con.execute(
            f"SELECT sql_text FROM {log_merge.LOG_TABLE} ORDER BY id"
        ).fetchall()

    assert names == [("Bob",)]
    assert log_rows == [
        ("INSERT INTO users (id, name) VALUES (1, 'Alice')",),
        ("INSERT INTO users (id, name) VALUES (2, 'Bob')",),
    ]
