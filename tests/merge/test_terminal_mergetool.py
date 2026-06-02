import sqlite3
import sys
from collections import deque
from contextlib import closing
from dataclasses import replace
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from merge import (
    control_db,
    log_merge,
    remaining_execution,
    remaining_metadata,
    terminal_mergetool,
    terminal_ui,
)
from merge.models import ConflictPair, StatementConflict


def txs(statements):
    return log_merge.group_logged_transactions(statements)


def flatten_transactions(transactions):
    return [
        statement
        for transaction in transactions
        for statement in transaction.statements
    ]


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


def replay_conn():
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def test_configured_editor_uses_git_like_precedence(monkeypatch):
    monkeypatch.delenv("GIT_EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setattr(terminal_ui, "_git_config_editor", lambda: "nano")

    assert terminal_ui._configured_editor() == "nano"

    monkeypatch.setenv("GIT_EDITOR", "code --wait")
    assert terminal_ui._configured_editor() == "code --wait"


def test_configured_editor_falls_back_to_terminal_editor(monkeypatch):
    monkeypatch.delenv("GIT_EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setattr(terminal_ui, "_git_config_editor", lambda: None)
    monkeypatch.setattr(terminal_ui.shutil, "which", lambda name: "/usr/bin/nano")

    assert terminal_ui._configured_editor() == "nano"

    monkeypatch.setattr(terminal_ui.shutil, "which", lambda name: None)
    assert terminal_ui._configured_editor() == "vi"


def test_prompt_replay_warning_uses_prefilled_external_editor(monkeypatch):
    table_columns = {"audit_events": {"id", "token"}}
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=4,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text="UPDATE audit_events SET token = random() WHERE id BETWEEN 4 AND 8",
        table_columns=table_columns,
        is_replay_safe=False,
        replay_block_reason="nondeterministic expression cannot be safely materialized",
    )
    responses = iter([":edit", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    monkeypatch.setattr(
        terminal_ui,
        "_edit_sql_in_editor",
        lambda label, sql: "UPDATE audit_events SET token = 'fixed' WHERE id BETWEEN 4 AND 8",
    )

    with closing(replay_conn()) as con:
        replacement, should_restart = terminal_ui._prompt_replay_warning(
            statement,
            table_columns,
            con,
            "nondeterministic expression cannot be safely materialized",
        )

    assert replacement is not None
    assert replacement.sql_text == (
        "UPDATE audit_events SET token = 'fixed' WHERE id BETWEEN 4 AND 8"
    )
    assert should_restart


def test_prompt_replay_warning_can_delete_after_edit(monkeypatch):
    table_columns = {"audit_events": {"id", "token"}}
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=4,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text="UPDATE audit_events SET token = random() WHERE id BETWEEN 4 AND 8",
        table_columns=table_columns,
        is_replay_safe=False,
        replay_block_reason="nondeterministic expression cannot be safely materialized",
    )
    responses = iter([":edit", "DELETE"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    monkeypatch.setattr(
        terminal_ui,
        "_edit_sql_in_editor",
        lambda label, sql: "UPDATE audit_events SET token = 'fixed' WHERE id BETWEEN 4 AND 8",
    )

    with closing(replay_conn()) as con:
        replacement, should_restart = terminal_ui._prompt_replay_warning(
            statement,
            table_columns,
            con,
            "nondeterministic expression cannot be safely materialized",
        )

    assert replacement is None
    assert should_restart


def test_transaction_resolution_rechecks_replay_safety_for_edited_sql(monkeypatch):
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text="UPDATE audit_events SET token = 'fixed' WHERE id = 1",
        table_columns={"audit_events": {"id", "token"}},
    )
    responses = iter(["edit L1.1;", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    monkeypatch.setattr(
        terminal_ui,
        "_edit_sql_in_editor",
        lambda label, sql: "UPDATE audit_events SET token = random() WHERE id = 1",
    )
    conflict = ConflictPair(
        current_branch="ours",
        other_index=None,
        ours_sql="",
        theirs_sql="",
        conflicts=(
            StatementConflict(
                kind="replay_error",
                message="statement failed",
                scope="ours",
                details=(("statement_log_id", "1"),),
            ),
        ),
        is_standalone=True,
    )

    with closing(replay_conn()) as con:
        replacement = terminal_ui._prompt_standalone_transaction_resolution(
            conflict,
            "ours",
            txs([statement])[0],
            {"audit_events": {"id", "token"}},
            con,
        )

    assert replacement is not None
    assert not replacement[0].is_replay_safe
    assert replacement[0].replay_block_reason == (
        "nondeterministic expression cannot be safely materialized"
    )


def test_prompt_standalone_transaction_resolution_prints_statement_once(monkeypatch, capsys):
    statement = log_merge.make_logged_statement(
        branch="theirs",
        branch_index=3,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text="INSERT INTO coupons (id, code, discount) VALUES (902, 'SPRING-DEMO', 25)",
    )
    conflict = ConflictPair(
        current_branch="theirs",
        other_index=None,
        ours_sql="",
        theirs_sql=statement.original_sql_text,
        conflicts=(
            StatementConflict(
                kind="integrity",
                message="UNIQUE constraint failed: coupons.code",
                scope="theirs",
            ),
        ),
        is_standalone=True,
    )
    monkeypatch.setattr("builtins.input", lambda _: ";")

    with closing(replay_conn()) as con:
        terminal_ui._prompt_standalone_transaction_resolution(
            conflict,
            "theirs",
            txs([statement])[0],
            {},
            con,
        )

    output = capsys.readouterr().out
    assert output.count("INSERT INTO coupons") == 1


def test_prompt_standalone_transaction_resolution_edits_recorded_statement_in_transaction(
    monkeypatch,
):
    first = log_merge.make_logged_statement(
        branch="theirs",
        branch_index=3,
        log_id=11,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text="INSERT INTO coupons (id, code) VALUES (1, 'OK')",
    )
    failing = log_merge.make_logged_statement(
        branch="theirs",
        branch_index=4,
        log_id=12,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text="INSERT INTO coupons (id, code) VALUES (2, 'DUP')",
    )
    conflict = ConflictPair(
        current_branch="theirs",
        other_index=None,
        ours_sql="",
        theirs_sql="",
        conflicts=(
            StatementConflict(
                kind="integrity",
                message="UNIQUE constraint failed: coupons.code",
                scope="theirs",
                details=(("statement_log_id", "12"),),
            ),
        ),
        is_standalone=True,
    )
    monkeypatch.setattr(
        terminal_ui,
        "_edit_sql_in_editor",
        lambda label, sql: "INSERT INTO coupons (id, code) VALUES (2, 'FIXED')",
    )
    responses = iter(["edit R1.2;", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))

    with closing(replay_conn()) as con:
        replacement = terminal_ui._prompt_standalone_transaction_resolution(
            conflict,
            "theirs",
            txs([first, failing])[0],
            {},
            con,
        )

    assert replacement is not None
    assert [statement.sql_text for statement in replacement] == [
        "INSERT INTO coupons (id, code) VALUES (1, 'OK')",
        "INSERT INTO coupons (id, code) VALUES (2, 'FIXED')",
    ]


def test_transaction_scoped_statement_labels_for_ui(capsys):
    transaction = replace(
        txs([
            log_merge.make_logged_statement(
                branch="ours",
                branch_index=4,
                log_id=11,
                transaction_id=1,
                committed_at="2026-01-01T00:00:00",
                sql_text="UPDATE coupons SET code = 'A' WHERE id = 1",
            ),
            log_merge.make_logged_statement(
                branch="ours",
                branch_index=5,
                log_id=12,
                transaction_id=1,
                committed_at="2026-01-01T00:00:00",
                sql_text="UPDATE coupons SET code = 'B' WHERE id = 2",
            ),
        ])[0],
        branch_index=1,
    )

    terminal_ui._print_statement_group("L2", transaction.statements)
    output = capsys.readouterr().out

    assert terminal_ui.transaction_label(transaction) == "L2"
    assert "Local transaction L2:" in output
    assert "L2.1:" in output
    assert "L2.2:" in output
    assert "L5:" not in output
    assert set(terminal_ui._statement_lookup({"L2": transaction})) == {
        "L2.1",
        "L2.2",
    }


def test_user_inserted_statement_gets_transaction_scoped_new_label(capsys):
    transaction = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            log_id=11,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE coupons SET code = 'A' WHERE id = 1",
        )
    ])[0]
    with closing(replay_conn()) as con:
        inserted = terminal_ui._new_statement_sql(
            transaction,
            transaction.statements,
            "UPDATE coupons SET code = 'B' WHERE id = 2",
            con,
            {},
        )
    updated = terminal_ui._insert_transaction_statement(
        transaction,
        transaction.statements[0],
        "after",
        inserted,
    )

    terminal_ui._print_statement_group("R1", updated.statements)
    output = capsys.readouterr().out

    assert set(terminal_ui._statement_lookup({"R1": updated})) == {
        "R1.1",
        "R1.N1",
    }
    assert "R1.1:" in output
    assert "R1.N1:" in output


def test_replace_or_delete_transaction_keeps_edited_transaction_for_recheck():
    ours = [
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            log_id=1,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO coupons (id, code) VALUES (1, 'OLD')",
        ),
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=1,
            log_id=2,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE coupons SET code = 'NEXT' WHERE id = 1",
        ),
    ]
    theirs = [
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            log_id=3,
            transaction_id=3,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO coupons (id, code) VALUES (2, 'REMOTE')",
        )
    ]
    replacement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text="INSERT INTO coupons (id, code) VALUES (1, 'FIXED')",
    )
    conflict = ConflictPair(
        current_branch="ours",
        other_index=0,
        ours_sql="",
        theirs_sql="",
        conflicts=(
            StatementConflict(
                kind="integrity",
                message="UNIQUE constraint failed: coupons.code",
                scope="ours",
            ),
        ),
    )

    remaining_ours = deque(txs(ours))
    remaining_theirs = deque(txs(theirs))
    with closing(sqlite3.connect(":memory:")) as con:
        con.execute("CREATE TABLE coupons (id INTEGER PRIMARY KEY, code TEXT UNIQUE)")
        context = log_merge.ConflictCheckContext(
            base_cursor=con.cursor(),
            base_db_path=":memory:",
            table_columns={"coupons": {"id", "code"}},
        )
        metadata_index = remaining_metadata.RemainingMetadataIndex.from_transactions(
            context,
            remaining_ours,
        )
        terminal_mergetool._replace_or_delete_transaction(
            context,
            remaining_ours,
            metadata_index,
            conflict.index_for_branch("ours"),
            terminal_ui._transaction_with_statements(
                remaining_ours[conflict.index_for_branch("ours")],
                [replacement],
            ),
        )

    assert [transaction.statements[0].sql_text for transaction in remaining_ours] == [
        replacement.sql_text,
        ours[1].sql_text,
    ]
    assert metadata_index.transaction_count == 2
    assert [transaction.statements[0] for transaction in remaining_theirs] == theirs


