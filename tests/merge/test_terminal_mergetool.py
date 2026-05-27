import sqlite3
import sys
from contextlib import closing
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from merge import log_merge, terminal_mergetool, terminal_ui


def txs(statements):
    return log_merge.group_logged_transactions(statements)


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


def test_edit_sql_inline_reads_prefilled_replacement(monkeypatch):
    monkeypatch.setattr(
        terminal_ui,
        "_input_with_prefill",
        lambda prompt, initial: "UPDATE users SET name = 'fixed' WHERE id = 1",
    )

    assert terminal_ui._edit_sql_inline("L1", "old sql") == (
        "UPDATE users SET name = 'fixed' WHERE id = 1"
    )


def test_edit_sql_inline_keeps_unchanged_prefilled_sql(monkeypatch):
    monkeypatch.setattr(
        terminal_ui,
        "_input_with_prefill",
        lambda prompt, initial: initial,
    )

    assert terminal_ui._edit_sql_inline("L1", "SELECT 1") is None


def test_prompt_replacement_enter_skips_before_edit(monkeypatch, capsys):
    statement = log_merge.make_logged_statement(
        branch="theirs",
        branch_index=2,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text="UPDATE products SET price = price - 50 WHERE id = 75",
    )
    monkeypatch.setattr("builtins.input", lambda _: "")

    replacement = terminal_ui._prompt_replacement(statement, {})

    assert replacement is None
    assert "R3: UPDATE products SET price = price - 50 WHERE id = 75" in (
        capsys.readouterr().out
    )


def test_prompt_pair_resolution_accepts_order_without_replacement_prompts(
    monkeypatch,
    capsys,
):
    table_columns = {"products": {"id", "price"}}
    ours = [
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=2,
            log_id=1,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE products SET price = price + 100 WHERE id = 75",
            table_columns=table_columns,
        )
    ]
    theirs = [
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=2,
            log_id=2,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE products SET price = price - 50 WHERE id = 75",
            table_columns=table_columns,
        )
    ]
    conflict = log_merge.ConflictPair(
        ours_index=0,
        theirs_index=0,
        ours_sql=ours[0].original_sql_text,
        theirs_sql=theirs[0].original_sql_text,
        conflicts=(
            log_merge.StatementConflict(
                kind="write_write",
                message="write-write row overlap",
            ),
        ),
    )
    monkeypatch.setattr("builtins.input", lambda _: "R3;")

    resolution = terminal_ui._prompt_pair_resolution(
        conflict,
        txs(ours),
        txs(theirs),
        table_columns,
    )

    assert list(log_merge.flatten_transactions(resolution)) == [theirs[0]]
    output = capsys.readouterr().out
    assert "R3 replacement SQL" not in output
    assert "Use 'edit L3;' or 'edit R3;'" in output


