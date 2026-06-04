import sqlite3
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from merge import cascade_metadata, log_merge, static_analysis
from merge.models import BranchName
from merge.remaining_metadata import (
    RemainingMetadataIndex,
    remaining_individual_check_kinds,
)
from merge.utils import ALL_COLUMNS


def schema_cache(table_columns, primary_key_columns=None, key_column_sets=None):
    return log_merge.SchemaCache(
        table_columns=table_columns,
        primary_key_columns=primary_key_columns or {},
        key_column_sets=key_column_sets or {},
    )


def make_context(table_columns, schema=()):
    con = sqlite3.connect(":memory:")
    for sql_text in schema:
        con.execute(sql_text)
    return log_merge.ConflictCheckContext(
        base_cursor=con.cursor(),
        base_db_path=":memory:",
        schema_cache=schema_cache(table_columns),
    )


def make_statement(
    sql_text,
    table_columns,
    branch: BranchName = "ours",
    index=0,
    context=None,
):
    return log_merge.make_logged_statement(
        branch=branch,
        branch_index=index,
        transaction_id=index + 1,
        committed_at="2026-01-01T00:00:00",
        sql_text=sql_text,
        table_columns=table_columns,
        metadata_context=context,
    )


def conflict_kinds(result):
    return [conflict.kind for conflict in result.conflicts]


def static_match(context, ours, theirs, *, current_branch=None):
    return static_analysis.static_analysis_matching(
        context,
        log_merge.group_logged_transactions([ours])[0].metadata,
        log_merge.group_logged_transactions([theirs])[0].metadata,
        current_branch=current_branch,
    )


def transaction(statement):
    return log_merge.group_logged_transactions([statement])[0]


def make_cascade_context():
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(
        """
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
            status TEXT
        );
        CREATE TABLE order_items (
            id INTEGER PRIMARY KEY,
            order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
            sku TEXT
        );
        CREATE TABLE order_summary (
            id INTEGER PRIMARY KEY,
            item_count INTEGER
        );
        """
    )
    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata(con.cursor())
    )
    return log_merge.ConflictCheckContext(
        base_cursor=con.cursor(),
        base_db_path=":memory:",
        schema_cache=schema_cache(
            table_columns,
            primary_key_columns,
            key_column_sets,
        ),
    )


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

    result = static_match(context, ours, theirs)

    assert conflict_kinds(result) == ["write_write"]


def test_foreign_key_edges_skip_mismatched_parent_pk_shorthand():
    with sqlite3.connect(":memory:") as con:
        con.execute("CREATE TABLE parent (a INTEGER, b INTEGER, PRIMARY KEY (a, b))")
        con.execute("CREATE TABLE child (x INTEGER REFERENCES parent)")
        table_columns, primary_key_columns, key_column_sets = (
            log_merge.load_schema_metadata(con.cursor())
        )
        context = log_merge.ConflictCheckContext(
            base_cursor=con.cursor(),
            base_db_path=":memory:",
            schema_cache=schema_cache(
                table_columns,
                primary_key_columns,
                key_column_sets,
            ),
        )

        assert cascade_metadata.foreign_key_edges(context) == ()


def test_cascade_effects_include_recursive_hidden_reads_and_writes():
    context = make_cascade_context()

    effects = cascade_metadata.cascade_effects_for_parsed_statement(
        context,
        statement_kind="delete",
        table_updated="accounts",
        columns_updated={ALL_COLUMNS},
    )

    assert effects.tables_referenced_to_columns_referenced == {
        "orders": {"account_id"},
        "order_items": {"order_id"},
    }
    assert effects.tables_updated_to_columns_updated == {
        "orders": {ALL_COLUMNS},
        "order_items": {ALL_COLUMNS},
    }


def test_cascade_metadata_is_stored_on_statement_and_transaction_metadata():
    context = make_cascade_context()
    statement = make_statement(
        "DELETE FROM accounts WHERE id = 1",
        context.table_columns,
        context=context,
    )
    transaction = log_merge.group_logged_transactions([statement])[0]

    assert statement.metadata.has_cascade_effects
    assert statement.metadata.tables_updated_to_columns_updated == {
        "accounts": {ALL_COLUMNS},
        "orders": {ALL_COLUMNS},
        "order_items": {ALL_COLUMNS},
    }
    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "accounts": {"id"},
        "orders": {"account_id"},
        "order_items": {"order_id"},
    }
    assert transaction.metadata.has_cascade_effects
    assert transaction.metadata.tables_updated_to_columns_updated == (
        statement.metadata.tables_updated_to_columns_updated
    )