def test_pair_resolution_deleting_current_removes_only_current_transaction(monkeypatch):
    ours = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            log_id=1,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO coupons (id, code) VALUES (1, 'LOCAL')",
        )
    ])
    theirs = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            log_id=2,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO coupons (id, code) VALUES (2, 'REMOTE')",
        ),
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=1,
            log_id=3,
            transaction_id=3,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO coupons (id, code) VALUES (3, 'REMOTE')",
        ),
    ])
    remaining_ours = deque(ours)
    remaining_theirs = deque(theirs)
    conflict = ConflictPair(
        current_branch="ours",
        other_index=1,
        ours_sql="",
        theirs_sql="",
        conflicts=(StatementConflict(kind="write_write", message="conflict"),),
    )
    monkeypatch.setattr(
        terminal_mergetool,
        "_prompt_pair_transaction_resolution",
        lambda *args, **kwargs: terminal_ui.PairTransactionResolution(
            action="replace",
            ours=None,
            theirs=theirs[1],
            changed_ours=True,
            changed_theirs=False,
        ),
    )

    with closing(sqlite3.connect(":memory:")) as con:
        con.execute("CREATE TABLE coupons (id INTEGER PRIMARY KEY, code TEXT)")
        context = log_merge.ConflictCheckContext(
            base_cursor=con.cursor(),
            base_db_path=":memory:",
            table_columns={"coupons": {"id", "code"}},
        )
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
        outcome = terminal_mergetool._resolve_remaining_pair_conflict(
            context,
            conflict,
            remaining_ours,
            remaining_theirs,
            metadata_indexes,
            {},
            current_branch="ours",
        )

    assert outcome.action == "resolved"
    assert outcome.ours == "deleted"
    assert outcome.theirs == "unchanged"
    assert not remaining_ours
    assert list(remaining_theirs) == theirs


