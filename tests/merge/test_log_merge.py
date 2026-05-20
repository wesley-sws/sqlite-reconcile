import json
import shutil
import sqlite3
import sys
from pathlib import Path
from contextlib import closing

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from merge import log_merge


def make_statement(branch, index):
    sql_text = f"{branch.upper()}{index + 1}"
    return log_merge.make_logged_statement(
        branch=branch,
        branch_index=index,
        log_id=index + 1,
        transaction_id=index + 1,
        committed_at="2026-01-01T00:00:00",
        sql_text=sql_text,
    )


def make_detector(conflicting_pairs):
    def detector(context, ours_statement, theirs_statement):
        if (ours_statement.sql_text, theirs_statement.sql_text) in conflicting_pairs:
            return log_merge.ConflictCheckResult((
                log_merge.StatementConflict(
                    kind="write_write",
                    message="test conflict",
                ),
            ))
        return log_merge.ConflictCheckResult()

    return detector


@pytest.fixture
def conflict_context():
    with closing(sqlite3.connect(":memory:")) as con:
        yield log_merge.ConflictCheckContext(
            base_cursor=con.cursor(),
            base_db_path=":memory:",
            table_columns={},
        )


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
                original_sql_text TEXT NOT NULL,
                to_replay_sql_text TEXT NOT NULL,
                is_replay_safe INTEGER NOT NULL DEFAULT 1,
                replay_block_reason TEXT
            )
            """
        )
        con.commit()


def append_log(
    path,
    sql_text,
    committed_at="2026-01-01T00:00:00",
    original_sql_text=None,
    is_replay_safe=True,
    replay_block_reason=None,
):
    with sqlite3.connect(path) as con:
        cursor = con.execute(
            f"INSERT INTO {log_merge.TX_TABLE} (committed_at) VALUES (?)",
            (committed_at,),
        )
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
                original_sql_text or sql_text,
                sql_text,
                int(is_replay_safe),
                replay_block_reason,
            ),
        )
        con.commit()


def init_unlogged_db(path):
    with closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        con.commit()


def create_log_tables(con):
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
            original_sql_text TEXT NOT NULL,
            to_replay_sql_text TEXT NOT NULL,
            is_replay_safe INTEGER NOT NULL DEFAULT 1,
            replay_block_reason TEXT
        )
        """
    )


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


