import shutil
import sqlite3

import pytest
import sqlglot


def _init_db(path: str, schema_sql: str, seed_sql: list[str]) -> None:
    with sqlite3.connect(path) as con:
        con.executescript(schema_sql)
        for stmt in seed_sql:
            con.execute(stmt)
        con.commit()


def _apply_sql(path: str, sql_statements: list[str]) -> None:
    with sqlite3.connect(path) as con:
        for stmt in sql_statements:
            con.execute(stmt)
        con.commit()


def _run_conflict_detection(
    merge_driver,
    base_path: str,
    ours_path: str,
    theirs_path: str,
    base_to_ours_sql: list[str],
    base_to_theirs_sql: list[str],
):
    base_to_ours_parsed = [sqlglot.parse_one(sql) for sql in base_to_ours_sql]
    base_to_theirs_parsed = [sqlglot.parse_one(sql) for sql in base_to_theirs_sql]
    return merge_driver.check_conflict_and_return_final_diff(
        base_to_ours_parsed,
        base_to_theirs_parsed,
        base_path,
        ours_path,
        theirs_path,
    )


class TestUniqueIndexConflicts:
    def test_unique_index_insert_insert_conflict(self, tmp_path, merge_driver):
        schema_sql = """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            email TEXT UNIQUE
        );
        """
        base_path = tmp_path / "base.db"
        ours_path = tmp_path / "ours.db"
        theirs_path = tmp_path / "theirs.db"

        _init_db(str(base_path), schema_sql, [])
        shutil.copy2(base_path, ours_path)
        shutil.copy2(base_path, theirs_path)

        base_to_ours_sql = [
            "INSERT INTO users (id, email) VALUES (1, 'dup@example.com')",
        ]
        base_to_theirs_sql = [
            "INSERT INTO users (id, email) VALUES (2, 'dup@example.com')",
        ]

        _apply_sql(str(ours_path), base_to_ours_sql)
        _apply_sql(str(theirs_path), base_to_theirs_sql)

        diffs = _run_conflict_detection(
            merge_driver,
            str(base_path),
            str(ours_path),
            str(theirs_path),
            base_to_ours_sql,
            base_to_theirs_sql,
        )

        assert len(diffs.conflict_pairs) == 1
        pair, conflict_list = next(iter(diffs.conflict_pairs.items()))
        assert pair == (0, 0)
        assert len(conflict_list) == 1
        conflict = conflict_list[0]
        assert isinstance(conflict, merge_driver.conflict_pairs.UniqueIndexesConflict)

    def test_composite_unique_index_insert_insert_conflict(self, tmp_path, merge_driver):
        schema_sql = """
        CREATE TABLE movies (
            id INTEGER PRIMARY KEY,
            year INTEGER,
            score REAL,
            UNIQUE(year, score)
        );
        """
        base_path = tmp_path / "base.db"
        ours_path = tmp_path / "ours.db"
        theirs_path = tmp_path / "theirs.db"

        _init_db(str(base_path), schema_sql, [])
        shutil.copy2(base_path, ours_path)
        shutil.copy2(base_path, theirs_path)

        base_to_ours_sql = [
            "INSERT INTO movies (id, year, score) VALUES (1, 1999, 8.7)",
        ]
        base_to_theirs_sql = [
            "INSERT INTO movies (id, year, score) VALUES (2, 1999, 8.7)",
        ]

        _apply_sql(str(ours_path), base_to_ours_sql)
        _apply_sql(str(theirs_path), base_to_theirs_sql)

        diffs = _run_conflict_detection(
            merge_driver,
            str(base_path),
            str(ours_path),
            str(theirs_path),
            base_to_ours_sql,
            base_to_theirs_sql,
        )

        assert len(diffs.conflict_pairs) == 1
        pair, conflict_list = next(iter(diffs.conflict_pairs.items()))
        assert pair == (0, 0)
        assert len(conflict_list) == 1
        conflict = conflict_list[0]
        assert isinstance(conflict, merge_driver.conflict_pairs.UniqueIndexesConflict)

    def test_unique_index_null_values_do_not_conflict(self, tmp_path, merge_driver):
        schema_sql = """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            email TEXT UNIQUE
        );
        """
        base_path = tmp_path / "base.db"
        ours_path = tmp_path / "ours.db"
        theirs_path = tmp_path / "theirs.db"

        _init_db(str(base_path), schema_sql, [])
        shutil.copy2(base_path, ours_path)
        shutil.copy2(base_path, theirs_path)

        base_to_ours_sql = [
            "INSERT INTO users (id, email) VALUES (1, NULL)",
        ]
        base_to_theirs_sql = [
            "INSERT INTO users (id, email) VALUES (2, NULL)",
        ]

        _apply_sql(str(ours_path), base_to_ours_sql)
        _apply_sql(str(theirs_path), base_to_theirs_sql)

        diffs = _run_conflict_detection(
            merge_driver,
            str(base_path),
            str(ours_path),
            str(theirs_path),
            base_to_ours_sql,
            base_to_theirs_sql,
        )

        assert len(diffs.conflict_pairs) == 0

    def test_distinct_unique_values_no_conflict(self, tmp_path, merge_driver):
        schema_sql = """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            email TEXT UNIQUE
        );
        """
        base_path = tmp_path / "base.db"
        ours_path = tmp_path / "ours.db"
        theirs_path = tmp_path / "theirs.db"

        _init_db(str(base_path), schema_sql, [])
        shutil.copy2(base_path, ours_path)
        shutil.copy2(base_path, theirs_path)

        base_to_ours_sql = [
            "INSERT INTO users (id, email) VALUES (1, 'a@example.com')",
        ]
        base_to_theirs_sql = [
            "INSERT INTO users (id, email) VALUES (2, 'b@example.com')",
        ]

        _apply_sql(str(ours_path), base_to_ours_sql)
        _apply_sql(str(theirs_path), base_to_theirs_sql)

        diffs = _run_conflict_detection(
            merge_driver,
            str(base_path),
            str(ours_path),
            str(theirs_path),
            base_to_ours_sql,
            base_to_theirs_sql,
        )

        assert len(diffs.conflict_pairs) == 0