def test_pair_resolution_deleting_later_other_keeps_current_for_recheck(monkeypatch):
    ours = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            log_id=1,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO coupons (id, code) VALUES (1, 'LOCAL')",
        )
    ])
    theirs = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            log_id=2,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO coupons (id, code) VALUES (2, 'REMOTE')",
        ),
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=1,
            log_id=3,
            transaction_id=3,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO coupons (id, code) VALUES (3, 'REMOTE')",
        ),
    ])
    remaining_ours = deque(ours)
    remaining_theirs = deque(theirs)
    conflict = ConflictPair(
        current_branch="ours",
        other_index=1,
        ours_sql="",
        theirs_sql="",
        conflicts=(StatementConflict(kind="write_write", message="conflict"),),
    )
    monkeypatch.setattr(
        terminal_mergetool,
        "_prompt_pair_transaction_resolution",
        lambda *args, **kwargs: terminal_ui.PairTransactionResolution(
            action="replace",
            ours=ours[0],
            theirs=None,
            changed_ours=False,
            changed_theirs=True,
        ),
    )

    with closing(sqlite3.connect(":memory:")) as con:
        con.execute("CREATE TABLE coupons (id INTEGER PRIMARY KEY, code TEXT)")
        context = log_merge.ConflictCheckContext(
            base_cursor=con.cursor(),
            base_db_path=":memory:",
            table_columns={"coupons": {"id", "code"}},
        )
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
        outcome = terminal_mergetool._resolve_remaining_pair_conflict(
            context,
            conflict,
            remaining_ours,
            remaining_theirs,
            metadata_indexes,
            {},
            current_branch="ours",
        )

    assert outcome.action == "resolved"
    assert outcome.ours == "unchanged"
    assert outcome.theirs == "deleted"
    assert list(remaining_ours) == ours
    assert list(remaining_theirs) == [theirs[0]]


