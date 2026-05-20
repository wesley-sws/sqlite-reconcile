import sqlite3
import sys
from contextlib import closing
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from merge import execution_based_analysis, log_merge, static_analysis


def init_base_db(tmp_path, statements):
    path = tmp_path / "base.db"
    with closing(sqlite3.connect(path)) as con:
        for statement in statements:
            con.execute(statement)
        con.commit()
    return path


def make_context(path, table_columns):
    con = sqlite3.connect(path)
    return con, log_merge.ConflictCheckContext(
        base_cursor=con.cursor(),
        base_db_path=path,
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


def static_result(context, ours, theirs):
    return static_analysis.static_analysis_matching(context, ours, theirs)


def test_commutativity_check_reports_sqldiff_difference(tmp_path):
    table_columns = {"counters": {"id", "value"}}
    base_path = init_base_db(
        tmp_path,
        [
            "CREATE TABLE counters (id INTEGER PRIMARY KEY, value INTEGER)",
            "INSERT INTO counters VALUES (1, 1)",
        ],
    )
    con, context = make_context(base_path, table_columns)
    with closing(con):
        ours = make_statement(
            "UPDATE counters SET value = value + 1 WHERE id = 1",
            table_columns,
            branch="ours",
        )
        theirs = make_statement(
            "UPDATE counters SET value = value * 2 WHERE id = 1",
            table_columns,
            branch="theirs",
        )

        result = execution_based_analysis.commutativity_check(context, ours, theirs)

    assert conflict_kinds(result) == ["non_commutative"]
    assert result.conflicts[0].message == "commutativity check"


def test_execution_write_write_allows_disjoint_update_rows(tmp_path):
    table_columns = {"products": {"id", "discount", "name"}}
    base_path = init_base_db(
        tmp_path,
        [
            "CREATE TABLE products (id INTEGER PRIMARY KEY, discount INTEGER, name TEXT)",
            "INSERT INTO products VALUES (1, 0, 'A')",
            "INSERT INTO products VALUES (2, 0, 'B')",
        ],
    )
    con, context = make_context(base_path, table_columns)
    with closing(con):
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

        result = execution_based_analysis.execution_based_matching(
            context,
            ours,
            theirs,
            static_result(context, ours, theirs),
        )

    assert not result.has_conflict


def test_execution_write_write_reports_overlapping_update_rows(tmp_path):
    table_columns = {"products": {"id", "discount", "name"}}
    base_path = init_base_db(
        tmp_path,
        [
            "CREATE TABLE products (id INTEGER PRIMARY KEY, discount INTEGER, name TEXT)",
            "INSERT INTO products VALUES (1, 0, 'A')",
            "INSERT INTO products VALUES (2, 0, 'B')",
        ],
    )
    con, context = make_context(base_path, table_columns)
    with closing(con):
        ours = make_statement(
            "UPDATE products SET discount = 10 WHERE id = 1",
            table_columns,
            branch="ours",
        )
        theirs = make_statement(
            "UPDATE products SET discount = 9 WHERE id IN (1, 2)",
            table_columns,
            branch="theirs",
        )

        result = execution_based_analysis.execution_based_matching(
            context,
            ours,
            theirs,
            static_result(context, ours, theirs),
        )

    assert conflict_kinds(result) == ["write_write"]
    assert result.conflicts[0].message == "write-write row overlap"


def test_execution_write_write_reports_update_delete_overlap(tmp_path):
    table_columns = {"products": {"id", "discount", "name"}}
    base_path = init_base_db(
        tmp_path,
        [
            "CREATE TABLE products (id INTEGER PRIMARY KEY, discount INTEGER, name TEXT)",
            "INSERT INTO products VALUES (1, 0, 'A')",
            "INSERT INTO products VALUES (2, 0, 'B')",
        ],
    )
    con, context = make_context(base_path, table_columns)
    with closing(con):
        ours = make_statement(
            "UPDATE products SET discount = 10 WHERE id = 1",
            table_columns,
            branch="ours",
        )
        theirs = make_statement(
            "DELETE FROM products WHERE id = 1",
            table_columns,
            branch="theirs",
        )

        result = execution_based_analysis.execution_based_matching(
            context,
            ours,
            theirs,
            static_result(context, ours, theirs),
        )

    assert conflict_kinds(result) == ["write_read", "write_write"]


def test_execution_write_write_supports_update_from_clause(tmp_path):
    table_columns = {
        "products": {"id", "category_id", "discount"},
        "categories": {"id", "name"},
    }
    base_path = init_base_db(
        tmp_path,
        [
            """
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                category_id INTEGER,
                discount INTEGER
            )
            """,
            "CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT)",
            "INSERT INTO categories VALUES (1, 'A')",
            "INSERT INTO categories VALUES (2, 'B')",
            "INSERT INTO products VALUES (1, 1, 0)",
            "INSERT INTO products VALUES (2, 2, 0)",
        ],
    )
    con, context = make_context(base_path, table_columns)
    with closing(con):
        ours = make_statement(
            "UPDATE products "
            "SET discount = 10 "
            "FROM categories "
            "WHERE products.category_id = categories.id "
            "AND categories.name = 'A'",
            table_columns,
            branch="ours",
        )
        theirs = make_statement(
            "DELETE FROM products WHERE id = 1",
            table_columns,
            branch="theirs",
        )

        result = execution_based_analysis.execution_based_matching(
            context,
            ours,
            theirs,
            static_result(context, ours, theirs),
        )

    assert conflict_kinds(result) == ["write_read", "write_write"]


def test_execution_write_write_keeps_static_result_when_pk_select_unsupported(tmp_path):
    table_columns = {"products": {"discount", "name"}}
    base_path = init_base_db(
        tmp_path,
        [
            "CREATE TABLE products (discount INTEGER, name TEXT)",
            "INSERT INTO products VALUES (0, 'A')",
        ],
    )
    con, context = make_context(base_path, table_columns)
    with closing(con):
        ours = make_statement(
            "UPDATE products SET discount = 10 WHERE name = 'A'",
            table_columns,
            branch="ours",
        )
        theirs = make_statement(
            "UPDATE products SET discount = 9 WHERE name = 'A'",
            table_columns,
            branch="theirs",
        )
        static = static_result(context, ours, theirs)

        result = execution_based_analysis.execution_based_matching(
            context,
            ours,
            theirs,
            static,
        )

    assert result == static
    assert conflict_kinds(result) == ["write_write"]
    assert result.conflicts[0].message.startswith("Both statements write")


def test_execution_skips_when_static_has_no_write_write(tmp_path):
    table_columns = {"products": {"id", "discount", "name"}}
    base_path = init_base_db(
        tmp_path,
        [
            "CREATE TABLE products (id INTEGER PRIMARY KEY, discount INTEGER, name TEXT)",
            "INSERT INTO products VALUES (1, 0, 'A')",
        ],
    )
    con, context = make_context(base_path, table_columns)
    with closing(con):
        ours = make_statement(
            "UPDATE products SET discount = 10 WHERE id = 1",
            table_columns,
            branch="ours",
        )
        theirs = make_statement(
            "UPDATE products SET name = 'B' WHERE id = 1",
            table_columns,
            branch="theirs",
        )
        assert not static_result(context, ours, theirs).has_conflict

        result = execution_based_analysis.execution_based_matching(
            context,
            ours,
            theirs,
            static_result(context, ours, theirs),
        )

    assert not result.has_conflict


def test_execution_write_read_clears_when_update_probe_unchanged(tmp_path):
    table_columns = {
        "products": {"id", "discount"},
        "stats": {"id", "value"},
    }
    base_path = init_base_db(
        tmp_path,
        [
            "CREATE TABLE products (id INTEGER PRIMARY KEY, discount INTEGER)",
            "CREATE TABLE stats (id INTEGER PRIMARY KEY, value INTEGER)",
            "INSERT INTO products VALUES (1, 5)",
            "INSERT INTO products VALUES (2, 0)",
            "INSERT INTO stats VALUES (1, 0)",
        ],
    )
    con, context = make_context(base_path, table_columns)
    with closing(con):
        ours = make_statement(
            "UPDATE products SET discount = 10 WHERE id = 2",
            table_columns,
            branch="ours",
        )
        theirs = make_statement(
            "UPDATE stats "
            "SET value = (SELECT discount FROM products WHERE id = 1) "
            "WHERE id = 1",
            table_columns,
            branch="theirs",
        )
        assert conflict_kinds(static_result(context, ours, theirs)) == ["write_read"]

        result = execution_based_analysis.execution_based_matching(
            context,
            ours,
            theirs,
            static_result(context, ours, theirs),
        )

    assert not result.has_conflict


def test_execution_write_read_reports_when_update_probe_changes(tmp_path):
    table_columns = {
        "products": {"id", "discount"},
        "stats": {"id", "value"},
    }
    base_path = init_base_db(
        tmp_path,
        [
            "CREATE TABLE products (id INTEGER PRIMARY KEY, discount INTEGER)",
            "CREATE TABLE stats (id INTEGER PRIMARY KEY, value INTEGER)",
            "INSERT INTO products VALUES (1, 5)",
            "INSERT INTO stats VALUES (1, 0)",
        ],
    )
    con, context = make_context(base_path, table_columns)
    with closing(con):
        ours = make_statement(
            "UPDATE products SET discount = 10 WHERE id = 1",
            table_columns,
            branch="ours",
        )
        theirs = make_statement(
            "UPDATE stats "
            "SET value = (SELECT discount FROM products WHERE id = 1) "
            "WHERE id = 1",
            table_columns,
            branch="theirs",
        )

        result = execution_based_analysis.execution_based_matching(
            context,
            ours,
            theirs,
            static_result(context, ours, theirs),
        )

    assert conflict_kinds(result) == ["write_read"]
    assert result.conflicts[0].message == "write-read dependency: ours writes; theirs reads"


def test_execution_write_read_clears_unchanged_insert_values_probe(tmp_path):
    table_columns = {
        "products": {"id", "category"},
        "logs": {"message"},
    }
    base_path = init_base_db(
        tmp_path,
        [
            "CREATE TABLE products (id INTEGER PRIMARY KEY, category TEXT)",
            "CREATE TABLE logs (message TEXT)",
            "INSERT INTO products VALUES (1, 'sale')",
            "INSERT INTO products VALUES (2, 'clearance')",
        ],
    )
    con, context = make_context(base_path, table_columns)
    with closing(con):
        ours = make_statement(
            "UPDATE products SET category = 'new' WHERE id = 2",
            table_columns,
            branch="ours",
        )
        theirs = make_statement(
            "INSERT INTO logs(message) "
            "VALUES ((SELECT category FROM products WHERE id = 1))",
            table_columns,
            branch="theirs",
        )
        assert conflict_kinds(static_result(context, ours, theirs)) == ["write_read"]

        result = execution_based_analysis.execution_based_matching(
            context,
            ours,
            theirs,
            static_result(context, ours, theirs),
        )

    assert not result.has_conflict


def test_execution_write_read_clears_unchanged_insert_select_probe(tmp_path):
    table_columns = {
        "products": {"id", "category"},
        "logs": {"message"},
    }
    base_path = init_base_db(
        tmp_path,
        [
            "CREATE TABLE products (id INTEGER PRIMARY KEY, category TEXT)",
            "CREATE TABLE logs (message TEXT)",
            "INSERT INTO products VALUES (1, 'sale')",
            "INSERT INTO products VALUES (2, 'clearance')",
        ],
    )
    con, context = make_context(base_path, table_columns)
    with closing(con):
        ours = make_statement(
            "UPDATE products SET category = 'new' WHERE id = 2",
            table_columns,
            branch="ours",
        )
        theirs = make_statement(
            "INSERT INTO logs(message) "
            "SELECT category FROM products WHERE id = 1",
            table_columns,
            branch="theirs",
        )
        assert conflict_kinds(static_result(context, ours, theirs)) == ["write_read"]

        result = execution_based_analysis.execution_based_matching(
            context,
            ours,
            theirs,
            static_result(context, ours, theirs),
        )

    assert not result.has_conflict


def test_execution_write_read_insert_select_preserves_duplicate_counts(tmp_path):
    table_columns = {
        "products": {"id", "category"},
        "logs": {"message"},
    }
    base_path = init_base_db(
        tmp_path,
        [
            "CREATE TABLE products (id INTEGER PRIMARY KEY, category TEXT)",
            "CREATE TABLE logs (message TEXT)",
            "INSERT INTO products VALUES (1, 'sale')",
            "INSERT INTO products VALUES (2, 'clearance')",
        ],
    )
    con, context = make_context(base_path, table_columns)
    with closing(con):
        ours = make_statement(
            "UPDATE products SET category = 'sale' WHERE id = 2",
            table_columns,
            branch="ours",
        )
        theirs = make_statement(
            "INSERT INTO logs(message) "
            "SELECT 'sale' FROM products WHERE category = 'sale'",
            table_columns,
            branch="theirs",
        )
        assert conflict_kinds(static_result(context, ours, theirs)) == ["write_read"]

        result = execution_based_analysis.execution_based_matching(
            context,
            ours,
            theirs,
            static_result(context, ours, theirs),
        )

    assert conflict_kinds(result) == ["write_read"]


def test_execution_reports_integrity_conflict_for_duplicate_insert_key(tmp_path):
    table_columns = {"products": {"id", "name"}}
    base_path = init_base_db(
        tmp_path,
        [
            "CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT)",
        ],
    )
    con, context = make_context(base_path, table_columns)
    with closing(con):
        ours = make_statement(
            "INSERT INTO products(id, name) VALUES (1, 'A')",
            table_columns,
            branch="ours",
        )
        theirs = make_statement(
            "INSERT INTO products(id, name) VALUES (1, 'B')",
            table_columns,
            branch="theirs",
        )
        assert not static_result(context, ours, theirs).has_conflict

        result = execution_based_analysis.execution_based_matching(
            context,
            ours,
            theirs,
            static_result(context, ours, theirs),
        )

    assert conflict_kinds(result) == ["integrity"]
    assert "UNIQUE constraint failed" in result.conflicts[0].message


def test_execution_update_from_duplicate_source_rows_keep_static(tmp_path):
    table_columns = {
        "products": {"id", "category_id", "discount"},
        "categories": {"id", "rate"},
    }
    base_path = init_base_db(
        tmp_path,
        [
            "CREATE TABLE products (id INTEGER PRIMARY KEY, category_id INTEGER, discount INTEGER)",
            "CREATE TABLE categories (id INTEGER, rate INTEGER)",
            "INSERT INTO products VALUES (1, 1, 0)",
            "INSERT INTO products VALUES (2, 2, 0)",
            "INSERT INTO categories VALUES (1, 5)",
            "INSERT INTO categories VALUES (1, 7)",
            "INSERT INTO categories VALUES (2, 9)",
        ],
    )
    con, context = make_context(base_path, table_columns)
    with closing(con):
        ours = make_statement(
            "UPDATE products "
            "SET discount = categories.rate "
            "FROM categories "
            "WHERE products.category_id = categories.id "
            "AND products.id = 1",
            table_columns,
            branch="ours",
        )
        theirs = make_statement(
            "UPDATE products SET discount = 9 WHERE id = 2",
            table_columns,
            branch="theirs",
        )
        static = static_result(context, ours, theirs)

        result = execution_based_analysis.execution_based_matching(
            context,
            ours,
            theirs,
            static,
        )

    assert result == static
    assert conflict_kinds(result) == ["write_write"]
