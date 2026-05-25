import json
import shutil
import sqlite3
import sys
from pathlib import Path
from contextlib import closing

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from merge import log_merge
from merge import session as merge_session


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


def noop_applier(context, statements):
    return None


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


def append_log(
    path,
    sql_text,
    original_sql_text=None,
    is_replay_safe=True,
    replay_block_reason=None,
):
    with sqlite3.connect(path) as con:
        cursor = con.execute(
            f"INSERT INTO {log_merge.TX_TABLE} DEFAULT VALUES",
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
                committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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


def test_make_logged_statement_marks_unparseable_sql_unsafe():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text="NOT VALID SQL @@@",
    )

    assert not statement.is_replay_safe
    assert log_merge.METADATA_PARSE_ERROR_REASON in statement.replay_block_reason
    assert statement.metadata.table_updated is None


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


def test_load_logged_statements_tolerates_unparseable_unsafe_sql(tmp_path):
    db_path = tmp_path / "branch.db"
    init_logged_db(db_path)
    append_log(
        db_path,
        "NOT VALID SQL @@@",
        is_replay_safe=False,
        replay_block_reason="statement could not be parsed for replay preparation",
    )

    with closing(sqlite3.connect(db_path)) as con:
        con.row_factory = sqlite3.Row
        statements = log_merge.load_logged_statements(con.cursor(), "ours", 0, db_path)

    assert len(statements) == 1
    assert not statements[0].is_replay_safe
    assert statements[0].metadata.table_updated is None


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


def test_pairwise_detection_uses_state_from_previous_clean_pairs(tmp_path):
    db_path = tmp_path / "base.db"
    with closing(sqlite3.connect(db_path)) as con:
        con.execute(
            "CREATE TABLE products (id INTEGER PRIMARY KEY, discount INTEGER)"
        )
        con.commit()

    table_columns = {"products": {"id", "discount"}}
    with closing(sqlite3.connect(db_path)) as con:
        context = log_merge.ConflictCheckContext(
            base_cursor=con.cursor(),
            base_db_path=db_path,
            table_columns=table_columns,
            primary_key_columns={"products": ("id",)},
            key_column_sets={"products": ({"id"},)},
        )
        ours = [
            log_merge.make_logged_statement(
                branch="ours",
                branch_index=0,
                log_id=1,
                transaction_id=1,
                committed_at="2026-01-01T00:00:00",
                sql_text="INSERT INTO products(id, discount) VALUES (1, 0)",
                table_columns=table_columns,
            ),
            log_merge.make_logged_statement(
                branch="ours",
                branch_index=1,
                log_id=2,
                transaction_id=2,
                committed_at="2026-01-01T00:00:00",
                sql_text="UPDATE products SET discount = 10 WHERE id = 1",
                table_columns=table_columns,
            ),
        ]
        theirs = [
            log_merge.make_logged_statement(
                branch="theirs",
                branch_index=0,
                log_id=3,
                transaction_id=3,
                committed_at="2026-01-01T00:00:00",
                sql_text="UPDATE products SET discount = 0 WHERE id = 2",
                table_columns=table_columns,
            ),
            log_merge.make_logged_statement(
                branch="theirs",
                branch_index=1,
                log_id=4,
                transaction_id=4,
                committed_at="2026-01-01T00:00:00",
                sql_text="UPDATE products SET discount = 9 WHERE id = 1",
                table_columns=table_columns,
            ),
        ]

        first = log_merge.find_first_pairwise_conflict(ours, theirs, context)

    assert first is not None
    assert first.ours_index == 1
    assert first.theirs_index == 1
    assert [conflict.kind for conflict in first.conflicts] == ["write_write"]


