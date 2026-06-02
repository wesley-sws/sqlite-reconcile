import shutil
import sqlite3
import sys
from pathlib import Path
from collections import deque
from contextlib import closing

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from merge import (
    accepted_replay,
    control_db,
    log_merge,
    remaining_metadata,
    static_analysis,
    terminal_mergetool,
)
from merge.utils import ALL_COLUMNS


def txs(statements):
    return log_merge.group_logged_transactions(statements)


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


def test_invalid_base_database_is_not_applicable(tmp_path):
    base = tmp_path / "base.db"
    ours = tmp_path / "ours.db"
    theirs = tmp_path / "theirs.db"
    init_logged_db(base)
    with closing(sqlite3.connect(base)) as con:
        con.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
        con.execute(
            "CREATE TABLE children ("
            "id INTEGER PRIMARY KEY, "
            "parent_id INTEGER REFERENCES parents(id))"
        )
        con.execute("INSERT INTO children VALUES (1, 999)")
        con.commit()
    shutil.copy2(base, ours)
    shutil.copy2(base, theirs)

    with pytest.raises(log_merge.MergeNotApplicableError) as exc_info:
        log_merge.load_merge_inputs(base, ours, theirs)

    assert exc_info.value.role == "base"
    assert exc_info.value.missing_tables == []
    assert any("foreign_key_check" in error for error in exc_info.value.details)


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
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text="NOT VALID SQL @@@",
    )

    assert not statement.is_replay_safe
    assert statement.replay_block_reason is not None
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
    assert statements[0].metadata.columns_updated == {ALL_COLUMNS}
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


def test_remaining_conflict_checks_current_against_later_remote(tmp_path):
    db_path = tmp_path / "base.db"
    init_logged_db(db_path)
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("CREATE TABLE audit (id INTEGER PRIMARY KEY, message TEXT)")
        con.execute("INSERT INTO users (id, name) VALUES (1, 'base')")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )
    current = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE users SET name = 'local' WHERE id = 1",
            table_columns=table_columns,
        )
    ])[0]
    remaining_theirs = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO audit (id, message) VALUES (1, 'remote')",
            table_columns=table_columns,
        ),
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=1,
            transaction_id=3,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE users SET name = 'remote' WHERE id = 1",
            table_columns=table_columns,
        ),
    ])

    with control_db._open_merge_working_context(
        db_path,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) as context:
        remaining_index = remaining_metadata.RemainingMetadataIndex.from_transactions(
            context,
            remaining_theirs,
        )
        conflict = log_merge._remaining_conflict_for_current(
            current,
            remaining_theirs,
            current_branch="ours",
            context=context,
            remaining_other_index=remaining_index,
        )

    assert conflict is not None
    assert conflict.current_branch == "ours"
    assert conflict.other_index == 1
    assert conflict.conflicts[0].kind == "write_write"


def test_remaining_conflict_uses_rolling_control_state_for_later_write_read(tmp_path):
    db_path = tmp_path / "base.db"
    init_logged_db(db_path)
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("CREATE TABLE audit (id INTEGER PRIMARY KEY, message TEXT)")
        con.execute("INSERT INTO users (id, name) VALUES (1, 'base')")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )
    current = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE users SET name = 'local' WHERE id = 1",
            table_columns=table_columns,
        )
    ])[0]
    remaining_theirs = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO audit (id, message) VALUES (1, 'prefix')",
            table_columns=table_columns,
        ),
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=1,
            transaction_id=3,
            committed_at="2026-01-01T00:00:00",
            sql_text=(
                "UPDATE audit SET message = "
                "(SELECT name FROM users WHERE id = 1) WHERE id = 1"
            ),
            table_columns=table_columns,
        ),
    ])

    with control_db._open_merge_working_context(
        db_path,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) as context:
        remaining_index = remaining_metadata.RemainingMetadataIndex.from_transactions(
            context,
            remaining_theirs,
        )
        conflict = log_merge._remaining_conflict_for_current(
            current,
            remaining_theirs,
            current_branch="ours",
            context=context,
            remaining_other_index=remaining_index,
        )

    assert conflict is not None
    assert conflict.other_index == 1
    assert conflict.conflicts[0].kind == "write_read"


