import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlite_conflict_resolution import (  # noqa: E402
    SQLiteConflictResolution,
    neutralize_rollback_conflict_resolution,
    normalize_sql_for_sqlglot,
    restore_update_conflict_resolution,
    strict_conflict_resolution_rewrite,
    strip_top_level_upsert,
)


def _resolution_tuple(resolution: SQLiteConflictResolution | None):
    assert resolution is not None
    return resolution.statement_kind, resolution.algorithm


@pytest.mark.parametrize(
    "algorithm",
    ["ROLLBACK", "ABORT", "FAIL", "IGNORE", "REPLACE"],
)
def test_normalize_records_insert_or_algorithms_without_rewriting(algorithm):
    sql = f"INSERT OR {algorithm.lower()} INTO users(id) VALUES (1)"

    compatible = normalize_sql_for_sqlglot(sql)

    assert compatible.sql == sql
    assert _resolution_tuple(compatible.conflict_resolution) == (
        "insert",
        algorithm,
    )
    assert compatible.stripped_upsert is False


@pytest.mark.parametrize(
    "algorithm",
    ["ROLLBACK", "ABORT", "FAIL", "IGNORE", "REPLACE"],
)
def test_normalize_removes_update_or_algorithms_for_sqlglot(algorithm):
    sql = f"UPDATE OR {algorithm.lower()} users SET email = 'a' WHERE id = 1"

    compatible = normalize_sql_for_sqlglot(sql)

    assert compatible.sql == "UPDATE users SET email = 'a' WHERE id = 1"
    assert _resolution_tuple(compatible.conflict_resolution) == (
        "update",
        algorithm,
    )
    assert compatible.stripped_upsert is False


def test_normalize_rewrites_replace_into_as_insert_or_replace():
    compatible = normalize_sql_for_sqlglot(
        "REPLACE INTO users(id, email) VALUES (1, 'a')"
    )

    assert compatible.sql == "INSERT OR REPLACE INTO users(id, email) VALUES (1, 'a')"
    assert _resolution_tuple(compatible.conflict_resolution) == (
        "insert",
        "REPLACE",
    )
    assert compatible.stripped_upsert is False


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "INSERT OR ROLLBACK INTO users(id) VALUES (1)",
            "INSERT INTO users(id) VALUES (1)",
        ),
        (
            "UPDATE OR ROLLBACK users SET email = 'a' WHERE id = 1",
            "UPDATE users SET email = 'a' WHERE id = 1",
        ),
        (
            "WITH target(id) AS (SELECT 1) "
            "UPDATE OR ROLLBACK users SET email = 'a' "
            "WHERE id IN (SELECT id FROM target)",
            "WITH target(id) AS (SELECT 1) "
            "UPDATE users SET email = 'a' "
            "WHERE id IN (SELECT id FROM target)",
        ),
        (
            "INSERT OR ABORT INTO users(id) VALUES (1)",
            "INSERT OR ABORT INTO users(id) VALUES (1)",
        ),
    ],
)
def test_neutralize_rollback_conflict_resolution(sql, expected):
    assert neutralize_rollback_conflict_resolution(sql) == expected


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "INSERT INTO users(id, email) VALUES (1, 'a') "
            "ON CONFLICT(id) DO NOTHING",
            "INSERT INTO users(id, email) VALUES (1, 'a')",
        ),
        (
            "INSERT INTO users(id, email, score) VALUES (1, 'a', 5) "
            "ON CONFLICT(id) DO UPDATE SET score = excluded.score "
            "WHERE users.score < excluded.score;",
            "INSERT INTO users(id, email, score) VALUES (1, 'a', 5);",
        ),
        (
            "WITH incoming(id, email) AS (SELECT 1, 'a') "
            "INSERT INTO users(id, email) SELECT id, email FROM incoming "
            "ON CONFLICT(id) DO NOTHING",
            "WITH incoming(id, email) AS (SELECT 1, 'a') "
            "INSERT INTO users(id, email) SELECT id, email FROM incoming",
        ),
    ],
)
def test_strip_top_level_upsert(sql, expected):
    assert strip_top_level_upsert(sql) == expected


def test_top_level_upsert_strip_ignores_nested_quotes_and_comments():
    sql = (
        "INSERT INTO logs(message) "
        "VALUES ('ON CONFLICT(id) DO NOTHING', "
        "/* ON CONFLICT(fake) DO NOTHING */ 'ok') "
        "ON CONFLICT(message) DO NOTHING"
    )

    assert (
        strip_top_level_upsert(sql)
        == "INSERT INTO logs(message) "
        "VALUES ('ON CONFLICT(id) DO NOTHING', "
        "/* ON CONFLICT(fake) DO NOTHING */ 'ok')"
    )