def test_ordered_statement_plan_interleaves_branch_prefixes():
    ours = [make_statement("ours", index) for index in range(2)]
    theirs = [make_statement("theirs", index) for index in range(3)]
    frontier = log_merge.FrontierCandidate(
        name="test",
        ours_count=2,
        theirs_count=3,
        next_conflict=None,
    )

    plan = log_merge.ordered_statement_plan(ours, theirs, frontier)

    assert [statement.sql_text for statement in plan] == [
        "OURS1",
        "THEIRS1",
        "OURS2",
        "THEIRS2",
        "THEIRS3",
    ]


def test_build_merge_plan_reports_replay_failure_in_unpaired_tail(tmp_path):
    db_path = tmp_path / "base.db"
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("CREATE TABLE coupons (id INTEGER PRIMARY KEY, code TEXT UNIQUE)")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )
    ours = [
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            log_id=1,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO coupons (id, code) VALUES (1, 'shared')",
            table_columns=table_columns,
        )
    ]
    theirs = [
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            log_id=2,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO coupons (id, code) VALUES (2, 'remote-only')",
            table_columns=table_columns,
        ),
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=1,
            log_id=3,
            transaction_id=3,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO coupons (id, code) VALUES (3, 'shared')",
            table_columns=table_columns,
        ),
    ]

    with closing(sqlite3.connect(db_path)) as con:
        con.row_factory = sqlite3.Row
        plan = log_merge.build_merge_plan_from_connection(
            con,
            str(db_path),
            0,
            ours,
            theirs,
            table_columns,
            primary_key_columns,
            key_column_sets,
        )

    assert plan.status == "conflict"
    assert plan.selected.name == "standalone_replay"
    assert plan.selected.scope == "theirs"
    assert plan.selected.ours_count == 1
    assert plan.selected.theirs_count == 1
    assert [statement.sql_text for statement in plan.statement_plan] == [
        "INSERT INTO coupons (id, code) VALUES (1, 'shared')",
        "INSERT INTO coupons (id, code) VALUES (2, 'remote-only')",
    ]
    assert plan.selected.next_conflict is not None
    assert plan.selected.next_conflict.theirs_index == 1
    assert plan.selected.next_conflict.conflicts[0].kind == "integrity"


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
        statement_applier=noop_applier,
    )
    candidate = log_merge.search_by_backtracking_ours(
        ours,
        theirs,
        first,
        conflict_context,
        detector,
        noop_applier,
    )

    assert first.ours_index == 2
    assert first.theirs_index == 2
    assert first.ours_sql == "OURS3"
    assert first.theirs_sql == "THEIRS3"
    assert first.conflicts == (
        log_merge.StatementConflict(kind="write_write", message="test conflict"),
    )
    assert candidate.ours_count == 2
    assert candidate.theirs_count == 2
    assert candidate.next_conflict == first


def test_backtracking_checks_candidates_in_rolled_back_prefix_state(tmp_path):
    db_path = tmp_path / "base.db"
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")
        con.commit()

    table_columns = {"events": {"id"}}
    ours = [
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=index,
            log_id=index + 1,
            transaction_id=index + 1,
            committed_at="2026-01-01T00:00:00",
            sql_text=f"INSERT INTO events(id) VALUES ({index + 1})",
            table_columns=table_columns,
        )
        for index in range(3)
    ]
    theirs = [
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=index,
            log_id=index + 4,
            transaction_id=index + 4,
            committed_at="2026-01-01T00:00:00",
            sql_text=f"INSERT INTO events(id) VALUES ({(index + 1) * 10})",
            table_columns=table_columns,
        )
        for index in range(4)
    ]
    observations = {}

    def detector(context, ours_statement, theirs_statement):
        pair = (ours_statement.sql_text, theirs_statement.sql_text)
        observations[pair] = {
            row[0]
            for row in context.base_cursor.execute("SELECT id FROM events")
        }
        if pair == (ours[1].sql_text, theirs[3].sql_text):
            return log_merge.ConflictCheckResult((
                log_merge.StatementConflict(
                    kind="write_write",
                    message="test conflict",
                ),
            ))
        return log_merge.ConflictCheckResult()

    with closing(sqlite3.connect(db_path)) as con:
        context = log_merge.ConflictCheckContext(
            base_cursor=con.cursor(),
            base_db_path=db_path,
            table_columns=table_columns,
        )
        candidate = log_merge.search_by_backtracking_ours(
            ours,
            theirs,
            initial_conflict=log_merge.ConflictPair(
                ours_index=2,
                theirs_index=2,
                ours_sql=ours[2].sql_text,
                theirs_sql=theirs[2].sql_text,
                conflicts=(
                    log_merge.StatementConflict(
                        kind="write_write",
                        message="test conflict",
                    ),
                ),
            ),
            context=context,
            conflict_detector=detector,
        )
        rows_after = con.execute("SELECT id FROM events").fetchall()

    assert observations[(ours[1].sql_text, theirs[2].sql_text)] == {1, 10, 20}
    assert observations[(ours[1].sql_text, theirs[3].sql_text)] == {1, 10, 20, 30}
    assert rows_after == []
    assert candidate.ours_count == 2
    assert candidate.theirs_count == 2
    assert candidate.next_conflict is not None