def test_remaining_conflict_uses_current_write_probe_before_suffix(tmp_path):
    db_path = tmp_path / "base.db"
    init_logged_db(db_path)
    with closing(sqlite3.connect(db_path)) as con:
        con.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, status TEXT, flag INTEGER)"
        )
        con.execute("INSERT INTO items (id, status, flag) VALUES (1, 'open', 0)")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )
    current = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE items SET flag = 1 WHERE status = 'open'",
            table_columns=table_columns,
        )
    ])[0]
    remaining_theirs = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO items (id, status, flag) VALUES (2, 'open', 0)",
            table_columns=table_columns,
        ),
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=1,
            transaction_id=3,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE items SET flag = 2 WHERE id = 2",
            table_columns=table_columns,
        ),
    ])

    with control_db._open_merge_working_context(
        db_path,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) as context:
        remaining_index = remaining_metadata.RemainingMetadataIndex.from_transactions(
            context,
            remaining_theirs,
        )
        conflict = log_merge._remaining_conflict_for_current(
            current,
            remaining_theirs,
            current_branch="ours",
            context=context,
            remaining_other_index=remaining_index,
        )

    assert conflict is None


def test_remaining_conflict_keeps_same_table_current_write_probes_separate(tmp_path):
    db_path = tmp_path / "base.db"
    init_logged_db(db_path)
    with closing(sqlite3.connect(db_path)) as con:
        con.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, status TEXT, flag INTEGER)"
        )
        con.execute("INSERT INTO items (id, status, flag) VALUES (1, 'open', 0)")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )
    current = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE items SET flag = 1 WHERE id = 1",
            table_columns=table_columns,
        ),
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=1,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE items SET status = 'local' WHERE flag = 1",
            table_columns=table_columns,
        ),
    ])[0]
    remaining_theirs = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE items SET status = 'remote' WHERE id = 1",
            table_columns=table_columns,
        )
    ])

    with control_db._open_merge_working_context(
        db_path,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) as context:
        remaining_index = remaining_metadata.RemainingMetadataIndex.from_transactions(
            context,
            remaining_theirs,
        )
        conflict = log_merge._remaining_conflict_for_current(
            current,
            remaining_theirs,
            current_branch="ours",
            context=context,
            remaining_other_index=remaining_index,
        )

    assert conflict is not None
    assert conflict.conflicts[0].kind == "write_write"
    assert "L1.2 and R1.1" in conflict.conflicts[0].message


def test_remaining_conflict_skips_individual_checks_when_aggregate_metadata_is_clean(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "base.db"
    init_logged_db(db_path)
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("CREATE TABLE audit (id INTEGER PRIMARY KEY, message TEXT)")
        con.execute("INSERT INTO users (id, name) VALUES (1, 'base')")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )
    current = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE users SET name = 'local' WHERE id = 1",
            table_columns=table_columns,
        )
    ])[0]
    remaining_theirs = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO audit (id, message) VALUES (1, 'remote')",
            table_columns=table_columns,
        )
    ])

    def fail_if_called(*args, **kwargs):
        raise AssertionError("individual pair detector should not be called")

    monkeypatch.setattr(
        log_merge,
        "OrderedRemainingConflictScanner",
        fail_if_called,
    )

    with control_db._open_merge_working_context(
        db_path,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) as context:
        remaining_index = remaining_metadata.RemainingMetadataIndex.from_transactions(
            context,
            remaining_theirs,
        )
        conflict = log_merge._remaining_conflict_for_current(
            current,
            remaining_theirs,
            current_branch="ours",
            context=context,
            remaining_other_index=remaining_index,
        )

    assert conflict is None