def test_pair_resolution_accepts_reviewable_conflict_without_queue_changes(monkeypatch):
    ours = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            log_id=1,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE coupons SET code = 'LOCAL' WHERE id = 1",
        )
    ])
    theirs = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            log_id=2,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE coupons SET code = 'REMOTE' WHERE id = 1",
        )
    ])
    remaining_ours = deque(ours)
    remaining_theirs = deque(theirs)
    conflict = ConflictPair(
        current_branch="ours",
        other_index=0,
        ours_sql="",
        theirs_sql="",
        conflicts=(StatementConflict(kind="write_write", message="conflict"),),
        resolution_key=("accepted",),
    )
    monkeypatch.setattr(
        terminal_mergetool,
        "_prompt_pair_transaction_resolution",
        lambda *args, **kwargs: terminal_ui.PairTransactionResolution(
            action="accept",
            ours=ours[0],
            theirs=theirs[0],
        ),
    )
    with closing(sqlite3.connect(":memory:")) as con:
        con.execute("CREATE TABLE coupons (id INTEGER PRIMARY KEY, code TEXT)")
        context = log_merge.ConflictCheckContext(
            base_cursor=con.cursor(),
            base_db_path=":memory:",
            table_columns={"coupons": {"id", "code"}},
        )
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
        outcome = terminal_mergetool._resolve_remaining_pair_conflict(
            context,
            conflict,
            remaining_ours,
            remaining_theirs,
            metadata_indexes,
            {},
            current_branch="ours",
        )

    assert outcome.action == "accepted"
    assert outcome.ours == "unchanged"
    assert outcome.theirs == "unchanged"
    assert list(remaining_ours) == ours
    assert list(remaining_theirs) == theirs


def test_branch_replay_safety_edits_whole_transaction_after_error(
    tmp_path,
    monkeypatch,
):
    base = tmp_path / "base.db"
    with closing(sqlite3.connect(base)) as con:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
        con.execute(
            """
            CREATE TABLE children (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER NOT NULL REFERENCES parents(id)
            )
            """
        )
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(base)
    )
    statements = [
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            log_id=1,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO parents(id) VALUES (1)",
            table_columns=table_columns,
            is_replay_safe=False,
            replay_block_reason="wrapper blocked statement",
        ),
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=1,
            log_id=2,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO children(id, parent_id) VALUES (1, 1)",
            table_columns=table_columns,
        ),
    ]
    responses = iter(["edit L1.1;", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    monkeypatch.setattr(
        terminal_ui,
        "_edit_sql_in_editor",
        lambda label, sql: "INSERT INTO parents(id) VALUES (1)",
    )

    resolved = terminal_mergetool._resolve_branch_replay_safety(
        base,
        "ours",
        txs(statements),
        table_columns,
        primary_key_columns,
        key_column_sets,
    )

    resolved_statements = flatten_transactions(resolved)
    assert [statement.sql_text for statement in resolved_statements] == [
        "INSERT INTO parents(id) VALUES (1)",
        "INSERT INTO children(id, parent_id) VALUES (1, 1)",
    ]


def test_branch_replay_safety_warns_for_update_from_duplicates(
    tmp_path,
    monkeypatch,
):
    base = tmp_path / "base.db"
    with closing(sqlite3.connect(base)) as con:
        con.execute(
            "CREATE TABLE products ("
            "id INTEGER PRIMARY KEY, category_id INTEGER, discount INTEGER)"
        )
        con.execute("CREATE TABLE categories (id INTEGER, rate INTEGER)")
        con.execute("INSERT INTO products VALUES (1, 1, 0)")
        con.execute("INSERT INTO categories VALUES (1, 5)")
        con.execute("INSERT INTO categories VALUES (1, 7)")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(base)
    )
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "UPDATE products "
            "SET discount = categories.rate "
            "FROM categories "
            "WHERE products.category_id = categories.id"
        ),
        table_columns=table_columns,
    )
    responses = [""]
    monkeypatch.setattr("builtins.input", lambda _: responses.pop(0))

    resolved = terminal_mergetool._resolve_branch_replay_safety(
        base,
        "ours",
        txs([statement]),
        table_columns,
        primary_key_columns,
        key_column_sets,
    )

    assert flatten_transactions(resolved) == [statement]
    assert responses == []


