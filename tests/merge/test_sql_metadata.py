import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from merge import log_merge


def test_logged_statement_metadata_for_update_reads_and_writes():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "UPDATE users "
            "SET name = 'Alice', "
            "email = (SELECT email FROM profiles WHERE profiles.user_id = users.id) "
            "WHERE id IN (SELECT user_id FROM orders WHERE total > 10)"
        ),
    )

    assert statement.metadata.table_updated == "users"
    assert statement.metadata.columns_updated == {"name", "email"}
    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "users": {"id"},
        "profiles": {"email", "user_id"},
        "orders": {"user_id", "total"},
    }


def test_logged_statement_metadata_for_insert_select_reads_source_columns():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "INSERT INTO users (id, name) "
            "SELECT id, name FROM staging_users WHERE active = 1"
        ),
    )

    assert statement.metadata.table_updated == "users"
    assert statement.metadata.columns_updated == {log_merge.ALL_COLUMNS}
    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "staging_users": {"id", "name", "active"},
    }


def test_logged_statement_metadata_parses_sqlite_conflict_resolution_syntax():
    table_columns = {"users": {"id", "email", "score"}}
    statements = [
        (
            "UPDATE OR REPLACE users SET email = 'a' WHERE id = 1",
            "users",
            {"email"},
            ("update", "REPLACE"),
        ),
        (
            "REPLACE INTO users(id, email) VALUES (1, 'a')",
            "users",
            {log_merge.ALL_COLUMNS},
            ("insert", "REPLACE"),
        ),
        (
            "INSERT INTO users(id, email, score) VALUES (1, 'a', 5) "
            "ON CONFLICT(id) DO UPDATE SET score = excluded.score "
            "WHERE users.score < excluded.score",
            "users",
            {log_merge.ALL_COLUMNS},
            None,
        ),
        (
            "WITH incoming(id, email) AS (SELECT 2, 'b') "
            "INSERT OR IGNORE INTO users(id, email) "
            "SELECT id, email FROM incoming",
            "users",
            {log_merge.ALL_COLUMNS},
            ("insert", "IGNORE"),
        ),
        (
            "WITH target(id) AS (SELECT 1) "
            "UPDATE OR REPLACE users SET email = 'a' "
            "WHERE id IN (SELECT id FROM target)",
            "users",
            {"email"},
            ("update", "REPLACE"),
        ),
    ]

    for sql_text, table, columns, expected_resolution in statements:
        statement = log_merge.make_logged_statement(
            branch="ours",
            branch_index=0,
            log_id=1,
            transaction_id=1,
            committed_at="2026-01-01T00:00:00",
            sql_text=sql_text,
            table_columns=table_columns,
        )

        assert statement.is_replay_safe
        assert statement.metadata.table_updated == table
        assert statement.metadata.columns_updated == columns
        resolution = statement.metadata.conflict_resolution
        if expected_resolution is None:
            assert resolution is None
        else:
            assert resolution is not None
            assert (
                resolution.statement_kind,
                resolution.algorithm,
            ) == expected_resolution


def test_logged_statement_metadata_for_insert_select_join_reads_join_columns():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "INSERT INTO accounts(user_id) "
            "SELECT u.id "
            "FROM users AS u "
            "JOIN orders AS o ON o.user_id = u.id "
            "WHERE o.total > 10"
        ),
        table_columns={
            "accounts": {"user_id"},
            "users": {"id"},
            "orders": {"user_id", "total"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "users": {"id"},
        "orders": {"user_id", "total"},
    }


def test_logged_statement_metadata_for_compound_select_reads_all_branches():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "SELECT id FROM users "
            "UNION "
            "SELECT user_id FROM orders"
        ),
        table_columns={
            "users": {"id"},
            "orders": {"user_id"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "users": {"id"},
        "orders": {"user_id"},
    }


def test_logged_statement_metadata_for_delete_reads_predicate_columns():
    statement = log_merge.make_logged_statement(
        branch="theirs",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "DELETE FROM users "
            "WHERE id IN (SELECT user_id FROM orders WHERE total > 10)"
        ),
    )

    assert statement.metadata.table_updated == "users"
    assert statement.metadata.columns_updated == {log_merge.ALL_COLUMNS}
    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "users": {"id"},
        "orders": {"user_id", "total"},
    }