def test_prompt_pair_resolution_edits_statement_before_order(monkeypatch):
    table_columns = {"products": {"id", "price"}}
    ours = [
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=2,
            log_id=1,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE products SET price = price + 100 WHERE id = 75",
            table_columns=table_columns,
        )
    ]
    theirs = [
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=2,
            log_id=2,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE products SET price = price - 50 WHERE id = 75",
            table_columns=table_columns,
        )
    ]
    conflict = log_merge.ConflictPair(
        ours_index=0,
        theirs_index=0,
        ours_sql=ours[0].original_sql_text,
        theirs_sql=theirs[0].original_sql_text,
        conflicts=(
            log_merge.StatementConflict(
                kind="write_write",
                message="write-write row overlap",
            ),
        ),
    )
    responses = iter([":edit L3", "L3; R3;"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    monkeypatch.setattr(
        terminal_ui,
        "_edit_sql_in_editor",
        lambda label, sql: "UPDATE products SET price = price + 20 WHERE id = 75",
    )

    resolution = terminal_ui._prompt_pair_resolution(
        conflict,
        txs(ours),
        txs(theirs),
        table_columns,
    )

    assert [
        statement.sql_text
        for statement in log_merge.flatten_transactions(resolution)
    ] == [
        "UPDATE products SET price = price + 20 WHERE id = 75",
        theirs[0].sql_text,
    ]


def test_prompt_pair_resolution_empty_edit_deletes_statement(monkeypatch):
    table_columns = {"products": {"id", "price"}}
    ours = [
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=2,
            log_id=1,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE products SET price = price + 100 WHERE id = 75",
            table_columns=table_columns,
        ),
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=3,
            log_id=2,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE products SET price = price + 10 WHERE id = 76",
            table_columns=table_columns,
        ),
    ]
    theirs = [
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=2,
            log_id=3,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE products SET price = price - 50 WHERE id = 75",
            table_columns=table_columns,
        )
    ]
    conflict = log_merge.ConflictPair(
        ours_index=0,
        theirs_index=0,
        ours_sql="",
        theirs_sql="",
        conflicts=(
            log_merge.StatementConflict(
                kind="write_write",
                message="write-write row overlap",
            ),
        ),
    )
    responses = iter(["edit L4;", "L3-L4; R3;"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    monkeypatch.setattr(terminal_ui, "_edit_sql_in_editor", lambda label, sql: "")

    resolution = terminal_ui._prompt_pair_resolution(
        conflict,
        txs(ours),
        txs(theirs),
        table_columns,
    )

    assert [
        statement.sql_text
        for statement in log_merge.flatten_transactions(resolution)
    ] == [
        "UPDATE products SET price = price + 100 WHERE id = 75",
        theirs[0].sql_text,
    ]


def test_prompt_pair_resolution_inserts_statement_after_label(monkeypatch):
    table_columns = {"products": {"id", "price"}}
    ours = [
        log_merge.make_logged_statement(
            branch="ours",
            branch_index=2,
            log_id=1,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE products SET price = price + 100 WHERE id = 75",
            table_columns=table_columns,
        )
    ]
    theirs = [
        log_merge.make_logged_statement(
            branch="theirs",
            branch_index=2,
            log_id=2,
            transaction_id=2,
            committed_at="2026-01-01T00:00:00",
            sql_text="UPDATE products SET price = price - 50 WHERE id = 75",
            table_columns=table_columns,
        )
    ]
    conflict = log_merge.ConflictPair(
        ours_index=0,
        theirs_index=0,
        ours_sql="",
        theirs_sql="",
        conflicts=(
            log_merge.StatementConflict(
                kind="write_write",
                message="write-write row overlap",
            ),
        ),
    )
    inserted_sql = "UPDATE products SET price = price + 5 WHERE id = 76"
    responses = iter(["insert after L3;", "L3;"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    monkeypatch.setattr(
        terminal_ui,
        "_edit_sql_in_editor",
        lambda label, sql: inserted_sql,
    )

    resolution = terminal_ui._prompt_pair_resolution(
        conflict,
        txs(ours),
        txs(theirs),
        table_columns,
    )

    assert [
        statement.sql_text
        for statement in log_merge.flatten_transactions(resolution)
    ] == [
        ours[0].sql_text,
        inserted_sql,
    ]


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
    monkeypatch.setattr(
        terminal_ui,
        "_edit_sql_inline",
        lambda label, sql: (_ for _ in ()).throw(AssertionError("inline editor used")),
    )

    replacement, should_restart = terminal_ui._prompt_replay_warning(
        statement,
        table_columns,
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

    replacement, should_restart = terminal_ui._prompt_replay_warning(
        statement,
        table_columns,
        "nondeterministic expression cannot be safely materialized",
    )

    assert replacement is None
    assert should_restart


def test_prompt_standalone_resolution_prints_statement_once(monkeypatch, capsys):
    statement = log_merge.make_logged_statement(
        branch="theirs",
        branch_index=3,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text="INSERT INTO coupons (id, code, discount) VALUES (902, 'SPRING-DEMO', 25)",
    )
    conflict = log_merge.ConflictPair(
        ours_index=0,
        theirs_index=0,
        ours_sql="",
        theirs_sql=statement.original_sql_text,
        conflicts=(
            log_merge.StatementConflict(
                kind="integrity",
                message="UNIQUE constraint failed: coupons.code",
                scope="theirs",
            ),
        ),
    )
    monkeypatch.setattr("builtins.input", lambda _: "")

    terminal_ui._prompt_standalone_resolution(
        conflict,
        "theirs",
        [],
        txs([statement]),
        {},
    )

    output = capsys.readouterr().out
    assert output.count("INSERT INTO coupons") == 1


def test_prompt_standalone_resolution_edits_recorded_statement_in_transaction(
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
    conflict = log_merge.ConflictPair(
        ours_index=0,
        theirs_index=0,
        ours_sql="",
        theirs_sql="",
        conflicts=(
            log_merge.StatementConflict(
                kind="integrity",
                message="UNIQUE constraint failed: coupons.code",
                scope="theirs",
                details=(("statement_log_id", "12"),),
            ),
        ),
    )
    monkeypatch.setattr(
        terminal_ui,
        "_edit_sql_in_editor",
        lambda label, sql: "INSERT INTO coupons (id, code) VALUES (2, 'FIXED')",
    )
    responses = iter(["edit R5;", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))

    replacement = terminal_ui._prompt_standalone_resolution(
        conflict,
        "theirs",
        [],
        txs([first, failing]),
        {},
    )

    assert replacement is not None
    assert [statement.sql_text for statement in replacement] == [
        "INSERT INTO coupons (id, code) VALUES (1, 'OK')",
        "INSERT INTO coupons (id, code) VALUES (2, 'FIXED')",
    ]


def test_replace_remaining_standalone_keeps_edited_transaction_for_recheck():
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
    conflict = log_merge.ConflictPair(
        ours_index=0,
        theirs_index=0,
        ours_sql="",
        theirs_sql="",
        conflicts=(
            log_merge.StatementConflict(
                kind="integrity",
                message="UNIQUE constraint failed: coupons.code",
                scope="ours",
            ),
        ),
    )

    remaining_ours, remaining_theirs = terminal_mergetool._replace_remaining_standalone(
        conflict,
        "ours",
        [replacement],
        txs(ours),
        txs(theirs),
        frontier_ours_count=0,
        frontier_theirs_count=0,
    )

    assert [transaction.statements[0].sql_text for transaction in remaining_ours] == [
        replacement.sql_text,
        ours[1].sql_text,
    ]
    assert [transaction.statements[0] for transaction in remaining_theirs] == theirs


def test_branch_replay_safety_reruns_later_statements_after_edit(tmp_path, monkeypatch):
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
    responses = iter(["", "INSERT INTO parents(id) VALUES (1)", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))

    resolved = terminal_mergetool._resolve_branch_replay_safety(
        base,
        "ours",
        statements,
        table_columns,
        primary_key_columns,
        key_column_sets,
    )

    assert [statement.sql_text for statement in resolved] == [
        "INSERT INTO parents(id) VALUES (1)"
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
        [statement],
        table_columns,
        primary_key_columns,
        key_column_sets,
    )

    assert resolved == [statement]
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
        [statement],
        table_columns,
        primary_key_columns,
        key_column_sets,
    )

    assert len(resolved) == 1
    assert resolved[0].is_replay_safe
    assert resolved[0].replay_warnings == (
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
        [statement],
        table_columns,
        primary_key_columns,
        key_column_sets,
    )

    assert resolved == [statement]
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
        [statement],
        table_columns,
        primary_key_columns,
        key_column_sets,
    )

    assert resolved == [statement]