def test_branch_replay_safety_can_acknowledge_nondeterministic_warning(
    tmp_path,
    monkeypatch,
):
    base = tmp_path / "base.db"
    with closing(sqlite3.connect(base)) as con:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        con.execute("INSERT INTO users VALUES (1, 'old')")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(base)
    )
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text="UPDATE users SET name = random() WHERE id = 1",
        table_columns=table_columns,
        is_replay_safe=False,
        replay_block_reason="nondeterministic expression cannot be safely materialized",
    )
    responses = [""]
    monkeypatch.setattr("builtins.input", lambda _: responses.pop(0))

    resolved = terminal_mergetool._resolve_branch_replay_safety(
        base,
        "ours",
        txs([statement]),
        table_columns,
        primary_key_columns,
        key_column_sets,
    )

    resolved_statements = flatten_transactions(resolved)
    assert len(resolved_statements) == 1
    assert resolved_statements[0].is_replay_safe
    assert resolved_statements[0].replay_warnings == (
        "nondeterministic expression cannot be safely materialized",
    )
    assert responses == []


def test_branch_replay_safety_prompts_stored_nondeterministic_warning(
    tmp_path,
    monkeypatch,
):
    base = tmp_path / "base.db"
    with closing(sqlite3.connect(base)) as con:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        con.execute("INSERT INTO users VALUES (1, 'old')")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(base)
    )
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text="UPDATE users SET name = random() WHERE id = 1",
        table_columns=table_columns,
        replay_warnings=("nondeterministic expression cannot be safely materialized",),
    )
    responses = [""]
    monkeypatch.setattr("builtins.input", lambda _: responses.pop(0))

    resolved = terminal_mergetool._resolve_branch_replay_safety(
        base,
        "ours",
        txs([statement]),
        table_columns,
        primary_key_columns,
        key_column_sets,
    )

    assert flatten_transactions(resolved) == [statement]
    assert responses == []


def test_branch_replay_safety_ignores_stale_update_from_warning(
    tmp_path,
    monkeypatch,
):
    base = tmp_path / "base.db"
    with closing(sqlite3.connect(base)) as con:
        con.execute(
            "CREATE TABLE products ("
            "id INTEGER PRIMARY KEY, category_id INTEGER, discount INTEGER)"
        )
        con.execute("CREATE TABLE categories (id INTEGER, rate INTEGER)")
        con.execute("INSERT INTO products VALUES (1, 1, 0)")
        con.execute("INSERT INTO categories VALUES (1, 5)")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(base)
    )
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "UPDATE products "
            "SET discount = categories.rate "
            "FROM categories "
            "WHERE products.category_id = categories.id"
        ),
        table_columns=table_columns,
        replay_warnings=(log_merge.UPDATE_FROM_DUPLICATE_TARGET_WARNING,),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda _: (_ for _ in ()).throw(AssertionError("unexpected prompt")),
    )

    resolved = terminal_mergetool._resolve_branch_replay_safety(
        base,
        "ours",
        txs([statement]),
        table_columns,
        primary_key_columns,
        key_column_sets,
    )

    assert flatten_transactions(resolved) == [statement]


def test_merge_working_context_attaches_control_copy(tmp_path):
    base = tmp_path / "base.db"
    with closing(sqlite3.connect(base)) as con:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        con.execute("INSERT INTO users (id, name) VALUES (1, 'base')")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(base)
    )

    with control_db._open_merge_working_context(
        base,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) as context:
        cursor = context.base_cursor
        assert context.control_schema == control_db.CONTROL_DB_SCHEMA
        assert context.control_sql_rewriter is not None
        assert cursor.execute(
            "SELECT name FROM control.sqlite_master WHERE name = 'users'"
        ).fetchone() is not None

        cursor.execute("INSERT INTO users (id, name) VALUES (2, 'main')")
        main_count = cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        control_count = cursor.execute(
            "SELECT COUNT(*) FROM control.users"
        ).fetchone()[0]

    assert main_count == 2
    assert control_count == 1


def test_control_rewrite_qualifies_persistent_tables_and_preserves_ctes():
    sql = (
        "WITH incoming(id, name) AS (SELECT id, name FROM users) "
        "INSERT INTO audit(id, message) "
        "SELECT id, name FROM incoming"
    )

    rewritten = control_db._rewrite_sql_for_control_db(
        sql,
        table_columns={
            "users": {"id", "name"},
            "audit": {"id", "message"},
        },
    )

    assert rewritten == (
        "WITH incoming(id, name) AS "
        "(SELECT id, name FROM control.users AS users) "
        "INSERT INTO control.audit AS audit (id, message) "
        "SELECT id, name FROM incoming"
    )