def test_remaining_metadata_index_decrements_removed_transaction(tmp_path):
    db_path = tmp_path / "base.db"
    init_logged_db(db_path)
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("INSERT INTO users (id, name) VALUES (1, 'base')")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )
    transactions = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE users SET name = 'remote' WHERE id = 1",
            table_columns=table_columns,
        ),
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=1,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE users SET name = 'other' WHERE id = 1",
            table_columns=table_columns,
        ),
    ])

    with closing(sqlite3.connect(db_path)) as con:
        con.row_factory = sqlite3.Row
        context = log_merge.ConflictCheckContext(
            base_cursor=con.cursor(),
            base_db_path=str(db_path),
            table_columns=table_columns,
            primary_key_columns=primary_key_columns,
            key_column_sets=key_column_sets,
        )
        index = remaining_metadata.RemainingMetadataIndex.from_transactions(
            context,
            transactions,
        )

        assert index.update_column_counts["users"]["name"] == 2
        index.remove_transaction(context, transactions[0])
        assert index.transaction_count == 1
        assert index.update_column_counts["users"]["name"] == 1
        assert index.tables_referenced_to_column_counts["users"]["id"] == 1
        index.remove_transaction(context, transactions[1])

    assert index.transaction_count == 0
    assert index.tables_referenced_to_column_counts == {}
    assert index.write_write_column_counts == {}
    assert index.create_or_change_key_column_counts == {}
    assert index.remove_or_change_key_column_counts == {}
    assert index.update_column_counts == {}
    assert index.omitted_integer_primary_key_counts == {}