def test_cascade_effects_model_update_actions_and_restrict_reads():
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(
        """
        CREATE TABLE parents (
            id INTEGER PRIMARY KEY
        );
        CREATE TABLE nullable_children (
            id INTEGER PRIMARY KEY,
            parent_id INTEGER REFERENCES parents(id) ON UPDATE SET NULL
        );
        CREATE TABLE restricted_children (
            id INTEGER PRIMARY KEY,
            parent_id INTEGER REFERENCES parents(id) ON DELETE RESTRICT
        );
        """
    )
    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata(con.cursor())
    )
    context = log_merge.ConflictCheckContext(
        base_cursor=con.cursor(),
        base_db_path=":memory:",
        schema_cache=schema_cache(
            table_columns,
            primary_key_columns,
            key_column_sets,
        ),
    )

    update_effects = cascade_metadata.cascade_effects_for_parsed_statement(
        context,
        statement_kind="update",
        table_updated="parents",
        columns_updated={"id"},
    )
    delete_effects = cascade_metadata.cascade_effects_for_parsed_statement(
        context,
        statement_kind="delete",
        table_updated="parents",
        columns_updated={ALL_COLUMNS},
    )

    assert update_effects.tables_referenced_to_columns_referenced == {
        "nullable_children": {"parent_id"},
        "restricted_children": {"parent_id"},
    }
    assert update_effects.tables_updated_to_columns_updated == {
        "nullable_children": {"parent_id"},
    }
    assert delete_effects.tables_referenced_to_columns_referenced == {
        "nullable_children": {"parent_id"},
        "restricted_children": {"parent_id"},
    }
    assert delete_effects.tables_updated_to_columns_updated == {}


def test_static_analysis_flags_cascade_write_read_overlap():
    context = make_cascade_context()
    ours = make_statement(
        "DELETE FROM accounts WHERE id = 1",
        context.table_columns,
        branch="ours",
        context=context,
    )
    theirs = make_statement(
        "UPDATE order_summary "
        "SET item_count = (SELECT COUNT(*) FROM order_items)",
        context.table_columns,
        branch="theirs",
        context=context,
    )

    result = static_match(context, ours, theirs, current_branch="ours")

    assert conflict_kinds(result) == ["write_read"]
    assert ("metadata_source", "cascade") in result.conflicts[0].details


def test_remaining_metadata_flags_fk_integrity_from_current_cascade_update():
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(
        """
        CREATE TABLE parents (
            id INTEGER PRIMARY KEY
        );
        CREATE TABLE children (
            id INTEGER PRIMARY KEY,
            parent_id INTEGER DEFAULT 0
                REFERENCES parents(id) ON DELETE SET DEFAULT
        );
        """
    )
    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata(con.cursor())
    )
    context = log_merge.ConflictCheckContext(
        base_cursor=con.cursor(),
        base_db_path=":memory:",
        schema_cache=schema_cache(
            table_columns,
            primary_key_columns,
            key_column_sets,
        ),
    )
    current = transaction(
        make_statement(
            "DELETE FROM parents WHERE id = 1",
            context.table_columns,
            context=context,
        )
    )
    remaining = transaction(
        make_statement(
            "DELETE FROM parents WHERE id = 0",
            context.table_columns,
            branch="theirs",
            context=context,
        )
    )
    remaining_index = RemainingMetadataIndex.from_transactions(context, [remaining])

    needed_kinds = remaining_individual_check_kinds(
        context,
        current,
        remaining_index,
    )

    assert "integrity" in needed_kinds