def test_control_rewrite_keeps_cte_shadowing_scope_local():
    rewritten = control_db._rewrite_sql_for_control_db(
        (
            "SELECT users.id FROM users "
            "WHERE id IN ("
            "WITH users AS (SELECT 1 AS id) "
            "SELECT id FROM users"
            ")"
        ),
        table_columns={
            "users": {"id"},
        },
    )

    assert rewritten == (
        "SELECT users.id FROM control.users AS users "
        "WHERE id IN (WITH users AS (SELECT 1 AS id) SELECT id FROM users)"
    )


def test_control_rewrite_preserves_existing_aliases_and_main_schema():
    rewritten = control_db._rewrite_sql_for_control_db(
        "SELECT u.id FROM main.users AS u",
        table_columns={
            "users": {"id"},
        },
    )

    assert rewritten == "SELECT u.id FROM control.users AS u"


def test_control_rewrite_qualifies_update_cte_and_subquery_tables():
    rewritten = control_db._rewrite_sql_for_control_db(
        (
            "WITH target AS (SELECT id FROM audit) "
            "UPDATE users SET name = 'x' "
            "WHERE id IN (SELECT id FROM target)"
        ),
        table_columns={
            "users": {"id", "name"},
            "audit": {"id"},
        },
    )

    assert rewritten == (
        "WITH target AS (SELECT id FROM control.audit AS audit) "
        "UPDATE control.users AS users SET name = 'x' "
        "WHERE id IN (SELECT id FROM target)"
    )


def test_control_rewrite_keeps_dml_target_when_cte_has_same_name():
    rewritten = control_db._rewrite_sql_for_control_db(
        (
            "WITH users AS (SELECT id FROM archive) "
            "INSERT INTO users(id) SELECT id FROM users"
        ),
        table_columns={
            "users": {"id"},
            "archive": {"id"},
        },
    )

    assert rewritten == (
        "WITH users AS (SELECT id FROM control.archive AS archive) "
        "INSERT INTO control.users AS users (id) SELECT id FROM users"
    )


def test_control_rewrite_skips_later_cte_reference_to_earlier_cte():
    rewritten = control_db._rewrite_sql_for_control_db(
        (
            "WITH c AS (SELECT id FROM users), "
            "d AS (SELECT id FROM c) "
            "UPDATE accounts SET flag = 1 "
            "FROM d "
            "WHERE accounts.id = d.id"
        ),
        table_columns={
            "accounts": {"id", "flag"},
            "users": {"id"},
        },
    )

    assert rewritten == (
        "WITH c AS (SELECT id FROM control.users AS users), "
        "d AS (SELECT id FROM c) "
        "UPDATE control.accounts AS accounts SET flag = 1 "
        "FROM d "
        "WHERE accounts.id = d.id"
    )


def test_control_rewrite_skips_cte_reference_in_nested_subquery():
    rewritten = control_db._rewrite_sql_for_control_db(
        (
            "WITH c AS (SELECT id FROM users) "
            "UPDATE accounts SET flag = 1 "
            "WHERE EXISTS ("
            "  SELECT 1 WHERE EXISTS (SELECT 1 FROM c)"
            ")"
        ),
        table_columns={
            "accounts": {"id", "flag"},
            "users": {"id"},
        },
    )

    assert rewritten == (
        "WITH c AS (SELECT id FROM control.users AS users) "
        "UPDATE control.accounts AS accounts SET flag = 1 "
        "WHERE EXISTS(SELECT 1 WHERE EXISTS(SELECT 1 FROM c))"
    )


def test_control_rewrite_keeps_unrelated_real_table_in_nested_subquery():
    rewritten = control_db._rewrite_sql_for_control_db(
        (
            "WITH c AS (SELECT id FROM users) "
            "UPDATE accounts SET flag = 1 "
            "WHERE EXISTS (SELECT 1 FROM audit)"
        ),
        table_columns={
            "accounts": {"id", "flag"},
            "users": {"id"},
            "audit": {"id"},
        },
    )

    assert rewritten == (
        "WITH c AS (SELECT id FROM control.users AS users) "
        "UPDATE control.accounts AS accounts SET flag = 1 "
        "WHERE EXISTS(SELECT 1 FROM control.audit AS audit)"
    )


def test_control_rewrite_qualifies_real_table_inside_derived_table():
    rewritten = control_db._rewrite_sql_for_control_db(
        "SELECT u.id FROM (SELECT id FROM users) AS u",
        table_columns={
            "users": {"id"},
        },
    )

    assert rewritten == (
        "SELECT u.id FROM (SELECT id FROM control.users AS users) AS u"
    )


def test_control_rewrite_cte_scope_does_not_leak_to_sibling_table():
    rewritten = control_db._rewrite_sql_for_control_db(
        (
            "SELECT sub.id, c.id "
            "FROM ("
            "  WITH c AS (SELECT id FROM users) "
            "  SELECT id FROM c"
            ") AS sub, c"
        ),
        table_columns={
            "users": {"id"},
            "c": {"id"},
        },
    )

    assert rewritten == (
        "SELECT sub.id, c.id "
        "FROM (WITH c AS (SELECT id FROM control.users AS users) "
        "SELECT id FROM c) AS sub, control.c AS c"
    )


