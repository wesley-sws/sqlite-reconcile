import sqlite3
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from merge import log_merge, static_analysis


def make_context(table_columns, schema=()):
    con = sqlite3.connect(":memory:")
    for sql_text in schema:
        con.execute(sql_text)
    return log_merge.ConflictCheckContext(
        base_cursor=con.cursor(),
        base_db_path=":memory:",
        table_columns=table_columns,
    )


def make_statement(sql_text, table_columns, branch="ours", index=0):
    return log_merge.make_logged_statement(
        branch=branch,
        branch_index=index,
        log_id=index + 1,
        transaction_id=index + 1,
        committed_at="2026-01-01T00:00:00",
        sql_text=sql_text,
        table_columns=table_columns,
    )


def conflict_kinds(result):
    return [conflict.kind for conflict in result.conflicts]


def test_static_analysis_flags_update_same_column_write_write():
    table_columns = {
        "products": {"id", "discount", "name"},
    }
    context = make_context(table_columns)
    ours = make_statement(
        "UPDATE products SET discount = 10 WHERE id = 1",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "UPDATE products SET discount = 9 WHERE id = 2",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert conflict_kinds(result) == ["write_write"]
    assert "products.discount" in result.conflicts[0].message


def test_static_analysis_allows_update_different_columns():
    table_columns = {
        "products": {"id", "discount", "name"},
    }
    context = make_context(table_columns)
    ours = make_statement(
        "UPDATE products SET discount = 10 WHERE id = 1",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "UPDATE products SET name = 'Widget' WHERE id = 2",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert not result.has_conflict


def test_static_analysis_flags_write_read_in_both_directions():
    table_columns = {
        "products": {"id", "discount"},
        "product_statistics": {"id", "average_discount"},
    }
    context = make_context(table_columns)
    ours = make_statement(
        "UPDATE products SET discount = 10 WHERE id = 1",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "UPDATE product_statistics "
        "SET average_discount = (SELECT AVG(discount) FROM products)",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert conflict_kinds(result) == ["write_read"]
    assert "ours writes products.discount" in result.conflicts[0].message


def test_static_analysis_treats_insert_as_writing_all_columns_for_write_read():
    table_columns = {
        "products": {"id", "discount", "name"},
    }
    context = make_context(table_columns)
    ours = make_statement(
        "INSERT INTO products(id, name) VALUES (1, 'Widget')",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "UPDATE products SET discount = 10 WHERE id = 1",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert conflict_kinds(result) == ["write_read"]
    assert "ours writes products.id" in result.conflicts[0].message


def test_static_analysis_delete_delete_can_still_report_write_read():
    table_columns = {
        "products": {"id", "discount", "name"},
    }
    context = make_context(table_columns)
    ours = make_statement(
        "DELETE FROM products WHERE id = 1",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "DELETE FROM products WHERE id = 2",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert conflict_kinds(result) == ["write_read", "write_read"]


def test_static_analysis_implicit_key_insert_conflicts_with_other_insert():
    table_columns = {
        "products": {"id", "sku", "name"},
    }
    context = make_context(
        table_columns,
        schema=[
            "CREATE TABLE products ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "sku TEXT UNIQUE, "
            "name TEXT)"
        ],
    )
    ours = make_statement(
        "INSERT INTO products(name) VALUES ('Widget')",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "INSERT INTO products(id, name) VALUES (1, 'Gadget')",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert conflict_kinds(result) == ["implicit_insert_key"]
    assert "ours INSERT omits explicit key values" in result.conflicts[0].message


def test_static_analysis_unique_insert_still_conflicts_when_pk_is_omitted():
    table_columns = {
        "products": {"id", "sku", "name"},
    }
    context = make_context(
        table_columns,
        schema=[
            "CREATE TABLE products ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "sku TEXT UNIQUE, "
            "name TEXT)"
        ],
    )
    ours = make_statement(
        "INSERT INTO products(sku, name) VALUES ('A-1', 'Widget')",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "INSERT INTO products(id, name) VALUES (1, 'Gadget')",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert conflict_kinds(result) == ["implicit_insert_key"]
    assert "ours INSERT omits explicit key values" in result.conflicts[0].message


def test_static_analysis_all_key_sets_explicit_does_not_trigger_implicit_key():
    table_columns = {
        "products": {"id", "sku", "name"},
    }
    context = make_context(
        table_columns,
        schema=[
            "CREATE TABLE products ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "sku TEXT UNIQUE, "
            "name TEXT)"
        ],
    )
    ours = make_statement(
        "INSERT INTO products(id, sku, name) VALUES (2, 'A-1', 'Widget')",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "INSERT INTO products(id, sku, name) VALUES (1, 'B-1', 'Gadget')",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert not result.has_conflict


def test_static_analysis_implicit_key_insert_conflicts_with_key_update():
    table_columns = {
        "products": {"id", "name"},
    }
    context = make_context(
        table_columns,
        schema=[
            "CREATE TABLE products ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT)"
        ],
    )
    ours = make_statement(
        "INSERT INTO products(name) VALUES ('Widget')",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "UPDATE products SET id = 5 WHERE name = 'Widget'",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert "implicit_insert_key" in conflict_kinds(result)


def test_static_analysis_implicit_key_insert_allows_non_key_update():
    table_columns = {
        "products": {"id", "name"},
    }
    context = make_context(
        table_columns,
        schema=[
            "CREATE TABLE products ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT)"
        ],
    )
    ours = make_statement(
        "INSERT INTO products(name) VALUES ('Widget')",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "UPDATE products SET name = 'Gadget'",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert not result.has_conflict


def test_static_analysis_implicit_key_insert_conflicts_with_key_read():
    table_columns = {
        "products": {"id", "name"},
    }
    context = make_context(
        table_columns,
        schema=[
            "CREATE TABLE products ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT)"
        ],
    )
    ours = make_statement(
        "INSERT INTO products(name) VALUES ('Widget')",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "UPDATE products SET name = 'Gadget' WHERE id = 1",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert conflict_kinds(result) == ["implicit_insert_key", "write_read"]
    assert "references or writes id" in result.conflicts[0].message


def test_static_analysis_implicit_key_dml_uses_omitted_key_columns_only():
    table_columns = {
        "memberships": {"team_id", "user_id", "note"},
    }
    context = make_context(
        table_columns,
        schema=[
            "CREATE TABLE memberships ("
            "team_id INTEGER, "
            "user_id INTEGER, "
            "note TEXT, "
            "UNIQUE(team_id, user_id))"
        ],
    )
    ours = make_statement(
        "INSERT INTO memberships(team_id, note) VALUES (1, 'new')",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "UPDATE memberships SET team_id = 2",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert not result.has_conflict


def test_static_analysis_implicit_key_dml_conflicts_on_omitted_key_column():
    table_columns = {
        "memberships": {"team_id", "user_id", "note"},
    }
    context = make_context(
        table_columns,
        schema=[
            "CREATE TABLE memberships ("
            "team_id INTEGER, "
            "user_id INTEGER, "
            "note TEXT, "
            "UNIQUE(team_id, user_id))"
        ],
    )
    ours = make_statement(
        "INSERT INTO memberships(team_id, note) VALUES (1, 'new')",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "UPDATE memberships SET user_id = 2",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert conflict_kinds(result) == ["implicit_insert_key"]
    assert "user_id" in result.conflicts[0].message


def test_static_analysis_blocks_insert_omitting_current_timestamp_default():
    table_columns = {
        "products": {"id", "name", "created_at"},
        "audit": {"id", "value"},
    }
    context = make_context(
        table_columns,
        schema=[
            "CREATE TABLE products ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT, "
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE audit (id INTEGER PRIMARY KEY, value INTEGER)",
        ],
    )
    ours = make_statement(
        "INSERT INTO products(id, name) VALUES (1, 'Widget')",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "UPDATE audit SET value = 1 WHERE id = 1",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert conflict_kinds(result) == ["implicit_insert_default"]
    assert "created_at" in result.conflicts[0].message


def test_static_analysis_blocks_insert_default_values_with_nondeterministic_default():
    table_columns = {
        "products": {"id", "created_at"},
        "audit": {"id", "value"},
    }
    context = make_context(
        table_columns,
        schema=[
            "CREATE TABLE products ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE audit (id INTEGER PRIMARY KEY, value INTEGER)",
        ],
    )
    ours = make_statement(
        "INSERT INTO products DEFAULT VALUES",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "UPDATE audit SET value = 1 WHERE id = 1",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert conflict_kinds(result) == ["implicit_insert_default"]
    assert "created_at" in result.conflicts[0].message


def test_static_analysis_blocks_insert_omitting_random_default():
    table_columns = {
        "products": {"id", "token"},
        "audit": {"id", "value"},
    }
    context = make_context(
        table_columns,
        schema=[
            "CREATE TABLE products ("
            "id INTEGER PRIMARY KEY, "
            "token INTEGER DEFAULT (random()))",
            "CREATE TABLE audit (id INTEGER PRIMARY KEY, value INTEGER)",
        ],
    )
    ours = make_statement(
        "INSERT INTO products(id) VALUES (1)",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "UPDATE audit SET value = 1 WHERE id = 1",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert conflict_kinds(result) == ["implicit_insert_default"]
    assert "token" in result.conflicts[0].message


def test_static_analysis_allows_explicit_or_deterministic_insert_defaults():
    table_columns = {
        "products": {"id", "name", "created_at", "status"},
        "audit": {"id", "value"},
    }
    context = make_context(
        table_columns,
        schema=[
            "CREATE TABLE products ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT, "
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP, "
            "status TEXT DEFAULT 'new')",
            "CREATE TABLE audit (id INTEGER PRIMARY KEY, value INTEGER)",
        ],
    )
    ours = make_statement(
        "INSERT INTO products(id, name, created_at) "
        "VALUES (1, 'Widget', '2026-01-01T00:00:00')",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "UPDATE audit SET value = 1 WHERE id = 1",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert not result.has_conflict


def test_static_analysis_flags_unqualified_read_overlap_conservatively():
    table_columns = {
        "products": {"id", "discount"},
        "archived_products": {"id", "discount"},
        "audit": {"value"},
    }
    context = make_context(table_columns)
    ours = make_statement(
        "UPDATE products SET discount = 10 WHERE id = 1",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "UPDATE audit "
        "SET value = ("
        "  SELECT discount "
        "  FROM products JOIN archived_products ON products.id = archived_products.id"
        ")",
        table_columns,
        branch="theirs",
    )

    result = static_analysis.static_analysis_matching(context, ours, theirs)

    assert conflict_kinds(result) == ["write_read"]
    assert "unresolved reads" in result.conflicts[0].message