def test_standalone_integrity_backtracks_to_earlier_retained_cause(tmp_path):
    db_path = tmp_path / "base.db"
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT)")
        con.commit()

    table_columns = {"products": {"id", "name"}}
    ours = [
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            log_id=1,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO products(id, name) VALUES (1, 'ours earlier')",
            table_columns=table_columns,
        ),
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=1,
            log_id=2,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO products(id, name) VALUES (2, 'ours later')",
            table_columns=table_columns,
        ),
    ]
    theirs = [
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            log_id=3,
            transaction_id=3,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO products(id, name) VALUES (10, 'theirs earlier')",
            table_columns=table_columns,
        ),
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=1,
            log_id=4,
            transaction_id=4,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO products(id, name) VALUES (1, 'theirs later')",
            table_columns=table_columns,
        ),
    ]

    context_kwargs = dict(
        table_columns=table_columns,
        primary_key_columns={"products": ("id",)},
        key_column_sets={"products": ({"id"},)},
    )
    with closing(sqlite3.connect(db_path)) as con:
        first_context = log_merge.ConflictCheckContext(
            base_cursor=con.cursor(),
            base_db_path=db_path,
            **context_kwargs,
        )
        first = log_merge.find_first_pairwise_conflict(
            ours,
            theirs,
            first_context,
        )

    with closing(sqlite3.connect(db_path)) as con:
        backtrack_context = log_merge.ConflictCheckContext(
            base_cursor=con.cursor(),
            base_db_path=db_path,
            **context_kwargs,
        )
        candidates = log_merge.frontier_candidates_for_conflict(
            ours,
            theirs,
            first,
            backtrack_context,
        )

    assert first is not None
    assert first.ours_index == 1
    assert first.theirs_index == 1
    assert first.conflicts[0].kind == "integrity"
    assert first.conflicts[0].scope == "theirs"

    assert [candidate.name for candidate in candidates] == [
        "backtrack_ours",
        "standalone_replay",
    ]
    assert candidates[0].ours_count == 0
    assert candidates[0].theirs_count == 1
    assert candidates[0].next_conflict is not None
    assert candidates[0].next_conflict.ours_index == 0
    assert candidates[0].next_conflict.theirs_index == 1
    assert candidates[0].next_conflict.conflicts[0].scope == "pair"
    assert candidates[1].scope == "theirs"


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
        statement_applier=noop_applier,
    )
    candidates = [
        log_merge.search_by_backtracking_ours(
            ours,
            theirs,
            first,
            conflict_context,
            detector,
            noop_applier,
        ),
        log_merge.search_by_backtracking_theirs(
            ours,
            theirs,
            first,
            conflict_context,
            detector,
            noop_applier,
        ),
    ]
    selected = log_merge.choose_frontier(candidates)

    assert selected.name == "backtrack_theirs"
    assert selected.ours_count == 4
    assert selected.theirs_count == 1
    assert selected.score == 5