def test_control_rewrite_nested_cte_shadows_real_table_only_in_scope():
    rewritten = control_db._rewrite_sql_for_control_db(
        (
            "SELECT audit.id "
            "FROM audit "
            "WHERE EXISTS ("
            "  WITH audit AS (SELECT id FROM users) "
            "  SELECT 1 FROM audit"
            ")"
        ),
        table_columns={
            "audit": {"id"},
            "users": {"id"},
        },
    )

    assert rewritten == (
        "SELECT audit.id "
        "FROM control.audit AS audit "
        "WHERE EXISTS(WITH audit AS "
        "(SELECT id FROM control.users AS users) SELECT 1 FROM audit)"
    )


def test_control_rewrite_delete_with_cte_reference():
    rewritten = control_db._rewrite_sql_for_control_db(
        (
            "WITH doomed AS (SELECT id FROM users) "
            "DELETE FROM audit "
            "WHERE id IN (SELECT id FROM doomed)"
        ),
        table_columns={
            "audit": {"id"},
            "users": {"id"},
        },
    )

    assert rewritten == (
        "WITH doomed AS (SELECT id FROM control.users AS users) "
        "DELETE FROM control.audit AS audit "
        "WHERE id IN (SELECT id FROM doomed)"
    )


def test_control_rewrite_update_target_not_shadowed_by_same_named_cte():
    rewritten = control_db._rewrite_sql_for_control_db(
        (
            "WITH users AS (SELECT id FROM archive) "
            "UPDATE users SET name = 'x' "
            "WHERE EXISTS (SELECT 1 FROM users)"
        ),
        table_columns={
            "users": {"id", "name"},
            "archive": {"id"},
        },
    )

    assert rewritten == (
        "WITH users AS (SELECT id FROM control.archive AS archive) "
        "UPDATE control.users AS users SET name = 'x' "
        "WHERE EXISTS(SELECT 1 FROM users)"
    )