def test_top_level_upsert_strip_ignores_nested_cte_conflict_text():
    sql = (
        "WITH text(value) AS (SELECT 'ON CONFLICT(id) DO NOTHING') "
        "INSERT INTO logs(message) SELECT value FROM text "
        "ON CONFLICT(message) DO NOTHING"
    )

    assert (
        strip_top_level_upsert(sql)
        == "WITH text(value) AS (SELECT 'ON CONFLICT(id) DO NOTHING') "
        "INSERT INTO logs(message) SELECT value FROM text"
    )


def test_strip_top_level_upsert_returns_none_when_no_real_upsert_clause():
    assert (
        strip_top_level_upsert(
            "INSERT INTO logs(message) VALUES ('ON CONFLICT(id) DO NOTHING')"
        )
        is None
    )
    assert strip_top_level_upsert("UPDATE users SET email = 'a' WHERE id = 1") is None


@pytest.mark.parametrize(
    ("sql", "expected_sql", "expected_label"),
    [
        (
            "INSERT OR IGNORE INTO users(id) VALUES (1)",
            "INSERT INTO users(id) VALUES (1)",
            "INSERT OR IGNORE",
        ),
        (
            "INSERT OR REPLACE INTO users(id) VALUES (1)",
            "INSERT INTO users(id) VALUES (1)",
            "INSERT OR REPLACE",
        ),
        (
            "UPDATE OR IGNORE users SET email = 'a' WHERE id = 1",
            "UPDATE users SET email = 'a' WHERE id = 1",
            "UPDATE OR IGNORE",
        ),
        (
            "UPDATE OR REPLACE users SET email = 'a' WHERE id = 1",
            "UPDATE users SET email = 'a' WHERE id = 1",
            "UPDATE OR REPLACE",
        ),
        (
            "REPLACE INTO users(id) VALUES (1)",
            "INSERT INTO users(id) VALUES (1)",
            "REPLACE INTO",
        ),
        (
            "INSERT INTO users(id) VALUES (1) ON CONFLICT(id) DO NOTHING",
            "INSERT INTO users(id) VALUES (1)",
            "UPSERT",
        ),
    ],
)
def test_strict_conflict_resolution_rewrite_for_reviewable_cases(
    sql,
    expected_sql,
    expected_label,
):
    rewrite = strict_conflict_resolution_rewrite(sql)

    assert rewrite is not None
    assert rewrite.sql == expected_sql
    assert rewrite.label == expected_label


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT OR ABORT INTO users(id) VALUES (1)",
        "INSERT OR FAIL INTO users(id) VALUES (1)",
        "INSERT OR ROLLBACK INTO users(id) VALUES (1)",
        "UPDATE OR ABORT users SET email = 'a' WHERE id = 1",
        "UPDATE OR FAIL users SET email = 'a' WHERE id = 1",
        "UPDATE OR ROLLBACK users SET email = 'a' WHERE id = 1",
        "INSERT INTO users(id) VALUES (1)",
    ],
)
def test_strict_conflict_resolution_rewrite_ignores_non_reviewable_cases(sql):
    assert strict_conflict_resolution_rewrite(sql) is None


def test_strict_conflict_resolution_rewrite_can_report_combined_labels():
    rewrite = strict_conflict_resolution_rewrite(
        "INSERT OR IGNORE INTO users(id) VALUES (1) ON CONFLICT(id) DO NOTHING"
    )

    assert rewrite is not None
    assert rewrite.sql == "INSERT INTO users(id) VALUES (1)"
    assert rewrite.label == "INSERT OR IGNORE / UPSERT"


def test_restore_update_conflict_resolution_reinserts_update_or_clause():
    resolution = SQLiteConflictResolution("update", "REPLACE")

    assert (
        restore_update_conflict_resolution(
            "UPDATE users SET email = 'a' WHERE id = 1",
            resolution,
        )
        == "UPDATE OR REPLACE users SET email = 'a' WHERE id = 1"
    )
    assert (
        restore_update_conflict_resolution(
            "WITH target(id) AS (SELECT 1) "
            "UPDATE users SET email = 'a' WHERE id IN (SELECT id FROM target)",
            resolution,
        )
        == "WITH target(id) AS (SELECT 1) "
        "UPDATE OR REPLACE users SET email = 'a' WHERE id IN (SELECT id FROM target)"
    )


def test_restore_update_conflict_resolution_ignores_insert_resolution():
    resolution = SQLiteConflictResolution("insert", "REPLACE")

    assert (
        restore_update_conflict_resolution(
            "INSERT OR REPLACE INTO users(id) VALUES (1)",
            resolution,
        )
        == "INSERT OR REPLACE INTO users(id) VALUES (1)"
    )