def test_context_caches_foreign_key_edges(tmp_path):
    db_path = tmp_path / "base.db"
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT UNIQUE)")
        con.execute(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                user_id INTEGER REFERENCES users(id)
            )
            """
        )
        table_columns, primary_key_columns, key_column_sets = (
            log_merge.load_schema_metadata(con.cursor())
        )
        context = log_merge.ConflictCheckContext(
            base_cursor=con.cursor(),
            base_db_path=db_path,
            table_columns=table_columns,
            primary_key_columns=primary_key_columns,
            key_column_sets=key_column_sets,
        )
        edges = static_analysis.foreign_key_edges(context)

        assert (
            "orders",
            ("user_id",),
            "users",
            ("id",),
        ) in edges
        assert static_analysis.foreign_key_edges(context) is edges


def test_remaining_metadata_index_does_not_cache_schema_constraints(tmp_path):
    db_path = tmp_path / "base.db"
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT UNIQUE)")
        table_columns, primary_key_columns, key_column_sets = (
            log_merge.load_schema_metadata(con.cursor())
        )
        context = log_merge.ConflictCheckContext(
            base_cursor=con.cursor(),
            base_db_path=db_path,
            table_columns=table_columns,
            primary_key_columns=primary_key_columns,
            key_column_sets=key_column_sets,
        )
        remaining = txs([
            log_merge.make_logged_statement(
                branch="theirs",
                branch_index=0,
                transaction_id=1,
                committed_at="2026-01-01T00:00:00",
                sql_text="INSERT INTO users (id, email) VALUES (1, 'a@example.com')",
                table_columns=table_columns,
            )
        ])
        index = remaining_metadata.RemainingMetadataIndex.from_transactions(
            context,
            remaining,
        )

    assert not hasattr(index, "foreign_key_edges")


def test_remaining_individual_check_kinds_reports_write_read_only(tmp_path):
    db_path = tmp_path / "base.db"
    init_logged_db(db_path)
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("CREATE TABLE stats (id INTEGER PRIMARY KEY, value TEXT)")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )
    current = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE users SET name = 'local' WHERE id = 1",
            table_columns=table_columns,
        )
    ])[0]
    remaining = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text=(
                "UPDATE stats SET value = "
                "(SELECT name FROM users WHERE id = 1) WHERE id = 1"
            ),
            table_columns=table_columns,
        )
    ])

    with closing(sqlite3.connect(db_path)) as con:
        context = log_merge.ConflictCheckContext(
            base_cursor=con.cursor(),
            base_db_path=str(db_path),
            table_columns=table_columns,
            primary_key_columns=primary_key_columns,
            key_column_sets=key_column_sets,
        )
        index = remaining_metadata.RemainingMetadataIndex.from_transactions(context, remaining)

        assert remaining_metadata.remaining_individual_check_kinds(
            context,
            current,
            index,
        ) == {"write_read"}


def test_remaining_individual_check_kinds_reports_write_write_only(tmp_path):
    db_path = tmp_path / "base.db"
    init_logged_db(db_path)

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )
    current = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE users SET name = 'local' WHERE id = 1",
            table_columns=table_columns,
        )
    ])[0]
    remaining = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE users SET name = 'remote' WHERE id = 2",
            table_columns=table_columns,
        )
    ])

    with closing(sqlite3.connect(db_path)) as con:
        context = log_merge.ConflictCheckContext(
            base_cursor=con.cursor(),
            base_db_path=str(db_path),
            table_columns=table_columns,
            primary_key_columns=primary_key_columns,
            key_column_sets=key_column_sets,
        )
        index = remaining_metadata.RemainingMetadataIndex.from_transactions(context, remaining)

        assert remaining_metadata.remaining_individual_check_kinds(
            context,
            current,
            index,
        ) == {"write_write"}


def test_remaining_individual_check_kinds_reports_integrity_only(tmp_path):
    db_path = tmp_path / "base.db"
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("CREATE TABLE coupons (id INTEGER PRIMARY KEY, code TEXT UNIQUE)")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )
    current = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO coupons (id, code) VALUES (1, 'shared')",
            table_columns=table_columns,
        )
    ])[0]
    remaining = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO coupons (id, code) VALUES (2, 'shared')",
            table_columns=table_columns,
        )
    ])

    with closing(sqlite3.connect(db_path)) as con:
        context = log_merge.ConflictCheckContext(
            base_cursor=con.cursor(),
            base_db_path=str(db_path),
            table_columns=table_columns,
            primary_key_columns=primary_key_columns,
            key_column_sets=key_column_sets,
        )
        index = remaining_metadata.RemainingMetadataIndex.from_transactions(context, remaining)

        assert remaining_metadata.remaining_individual_check_kinds(
            context,
            current,
            index,
        ) == {"integrity"}


def test_remaining_individual_check_kinds_does_not_scan_for_current_or_ignore_only(
    tmp_path,
):
    db_path = tmp_path / "base.db"
    init_logged_db(db_path)
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("CREATE TABLE stats (id INTEGER PRIMARY KEY, value TEXT)")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )
    current = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT OR IGNORE INTO users (id, name) VALUES (1, 'local')",
            table_columns=table_columns,
        )
    ])[0]
    remaining = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE stats SET value = 'remote' WHERE id = 1",
            table_columns=table_columns,
        )
    ])

    with closing(sqlite3.connect(db_path)) as con:
        context = log_merge.ConflictCheckContext(
            base_cursor=con.cursor(),
            base_db_path=str(db_path),
            table_columns=table_columns,
            primary_key_columns=primary_key_columns,
            key_column_sets=key_column_sets,
        )
        index = remaining_metadata.RemainingMetadataIndex.from_transactions(
            context,
            remaining,
        )

        assert remaining_metadata.remaining_individual_check_kinds(
            context,
            current,
            index,
        ) == set()


def test_remaining_conflict_reports_constraint_resolution_during_integrity_scan(
    tmp_path,
):
    db_path = tmp_path / "base.db"
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("CREATE TABLE coupons (id INTEGER PRIMARY KEY, code TEXT UNIQUE)")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )
    current = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO coupons (id, code) VALUES (1, 'shared')",
            table_columns=table_columns,
        )
    ])[0]
    remaining = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT OR IGNORE INTO coupons (id, code) VALUES (2, 'shared')",
            table_columns=table_columns,
        )
    ])

    with control_db._open_merge_working_context(
        db_path,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) as context:
        index = remaining_metadata.RemainingMetadataIndex.from_transactions(
            context,
            remaining,
        )

        assert remaining_metadata.remaining_individual_check_kinds(
            context,
            current,
            index,
        ) == {"integrity"}
        conflict = log_merge._remaining_conflict_for_current(
            current,
            remaining,
            current_branch="ours",
            context=context,
            remaining_other_index=index,
        )

    assert conflict is not None
    assert conflict.conflicts[0].kind == "constraint_resolution"


def test_apply_accepted_transaction_advances_live_context(tmp_path):
    db_path = tmp_path / "base.db"
    init_logged_db(db_path)

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )
    transaction = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO users (id, name) VALUES (1, 'local')",
            table_columns=table_columns,
        )
    ])[0]

    with control_db._open_merge_working_context(
        db_path,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) as context:
        error = accepted_replay.apply_accepted_transaction(
            context,
            transaction,
        )
        row = context.base_cursor.connection.execute(
            "SELECT name FROM users WHERE id = 1"
        ).fetchone()
        log_rows = [
            tuple(row)
            for row in context.base_cursor.connection.execute(
                f"""
                SELECT to_replay_sql_text
                FROM {log_merge.LOG_TABLE}
                ORDER BY id
                """
            ).fetchall()
        ]

    assert error is None
    assert row["name"] == "local"
    assert log_rows == [("INSERT INTO users (id, name) VALUES (1, 'local')",)]


def test_remaining_conflict_reports_later_remote_integrity_conflict(tmp_path):
    db_path = tmp_path / "base.db"
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("CREATE TABLE coupons (id INTEGER PRIMARY KEY, code TEXT UNIQUE)")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )
    current = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO coupons (id, code) VALUES (1, 'shared')",
            table_columns=table_columns,
        )
    ])[0]
    remaining_theirs = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO coupons (id, code) VALUES (2, 'remote-only')",
            table_columns=table_columns,
        ),
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=1,
            transaction_id=3,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO coupons (id, code) VALUES (3, 'shared')",
            table_columns=table_columns,
        ),
    ])

    with control_db._open_merge_working_context(
        db_path,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) as context:
        remaining_index = remaining_metadata.RemainingMetadataIndex.from_transactions(
            context,
            remaining_theirs,
        )
        conflict = log_merge._remaining_conflict_for_current(
            current,
            remaining_theirs,
            current_branch="ours",
            context=context,
            remaining_other_index=remaining_index,
        )

    assert conflict is not None
    assert conflict.other_index == 1
    assert conflict.conflicts[0].kind == "integrity"


def test_apply_accepted_transaction_preserves_transaction_log_boundaries(tmp_path):
    db_path = tmp_path / "base.db"
    init_logged_db(db_path)
    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )

    transaction = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            transaction_id=2,
            committed_at="2026-01-02T00:00:00",
            sql_text="INSERT INTO users (id, name) VALUES (1, 'Bob')",
        ),
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=1,
            transaction_id=2,
            committed_at="2026-01-02T00:00:00",
            sql_text="INSERT INTO users (id, name) VALUES (2, 'Cara')",
        ),
    ])[0]

    with control_db._open_merge_working_context(
        db_path,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) as context:
        error = accepted_replay.apply_accepted_transaction(context, transaction)
        rows = context.base_cursor.connection.execute(
            f"""
            SELECT transaction_id, to_replay_sql_text
            FROM {log_merge.LOG_TABLE}
            ORDER BY id
            """
        ).fetchall()

    assert error is None
    assert len({transaction_id for transaction_id, _ in rows}) == 1
    assert [sql_text for _, sql_text in rows] == [
        "INSERT INTO users (id, name) VALUES (1, 'Bob')",
        "INSERT INTO users (id, name) VALUES (2, 'Cara')",
    ]


def test_apply_accepted_transaction_keeps_same_id_branch_transactions_separate(tmp_path):
    db_path = tmp_path / "base.db"
    init_logged_db(db_path)
    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )

    statements = [
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            transaction_id=2,
            committed_at="2026-01-02T00:00:00",
            sql_text="INSERT INTO users (id, name) VALUES (1, 'Local')",
        ),
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            transaction_id=2,
            committed_at="2026-01-02T00:00:00",
            sql_text="INSERT INTO users (id, name) VALUES (2, 'Remote')",
        ),
    ]

    with control_db._open_merge_working_context(
        db_path,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) as context:
        errors = [
            accepted_replay.apply_accepted_transaction(context, txs([statement])[0])
            for statement in statements
        ]
        rows = context.base_cursor.connection.execute(
            f"""
            SELECT transaction_id, to_replay_sql_text
            FROM {log_merge.LOG_TABLE}
            ORDER BY id
            """
        ).fetchall()

    assert errors == [None, None]
    assert len({transaction_id for transaction_id, _ in rows}) == 2


def test_apply_accepted_transaction_rolls_back_failed_transaction_group(tmp_path):
    db_path = tmp_path / "base.db"
    init_logged_db(db_path)
    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )

    transaction = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            transaction_id=2,
            committed_at="2026-01-02T00:00:00",
            sql_text="INSERT INTO users (id, name) VALUES (1, 'Bob')",
        ),
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=1,
            transaction_id=2,
            committed_at="2026-01-02T00:00:00",
            sql_text="INSERT INTO users (id, name) VALUES (1, 'Duplicate')",
        ),
    ])[0]

    with control_db._open_merge_working_context(
        db_path,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) as context:
        error = accepted_replay.apply_accepted_transaction(context, transaction)
        users = context.base_cursor.connection.execute("SELECT * FROM users").fetchall()
        logs = context.base_cursor.connection.execute(
            f"SELECT * FROM {log_merge.LOG_TABLE}"
        ).fetchall()

    assert error is not None
    assert error.kind == "integrity"
    assert users == []
    assert logs == []


def test_apply_accepted_transaction_reports_deferred_foreign_key_failure(tmp_path):
    db_path = tmp_path / "base.db"
    with closing(sqlite3.connect(db_path)) as con:
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
    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )

    valid_sql_text = "INSERT INTO parents (id) VALUES (1)"
    valid_statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        transaction_id=1,
        committed_at="2026-01-02T00:00:00",
        sql_text=valid_sql_text,
    )
    invalid_sql_text = "INSERT INTO children (id, parent_id) VALUES (1, 99)"
    invalid_statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=1,
        transaction_id=2,
        committed_at="2026-01-03T00:00:00",
        sql_text=invalid_sql_text,
    )

    with control_db._open_merge_working_context(
        db_path,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) as context:
        first_error = accepted_replay.apply_accepted_transaction(
            context,
            txs([valid_statement])[0],
        )
        second_error = accepted_replay.apply_accepted_transaction(
            context,
            txs([invalid_statement])[0],
        )
        parent_rows = [
            tuple(row)
            for row in context.base_cursor.connection.execute(
                "SELECT * FROM parents"
            ).fetchall()
        ]
        child_rows = [
            tuple(row)
            for row in context.base_cursor.connection.execute(
                "SELECT * FROM children"
            ).fetchall()
        ]
        log_rows = [
            tuple(row)
            for row in context.base_cursor.connection.execute(
                f"SELECT to_replay_sql_text FROM {log_merge.LOG_TABLE} ORDER BY id"
            ).fetchall()
        ]

    assert first_error is None
    assert second_error is not None
    assert second_error.kind == "integrity"
    assert "foreign_key_check" in second_error.message
    assert parent_rows == [(1,)]
    assert child_rows == []
    assert log_rows == [(valid_sql_text,)]


def test_current_unsafe_replay_statement_is_resolved_before_pair_scan(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "base.db"
    init_logged_db(db_path)

    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        transaction_id=1,
        committed_at="2026-01-02T00:00:00",
        sql_text="UPDATE users SET name = random()",
        is_replay_safe=False,
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("unsafe current transaction should be resolved first")

    monkeypatch.setattr(
        terminal_mergetool,
        "_RemainingCurrentConflictScan",
        fail_if_called,
    )
    responses = iter([";"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(db_path)
    )
    remaining_ours = deque(txs([statement]))
    remaining_theirs = deque()
    with control_db._open_merge_working_context(
        db_path,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) as context:
        metadata_indexes: terminal_mergetool.MetadataIndexes = {
            "ours": remaining_metadata.RemainingMetadataIndex.from_transactions(
                context,
                remaining_ours,
            ),
            "theirs": remaining_metadata.RemainingMetadataIndex.from_transactions(
                context,
                remaining_theirs,
            ),
        }
        accepted = terminal_mergetool._check_accept_current(
            "ours",
            remaining_ours,
            remaining_theirs,
            metadata_indexes,
            context,
            table_columns,
        )

    assert accepted
    assert not remaining_ours