def test_remaining_metadata_flags_unique_key_from_current_cascade_update():
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(
        """
        CREATE TABLE parents (
            id INTEGER PRIMARY KEY
        );
        CREATE TABLE children (
            id INTEGER PRIMARY KEY,
            parent_id INTEGER UNIQUE
                REFERENCES parents(id) ON DELETE SET DEFAULT
        );
        """
    )
    table_columns, primary_key_columns, key_column_sets = (
        log_merge.load_schema_metadata(con.cursor())
    )
    context = log_merge.ConflictCheckContext(
        base_cursor=con.cursor(),
        base_db_path=":memory:",
        schema_cache=schema_cache(
            table_columns,
            primary_key_columns,
            key_column_sets,
        ),
    )
    current = transaction(
        make_statement(
            "DELETE FROM parents WHERE id = 1",
            context.table_columns,
            context=context,
        )
    )
    remaining = transaction(
        make_statement(
            "UPDATE children SET parent_id = 0 WHERE id = 2",
            context.table_columns,
            branch="theirs",
            context=context,
        )
    )
    remaining_index = RemainingMetadataIndex.from_transactions(context, [remaining])

    needed_kinds = remaining_individual_check_kinds(
        context,
        current,
        remaining_index,
    )

    assert "integrity" in needed_kinds


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

    result = static_match(context, ours, theirs)

    assert not result.has_conflict


def test_static_analysis_flags_write_read():
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

    result = static_match(context, ours, theirs)

    assert conflict_kinds(result) == ["write_read"]
    assert result.conflicts[0].details == (("writer", "ours"), ("reader", "theirs"))


def test_static_analysis_can_limit_write_read_to_one_direction():
    table_columns = {
        "products": {"id", "discount", "name"},
    }
    context = make_context(table_columns)
    ours = make_statement(
        "UPDATE products SET discount = (SELECT name FROM products WHERE id = 1)",
        table_columns,
        branch="ours",
    )
    theirs = make_statement(
        "UPDATE products SET name = (SELECT discount FROM products WHERE id = 1)",
        table_columns,
        branch="theirs",
    )

    both = static_match(context, ours, theirs)
    ours_current = static_match(context, ours, theirs, current_branch="ours")
    theirs_current = static_match(context, ours, theirs, current_branch="theirs")

    assert conflict_kinds(both) == ["write_read", "write_read"]
    assert conflict_kinds(ours_current) == ["write_read"]
    assert ours_current.conflicts[-1].details == (
        ("writer", "ours"),
        ("reader", "theirs"),
    )
    assert conflict_kinds(theirs_current) == ["write_read"]
    assert theirs_current.conflicts[-1].details == (
        ("writer", "theirs"),
        ("reader", "ours"),
    )


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

    result = static_match(context, ours, theirs)

    assert conflict_kinds(result) == ["write_read"]
    assert result.conflicts[0].details == (("writer", "ours"), ("reader", "theirs"))


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

    result = static_match(context, ours, theirs)

    assert conflict_kinds(result) == ["write_read", "write_read"]


def test_static_analysis_implicit_key_insert_allows_explicit_other_insert():
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

    result = static_match(context, ours, theirs)

    assert not result.has_conflict


def test_static_analysis_implicit_key_insert_conflicts_with_other_implicit_insert():
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
        "INSERT INTO products(name) VALUES ('Gadget')",
        table_columns,
        branch="theirs",
    )

    result = static_match(context, ours, theirs)

    assert conflict_kinds(result) == ["implicit_insert_key"]
    assert "ours INSERT omits products.id" in result.conflicts[0].message


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

    result = static_match(context, ours, theirs)

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

    result = static_match(context, ours, theirs)

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

    result = static_match(context, ours, theirs)

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

    result = static_match(context, ours, theirs)

    assert conflict_kinds(result) == ["write_read"]


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

    result = static_match(context, ours, theirs)

    assert not result.has_conflict


def test_static_analysis_unique_key_omission_does_not_trigger_implicit_rowid():
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

    result = static_match(context, ours, theirs)

    assert not result.has_conflict


def test_static_analysis_ignores_insert_omitting_current_timestamp_default():
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

    result = static_match(context, ours, theirs)

    assert not result.has_conflict


def test_static_analysis_ignores_insert_default_values_with_nondeterministic_default():
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

    result = static_match(context, ours, theirs)

    assert not result.has_conflict


def test_static_analysis_ignores_insert_omitting_random_default():
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

    result = static_match(context, ours, theirs)

    assert not result.has_conflict


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

    result = static_match(context, ours, theirs)

    assert not result.has_conflict


def test_static_analysis_ignores_ambiguous_unqualified_read():
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

    result = static_match(context, ours, theirs)

    assert not result.has_conflict