def test_backtracking_keeps_larger_pairwise_candidate_when_smaller_prefix_is_standalone(
    conflict_context,
):
    ours = [make_statement("ours", index) for index in range(3)]
    theirs = [make_statement("theirs", index) for index in range(3)]

    def detector(context, ours_statement, theirs_statement):
        pair = (ours_statement.sql_text, theirs_statement.sql_text)
        if pair == ("OURS2", "THEIRS3"):
            return log_merge.ConflictCheckResult((
                log_merge.StatementConflict(
                    kind="integrity",
                    message="theirs blocked by retained prefix",
                    scope="theirs",
                ),
            ))
        if pair == ("OURS1", "THEIRS3"):
            return log_merge.ConflictCheckResult((
                log_merge.StatementConflict(
                    kind="write_write",
                    message="earlier retained cause",
                ),
            ))
        return log_merge.ConflictCheckResult()

    candidate = log_merge.search_by_backtracking_ours(
        ours,
        theirs,
        initial_conflict=log_merge.ConflictPair(
            ours_index=2,
            theirs_index=2,
            ours_sql=ours[2].sql_text,
            theirs_sql=theirs[2].sql_text,
            conflicts=(
                log_merge.StatementConflict(
                    kind="write_write",
                    message="test conflict",
                ),
            ),
        ),
        context=conflict_context,
        conflict_detector=detector,
        statement_applier=noop_applier,
    )

    assert candidate.name == "pairwise"
    assert candidate.ours_count == 2
    assert candidate.theirs_count == 2
    assert candidate.next_conflict is not None
    assert candidate.next_conflict.ours_index == 2
    assert candidate.next_conflict.theirs_index == 2
    assert candidate.next_conflict.conflicts[0].kind == "write_write"


def test_backtracking_reports_standalone_when_no_earlier_prefix_exists(
    conflict_context,
):
    ours = [make_statement("ours", 0)]
    theirs = [make_statement("theirs", 0)]
    first = log_merge.ConflictPair(
        ours_index=0,
        theirs_index=0,
        ours_sql=ours[0].sql_text,
        theirs_sql=theirs[0].sql_text,
        conflicts=(
            log_merge.StatementConflict(
                kind="integrity",
                message="theirs blocked by retained prefix",
                scope="theirs",
            ),
        ),
    )

    candidate = log_merge.search_by_backtracking_ours(
        ours,
        theirs,
        initial_conflict=first,
        context=conflict_context,
        statement_applier=noop_applier,
    )

    assert candidate.name == "standalone_replay"
    assert candidate.ours_count == 0
    assert candidate.theirs_count == 0
    assert candidate.next_conflict == first
    assert candidate.scope == "theirs"


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


def test_merge_session_serializes_compact_statement_handoff(tmp_path):
    base = tmp_path / "base.db"
    merged = tmp_path / "merged.db"
    init_logged_db(base)

    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text="INSERT INTO user_archive SELECT * FROM users",
    )
    replay = log_merge.ReplayResult(
        ok=True,
        output_path=str(merged),
        applied_count=1,
    )
    session_path = tmp_path / "merge-session.json"

    merge_session.write_merge_session(
        session_path,
        status="conflict",
        base_db_path=base,
        merged_db_path=merged,
        base_transaction_id=0,
        ours=[statement],
        theirs=[],
        replay=replay,
    )

    payload = json.loads(session_path.read_text())
    assert Path(payload["paths"]["base"]).exists()
    assert payload["paths"]["merged"] == str(merged)
    assert set(payload["paths"]) == {"base", "merged"}
    assert "first_conflict" not in payload
    assert payload["ours_transactions"][0]["statements"][0]["to_replay_sql_text"] == (
        "INSERT INTO user_archive SELECT * FROM users"
    )
    assert payload["theirs_transactions"] == []