def test_load_table_columns_skips_only_internal_log_tables():
    with closing(sqlite3.connect(":memory:")) as con:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        con.execute(
            f"""
            CREATE TABLE {log_merge.TX_TABLE} (
                id INTEGER PRIMARY KEY,
                committed_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            f"""
            CREATE TABLE {log_merge.LOG_TABLE} (
                id INTEGER PRIMARY KEY,
                transaction_id INTEGER NOT NULL,
                original_sql_text TEXT NOT NULL,
                to_replay_sql_text TEXT NOT NULL,
                is_replay_safe INTEGER NOT NULL DEFAULT 1,
                replay_block_reason TEXT
            )
            """
        )
        con.execute(
            "CREATE TABLE _sqlite_merge_notes (id INTEGER PRIMARY KEY, body TEXT)"
        )

        table_columns = log_merge.load_table_columns(con.cursor())

    assert table_columns == {
        "users": {"id", "name"},
        "_sqlite_merge_notes": {"id", "body"},
    }


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
    assert statements[0].original_sql_text == (
        "INSERT INTO users (id, name) VALUES (2, 'Bob')"
    )
    assert statements[0].is_replay_safe
    assert statements[0].metadata.parsed_sql_text.sql(dialect="sqlite") == (
        "INSERT INTO users (id, name) VALUES (2, 'Bob')"
    )
    assert statements[0].metadata.table_updated == "users"
    assert statements[0].metadata.columns_updated == {log_merge.ALL_COLUMNS}
    assert statements[0].metadata.tables_referenced_to_columns_referenced == {}


def test_load_logged_statements_uses_replay_sql_for_metadata(tmp_path):
    base = tmp_path / "base.db"
    ours = tmp_path / "ours.db"
    init_logged_db(base)
    shutil.copy2(base, ours)

    append_log(
        ours,
        "UPDATE users SET name = 'old-literal' WHERE id = 1",
        original_sql_text="UPDATE users SET name = datetime('now') WHERE id = 1",
    )

    with closing(sqlite3.connect(ours)) as con:
        con.row_factory = sqlite3.Row
        statements = log_merge.load_logged_statements(con.cursor(), "ours", 0, ours)

    assert statements[0].original_sql_text == (
        "UPDATE users SET name = datetime('now') WHERE id = 1"
    )
    assert statements[0].sql_text == "UPDATE users SET name = 'old-literal' WHERE id = 1"
    assert statements[0].metadata.parsed_sql_text.sql(dialect="sqlite") == (
        "UPDATE users SET name = 'old-literal' WHERE id = 1"
    )


def test_backtracking_ours_keeps_backtracking_after_later_conflict(conflict_context):
    ours = [make_statement("ours", index) for index in range(3)]
    theirs = [make_statement("theirs", index) for index in range(4)]
    detector = make_detector({
        ("OURS3", "THEIRS3"),
        ("OURS2", "THEIRS4"),
    })

    first = log_merge.find_first_pairwise_conflict(
        ours,
        theirs,
        conflict_context,
        detector,
    )
    candidate = log_merge.search_by_backtracking_ours(
        ours,
        theirs,
        first.ours_index,
        conflict_context,
        detector,
    )

    assert first.ours_index == 2
    assert first.theirs_index == 2
    assert first.ours_sql == "OURS3"
    assert first.theirs_sql == "THEIRS3"
    assert first.conflicts == (
        log_merge.StatementConflict(kind="write_write", message="test conflict"),
    )
    assert candidate.ours_count == 1
    assert candidate.theirs_count == 4
    assert candidate.next_conflict is None


def test_frontier_choice_uses_highest_total_statement_count(conflict_context):
    ours = [make_statement("ours", index) for index in range(5)]
    theirs = [make_statement("theirs", index) for index in range(4)]
    detector = make_detector({
        ("OURS3", "THEIRS3"),
        ("OURS2", "THEIRS4"),
        ("OURS1", "THEIRS4"),
        ("OURS5", "THEIRS2"),
        ("OURS5", "THEIRS1"),
    })

    first = log_merge.find_first_pairwise_conflict(
        ours,
        theirs,
        conflict_context,
        detector,
    )
    candidates = [
        log_merge.search_by_backtracking_ours(
            ours,
            theirs,
            first.ours_index,
            conflict_context,
            detector,
        ),
        log_merge.search_by_backtracking_theirs(
            ours,
            theirs,
            first.ours_index,
            conflict_context,
            detector,
        ),
    ]
    selected = log_merge.choose_frontier(candidates)

    assert selected.name == "backtrack_theirs"
    assert selected.ours_count == 4
    assert selected.theirs_count == 0
    assert selected.score == 4


def test_replay_statement_plan_applies_sql_and_appends_merge_log(tmp_path):
    base = tmp_path / "base.db"
    output = tmp_path / "merged.db"
    init_logged_db(base)
    append_log(base, "INSERT INTO users (id, name) VALUES (1, 'Alice')")

    sql_text = "INSERT INTO users (id, name) VALUES (2, 'Bob')"
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=2,
        transaction_id=2,
        committed_at="2026-01-02T00:00:00",
        sql_text=sql_text,
    )

    result = log_merge.replay_statement_plan(base, output, [statement])

    assert result.ok
    with closing(sqlite3.connect(output)) as con:
        names = con.execute("SELECT name FROM users ORDER BY id").fetchall()
        log_rows = con.execute(
            f"SELECT to_replay_sql_text FROM {log_merge.LOG_TABLE} ORDER BY id"
        ).fetchall()

    assert names == [("Bob",)]
    assert log_rows == [
        ("INSERT INTO users (id, name) VALUES (1, 'Alice')",),
        ("INSERT INTO users (id, name) VALUES (2, 'Bob')",),
    ]


def test_replay_statement_plan_reports_deferred_foreign_key_failure_in_loop(tmp_path):
    base = tmp_path / "base.db"
    output = tmp_path / "merged.db"
    with closing(sqlite3.connect(base)) as con:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
        con.execute(
            """
            CREATE TABLE children (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER NOT NULL,
                FOREIGN KEY(parent_id) REFERENCES parents(id)
                    DEFERRABLE INITIALLY DEFERRED
            )
            """
        )
        create_log_tables(con)
        con.commit()

    valid_sql_text = "INSERT INTO parents (id) VALUES (1)"
    valid_statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-02T00:00:00",
        sql_text=valid_sql_text,
    )
    invalid_sql_text = "INSERT INTO children (id, parent_id) VALUES (1, 99)"
    invalid_statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=1,
        log_id=2,
        transaction_id=2,
        committed_at="2026-01-03T00:00:00",
        sql_text=invalid_sql_text,
    )

    result = log_merge.replay_statement_plan(
        base,
        output,
        [valid_statement, invalid_statement],
    )

    assert not result.ok
    assert result.applied_count == 1
    assert result.failure is not None
    assert result.failure.statement is not None
    assert result.failure.statement["to_replay_sql_text"] == invalid_sql_text
    assert result.integrity_errors is not None
    assert any("foreign_key_check" in error for error in result.integrity_errors)

    with closing(sqlite3.connect(output)) as con:
        parent_rows = con.execute("SELECT * FROM parents").fetchall()
        child_rows = con.execute("SELECT * FROM children").fetchall()
        log_rows = con.execute(
            f"SELECT to_replay_sql_text FROM {log_merge.LOG_TABLE} ORDER BY id"
        ).fetchall()

    assert parent_rows == [(1,)]
    assert child_rows == []
    assert log_rows == [(valid_sql_text,)]


def test_replay_statement_plan_blocks_unsafe_replay_statement(tmp_path):
    base = tmp_path / "base.db"
    output = tmp_path / "merged.db"
    init_logged_db(base)

    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-02T00:00:00",
        sql_text="UPDATE users SET name = random()",
        is_replay_safe=False,
    )

    result = log_merge.replay_statement_plan(base, output, [statement])

    assert not result.ok
    assert result.applied_count == 0
    assert result.failure is not None
    assert "unsafe for automatic replay" in result.failure.error


def test_conflict_report_serializes_logged_statement_metadata(tmp_path):
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text="INSERT INTO user_archive SELECT * FROM users",
    )
    frontier = log_merge.FrontierCandidate(
        name="clean",
        ours_count=1,
        theirs_count=0,
        next_conflict=None,
    )
    plan = log_merge.MergePlan(
        status="clean",
        base_transaction_id=0,
        ours=[statement],
        theirs=[],
        selected=frontier,
        statement_plan=[statement],
    )
    replay = log_merge.ReplayResult(
        ok=True,
        output_path=str(tmp_path / "merged.db"),
        applied_count=1,
    )
    report_path = tmp_path / "report.json"

    log_merge.write_conflict_report(report_path, plan, replay)

    payload = json.loads(report_path.read_text())
    assert payload["statement_plan"][0]["metadata"]["parsed_sql_text"] == (
        "INSERT INTO user_archive SELECT * FROM users"
    )
    metadata = payload["statement_plan"][0]["metadata"]
    assert metadata["tables_referenced_to_columns_referenced"] == {
        "users": [log_merge.ALL_COLUMNS],
    }