def test_logged_statement_metadata_for_update_from_join_reads_join_columns():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "UPDATE accounts "
            "SET vip_total = o.total "
            "FROM users AS u "
            "JOIN orders AS o ON o.user_id = u.id "
            "WHERE accounts.user_id = u.id"
        ),
        table_columns={
            "accounts": {"user_id", "vip_total"},
            "users": {"id"},
            "orders": {"user_id", "total"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "accounts": {"user_id"},
        "users": {"id"},
        "orders": {"user_id", "total"},
    }


def test_logged_statement_metadata_reads_select_group_by_and_having_columns():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "SELECT user_id "
            "FROM orders "
            "GROUP BY user_id "
            "HAVING SUM(total) > 100"
        ),
        table_columns={
            "orders": {"user_id", "total"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "orders": {"user_id", "total"},
    }


def test_logged_statement_metadata_ignores_having_output_alias_after_reads():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "SELECT user_id, SUM(total) AS total_spent "
            "FROM orders "
            "GROUP BY user_id "
            "HAVING total_spent > 100"
        ),
        table_columns={
            "orders": {"user_id", "total"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "orders": {"user_id", "total"},
    }


def test_logged_statement_metadata_having_alias_does_not_fall_back_to_outer_scope():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "SELECT 1 "
            "FROM left_table AS l "
            "JOIN right_table AS r ON TRUE "
            "WHERE EXISTS ("
            "  SELECT 1 AS shared_name "
            "  HAVING shared_name > 0"
            ")"
        ),
        table_columns={
            "left_table": {"shared_name"},
            "right_table": {"shared_name"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {}


def test_logged_statement_metadata_uses_all_columns_marker_for_star():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text="INSERT INTO user_archive SELECT * FROM users",
    )

    assert statement.metadata.table_updated == "user_archive"
    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "users": {log_merge.ALL_COLUMNS},
    }