def test_check_accept_current_applies_and_pops_fixed_order_heads(tmp_path):
    base = tmp_path / "base.db"
    with closing(sqlite3.connect(base)) as con:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        create_log_tables(con)
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(base)
    )
    ours = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            log_id=1,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO users (id, name) VALUES (1, 'local one')",
            table_columns=table_columns,
        ),
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=1,
            log_id=2,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO users (id, name) VALUES (3, 'local two')",
            table_columns=table_columns,
        ),
    ])
    theirs = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            log_id=3,
            transaction_id=3,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO users (id, name) VALUES (2, 'remote one')",
            table_columns=table_columns,
        )
    ])
    remaining_ours = deque(ours)
    remaining_theirs = deque(theirs)

    with control_db._open_merge_working_context(
        base,
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
        while remaining_ours or remaining_theirs:
            assert terminal_mergetool._check_accept_current(
                "ours",
                remaining_ours,
                remaining_theirs,
                metadata_indexes,
                context,
                table_columns,
            )
            assert terminal_mergetool._check_accept_current(
                "theirs",
                remaining_ours,
                remaining_theirs,
                metadata_indexes,
                context,
                table_columns,
            )
        rows = [
            tuple(row)
            for row in context.base_cursor.execute(
                "SELECT id, name FROM users ORDER BY id"
            ).fetchall()
        ]
        control_rows = [
            tuple(row)
            for row in context.base_cursor.execute(
                "SELECT id, name FROM control.users ORDER BY id"
            ).fetchall()
        ]
        log_rows = [
            tuple(row)
            for row in context.base_cursor.execute(
                f"""
                SELECT to_replay_sql_text
                FROM {log_merge.LOG_TABLE}
                ORDER BY id
                """
            ).fetchall()
        ]

    assert not remaining_ours
    assert not remaining_theirs
    assert rows == [
        (1, "local one"),
        (2, "remote one"),
        (3, "local two"),
    ]
    assert control_rows == rows
    assert log_rows == [
        ("INSERT INTO users (id, name) VALUES (1, 'local one')",),
        ("INSERT INTO users (id, name) VALUES (2, 'remote one')",),
        ("INSERT INTO users (id, name) VALUES (3, 'local two')",),
    ]


def test_check_accept_current_continues_scan_after_deleting_other_conflict(
    tmp_path,
    monkeypatch,
):
    base = tmp_path / "base.db"
    with closing(sqlite3.connect(base)) as con:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        create_log_tables(con)
        con.execute("INSERT INTO users (id, name) VALUES (1, 'base')")
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(base)
    )
    ours = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            log_id=1,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE users SET name = 'local' WHERE id = 1",
            table_columns=table_columns,
        )
    ])
    theirs = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            log_id=2,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO users (id, name) VALUES (2, 'remote one')",
            table_columns=table_columns,
        ),
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=1,
            log_id=3,
            transaction_id=3,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE users SET name = 'remote conflict' WHERE id = 1",
            table_columns=table_columns,
        ),
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=2,
            log_id=4,
            transaction_id=4,
            committed_at="2026-01-01T00:00:00",
            sql_text="INSERT INTO users (id, name) VALUES (3, 'remote two')",
            table_columns=table_columns,
        ),
    ])
    remaining_ours = deque(ours)
    remaining_theirs = deque(theirs)

    checked_transaction_ids: list[int] = []
    original_check_next = remaining_execution.OrderedRemainingExecutionScanner.check_next

    def recording_check_next(self, ours_transaction, theirs_transaction, static_result):
        other_transaction = (
            theirs_transaction
            if self.current_branch == "ours"
            else ours_transaction
        )
        checked_transaction_ids.append(other_transaction.transaction_id)
        return original_check_next(
            self,
            ours_transaction,
            theirs_transaction,
            static_result,
        )

    monkeypatch.setattr(
        remaining_execution.OrderedRemainingExecutionScanner,
        "check_next",
        recording_check_next,
    )
    monkeypatch.setattr(
        terminal_mergetool,
        "_prompt_pair_transaction_resolution",
        lambda *args, **kwargs: terminal_ui.PairTransactionResolution(
            action="replace",
            ours=ours[0],
            theirs=None,
            changed_theirs=True,
        ),
    )

    with control_db._open_merge_working_context(
        base,
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
        assert terminal_mergetool._check_accept_current(
            "ours",
            remaining_ours,
            remaining_theirs,
            metadata_indexes,
            context,
            table_columns,
        )

    assert checked_transaction_ids == [2, 3, 4]
    assert not remaining_ours
    assert [transaction.transaction_id for transaction in remaining_theirs] == [2, 4]


def test_check_accept_current_fast_forwards_after_accepting_reviewable_pair(
    tmp_path,
    monkeypatch,
):
    base = tmp_path / "base.db"
    with closing(sqlite3.connect(base)) as con:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        con.executemany(
            "INSERT INTO users (id, name) VALUES (?, ?)",
            [(1, "base one"), (2, "base two")],
        )
        create_log_tables(con)
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(base)
    )
    ours = txs([
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            log_id=1,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE users SET name = 'local' WHERE id = 1",
            table_columns=table_columns,
        )
    ])
    theirs = txs([
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=0,
            log_id=2,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE users SET name = 'remote' WHERE id = 1",
            table_columns=table_columns,
        ),
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=1,
            log_id=3,
            transaction_id=3,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE users SET name = 'remote two' WHERE id = 2",
            table_columns=table_columns,
        ),
    ])
    remaining_ours = deque(ours)
    remaining_theirs = deque(theirs)

    checked_transaction_ids: list[int] = []
    original_check_next = remaining_execution.OrderedRemainingExecutionScanner.check_next

    def recording_check_next(self, ours_transaction, theirs_transaction, static_result):
        other_transaction = (
            theirs_transaction
            if self.current_branch == "ours"
            else ours_transaction
        )
        checked_transaction_ids.append(other_transaction.transaction_id)
        return original_check_next(
            self,
            ours_transaction,
            theirs_transaction,
            static_result,
        )

    monkeypatch.setattr(
        remaining_execution.OrderedRemainingExecutionScanner,
        "check_next",
        recording_check_next,
    )
    monkeypatch.setattr(
        terminal_mergetool,
        "_prompt_pair_transaction_resolution",
        lambda *args, **kwargs: terminal_ui.PairTransactionResolution(
            action="accept",
            ours=ours[0],
            theirs=theirs[0],
        ),
    )

    with control_db._open_merge_working_context(
        base,
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
        assert terminal_mergetool._check_accept_current(
            "ours",
            remaining_ours,
            remaining_theirs,
            metadata_indexes,
            context,
            table_columns,
        )

    assert checked_transaction_ids == [2, 3]
    assert not remaining_ours
    assert [transaction.transaction_id for transaction in remaining_theirs] == [2, 3]


def test_write_working_result_backs_up_accepted_database(tmp_path):
    base = tmp_path / "base.db"
    merged = tmp_path / "merged.db"
    with closing(sqlite3.connect(base)) as con:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        create_log_tables(con)
        con.commit()

    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata_from_db(base)
    )

    with control_db._open_merge_working_context(
        base,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) as context:
        context.base_cursor.execute(
            "INSERT INTO users (id, name) VALUES (1, 'merged')"
        )
        terminal_mergetool._write_working_result(context, merged)

    with closing(sqlite3.connect(merged)) as con:
        assert con.execute("SELECT id, name FROM users").fetchall() == [
            (1, "merged")
        ]