def test_logged_statement_metadata_uses_all_columns_marker_for_insert_qualified_star():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "INSERT INTO user_archive "
            "SELECT u.* "
            "FROM users AS u "
            "JOIN orders AS o ON o.user_id = u.id "
            "WHERE EXISTS ("
            "  SELECT 1 FROM profiles AS p WHERE p.user_id = u.id"
            ")"
        ),
        table_columns={
            "users": {"id", "name"},
            "orders": {"user_id"},
            "profiles": {"user_id"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "users": {log_merge.ALL_COLUMNS},
        "orders": {"user_id"},
        "profiles": {"user_id"},
    }


def test_logged_statement_metadata_uses_all_columns_marker_for_insert_bare_star_join():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "INSERT INTO order_user_archive "
            "SELECT * "
            "FROM users AS u "
            "JOIN orders AS o ON o.user_id = u.id "
            "WHERE o.total > 10"
        ),
        table_columns={
            "users": {"id", "name"},
            "orders": {"user_id", "total"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "users": {log_merge.ALL_COLUMNS},
        "orders": {log_merge.ALL_COLUMNS},
    }


def test_logged_statement_metadata_treats_count_star_as_all_columns_conservatively():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text="SELECT COUNT(*) FROM users",
        table_columns={
            "users": {"id", "name"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "users": {log_merge.ALL_COLUMNS},
    }


def test_logged_statement_metadata_resolves_unqualified_columns_with_schema_map():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "UPDATE accounts "
            "SET owner = s.name "
            "FROM staging AS s "
            "WHERE account_id = 1"
        ),
        table_columns={
            "accounts": {"account_id", "owner"},
            "staging": {"name"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "accounts": {"account_id"},
        "staging": {"name"},
    }


def test_logged_statement_metadata_ignores_ambiguous_unqualified_columns():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "UPDATE accounts "
            "SET owner = s.name "
            "FROM staging AS s "
            "WHERE user_id = s.user_id"
        ),
        table_columns={
            "accounts": {"user_id", "owner"},
            "staging": {"user_id", "name"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "staging": {"name", "user_id"},
    }


def test_logged_statement_metadata_falls_back_to_outer_scope_for_correlated_column():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "UPDATE accounts "
            "SET owner = ("
            "  SELECT name FROM users WHERE users.id = user_id"
            ")"
        ),
        table_columns={
            "accounts": {"user_id", "owner"},
            "users": {"id", "name"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "accounts": {"user_id"},
        "users": {"id", "name"},
    }


def test_logged_statement_metadata_inner_scope_shadows_outer_scope():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "UPDATE accounts "
            "SET owner = ("
            "  SELECT name FROM users WHERE users.id = user_id"
            ")"
        ),
        table_columns={
            "accounts": {"user_id", "owner"},
            "users": {"id", "name", "user_id"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "users": {"id", "name", "user_id"},
    }


def test_logged_statement_metadata_scans_cte_body_and_resolves_known_dml_columns():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "WITH c AS (SELECT user_id FROM users) "
            "UPDATE accounts "
            "SET owner = 'Alice' "
            "FROM c "
            "WHERE user_id = 1"
        ),
        table_columns={
            "accounts": {"user_id", "owner"},
            "users": {"user_id"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "accounts": {"user_id"},
        "users": {"user_id"},
    }


def test_logged_statement_metadata_skips_cte_pseudo_table_references():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "WITH c AS ("
            "  SELECT id, name FROM users WHERE active = 1"
            ") "
            "UPDATE accounts "
            "SET owner = c.name "
            "FROM c "
            "WHERE accounts.user_id = c.id"
        ),
        table_columns={
            "accounts": {"user_id", "owner"},
            "users": {"id", "name", "active"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "accounts": {"user_id"},
        "users": {"id", "name", "active"},
    }


def test_logged_statement_metadata_scans_cte_body_without_output_lineage():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "WITH c AS ("
            "  SELECT id, name, active FROM users"
            ") "
            "UPDATE accounts "
            "SET owner = c.name "
            "FROM c "
            "WHERE accounts.user_id = c.id"
        ),
        table_columns={
            "accounts": {"user_id", "owner"},
            "users": {"id", "name", "active"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "accounts": {"user_id"},
        "users": {"id", "name", "active"},
    }


def test_logged_statement_metadata_scans_aliased_cte_expressions():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "WITH c AS ("
            "  SELECT id AS user_id, lower(name) AS display_name "
            "  FROM users "
            "  WHERE active = 1"
            ") "
            "UPDATE accounts "
            "SET owner = c.display_name "
            "FROM c "
            "WHERE accounts.user_id = c.user_id"
        ),
        table_columns={
            "accounts": {"user_id", "owner"},
            "users": {"id", "name", "active"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "accounts": {"user_id"},
        "users": {"id", "name", "active"},
    }


def test_logged_statement_metadata_scans_cte_body_with_join_dependencies():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "WITH c AS ("
            "  SELECT u.id "
            "  FROM users AS u "
            "  JOIN orders AS o ON o.user_id = u.id "
            "  WHERE o.total > 10"
            ") "
            "UPDATE accounts "
            "SET owner_id = c.id "
            "FROM c "
            "WHERE accounts.user_id = c.id"
        ),
        table_columns={
            "accounts": {"user_id", "owner_id"},
            "users": {"id"},
            "orders": {"user_id", "total"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "accounts": {"user_id"},
        "users": {"id"},
        "orders": {"user_id", "total"},
    }


def test_logged_statement_metadata_handles_chained_ctes_as_derived_sources():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "WITH "
            "a AS (SELECT id FROM users), "
            "b AS (SELECT id FROM a) "
            "UPDATE accounts "
            "SET flag = 1 "
            "WHERE user_id IN (SELECT id FROM b)"
        ),
        table_columns={
            "accounts": {"user_id", "flag"},
            "users": {"id"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "accounts": {"user_id"},
        "users": {"id"},
    }


def test_logged_statement_metadata_handles_recursive_cte_without_fake_cte_table():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "WITH RECURSIVE ancestors(id) AS ("
            "  SELECT parent_id FROM nodes WHERE id = 10 "
            "  UNION ALL "
            "  SELECT nodes.parent_id "
            "  FROM nodes JOIN ancestors ON nodes.id = ancestors.id"
            ") "
            "INSERT INTO seen(id) SELECT id FROM ancestors"
        ),
        table_columns={
            "nodes": {"id", "parent_id"},
            "seen": {"id"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "nodes": {"id", "parent_id"},
    }


def test_logged_statement_metadata_top_level_cte_does_not_use_dml_outer_scope():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "WITH c AS ("
            "  SELECT id FROM users WHERE users.id = accounts.user_id"
            ") "
            "UPDATE accounts "
            "SET flag = 1 "
            "WHERE EXISTS (SELECT 1 FROM c)"
        ),
        table_columns={
            "accounts": {"user_id", "flag"},
            "users": {"id"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "users": {"id"},
    }


def test_logged_statement_metadata_top_level_cte_does_not_use_update_from_alias():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "WITH c AS (SELECT s.user_id) "
            "UPDATE accounts "
            "SET flag = 1 "
            "FROM staging AS s "
            "WHERE EXISTS (SELECT 1 FROM c)"
        ),
        table_columns={
            "accounts": {"flag"},
            "staging": {"user_id"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {}


def test_logged_statement_metadata_nested_cte_reads_outer_dml_scope():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "UPDATE accounts "
            "SET flag = 1 "
            "WHERE EXISTS ("
            "  WITH c AS ("
            "    SELECT id FROM users WHERE users.id = accounts.user_id"
            "  ) "
            "  SELECT 1 FROM c"
            ")"
        ),
        table_columns={
            "accounts": {"user_id", "flag"},
            "users": {"id"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "accounts": {"user_id"},
        "users": {"id"},
    }


def test_logged_statement_metadata_derived_table_reads_outer_dml_scope():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "UPDATE accounts "
            "SET flag = 1 "
            "WHERE EXISTS ("
            "  SELECT 1 "
            "  FROM ("
            "    SELECT id FROM users WHERE users.id = accounts.user_id"
            "  ) AS d"
            ")"
        ),
        table_columns={
            "accounts": {"user_id", "flag"},
            "users": {"id"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "accounts": {"user_id"},
        "users": {"id"},
    }


def test_logged_statement_metadata_derived_child_can_see_derived_outer_frame():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "UPDATE accounts "
            "SET flag = 1 "
            "WHERE EXISTS ("
            "  SELECT 1 "
            "  FROM ("
            "    SELECT 1 "
            "    FROM users AS u "
            "    WHERE EXISTS ("
            "      SELECT 1 "
            "      FROM ("
            "        SELECT u.id, accounts.user_id"
            "      ) AS d2"
            "    )"
            "  ) AS d1"
            ")"
        ),
        table_columns={
            "accounts": {"user_id", "flag"},
            "users": {"id"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {
        "accounts": {"user_id"},
        "users": {"id"},
    }


def test_logged_statement_metadata_select_cte_cannot_see_later_select_sources():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "WITH c AS (SELECT u.id AS id) "
            "SELECT c.id FROM users AS u, c"
        ),
        table_columns={
            "users": {"id"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {}


def test_logged_statement_metadata_select_derived_cannot_see_sibling_sources():
    statement = log_merge.make_logged_statement(
        branch="ours",
        branch_index=0,
        log_id=1,
        transaction_id=1,
        committed_at="2026-01-01T00:00:00",
        sql_text=(
            "SELECT d.id "
            "FROM users AS u "
            "JOIN (SELECT u.id AS id) AS d ON TRUE"
        ),
        table_columns={
            "users": {"id"},
        },
    )

    assert statement.metadata.tables_referenced_to_columns_referenced == {}
